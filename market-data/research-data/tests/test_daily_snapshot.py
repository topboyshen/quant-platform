from contextlib import ExitStack
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import daily_snapshot


def sample_akshare_market(trade_date, sector_name="technology"):
    return {
        "sectors": [
            {
                "name": f"{sector_name}-{index}",
                "chg": 1.0,
                "netInflow": 1.0,
                "upCount": 20,
                "downCount": 10,
                "leader": "sample",
            }
            for index in range(60)
        ],
        "activity": {
            "上涨": 3000,
            "下跌": 2000,
            "平盘": 100,
            "涨停": 50,
            "跌停": 5,
            "统计日期": f"{trade_date} 15:00:00",
        },
    }


def sample_qlib_market(trade_date):
    return {
        "date": trade_date,
        "generation": {"tag": f"release-{trade_date}", "inode": 10},
        "coverage": {"valid": 5500, "eligible": 5501, "invalid": 1, "ratio": 0.9998},
        "breadth": {"up": 3000, "down": 2000, "flat": 500, "total": 5500},
        "limitUpCount": 10,
        "limitDownCount": 2,
    }


class QlibMarketJsonTests(unittest.TestCase):
    def test_numpy_feature_values_produce_json_serializable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory) / "cn_data"
            qlib_root.mkdir()
            (qlib_root.parent / ".tag").write_text("test-release\n", encoding="utf-8")
            (qlib_root / "calendars").mkdir()
            (qlib_root / "instruments").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n", encoding="utf-8"
            )
            (qlib_root / "instruments" / "all.txt").write_text(
                "SH600000 2026-08-20 2026-08-21\n"
                "SH600001 2026-08-20 2026-08-21\n",
                encoding="utf-8",
            )

            features_by_symbol = {
                "sh600000": {
                    "close": np.array([10.0, 11.0], dtype=np.float32),
                    "high": np.array([10.0, 11.0], dtype=np.float32),
                    "low": np.array([10.0, 10.5], dtype=np.float32),
                    "amount": np.array([1000.0, 2000.0], dtype=np.float32),
                },
                "sh600001": {
                    "close": np.array([10.0, 9.0], dtype=np.float32),
                    "high": np.array([10.0, 9.5], dtype=np.float32),
                    "low": np.array([10.0, 9.0], dtype=np.float32),
                    "amount": np.array([3000.0, 4000.0], dtype=np.float32),
                },
            }

            def fake_load(_root, symbol, fields):
                values = features_by_symbol[symbol]
                return 0, {field: values[field] for field in fields}

            with mock.patch.object(daily_snapshot, "QLIB", str(qlib_root)), mock.patch.object(
                daily_snapshot, "load_qlib_features", side_effect=fake_load
            ), mock.patch.object(
                daily_snapshot, "QLIB_MIN_ELIGIBLE", 2
            ), mock.patch.object(
                daily_snapshot, "QLIB_MIN_COVERAGE", 1.0
            ):
                with warnings.catch_warnings(record=True) as caught_warnings:
                    warnings.simplefilter("always", ResourceWarning)
                    result = daily_snapshot.qlib_market("2026-08-21")
                    gc.collect()

        resource_warnings = [
            str(item.message)
            for item in caught_warnings
            if issubclass(item.category, ResourceWarning)
        ]

        encoded = json.dumps(result, ensure_ascii=False)

        self.assertEqual(resource_warnings, [])
        self.assertIn('"date": "2026-08-21"', encoded)
        self.assertIsInstance(result["limitUp"][0]["chg"], float)
        self.assertIsInstance(result["limitUp"][0]["close"], float)
        self.assertIsInstance(result["limitDown"][0]["chg"], float)
        self.assertIsInstance(result["limitDown"][0]["close"], float)
        self.assertIsInstance(result["topGainers"][0]["chg"], float)
        self.assertIsInstance(result["topLosers"][0]["chg"], float)
        self.assertIsInstance(result["topAmount"][0]["amount"], float)
        self.assertEqual(result["coverage"]["valid"], 2)
        self.assertEqual(result["coverage"]["eligible"], 2)
        self.assertEqual(result["coverage"]["ratio"], 1.0)

    def test_explicit_missing_date_is_rejected_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory) / "cn_data"
            qlib_root.mkdir()
            (qlib_root.parent / ".tag").write_text("test-release\n", encoding="utf-8")
            (qlib_root / "calendars").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n", encoding="utf-8"
            )
            with mock.patch.object(daily_snapshot, "QLIB", str(qlib_root)):
                with self.assertRaisesRegex(
                    daily_snapshot.QlibDateError, "2026-08-22"
                ):
                    daily_snapshot.qlib_market("2026-08-22")

    def test_reference_symbols_must_have_the_same_latest_date(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory) / "cn_data"
            qlib_root.mkdir()
            (qlib_root.parent / ".tag").write_text("test-release\n", encoding="utf-8")
            (qlib_root / "calendars").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n2026-08-22\n", encoding="utf-8"
            )
            lengths = iter((3, 3, 3, 2))

            def fake_load(_root, _symbol, fields):
                values = np.ones(next(lengths), dtype=np.float32)
                return 0, {field: values for field in fields}

            with mock.patch.object(
                daily_snapshot, "QLIB", str(qlib_root)
            ), mock.patch.object(
                daily_snapshot, "load_qlib_features", side_effect=fake_load
            ):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotValidationError,
                    "reference dates disagree",
                ):
                    daily_snapshot.qlib_market(None)

    def test_partial_feature_coverage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory) / "cn_data"
            qlib_root.mkdir()
            (qlib_root.parent / ".tag").write_text("test-release\n", encoding="utf-8")
            (qlib_root / "calendars").mkdir()
            (qlib_root / "instruments").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n", encoding="utf-8"
            )
            (qlib_root / "instruments" / "all.txt").write_text(
                "SH600000 2026-08-20 2026-08-21\n"
                "SH600001 2026-08-20 2026-08-21\n",
                encoding="utf-8",
            )
            values = {
                "close": np.array([10.0, 11.0], dtype=np.float32),
                "high": np.array([10.0, 11.0], dtype=np.float32),
                "low": np.array([10.0, 10.5], dtype=np.float32),
                "amount": np.array([1000.0, 2000.0], dtype=np.float32),
            }

            def fake_load(_root, symbol, fields):
                if symbol == "sh600001":
                    broken = dict(values)
                    broken["close"] = np.array([10.0, np.nan], dtype=np.float32)
                    return 0, {field: broken[field] for field in fields}
                return 0, {field: values[field] for field in fields}

            with mock.patch.object(
                daily_snapshot, "QLIB", str(qlib_root)
            ), mock.patch.object(
                daily_snapshot, "load_qlib_features", side_effect=fake_load
            ), mock.patch.object(
                daily_snapshot, "QLIB_MIN_ELIGIBLE", 2
            ), mock.patch.object(
                daily_snapshot, "QLIB_MIN_COVERAGE", 1.0
            ):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotValidationError,
                    "coverage is incomplete",
                ):
                    daily_snapshot.qlib_market("2026-08-21")

    def test_generation_change_during_scan_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory) / "cn_data"
            qlib_root.mkdir()
            (qlib_root / "calendars").mkdir()
            (qlib_root / "instruments").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n", encoding="utf-8"
            )
            (qlib_root / "instruments" / "all.txt").write_text(
                "SH600000 2026-08-20 2026-08-21\n"
                "SH600001 2026-08-20 2026-08-21\n",
                encoding="utf-8",
            )
            values = {
                "close": np.array([10.0, 11.0], dtype=np.float32),
                "high": np.array([10.0, 11.0], dtype=np.float32),
                "low": np.array([10.0, 10.5], dtype=np.float32),
                "amount": np.array([1000.0, 2000.0], dtype=np.float32),
            }

            def fake_load(_root, _symbol, fields):
                return 0, {field: values[field] for field in fields}

            with mock.patch.object(
                daily_snapshot, "QLIB", str(qlib_root)
            ), mock.patch.object(
                daily_snapshot, "load_qlib_features", side_effect=fake_load
            ), mock.patch.object(
                daily_snapshot, "QLIB_MIN_ELIGIBLE", 2
            ), mock.patch.object(
                daily_snapshot, "QLIB_MIN_COVERAGE", 1.0
            ), mock.patch.object(
                daily_snapshot,
                "qlib_generation",
                side_effect=[
                    {"tag": "old", "inode": 1},
                    {"tag": "new", "inode": 2},
                ],
            ):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotValidationError,
                    "generation changed",
                ):
                    daily_snapshot.qlib_market("2026-08-21")

    def test_self_consistent_but_truncated_market_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory) / "cn_data"
            qlib_root.mkdir()
            (qlib_root.parent / ".tag").write_text("test-release\n", encoding="utf-8")
            (qlib_root / "calendars").mkdir()
            (qlib_root / "instruments").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n", encoding="utf-8"
            )
            (qlib_root / "instruments" / "all.txt").write_text(
                "".join(
                    f"SH{600000 + index} 2026-08-20 2026-08-21\n"
                    for index in range(1001)
                ),
                encoding="utf-8",
            )
            values = {
                "close": np.array([10.0, 11.0], dtype=np.float32),
                "high": np.array([10.0, 11.0], dtype=np.float32),
                "low": np.array([10.0, 10.5], dtype=np.float32),
                "amount": np.array([1000.0, 2000.0], dtype=np.float32),
            }

            def fake_load(_root, _symbol, fields):
                return 0, {field: values[field] for field in fields}

            with mock.patch.object(
                daily_snapshot, "QLIB", str(qlib_root)
            ), mock.patch.object(
                daily_snapshot, "load_qlib_features", side_effect=fake_load
            ):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotValidationError,
                    "coverage is incomplete",
                ):
                    daily_snapshot.qlib_market("2026-08-21")


class AkShareStageTests(unittest.TestCase):
    started_at = "2026-09-02T22:30:00+08:00"
    completed_at = "2026-09-02T22:30:02+08:00"

    def test_stage_is_private_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            payload = sample_akshare_market("2026-09-02")

            stored = daily_snapshot.seal_akshare_market_stage(
                payload, self.started_at, self.completed_at, stage_dir
            )
            path = Path(stored["path"])
            before = path.read_bytes()
            retried = daily_snapshot.seal_akshare_market_stage(
                payload,
                "2026-09-02T22:31:00+08:00",
                "2026-09-02T22:31:02+08:00",
                stage_dir,
            )
            loaded = daily_snapshot.load_akshare_market_stage(
                "2026-09-02", stage_dir
            )

            self.assertEqual(stored["status"], "stored")
            self.assertEqual(retried["status"], "idempotent")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(loaded["akshare"], payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_same_date_different_payload_conflicts_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            first = daily_snapshot.seal_akshare_market_stage(
                sample_akshare_market("2026-09-02", "first"),
                self.started_at,
                self.completed_at,
                stage_dir,
            )
            path = Path(first["path"])
            before = hashlib.sha256(path.read_bytes()).hexdigest()

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "immutable payload"
            ):
                daily_snapshot.seal_akshare_market_stage(
                    sample_akshare_market("2026-09-02", "changed"),
                    self.started_at,
                    self.completed_at,
                    stage_dir,
                )

            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_tampered_stage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            stored = daily_snapshot.seal_akshare_market_stage(
                sample_akshare_market("2026-09-02"),
                self.started_at,
                self.completed_at,
                stage_dir,
            )
            path = Path(stored["path"])
            document = json.loads(path.read_text(encoding="utf-8"))
            document["akshare"]["sectors"][0]["name"] = "tampered"
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "payload hash mismatch"
            ):
                daily_snapshot.load_akshare_market_stage("2026-09-02", stage_dir)

    def test_wrong_file_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            stored = daily_snapshot.seal_akshare_market_stage(
                sample_akshare_market("2026-09-02"),
                self.started_at,
                self.completed_at,
                stage_dir,
            )
            Path(stored["path"]).chmod(0o644)

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "mode must be 0600"
            ):
                daily_snapshot.load_akshare_market_stage("2026-09-02", stage_dir)

    def test_wrong_directory_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            stored = daily_snapshot.seal_akshare_market_stage(
                sample_akshare_market("2026-09-02"),
                self.started_at,
                self.completed_at,
                stage_dir,
            )
            Path(stage_dir).chmod(0o777)

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError,
                "directory mode must be 0700",
            ):
                daily_snapshot.load_akshare_market_stage("2026-09-02", stage_dir)
            self.assertTrue(Path(stored["path"]).is_file())

    def test_stage_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_dir = root / "akshare-market"
            stage_dir.mkdir(mode=0o700)
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            (stage_dir / "2026-09-02.json").symlink_to(outside)

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "cannot be opened"
            ):
                daily_snapshot.load_akshare_market_stage(
                    "2026-09-02", str(stage_dir)
                )

    def test_record_metadata_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            stored = daily_snapshot.seal_akshare_market_stage(
                sample_akshare_market("2026-09-02"),
                self.started_at,
                self.completed_at,
                stage_dir,
            )
            path = Path(stored["path"])
            document = json.loads(path.read_text(encoding="utf-8"))
            document["collectionWindow"]["completedAt"] = (
                "2026-09-02T22:30:03+08:00"
            )
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "record hash mismatch"
            ):
                daily_snapshot.load_akshare_market_stage("2026-09-02", stage_dir)

    def test_restrictive_umask_still_creates_private_readable_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            old_umask = os.umask(0o777)
            try:
                stored = daily_snapshot.seal_akshare_market_stage(
                    sample_akshare_market("2026-09-02"),
                    self.started_at,
                    self.completed_at,
                    stage_dir,
                )
            finally:
                os.umask(old_umask)

            self.assertEqual(stat.S_IMODE(Path(stage_dir).stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(Path(stored["path"]).stat().st_mode), 0o600
            )
            self.assertIsNotNone(
                daily_snapshot.load_akshare_market_stage("2026-09-02", stage_dir)
            )

    def test_concurrent_same_payload_publish_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            payload = sample_akshare_market("2026-09-02")

            def seal_once(_index):
                return daily_snapshot.seal_akshare_market_stage(
                    payload,
                    self.started_at,
                    self.completed_at,
                    stage_dir,
                )["status"]

            with ThreadPoolExecutor(max_workers=8) as executor:
                statuses = list(executor.map(seal_once, range(8)))

            self.assertEqual(statuses.count("stored"), 1)
            self.assertEqual(statuses.count("idempotent"), 7)

    def test_directory_fsync_failure_leaves_a_valid_idempotent_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = str(Path(directory) / "akshare-market")
            payload = sample_akshare_market("2026-09-02")
            real_fsync = os.fsync

            def fail_directory_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("simulated directory fsync failure")
                return real_fsync(fd)

            with mock.patch.object(os, "fsync", side_effect=fail_directory_fsync):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotValidationError, "stage write failed"
                ):
                    daily_snapshot.seal_akshare_market_stage(
                        payload,
                        self.started_at,
                        self.completed_at,
                        stage_dir,
                    )

            loaded = daily_snapshot.load_akshare_market_stage(
                "2026-09-02", stage_dir
            )
            retried = daily_snapshot.seal_akshare_market_stage(
                payload,
                self.started_at,
                self.completed_at,
                stage_dir,
            )
            self.assertEqual(loaded["akshare"], payload)
            self.assertEqual(retried["status"], "idempotent")

    def test_incomplete_current_payload_is_not_sealed(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = sample_akshare_market("2026-09-02")
            payload["sectors"] = payload["sectors"][:1]

            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError,
                "sector summary is incomplete",
            ):
                daily_snapshot.seal_akshare_market_stage(
                    payload,
                    self.started_at,
                    self.completed_at,
                    str(Path(directory) / "akshare-market"),
                )

    def test_collection_window_must_be_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "ends before it starts"
            ):
                daily_snapshot.seal_akshare_market_stage(
                    sample_akshare_market("2026-09-02"),
                    "2026-09-02T22:31:00+08:00",
                    "2026-09-02T22:30:00+08:00",
                    str(Path(directory) / "akshare-market"),
                )

    def test_existing_snapshot_fast_path_still_stages_current_day(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = str(Path(directory) / "state")
            emitted = []
            with mock.patch.object(
                daily_snapshot, "STATE_DIR", state_dir
            ), mock.patch.object(
                daily_snapshot, "AKSHARE_STAGE_DIR", None
            ), mock.patch.object(
                daily_snapshot,
                "akshare_market",
                return_value=sample_akshare_market("2026-09-02"),
            ), mock.patch.object(
                daily_snapshot,
                "qlib_market",
                return_value=sample_qlib_market("2026-09-01"),
            ), mock.patch.object(
                daily_snapshot,
                "existing_snapshot",
                return_value={"generatedAt": "old", "payloadSha256": "abc"},
            ), mock.patch.object(
                daily_snapshot, "emit_status", side_effect=emitted.append
            ), mock.patch.object(
                sys, "argv", ["daily_snapshot.py"]
            ):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotDeferredError,
                    r"qlib calendar behind observed market stage: qlib=2026-09-01, captured=2026-09-02",
                ):
                    daily_snapshot.run_snapshot("run", time.monotonic())

            self.assertEqual(emitted, [])
            self.assertTrue(
                (Path(state_dir) / "akshare-market" / "2026-09-02.json").is_file()
            )

    def test_existing_snapshot_same_qlib_date_still_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = str(Path(directory) / "state")
            emitted = []
            with mock.patch.object(
                daily_snapshot, "STATE_DIR", state_dir
            ), mock.patch.object(
                daily_snapshot, "AKSHARE_STAGE_DIR", None
            ), mock.patch.object(
                daily_snapshot,
                "akshare_market",
                return_value=sample_akshare_market("2026-09-01"),
            ), mock.patch.object(
                daily_snapshot,
                "qlib_market",
                return_value=sample_qlib_market("2026-09-01"),
            ), mock.patch.object(
                daily_snapshot,
                "existing_snapshot",
                return_value={"generatedAt": "old", "payloadSha256": "abc"},
            ), mock.patch.object(
                daily_snapshot, "emit_status", side_effect=emitted.append
            ), mock.patch.object(
                sys, "argv", ["daily_snapshot.py"]
            ):
                result = daily_snapshot.run_snapshot("run", time.monotonic())

            self.assertEqual(result, 0)
            self.assertEqual(emitted[0]["status"], "skipped_existing")
            self.assertEqual(emitted[0]["akshareStage"]["tradeDate"], "2026-09-01")

    def test_existing_snapshot_is_not_blocked_by_current_staging_failure(self):
        emitted = []
        with mock.patch.object(
            daily_snapshot,
            "qlib_market",
            return_value=sample_qlib_market("2026-09-01"),
        ), mock.patch.object(
            daily_snapshot,
            "existing_snapshot",
            return_value={"generatedAt": "old", "payloadSha256": "abc"},
        ), mock.patch.object(
            daily_snapshot,
            "capture_current_akshare_market",
            side_effect=daily_snapshot.SnapshotDeferredError("network unavailable"),
        ), mock.patch.object(
            daily_snapshot, "emit_status", side_effect=emitted.append
        ), mock.patch.object(sys, "argv", ["daily_snapshot.py"]):
            result = daily_snapshot.run_snapshot("run", time.monotonic())

        self.assertEqual(result, 0)
        self.assertEqual(emitted[0]["status"], "skipped_existing")
        self.assertEqual(emitted[0]["akshareStage"]["status"], "unavailable")
        self.assertIn("network unavailable", emitted[0]["warnings"][0])

    def test_two_day_sequence_defers_then_uses_exact_staged_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "research-data" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "stocks.json").write_text(
                json.dumps({"corePool": [{"code": "002617"}]}),
                encoding="utf-8",
            )
            db_path = str(root / "snapshot.db")
            state_dir = str(root / "state")
            qlib_days = iter(
                [sample_qlib_market("2026-09-01"), sample_qlib_market("2026-09-02")]
            )
            akshare_days = iter(
                [
                    sample_akshare_market("2026-09-02"),
                    RuntimeError("network unavailable"),
                    RuntimeError("network unavailable"),
                ]
            )

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(daily_snapshot, "DB", db_path))
                stack.enter_context(mock.patch.object(daily_snapshot, "STATE_DIR", state_dir))
                stack.enter_context(mock.patch.object(daily_snapshot, "AKSHARE_STAGE_DIR", None))
                stack.enter_context(mock.patch.object(daily_snapshot, "qlib_market", side_effect=qlib_days))
                stack.enter_context(mock.patch.object(daily_snapshot, "akshare_market", side_effect=akshare_days))
                stack.enter_context(mock.patch.object(daily_snapshot, "akshare_lhb", return_value={"date": "2026-09-02", "count": 0, "rows": []}))
                stack.enter_context(mock.patch.object(daily_snapshot, "spot_market", return_value={"date": "current", "breadth": {"total": 2000}, "limitUp": []}))
                stack.enter_context(mock.patch.object(daily_snapshot, "fund_flow_sina", return_value={"002617": [1]}))
                stack.enter_context(mock.patch.object(daily_snapshot, "lhb_official", return_value={"date": "2026-09-02", "szse": [], "sse": ""}))
                stack.enter_context(mock.patch.object(daily_snapshot, "mootdx_quotes", return_value={"002617": {"price": 10}}))
                stack.enter_context(mock.patch.object(daily_snapshot, "tencent_quote", return_value={"002617": {"price": "10"}}))
                stack.enter_context(mock.patch.object(daily_snapshot, "baostock_valuation", return_value={"002617": [{"date": "2026-09-02"}]}))
                stack.enter_context(mock.patch.object(daily_snapshot, "cninfo_announcements", return_value=[]))
                stack.enter_context(mock.patch.object(daily_snapshot, "cls_telegraph", return_value=[{"title": "news"}]))
                stack.enter_context(mock.patch.object(daily_snapshot, "northbound_flow", return_value={"hgt": 1.0, "sgt": 2.0, "total": 3.0}))
                from test_current_sources import values
                concepts, insiders = values()
                stack.enter_context(mock.patch.object(daily_snapshot, "collect_concepts", return_value=concepts))
                stack.enter_context(mock.patch.object(daily_snapshot, "collect_shareholders", return_value=insiders))
                stack.enter_context(mock.patch.object(daily_snapshot, "hot_stocks", return_value=[{"code": "002617"}]))
                stack.enter_context(mock.patch.object(daily_snapshot, "global_markets", return_value={"道指": {}, "纳指": {}, "标普": {}}))
                stack.enter_context(mock.patch.object(sys, "argv", ["daily_snapshot.py"]))
                stack.enter_context(mock.patch.dict(os.environ, {"HOME": directory}))

                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotDeferredError,
                    "qlib=2026-09-01, captured=2026-09-02",
                ):
                    daily_snapshot.run_snapshot("day-one", time.monotonic())

                result = daily_snapshot.run_snapshot("day-two", time.monotonic())

            self.assertEqual(result, 0)
            self.assertTrue(
                (Path(state_dir) / "akshare-market" / "2026-09-02.json").is_file()
            )
            self.assertFalse(
                (Path(state_dir) / "akshare-market" / "2026-09-03.json").exists()
            )
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("SELECT date, payload FROM snapshot").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "2026-09-02")
            stored_payload = json.loads(rows[0][1])
            self.assertEqual(stored_payload["assemblyKind"], "delayed_observation")
            self.assertEqual(stored_payload["concepts"], concepts)
            self.assertEqual(stored_payload["insiders"], insiders)
            self.assertEqual(
                stored_payload["akshare"]["activity"]["统计日期"][:10],
                "2026-09-02",
            )
            self.assertEqual(
                stored_payload["sourceAsOf"]["akshareActivity"]["kind"],
                "trade_date",
            )
            self.assertEqual(
                stored_payload["sourceAsOf"]["akshareActivity"]["provenance"],
                "immutable_stage",
            )
            last_run = json.loads(
                (Path(state_dir) / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(last_run["status"], "inserted")
            self.assertEqual(last_run["akshareStage"]["status"], "unavailable")
            self.assertIn("network unavailable", last_run["warnings"][0])


class SnapshotSafetyTests(unittest.TestCase):
    def test_activity_date_must_match_qlib_trade_date(self):
        payload = {"activity": {"统计日期": "2026-08-28 15:00:00"}}
        self.assertEqual(
            daily_snapshot.validate_daily_source_dates("2026-08-28", payload),
            "2026-08-28",
        )
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotValidationError, "trade-date mismatch"
        ):
            daily_snapshot.validate_daily_source_dates("2026-08-26", payload)

    def test_activity_date_is_required(self):
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotValidationError, "no valid 统计日期"
        ):
            daily_snapshot.validate_daily_source_dates(
                "2026-08-28", {"activity": {}}
            )

    def test_snapshot_rows_are_append_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            snapshot = {
                "date": "2026-08-28",
                "generatedAt": "2026-08-28T22:30:00+08:00",
                "value": 1,
            }
            inserted = daily_snapshot.store_snapshot(snapshot, db_path)
            retried = daily_snapshot.store_snapshot(snapshot, db_path)

            self.assertEqual(inserted["status"], "inserted")
            self.assertEqual(inserted["quickCheck"], "ok")
            self.assertEqual(retried["status"], "idempotent")
            self.assertEqual(
                inserted["payloadSha256"], retried["payloadSha256"]
            )

            changed = dict(snapshot, value=2)
            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "immutable row already exists"
            ):
                daily_snapshot.store_snapshot(changed, db_path)

            with sqlite3.connect(db_path) as conn:
                stored_payload = conn.execute(
                    "SELECT payload FROM snapshot WHERE date = ?", (snapshot["date"],)
                ).fetchone()[0]
                row_count = conn.execute("SELECT count(*) FROM snapshot").fetchone()[0]
            self.assertEqual(json.loads(stored_payload)["value"], 1)
            self.assertEqual(row_count, 1)

    def test_non_finite_payload_is_rejected_without_a_row(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            snapshot = {
                "date": "2026-08-28",
                "generatedAt": "2026-08-28T22:30:00+08:00",
                "value": float("nan"),
            }
            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "not strict JSON"
            ):
                daily_snapshot.store_snapshot(snapshot, db_path)
            self.assertFalse(Path(db_path).exists())

    def test_existing_mixed_date_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            payload = {
                "date": "2026-08-26",
                "qlib": {"date": "2026-08-26"},
                "akshare": {"activity": {"统计日期": "2026-08-28 15:00:00"}},
            }
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)"
                )
                conn.execute(
                    "INSERT INTO snapshot VALUES(?,?,?)",
                    ("2026-08-26", "2026-08-28T22:30:00+08:00", json.dumps(payload)),
                )
            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "mixed-date"
            ):
                daily_snapshot.existing_snapshot("2026-08-26", db_path)

    def test_existing_non_finite_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            payload = {
                "date": "2026-08-28",
                "qlib": {"date": "2026-08-28"},
                "akshare": {"activity": {"统计日期": "2026-08-28 15:00:00"}},
                "optional": float("nan"),
            }
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)"
                )
                conn.execute(
                    "INSERT INTO snapshot VALUES(?,?,?)",
                    ("2026-08-28", "2026-08-28T22:30:00+08:00", json.dumps(payload)),
                )
            with self.assertRaisesRegex(
                daily_snapshot.SnapshotValidationError, "invalid JSON"
            ):
                daily_snapshot.existing_snapshot("2026-08-28", db_path)

    def test_old_invalid_row_does_not_block_new_clean_target(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            old_payload = (
                '{"date":"2026-08-26","qlib":{"date":"2026-08-26"},'
                '"akshare":{"activity":{"统计日期":"2026-08-28 15:00:00"}},'
                '"legacy":NaN}'
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)"
                )
                conn.execute(
                    "INSERT INTO snapshot VALUES(?,?,?)",
                    ("2026-08-26", "2026-08-28T22:30:00+08:00", old_payload),
                )
            clean = {
                "date": "2026-08-28",
                "generatedAt": "2026-08-29T22:30:00+08:00",
                "qlib": {"date": "2026-08-28"},
                "akshare": {"activity": {"统计日期": "2026-08-28 15:00:00"}},
            }
            result = daily_snapshot.store_snapshot(clean, db_path)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT date, payload FROM snapshot ORDER BY date"
                ).fetchall()
            self.assertEqual(result["status"], "inserted")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0], ("2026-08-26", old_payload))
            self.assertEqual(json.loads(rows[1][1])["date"], "2026-08-28")

    def test_approved_optional_nan_is_normalized_to_null(self):
        self.assertIsNone(
            daily_snapshot.json_scalar(
                np.float64(np.nan), allow_nan_as_null=True
            )
        )
        count = daily_snapshot.json_scalar(np.int64(3))
        self.assertEqual(count, 3)
        self.assertIsInstance(count, int)

    def test_critical_source_nan_is_rejected(self):
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotValidationError, "non-finite numeric"
        ):
            daily_snapshot.json_scalar(np.float64(np.nan))

    def test_source_errors_are_rejected(self):
        sources = {
            "akshare": {"sectors": [1], "activity": {}},
            "spot": {"breadth": {"total": 5000}},
            "news": [1],
            "hotStocks": [1],
            "globalMarkets": {"a": 1, "b": 2, "c": 3},
            "fundFlow": {"002617": {"error": "timeout"}},
            "mootdx": {"002617": {}},
            "tencent": {"002617": {}},
            "baostock": {"002617": {}},
            "announcements": {"002617": []},
            "concepts": {"002617": {}},
            "insiders": {"002617": ""},
        }
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotValidationError, "contains errors"
        ):
            daily_snapshot.validate_source_completeness(
                sources, ["002617"], ["002617"]
            )

    def test_empty_critical_source_payloads_are_rejected(self):
        sources = {
            "akshare": {"sectors": [1], "activity": {}},
            "lhb": {"date": "2026-08-28", "count": 0, "rows": []},
            "spot": {"breadth": {"total": 5000}},
            "fundFlow": {"002617": []},
            "lhbOfficial": {"date": "2026-08-28", "szse": [], "sse": ""},
            "mootdx": {"002617": {"price": 10}},
            "tencent": {"002617": {"price": "10"}},
            "baostock": {"002617": [{"date": "2026-08-28"}]},
            "announcements": {"002617": []},
            "news": [1],
            "northbound": {"hgt": 1.0, "sgt": 2.0, "total": 3.0},
            "concepts": {"002617": {}},
            "insiders": {"002617": ""},
            "hotStocks": [1],
            "globalMarkets": {"a": 1, "b": 2, "c": 3},
        }
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotValidationError, "fundFlow has empty payloads"
        ):
            daily_snapshot.validate_source_completeness(
                sources, ["002617"], ["002617"]
            )

    def test_empty_northbound_payload_is_rejected(self):
        sources = {
            "akshare": {"sectors": [1], "activity": {}},
            "lhb": {"date": "2026-08-28", "count": 0, "rows": []},
            "spot": {"breadth": {"total": 5000}},
            "fundFlow": {"002617": [1]},
            "lhbOfficial": {"date": "2026-08-28", "szse": [], "sse": ""},
            "mootdx": {"002617": {"price": 10}},
            "tencent": {"002617": {"price": "10"}},
            "baostock": {"002617": [{"date": "2026-08-28"}]},
            "announcements": {"002617": []},
            "news": [1],
            "northbound": {"hgt": None, "sgt": None, "total": None},
            "concepts": {"002617": {}},
            "insiders": {"002617": ""},
            "hotStocks": [1],
            "globalMarkets": {"a": 1, "b": 2, "c": 3},
        }
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotValidationError, "no finite market value"
        ):
            daily_snapshot.validate_source_completeness(
                sources, ["002617"], ["002617"]
            )

    def test_historical_cli_arguments_are_disabled(self):
        with self.assertRaisesRegex(
            daily_snapshot.SnapshotUsageError, "historical date arguments"
        ):
            daily_snapshot.requested_trade_date(["2026-08-28"])

    def test_second_process_lock_attempt_is_temporary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with daily_snapshot.snapshot_run_lock(directory):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotTemporaryError, "holds the lock"
                ):
                    with daily_snapshot.snapshot_run_lock(directory):
                        pass

    def test_busy_database_is_temporary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            blocker = sqlite3.connect(db_path)
            blocker.execute("CREATE TABLE hold(value INTEGER)")
            blocker.commit()
            blocker.execute("BEGIN IMMEDIATE")
            snapshot = {
                "date": "2026-08-28",
                "generatedAt": "2026-08-28T22:30:00+08:00",
                "value": 1,
            }
            try:
                with mock.patch.object(daily_snapshot, "SQLITE_TIMEOUT", 0.01):
                    with self.assertRaisesRegex(
                        daily_snapshot.SnapshotTemporaryError, "is busy"
                    ):
                        daily_snapshot.store_snapshot(snapshot, db_path)
            finally:
                blocker.rollback()
                blocker.close()

    def test_missing_historical_stage_in_main_defers_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "snapshot.db")
            qlib_payload = {
                "date": "2026-08-26",
                "breadth": {"up": 1, "down": 1, "flat": 0, "total": 2},
                "limitUpCount": 0,
                "limitDownCount": 0,
            }
            with mock.patch.object(daily_snapshot, "DB", db_path), mock.patch.object(
                daily_snapshot, "STATE_DIR", str(Path(directory) / "state")
            ), mock.patch.object(
                daily_snapshot, "qlib_market", return_value=qlib_payload
            ), mock.patch.object(
                daily_snapshot,
                "akshare_market",
                return_value=sample_akshare_market("2026-08-28"),
            ), mock.patch.object(sys, "argv", ["daily_snapshot.py"]):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotDeferredError,
                    "qlib=2026-08-26, captured=2026-08-28",
                ):
                    daily_snapshot.main()
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT count(*) FROM snapshot").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
