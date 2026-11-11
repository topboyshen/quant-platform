"""Synthetic/local tests only. No app imports, DB, network, production or timers."""
import copy
import importlib.util
import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = str(Path(tempfile.gettempdir()).resolve())


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = load("candidate_evaluator", ROOT / "radar/swing/outcome_evaluator.py")
pr = load("candidate_provenance", ROOT / "radar/swing/outcome_provenance.py")
LOG = logging.getLogger("offline-provenance-tests")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False
CUTOVER = "2026-09-08T00:00:00+00:00"


def inputs(n=5):
    dates = pd.bdate_range("2026-09-09", periods=n).date.tolist()
    days = [datetime(2026, 9, 8).date()] + dates
    target = pd.DataFrame({"date": days, "open": [10.] * len(days),
        "high": [10.4] * len(days), "low": [9.8] * len(days),
        "close": [10.1] * len(days), "volume": [1000000] * len(days)})
    benchmark = target.copy(deep=True)
    for frame in (target, benchmark):
        frame.attrs["radar_lineage"] = {"source": "synthetic", "pipeline": "offline_test", "adjustment": "qfq"}
    decision = {"decision_id": "new-decision", "run_id": "new-run", "decision_date": "2026-09-08",
        "decision_at": "2026-09-08T07:30:00Z", "code": "518880", "data_date": "2026-09-08",
        "reference_close": 10.1, "completed_horizons": set(), "raw_trigger_count": 1,
        "raw_signals": [{"signal_type": "price_cross", "period": "D", "detail": {"data_date": "2026-09-08"}}],
        "decision_state": "allowed", "risk_action": "allowed", "data_status": "ok", "validation_split": "live_oos"}
    return decision, target, benchmark, dates[-1].isoformat()


class Store:
    def __init__(self, decision, accept=True, fail=False):
        self.decision, self.accept, self.fail, self.rows = decision, accept, fail, []
    def list_decisions_for_outcome(self, *, limit):
        return [self.decision]
    def record_decision_outcome(self, row):
        if self.fail:
            raise RuntimeError("synthetic store failure")
        self.rows.append(copy.deepcopy(row))
        return self.accept


class Fetcher:
    def __init__(self, target, benchmark):
        self.frames, self.calls = {"518880": target, "510300": benchmark}, []
    def get_historical_data(self, code, days):
        self.calls.append((code, days))
        return self.frames[code]


def outcome_args(n=5, horizon=5):
    decision, target, benchmark, as_of = inputs(n)
    evaluator = ev.SwingOutcomeEvaluator(Store(decision), Fetcher(target, benchmark), logger=LOG)
    evaluator.evaluate_pending(as_of=as_of)
    outcome = next(o for o in evaluator.store.rows if o["horizon_sessions"] == horizon)
    return {"decision": decision, "outcome": outcome, "target_frame": evaluator._frames["518880:" + as_of],
            "benchmark": evaluator._frames["510300:" + as_of], "settings": evaluator.settings, "as_of": as_of}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "evidence"
        self.args = outcome_args()
        self.recorder = pr.OutcomeRecorder(self.directory, cutover=CUTOVER, manifest={"source_sha256": {"synthetic": "abc"}},
            clock=lambda: datetime.fromisoformat(self.args["as_of"] + "T09:00:00+00:00"))

    def capture(self, args=None):
        return self.recorder.capture(**(args or self.args))

    def record(self, horizon=5):
        return json.loads((self.directory / f"new-decision-{horizon}.json").read_text())

    def test_exact_used_paths_and_outcome_saved_without_mutation(self):
        before = self.args["target_frame"].copy(deep=True)
        self.assertEqual(self.capture()["status"], "written")
        record = self.record()
        pd.testing.assert_frame_equal(before, self.args["target_frame"])
        self.assertEqual(record["payload"]["outcome"]["net_return"], self.args["outcome"]["net_return"])
        self.assertEqual(len(record["payload"]["paths"]["target"]), 5)
        self.assertIsNone(record["payload"]["decision_process_version"])
        self.args["target_frame"].iloc[1, 1] = 8.0
        self.assertEqual(self.record(), record)

    def test_repeat_is_idempotent_no_rewrite(self):
        self.capture()
        path = self.directory / "new-decision-5.json"
        raw, stat = path.read_bytes(), path.stat()
        self.assertEqual(self.capture()["status"], "existing")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_same_slot_different_payload_preserves_first(self):
        self.capture(); raw = (self.directory / "new-decision-5.json").read_bytes()
        self.args["outcome"]["net_return"] += .1
        self.assertEqual(self.capture()["status"], "conflict")
        self.assertEqual((self.directory / "new-decision-5.json").read_bytes(), raw)

    def test_cross_horizon_extension_is_not_drift(self):
        self.capture(outcome_args(horizon=3))
        self.assertFalse(self.capture()["basis_diff"])

    def test_cross_horizon_price_change_is_drift_not_price_override(self):
        self.capture(outcome_args(horizon=3))
        self.args["target_frame"].loc[1, "open"] = 10.2
        before = self.args["outcome"].copy()
        self.assertTrue(self.capture()["basis_diff"])
        self.assertEqual(self.args["outcome"], before)
        self.assertIn("target_overlap_ohlc", self.record()["cross_horizon_differences"][0]["fields"])

    def test_cross_horizon_missing_prefix_bar_is_visible(self):
        self.capture(outcome_args(horizon=3))
        self.args["target_frame"] = self.args["target_frame"].drop(index=2)
        self.assertTrue(self.capture()["basis_diff"])
        self.assertIn("target_shared_dates", self.record()["cross_horizon_differences"][0]["fields"])

    def test_lineage_parameters_versions_all_compared(self):
        self.capture(outcome_args(horizon=3))
        self.args["settings"] = ev.OutcomeSettings(commission_rate=.0004)
        self.recorder.manifest = {"source_sha256": {"synthetic": "new"}}
        self.args["target_frame"].attrs["radar_lineage"]["adjustment"] = "hfq"
        self.capture()
        fields = self.record()["cross_horizon_differences"][0]["fields"]
        self.assertTrue({"effective_settings", "process_version", "target_lineage"}.issubset(fields))

    def test_old_decision_future_horizon_skipped_without_directory(self):
        self.args["decision"]["decision_at"] = "2026-09-07T07:30:00Z"
        self.assertEqual(self.capture()["status"], "skipped_pre_cutover")
        self.assertFalse(self.directory.exists())

    def test_exact_cutover_and_equivalent_timezone_allowed(self):
        self.args["decision"]["decision_at"] = "2026-09-08T08:00:00+08:00"
        self.assertEqual(self.capture()["status"], "written")

    def test_bad_clock_date_naive_and_future_rejected(self):
        for value in (None, "2026-09-08T07:30:00", "2026-10-08T07:30:00Z", "2026-09-09T07:30:00Z"):
            with self.subTest(value=value):
                args = copy.deepcopy(self.args); args["decision"]["decision_at"] = value
                with self.assertRaises((ValueError, TypeError)):
                    self.capture(args)
        self.assertFalse(self.directory.exists())

    def test_historical_as_of_cannot_be_backfilled(self):
        self.recorder.clock = lambda: datetime(2026, 9, 16, 9, tzinfo=timezone.utc)
        with self.assertRaises(pr.EvidenceError):
            self.capture()
        self.assertFalse(self.directory.exists())

    def test_missing_lineage_unknown_on_first_and_repeat(self):
        self.args["target_frame"].attrs = {}
        self.assertTrue(self.capture()["provenance_unknown"])
        self.assertTrue(self.capture()["provenance_unknown"])

    def test_missing_target_bar_not_complete(self):
        self.args["target_frame"] = self.args["target_frame"].drop(index=2)
        self.assertTrue(self.capture()["provenance_unknown"])
        self.assertEqual(self.record()["payload"]["target_session_coverage"], "incomplete")

    def test_source_whitelist_excludes_urls_secrets_and_errors(self):
        self.args["target_frame"].attrs["radar_lineage"].update(source="https://secret.invalid/token", token="PRIVATE-CANARY", normalize_error="PRIVATE-CANARY")
        self.assertTrue(self.capture()["provenance_unknown"])
        raw = (self.directory / "new-decision-5.json").read_text()
        self.assertNotIn("PRIVATE-CANARY", raw)
        self.assertNotIn("secret.invalid", raw)
        self.assertTrue(self.record()["payload"]["lineage"]["target"]["normalization_error_present"])

    def test_unknown_adjustment_not_treated_as_known(self):
        self.args["target_frame"].attrs["radar_lineage"]["adjustment"] = "unknown"
        self.assertTrue(self.capture()["provenance_unknown"])

    def test_signal_date_not_replaced_and_titles_not_exported(self):
        self.args["decision"]["raw_signals"][0] = {"signal_type": "cross", "period": "D", "title": "PRIVATE-CANARY"}
        self.assertTrue(self.capture()["provenance_unknown"])
        self.assertEqual(self.record()["payload"]["signal_keys"], [])
        self.assertNotIn("PRIVATE-CANARY", (self.directory / "new-decision-5.json").read_text())

    def test_nonfinite_and_invalid_ohlc_rejected(self):
        for value in (float("nan"), float("inf"), -1, 99):
            with self.subTest(value=value):
                args = copy.deepcopy(self.args); args["target_frame"].loc[1, "low"] = value
                with self.assertRaises(ValueError):
                    self.capture(args)

    def test_source_change_during_process_rejected(self):
        self.recorder.validate_sources = lambda: {"changed": True}
        with self.assertRaises(pr.EvidenceError):
            self.capture()
        self.assertFalse(self.directory.exists())

    def test_filename_traversal_rejected(self):
        self.args["decision"]["decision_id"] = "../bad"
        self.args["outcome"]["decision_id"] = "../bad"
        with self.assertRaises(pr.EvidenceError):
            self.capture()
        self.assertFalse(self.directory.exists())

    def test_symlink_directory_or_destination_refused(self):
        target = Path(self.temp.name) / "other"; target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(pr.EvidenceError):
            self.capture()
        self.directory.unlink(); self.directory.mkdir()
        (self.directory / "new-decision-5.json").symlink_to(target / "foreign")
        with self.assertRaises(OSError):
            self.capture()

    def test_corrupt_envelope_not_silently_existing(self):
        self.capture()
        record = self.record(); record["observed_at"] = "forged"
        (self.directory / "new-decision-5.json").write_bytes(pr.canonical(record))
        with self.assertRaises(pr.EvidenceError):
            self.capture()

    def test_io_failure_does_not_publish_partial_event(self):
        for operation in ("fsync", "link"):
            with self.subTest(operation=operation), patch.object(pr.os, operation, side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    self.capture()
            self.assertFalse((self.directory / "new-decision-5.json").exists())
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_directory_and_record_budgets(self):
        with patch.object(pr, "MAX_RECORD", 100):
            with self.assertRaises(pr.EvidenceError):
                self.capture()
        with patch.object(pr, "MIN_FREE", 10**30):
            with self.assertRaises(pr.EvidenceError):
                self.capture()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invocation_is_recorded_not_invented(self):
        with patch.dict(pr.os.environ, {"INVOCATION_ID": "bad PRIVATE-CANARY"}):
            self.capture()
        self.assertIsNone(self.record()["invocation"]["systemd_id"])
        self.assertNotIn("PRIVATE-CANARY", (self.directory / "new-decision-5.json").read_text())

    def test_disabled_has_no_files_or_manifest_io(self):
        with patch.object(pr, "observed_manifest", side_effect=AssertionError("must not read")):
            for config in ({}, {"outcome_evaluation": {"provenance": {"enabled": False}}}):
                self.assertIsNone(pr.recorder_from_config(config, config_path="missing", repo=self.directory, logger=LOG))
        self.assertFalse(self.directory.exists())

    def test_invalid_config_returns_explicit_failed_recorder(self):
        result = pr.recorder_from_config({"outcome_evaluation": {"provenance": {"enabled": "yes"}}}, config_path="missing", repo=self.directory, logger=LOG)
        self.assertEqual(result.capture()["status"], "unavailable")

    def test_unavailable_with_valid_cutover_still_excludes_legacy(self):
        result = pr.recorder_from_config({"outcome_evaluation": {"provenance": {"enabled": True, "decision_at_or_after": CUTOVER}}}, config_path="missing", repo=self.directory, logger=LOG)
        self.assertEqual(result.capture(decision={"decision_at": "2026-09-07T07:30:00Z"})["status"], "skipped_pre_cutover")
        self.assertEqual(result.capture(decision={"decision_at": "2026-09-08T07:30:00Z"})["status"], "unavailable")



if __name__ == "__main__":
    unittest.main()
