"""Synthetic collector/read-only-audit contract regressions; no production data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import daily_snapshot as daily
import data_health as health
from source_policy import required_source_errors
from test_daily_snapshot import sample_akshare_market


class SnapshotJsonBoundaryTests(unittest.TestCase):
    day = "2026-09-21"

    def payload(self):
        return {"date": self.day, "generatedAt": self.day + "T22:30:00+08:00",
                "qlib": {"date": self.day},
                "akshare": sample_akshare_market(self.day),
                "mootdx": {"error": "optional provider unavailable"}}

    def test_existing_invalid_json_agrees_with_readonly_audit(self):
        for suffix in ('"value":1e999', '"value":-1e999',
                       '"value":1,"value":2', '"nested":{"key":1,"key":2}'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "snapshot.db"
                raw = json.dumps(self.payload())[:-1] + "," + suffix + "}"
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)")
                    conn.execute("INSERT INTO snapshot VALUES (?,?,?)", (self.day, "fixture", raw))
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(health.snapshot_rows(path)[self.day]["state"], "invalid")
                with self.assertRaisesRegex(daily.SnapshotValidationError, "invalid JSON"):
                    daily.existing_snapshot(self.day, str(path))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_stage_duplicate_json_keys_agree_with_readonly_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = Path(directory) / "stage"
            sealed = daily.seal_akshare_market_stage(
                sample_akshare_market(self.day), self.day + "T22:30:00+08:00",
                self.day + "T22:30:01+08:00", str(stage_dir))
            path = Path(sealed["path"])
            raw = path.read_text().replace('"schemaVersion":1',
                                           '"schemaVersion":1,"schemaVersion":1', 1)
            path.write_text(raw)
            stage_rows, _ = health.stage_evidence(stage_dir, health.timestamp(self.day + "T22:45:00+08:00"))
            self.assertEqual(stage_rows[self.day]["state"], "invalid")
            with self.assertRaisesRegex(daily.SnapshotValidationError, "invalid JSON"):
                daily.load_akshare_market_stage(self.day, str(stage_dir))
            self.assertEqual(path.read_text(), raw)

    def test_optional_error_is_exempt_only_at_the_exact_object_path(self):
        for payload in ({"mootdx": {"error": "optional"}},
                        {"mootdx": [{"error": "must reject"}]},
                        [{"mootdx": {"error": "must reject"}}],
                        {"nested": {"mootdx": {"error": "must reject"}}},
                        {"tencent": [{"error": "must reject"}]}):
            with self.subTest(payload=payload):
                self.assertEqual(health.has_source_error(payload),
                                 bool(required_source_errors(daily.source_error_paths(payload))))

    def test_new_row_reread_and_audit_preserve_historical_invalid_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.db"
            legacy_day = "2026-08-26"
            legacy = '{"date":"2026-08-26","legacy":NaN}'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)")
                conn.execute("INSERT INTO snapshot VALUES (?,?,?)", (legacy_day, "fixture", legacy))
            inserted = daily.store_snapshot(self.payload(), str(path))
            prior = daily.existing_snapshot(self.day, str(path))
            rows = health.snapshot_rows(path)
            self.assertEqual(inserted["status"], "inserted")
            self.assertEqual(inserted["payloadSha256"], prior["payloadSha256"])
            self.assertEqual(rows[self.day]["state"], "valid")
            self.assertEqual(rows[self.day]["payloadSha256"], inserted["payloadSha256"])
            self.assertEqual(rows[legacy_day]["state"], "invalid")
            self.assertEqual(rows[legacy_day]["payloadSha256"], hashlib.sha256(legacy.encode()).hexdigest())
            before = path.read_bytes()
            self.assertEqual(daily.store_snapshot(self.payload(), str(path))["status"], "idempotent")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
