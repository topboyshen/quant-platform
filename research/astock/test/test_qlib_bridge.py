from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))

from qlib_bridge import ArtifactPins, run_bridge, validate_control  # noqa: E402


CORRECT_RUN_ID = "4e23d21a8118423787052ff83d00ae56"
DECOY_RUN_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def write_csv_artifacts(root: Path, *, include_nonfinite: bool = False) -> Path:
    tuned = root / "tuned_csi800"
    rolling = root / "rolling_stitched"
    tuned.mkdir(parents=True)
    rolling.mkdir(parents=True)
    nonfinite_rows = (
        "excess_return_without_cost,information_ratio,nan\n"
        "excess_return_without_cost,other_metric,inf\n"
        if include_nonfinite
        else ""
    )
    (tuned / "port_analysis_summary.csv").write_text(
        "excess_return_without_cost,annualized_return,0.12\n"
        "excess_return_without_cost,max_drawdown,-0.08\n"
        + nonfinite_rows,
        encoding="utf-8",
    )
    tuned_report = tuned / "report_normal.csv"
    tuned_report.write_text(
        "date,return,bench\n"
        "2026-07-29,0.01,0.00\n"
        "2026-07-30,0.02,0.01\n",
        encoding="utf-8",
    )
    (rolling / "segment_summary.csv").write_text(
        "segment,days,annualized_return,max_drawdown\n"
        "0,20,0.10,-0.03\n",
        encoding="utf-8",
    )
    (rolling / "stitched_report.csv").write_text(
        "date,return\n2026-07-29,0.01\n2026-07-30,0.02\n",
        encoding="utf-8",
    )
    return tuned_report


def prediction_frame(*, include_last_day: bool = True) -> pd.DataFrame:
    rows = [
        ("2026-07-29", "SH600875", 0.30),
        ("2026-07-29", "SZ002617", 0.10),
        ("2026-07-29", "SH000001", 0.20),
    ]
    if include_last_day:
        rows.extend(
            [
                ("2026-07-30", "SH600875", 0.05),
                ("2026-07-30", "SZ002617", 0.40),
                ("2026-07-30", "SH000001", 0.20),
            ]
        )
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(day), instrument) for day, instrument, _ in rows],
        names=["datetime", "instrument"],
    )
    return pd.DataFrame({"score": [score for _, _, score in rows]}, index=index)


def write_run(
    mlruns_root: Path,
    run_id: str,
    frame: pd.DataFrame,
) -> tuple[Path, Path]:
    artifacts = mlruns_root / "1" / run_id / "artifacts"
    artifacts.mkdir(parents=True)
    pred = artifacts / "pred.pkl"
    frame.to_pickle(pred)
    companion = artifacts / "portfolio_analysis" / "report_normal_1day.pkl"
    companion.parent.mkdir()
    companion.write_bytes(b"fixed companion marker")
    return pred, companion


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_pins(
    results_root: Path,
    mlruns_root: Path,
    *,
    run_id: str = CORRECT_RUN_ID,
) -> ArtifactPins:
    pred = mlruns_root / "1" / run_id / "artifacts" / "pred.pkl"
    companion = (
        mlruns_root
        / "1"
        / run_id
        / "artifacts"
        / "portfolio_analysis"
        / "report_normal_1day.pkl"
    )
    return ArtifactPins(
        experiment_id="1",
        run_id=run_id,
        prediction_sha256=sha256(pred) if pred.is_file() else "0" * 64,
        companion_sha256=sha256(companion) if companion.is_file() else "0" * 64,
        tuned_report_sha256=sha256(results_root / "tuned_csi800" / "report_normal.csv"),
        tuned_summary_sha256=sha256(
            results_root / "tuned_csi800" / "port_analysis_summary.csv"
        ),
    )


class QlibBridgeTests(unittest.TestCase):
    def test_control_rejects_paths_invalid_dates_and_oversized_symbol_lists(self) -> None:
        with self.assertRaisesRegex(ValueError, "only targetDate and symbols"):
            validate_control({"artifactPath": "/tmp/attacker.pkl"})
        with self.assertRaisesRegex(ValueError, "real YYYY-MM-DD"):
            validate_control({"targetDate": "2026-02-30"})
        with self.assertRaisesRegex(ValueError, "at most 10"):
            validate_control({"symbols": [f"{value:06d}.SZ" for value in range(11)]})
        with self.assertRaisesRegex(ValueError, "invalid controlled stock symbol"):
            validate_control({"symbols": ["SH600875"]})
        with self.assertRaisesRegex(ValueError, "duplicates"):
            validate_control({"symbols": ["600875.SH", "600875.SH"]})

    def test_reads_only_pinned_tuned_run_and_ranks_requested_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            mlruns_root = base / "mlruns"
            write_csv_artifacts(results_root)

            # Discovery is disabled: an otherwise valid decoy run is ignored.
            write_run(
                mlruns_root,
                DECOY_RUN_ID,
                prediction_frame(),
            )
            write_run(
                mlruns_root,
                CORRECT_RUN_ID,
                prediction_frame(),
            )
            pins = artifact_pins(results_root, mlruns_root)

            context = run_bridge(
                {
                    "targetDate": "2026-07-30",
                    "symbols": ["002617.SZ", "600875.SH"],
                },
                results_root=results_root,
                mlruns_root=mlruns_root,
                artifact_pins=pins,
            )

            ranking = context["predictionRanking"]
            self.assertIsNotNone(ranking)
            assert ranking is not None
            self.assertEqual(ranking["artifact"]["runId"], CORRECT_RUN_ID)
            self.assertEqual(ranking["artifact"]["predictionSha256"], pins.prediction_sha256)
            self.assertEqual(ranking["dataAsOf"], "2026-07-30")
            self.assertEqual(ranking["universeCount"], 3)
            self.assertEqual(context["tunedCsi800"]["excess_return_without_cost"]["annualized_return"], 0.12)
            first, second = ranking["symbols"]
            self.assertEqual(first["symbol"], "002617.SZ")
            self.assertEqual(first["rawScore"], 0.40)
            self.assertEqual(first["rank"], 1)
            self.assertEqual(first["percentile"], 1.0)
            self.assertEqual(first["universeCount"], 3)
            self.assertEqual(first["dataAsOf"], "2026-07-30")
            self.assertEqual(second["rank"], 3)
            self.assertAlmostEqual(second["percentile"], 1 / 3)
            self.assertEqual(context["warnings"], [])

    def test_uses_last_cross_section_not_later_than_target_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            mlruns_root = base / "mlruns"
            write_csv_artifacts(results_root)
            write_run(
                mlruns_root,
                CORRECT_RUN_ID,
                prediction_frame(),
            )
            pins = artifact_pins(results_root, mlruns_root)

            context = run_bridge(
                {"targetDate": "2026-07-29", "symbols": ["600875.SH"]},
                results_root=results_root,
                mlruns_root=mlruns_root,
                artifact_pins=pins,
            )
            ranking = context["predictionRanking"]
            assert ranking is not None
            row = ranking["symbols"][0]
            self.assertEqual(ranking["dataAsOf"], "2026-07-29")
            self.assertEqual(row["rawScore"], 0.30)
            self.assertEqual(row["rank"], 1)
            self.assertEqual(row["dataAsOf"], "2026-07-29")
            self.assertFalse(context["usableForTargetDate"])

    def test_missing_prediction_artifacts_warns_but_preserves_csv_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            write_csv_artifacts(results_root)
            pins = artifact_pins(results_root, base / "missing-mlruns")

            context = run_bridge(
                {"targetDate": "2026-07-30", "symbols": ["600875.SH"]},
                results_root=results_root,
                mlruns_root=base / "missing-mlruns",
                artifact_pins=pins,
            )

            self.assertIsNone(context["predictionRanking"])
            self.assertEqual(context["dataAsOf"], "2026-07-30")
            self.assertIn("annualized_return", context["tunedCsi800"]["excess_return_without_cost"])
            self.assertTrue(context["warnings"])
            self.assertIn("历史 CSV 回测摘要仍可用", context["warnings"][0])
            self.assertIn("不会启动训练、动态回测或任何文件写入", context["operationSemantics"])

    def test_hash_tampering_disables_ranking_before_pickle_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            mlruns_root = base / "mlruns"
            write_csv_artifacts(results_root)
            pred, _ = write_run(mlruns_root, CORRECT_RUN_ID, prediction_frame())
            pins = artifact_pins(results_root, mlruns_root)

            with pred.open("ab") as handle:
                handle.write(b"tamper after pinning")
            with patch("qlib_bridge.pd.read_pickle") as read_pickle:
                context = run_bridge(
                    {"targetDate": "2026-07-30", "symbols": ["600875.SH"]},
                    results_root=results_root,
                    mlruns_root=mlruns_root,
                    artifact_pins=pins,
                )
            read_pickle.assert_not_called()

            self.assertIsNone(context["predictionRanking"])
            self.assertTrue(any("SHA-256" in warning for warning in context["warnings"]))
            self.assertIn("annualized_return", context["tunedCsi800"]["excess_return_without_cost"])

    def test_tuned_csv_hash_tampering_disables_tuned_summary_and_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            mlruns_root = base / "mlruns"
            write_csv_artifacts(results_root)
            write_run(mlruns_root, CORRECT_RUN_ID, prediction_frame())
            pins = artifact_pins(results_root, mlruns_root)

            summary = results_root / "tuned_csi800" / "port_analysis_summary.csv"
            with summary.open("a", encoding="utf-8") as handle:
                handle.write("tampered,metric,999\n")
            context = run_bridge(
                {"targetDate": "2026-07-30", "symbols": ["600875.SH"]},
                results_root=results_root,
                mlruns_root=mlruns_root,
                artifact_pins=pins,
            )

            self.assertIsNone(context["predictionRanking"])
            self.assertEqual(context["tunedCsi800"], {})
            self.assertFalse(context["integrity"]["tunedSummarySha256Verified"])
            self.assertTrue(any("SHA-256" in warning for warning in context["warnings"]))

    def test_nonfinite_csv_values_become_null_and_strict_json_stays_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            mlruns_root = base / "mlruns"
            write_csv_artifacts(results_root, include_nonfinite=True)
            write_run(mlruns_root, CORRECT_RUN_ID, prediction_frame())
            pins = artifact_pins(results_root, mlruns_root)

            context = run_bridge(
                {"targetDate": "2026-07-30", "symbols": ["600875.SH"]},
                results_root=results_root,
                mlruns_root=mlruns_root,
                artifact_pins=pins,
            )

            summary = context["tunedCsi800"]["excess_return_without_cost"]
            self.assertIsNone(summary["information_ratio"])
            self.assertIsNone(summary["other_metric"])
            json.dumps(context, ensure_ascii=False, allow_nan=False)

    def test_unparseable_tuned_as_of_never_falls_back_to_a_pinned_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            results_root = base / "results"
            mlruns_root = base / "mlruns"
            tuned_report = write_csv_artifacts(results_root)
            tuned_report.write_text(
                "date,return,bench\n2026-07-30T00:00:00,0.02,0.01\n",
                encoding="utf-8",
            )
            write_run(mlruns_root, CORRECT_RUN_ID, prediction_frame())
            pins = artifact_pins(results_root, mlruns_root)

            context = run_bridge(
                {"targetDate": "2026-07-30", "symbols": ["600875.SH"]},
                results_root=results_root,
                mlruns_root=mlruns_root,
                artifact_pins=pins,
            )

            self.assertIsNone(context["predictionRanking"])
            self.assertTrue(any("无法解析" in warning for warning in context["warnings"]))


if __name__ == "__main__":
    unittest.main()
