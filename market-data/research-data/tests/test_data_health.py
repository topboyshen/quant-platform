from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import data_health as health


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.qlib = self.root / "qlib" / "cn_data"
        (self.qlib / "calendars").mkdir(parents=True)
        self.days = ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        (self.qlib / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        (self.qlib.parent / ".tag").write_text("2026-09-05\n")
        (self.qlib / ".qlib-release").write_text("2026-09-05\n")
        for symbol in health.REFERENCES:
            (self.qlib / "features" / symbol).mkdir(parents=True)
            self.features(symbol, len(self.days))
        self.db = self.root / "snapshot.db"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY, payload TEXT)")
            for day in self.days:
                payload = {"date": day, "qlib": {"date": day},
                           "akshare": {"activity": {"统计日期": day + " 15:00:00"}}}
                conn.execute("INSERT INTO snapshot VALUES (?,?)", (day, json.dumps(payload)))
            conn.commit()
        self.stages = self.root / "stage"
        self.stages.mkdir(mode=0o700)
        self.stage("2026-09-04")
        self.last = self.root / "last-run.json"
        self.last.write_text(json.dumps({"status": "skipped_existing", "target": "2026-09-04",
                                        "timestamp": "2026-09-06T22:31:00+08:00"}))
        self.mode = self.root / "mode"
        self.mode.write_text("FROZEN\n")
        self.now = dt.datetime(2026, 9, 7, 20, 0, tzinfo=health.CST)

    def features(self, symbol, size):
        for field in health.FIELDS:
            (self.qlib / "features" / symbol / (field + ".day.bin")).write_bytes(
                struct.pack("<" + str(size + 1) + "f", 0, *range(10, 10 + size)))

    def stage(self, day):
        payload = {"sectors": [{"name": str(i), "chg": 1, "netInflow": 1,
                                 "upCount": 1, "downCount": 1, "leader": "x"} for i in range(50)],
                   "activity": {"统计日期": day + " 15:00:00", "上涨": 2000, "下跌": 2000,
                                "平盘": 100, "涨停": 20, "跌停": 10}}
        record = {"schemaVersion": 1, "tradeDate": day, "akshare": payload,
                  "collectionWindow": {"startedAt": day + "T16:00:00+08:00",
                                       "completedAt": day + "T16:00:01+08:00"},
                  "payloadSha256": health.canonical_hash(payload)}
        record["recordSha256"] = health.canonical_hash(record)
        path = self.stages / (day + ".json")
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return path

    def run_audit(self, **kwargs):
        args = dict(db=self.db, qlib=self.qlib, stages=self.stages, last_run=self.last,
                    mode=self.mode, since="2026-08-28", monitor_since="2026-08-31", now=self.now)
        args.update(kwargs)
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")):
            return health.audit(**args)

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(statement, args)
            conn.commit()

    def codes(self, result):
        return {issue["code"] for issue in result["issues"]}

    def fingerprint(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_read_only_files_and_database_unchanged(self):
        before = self.fingerprint()
        result = self.run_audit()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(before, self.fingerprint())
        self.assertFalse(result["automaticRecoveryAuthorized"])

    def test_missing_database_not_created(self):
        missing = self.root / "absent.db"
        with self.assertRaises(health.EvidenceError):
            self.run_audit(db=missing)
        self.assertFalse(missing.exists())

    def test_weekend_not_inferred_as_missing_trading_day(self):
        self.last.write_text(json.dumps({"status": "skipped_existing", "target": "2026-09-04",
                                        "timestamp": "2026-09-05T22:31:00+08:00"}))
        result = self.run_audit(now=dt.datetime(2026, 9, 6, 12, tzinfo=health.CST))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["qlib"]["referenceCompleteDate"], "2026-09-04")

    def test_calendar_can_lead_reference_data(self):
        (self.qlib / "calendars/day.txt").write_text("\n".join(self.days + ["2026-09-07"]) + "\n")
        result = self.run_audit()
        self.assertEqual(result["qlib"]["calendarLastDate"], "2026-09-07")
        self.assertEqual(result["qlib"]["referenceCompleteDate"], "2026-09-04")

    def test_disagreeing_reference_dates_fail_closed(self):
        self.features(health.REFERENCES[0], 5)
        with self.assertRaisesRegex(health.EvidenceError, "reference_dates_disagree"):
            self.run_audit()

    def test_tags_must_agree(self):
        (self.qlib / ".qlib-release").write_text("2026-09-04\n")
        with self.assertRaisesRegex(health.EvidenceError, "tags_disagree"):
            self.run_audit()

    def test_duplicate_calendar_is_rejected(self):
        (self.qlib / "calendars/day.txt").write_text("\n".join(self.days + [self.days[-1]]))
        with self.assertRaisesRegex(health.EvidenceError, "strictly_increasing"):
            self.run_audit()

    def test_generation_change_during_audit_is_rejected(self):
        real = health.generation
        values = [real(self.qlib)] * 2 + [(999, "2026-09-05")]
        with mock.patch.object(health, "generation", side_effect=values):
            with self.assertRaisesRegex(health.EvidenceError, "generation_changed_during_audit"):
                self.run_audit()

    def test_old_failed_day_survives_newer_success(self):
        self.sql("DELETE FROM snapshot WHERE date=?", ("2026-09-03",))
        self.stage("2026-09-03")
        result = self.run_audit()
        gap = next(r for r in result["ledger"] if r["tradeDate"] == "2026-09-03")
        self.assertEqual(gap["snapshotState"], "missing")
        self.assertEqual(gap["recoveryReadiness"], "partial_evidence_only")
        self.assertIn("unresolved_trading_day_gaps", self.codes(result))

    def test_two_known_unresolved_days_are_critical(self):
        self.sql("DELETE FROM snapshot WHERE date IN (?,?)", ("2026-09-02", "2026-09-03"))
        self.assertEqual(self.run_audit()["status"], "critical")

    def test_legacy_debt_is_separate_from_new_incidents(self):
        self.sql("DELETE FROM snapshot WHERE date IN (?,?)", ("2026-09-02", "2026-09-03"))
        result = self.run_audit(monitor_since="2026-09-07")
        self.assertEqual(result["status"], "warning")
        self.assertIn("legacy_gaps_or_invalid_rows", self.codes(result))

    def test_current_upstream_stage_is_pending_before_deadline(self):
        self.stage("2026-09-07")
        result = self.run_audit()
        self.assertTrue(result["qlib"]["pendingObservedStage"])
        self.assertNotIn("qlib_behind_observed_market_stage", self.codes(result))

    def test_previous_day_upstream_debt_does_not_reset_at_midnight(self):
        self.stage("2026-09-07")
        self.last.write_text(json.dumps({"status": "skipped_existing", "target": "2026-09-04",
                                        "timestamp": "2026-09-07T22:31:00+08:00"}))
        result = self.run_audit(now=dt.datetime(2026, 9, 8, 9, tzinfo=health.CST))
        self.assertFalse(result["qlib"]["pendingObservedStage"])
        self.assertIn("qlib_behind_observed_market_stage", self.codes(result))

    def test_scheduled_run_missing_after_deadline(self):
        result = self.run_audit(now=dt.datetime(2026, 9, 7, 23, tzinfo=health.CST))
        self.assertIn("scheduled_snapshot_missing", self.codes(result))

    def test_today_pending_stage_does_not_hide_older_overdue_stage(self):
        self.stage("2026-09-07")
        self.stage("2026-09-08")
        result = self.run_audit(now=dt.datetime(2026, 9, 8, 20, tzinfo=health.CST))
        self.assertTrue(result["qlib"]["pendingObservedStage"])
        issue = next(i for i in result["issues"] if i["code"] == "qlib_behind_observed_market_stage")
        self.assertEqual(issue["overdueStageDates"], ["2026-09-07"])

    def test_current_deadline_not_used_as_run_start_requirement(self):
        self.last.write_text(json.dumps({"status": "skipped_existing", "target": "2026-09-04",
                                        "timestamp": "2026-09-07T22:31:00+08:00"}))
        result = self.run_audit(now=dt.datetime(2026, 9, 7, 23, tzinfo=health.CST))
        self.assertNotIn("scheduled_snapshot_missing", self.codes(result))

    def test_invalid_stage_hash_is_not_recovery_evidence(self):
        path = self.stage("2026-09-03")
        data = json.loads(path.read_text()); data["payloadSha256"] = "0" * 64
        path.write_text(json.dumps(data))
        self.sql("DELETE FROM snapshot WHERE date=?", ("2026-09-03",))
        gap = next(r for r in self.run_audit()["ledger"] if r["tradeDate"] == "2026-09-03")
        self.assertEqual(gap["stageState"], "invalid")
        self.assertEqual(gap["recoveryReadiness"], "missing_or_invalid_market_evidence")

    def test_stage_symlink_and_wrong_permissions_rejected(self):
        original = self.stage("2026-09-03")
        original.chmod(0o644)
        link = self.stages / "2026-09-02.json"
        link.symlink_to(original)
        result = self.run_audit()
        self.assertEqual(result["stages"]["2026-09-03"]["state"], "invalid")
        self.assertEqual(result["stages"]["2026-09-02"]["state"], "invalid")

    def test_renamed_market_date_cannot_supply_old_evidence(self):
        self.stage("2026-09-03").rename(self.stages / "2026-09-02.json")
        self.assertEqual(self.run_audit()["stages"]["2026-09-02"]["state"], "invalid")

    def test_success_with_material_warning_is_not_green(self):
        self.last.write_text(json.dumps({"status": "skipped_existing", "target": "2026-09-04",
            "timestamp": "2026-09-06T22:31:00+08:00", "akshareStage": {"status": "unavailable"}}))
        self.assertIn("last_snapshot_material_warning", self.codes(self.run_audit()))

    def test_failed_target_unknown_is_not_replaced_with_current_target(self):
        self.last.write_text(json.dumps({"status": "rejected", "timestamp": "2026-09-06T22:31:00+08:00"}))
        result = self.run_audit()
        self.assertIsNone(result["lastRun"]["target"])
        self.assertIn("last_snapshot_not_successful", self.codes(result))

    def test_future_timestamp_is_unknown(self):
        self.last.write_text(json.dumps({"status": "inserted", "timestamp": "2026-09-08T22:31:00+08:00"}))
        self.assertEqual(self.run_audit()["lastRun"]["status"], "unknown")

    def test_malformed_run_metadata_is_unknown(self):
        for fields in ({"timestamp": None}, {"timestamp": []}, {"timestamp": 1},
                       {"akshareStage": []}, {"target": 123}):
            record = {"status": "inserted", "timestamp": "2026-09-06T22:31:00+08:00", **fields}
            self.last.write_text(json.dumps(record))
            self.assertEqual(self.run_audit()["lastRun"]["status"], "unknown")

    def test_strict_json_rejects_overflow_and_duplicate_keys(self):
        for raw in ('{"value":1e999}', '{"date":1,"date":2}', '{"value":NaN}'):
            with self.assertRaises(health.EvidenceError):
                health.strict_json(raw)

    def test_snapshot_float_overflow_is_invalid(self):
        raw = '{"date":"2026-08-28","qlib":{"date":"2026-08-28"},"akshare":{"activity":{"统计日期":"2026-08-28"}},"x":1e999}'
        self.sql("UPDATE snapshot SET payload=? WHERE date=?", (raw, "2026-08-28"))
        self.assertEqual(self.run_audit()["ledger"][0]["snapshotState"], "invalid")

    def test_weekend_rows_and_stages_within_calendar_are_flagged(self):
        day = "2026-08-29"
        payload = {"date": day, "qlib": {"date": day}, "akshare": {"activity": {"统计日期": day}}}
        self.sql("INSERT INTO snapshot VALUES (?,?)", (day, json.dumps(payload)))
        self.stage(day)
        result = self.run_audit()
        self.assertIn("snapshot_outside_known_trading_calendar", self.codes(result))
        self.assertIn("stage_outside_known_trading_calendar", self.codes(result))

    def test_claimed_stage_missing_or_mismatched_is_flagged(self):
        record = {"status": "skipped_existing", "timestamp": "2026-09-06T22:31:00+08:00",
                  "akshareStage": {"status": "reused", "tradeDate": "2026-09-03", "payloadSha256": "0" * 64}}
        self.last.write_text(json.dumps(record))
        self.assertIn("last_run_claimed_stage_missing_or_mismatched", self.codes(self.run_audit()))
        self.stage("2026-09-03")
        self.assertIn("last_run_claimed_stage_missing_or_mismatched", self.codes(self.run_audit()))

    def test_bad_baseline_is_rejected(self):
        baseline = self.root / "baseline.json"
        for hashes in ({"2026-08-28": "abc"}, {"bad_date": "0" * 64}, []):
            baseline.write_text(json.dumps({"rowHashes": hashes}))
            with self.assertRaises(health.EvidenceError):
                self.run_audit(baseline=baseline)

    def test_used_stage_claim_is_checked_separately(self):
        record = {"status": "inserted", "timestamp": "2026-09-06T22:31:00+08:00",
                  "akshareStage": {"usedTradeDate": "2026-09-03", "usedPayloadSha256": "0" * 64}}
        self.last.write_text(json.dumps(record))
        self.assertIn("last_run_claimed_stage_missing_or_mismatched", self.codes(self.run_audit()))

    def test_stage_claim_fields_must_be_paired(self):
        record = {"status": "inserted", "timestamp": "2026-09-06T22:31:00+08:00",
                  "akshareStage": {"tradeDate": "2026-09-03"}}
        self.last.write_text(json.dumps(record))
        self.assertEqual(self.run_audit()["lastRun"]["status"], "unknown")

    def test_latest_row_hash_claim_detects_unbaselined_change(self):
        hashes = self.run_audit()["snapshot"]["rowHashes"]
        record = {"status": "inserted", "timestamp": "2026-09-06T22:31:00+08:00",
                  "target": "2026-09-04", "payloadSha256": hashes["2026-09-04"]}
        self.last.write_text(json.dumps(record))
        self.assertEqual(self.run_audit()["status"], "ok")
        self.sql("UPDATE snapshot SET payload=payload || ' ' WHERE date=?", ("2026-09-04",))
        self.assertIn("last_run_snapshot_missing_or_mismatched", self.codes(self.run_audit()))

    def test_invalid_old_row_does_not_hide_good_rows(self):
        payload = {"date": "2026-08-28", "qlib": {"date": "2026-08-28"},
                   "akshare": {"activity": {"统计日期": "2026-09-04"}}}
        self.sql("UPDATE snapshot SET payload=? WHERE date=?", (json.dumps(payload), "2026-08-28"))
        result = self.run_audit()
        self.assertEqual(result["snapshot"]["rowCount"], 6)
        self.assertEqual(result["snapshot"]["latestValidDate"], "2026-09-04")
        self.assertEqual(result["ledger"][0]["snapshotState"], "invalid")

    def test_baseline_detects_changed_old_payload(self):
        baseline = self.root / "baseline.json"
        result = self.run_audit()
        baseline.write_text(json.dumps({"rowHashes": result["snapshot"]["rowHashes"]}))
        self.assertEqual(self.run_audit(baseline=baseline)["snapshot"]["baseline"], "matched")
        self.sql("UPDATE snapshot SET payload=payload || ' ' WHERE date=?", ("2026-08-28",))
        changed = self.run_audit(baseline=baseline)
        self.assertEqual(changed["status"], "critical")
        self.assertIn("historical_rows_changed_or_missing", self.codes(changed))

    def test_no_baseline_does_not_claim_unchanged(self):
        self.assertEqual(self.run_audit()["snapshot"]["baseline"], "not_provided")

    def test_frozen_policy_not_silently_relaxed(self):
        self.mode.write_text("RUNNING\n")
        result = self.run_audit()
        self.assertEqual(result["status"], "critical")
        self.assertEqual(self.mode.read_text(), "RUNNING\n")


if __name__ == "__main__":
    unittest.main()
