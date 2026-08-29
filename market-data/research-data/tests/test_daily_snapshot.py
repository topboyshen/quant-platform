import gc
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import warnings

import numpy as np

import daily_snapshot


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

    def test_date_mismatch_in_main_does_not_write(self):
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
                return_value={
                    "sectors": [1],
                    "activity": {"统计日期": "2026-08-28 15:00:00"},
                },
            ), mock.patch.object(sys, "argv", ["daily_snapshot.py"]):
                with self.assertRaisesRegex(
                    daily_snapshot.SnapshotValidationError, "trade-date mismatch"
                ):
                    daily_snapshot.main()
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT count(*) FROM snapshot").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
