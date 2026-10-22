from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import data_health as health
from test_qlib_incremental import index_claim, legacy_claim, price_claim

BRIDGE_EVIDENCE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                              / "snapshot_diagnostic_chain.json").read_text(encoding="utf-8"))


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

    def test_frozen_mode_does_not_downgrade_active_gaps(self):
        self.sql("DELETE FROM snapshot WHERE date IN (?,?)", ("2026-09-02", "2026-09-03"))
        result = self.run_audit()
        self.assertTrue(result["acceptedFreeze"])
        self.assertEqual(result["status"], "critical")
        gap = next(item for item in result["issues"] if item["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(gap["severity"], "critical")
        self.assertNotIn("acceptedFreeze", gap)
        self.assertTrue(any("does not waive" in item for item in result["limitations"]))

    def test_non_frozen_unresolved_days_stay_critical(self):
        self.mode.write_text("RUNNING\n")
        self.sql("DELETE FROM snapshot WHERE date IN (?,?)", ("2026-09-02", "2026-09-03"))
        result = self.run_audit()
        self.assertEqual(result["status"], "critical")
        self.assertFalse(result["acceptedFreeze"])
        gap = next(item for item in result["issues"] if item["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(gap["severity"], "critical")

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

    def test_deferred_market_endpoint_evidence_is_whitelisted_and_bound_to_run(self):
        record = BRIDGE_EVIDENCE["deferredLastRun"]
        self.last.write_text(json.dumps(record))
        before = self.fingerprint()
        result = self.run_audit()
        self.assertEqual(before, self.fingerprint())
        issue = next(i for i in result["issues"] if i["code"] == "last_snapshot_not_successful")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["status"], "deferred")
        self.assertEqual(issue["target"], "2026-09-04")
        self.assertEqual(issue["runId"], "a" * 32)
        self.assertEqual(issue["runAt"], "2026-09-06T22:31:00+08:00")
        self.assertEqual(issue["sourceFailure"]["attemptCount"], 2)
        self.assertEqual(issue["sourceFailure"]["attempts"][0]["endpoint"],
                         "stock_board_industry_summary_ths")
        self.assertEqual(issue["sourceFailure"]["attempts"][1]["errorType"], "OtherError")
        self.assertNotIn("UPSTREAM_RAW_RESPONSE_NOT_PUBLIC", json.dumps(result))
        self.assertNotIn("SensitiveVendorCodeDoNotPublish", json.dumps(result))

    def test_successful_snapshot_keeps_success_and_reports_separate_stage_warning(self):
        for status in ("inserted", "skipped_existing"):
            with self.subTest(status=status):
                record = json.loads(json.dumps(BRIDGE_EVIDENCE["insertedWithStageWarning"]))
                record["status"] = status
                self.last.write_text(json.dumps(record))
                result = self.run_audit()
                self.assertEqual(result["lastRun"]["status"], status)
                self.assertEqual(result["lastRun"]["target"], "2026-09-04")
                self.assertNotIn("last_snapshot_not_successful", self.codes(result))
                warning = next(i for i in result["issues"] if i["code"] == "last_snapshot_material_warning")
                self.assertEqual(warning["severity"], "warning")
                self.assertEqual(warning["runStatus"], status)
                self.assertTrue(warning["stageUnavailable"])
                self.assertEqual(warning["target"], "2026-09-04")
                self.assertEqual(warning["runId"], "b" * 32)
                self.assertEqual(warning["stageSourceFailure"]["attempts"][1]["errorType"], "OtherError")
                self.assertNotIn("UPSTREAM_RAW_RESPONSE_NOT_PUBLIC", json.dumps(result))
                self.assertNotIn("SensitiveVendorCodeDoNotPublish", json.dumps(result))

    def test_missing_or_bad_stage_failure_keeps_inserted_status_and_warning(self):
        base = BRIDGE_EVIDENCE["insertedWithStageWarning"]
        baseline = self.root / "row-baseline.json"
        baseline.write_text(json.dumps({"rowHashes": self.run_audit()["snapshot"]["rowHashes"]}))
        for failure in (None, [], {"provider": "akshare", "attemptCount": 1,
                                   "attempts": [{"attempt": 1, "endpoint": [], "errorType": "ValueError"}]}):
            with self.subTest(failure=failure):
                record = json.loads(json.dumps(base))
                if failure is None:
                    record["akshareStage"].pop("sourceFailure")
                else:
                    record["akshareStage"]["sourceFailure"] = failure
                self.last.write_text(json.dumps(record))
                result = self.run_audit(baseline=baseline)
                warning = next(i for i in result["issues"] if i["code"] == "last_snapshot_material_warning")
                self.assertEqual(result["lastRun"]["status"], "inserted")
                self.assertEqual(warning["severity"], "warning")
                self.assertEqual(result["snapshot"]["baseline"], "matched")
                self.assertTrue(warning["stageUnavailable"])
                self.assertNotIn("stageSourceFailure", warning)
                self.assertNotIn("last_snapshot_not_successful", self.codes(result))

    def test_missing_or_malformed_source_failure_does_not_change_snapshot_judgment(self):
        malformed = [None, [], {"provider": "akshare", "attemptCount": True, "attempts": []},
                     {"provider": "akshare", "attemptCount": 1,
                      "attempts": [{"attempt": 1, "endpoint": [], "errorType": "ValueError"}]},
                     {"provider": "akshare", "attemptCount": 1,
                      "attempts": [{"attempt": 1, "endpoint": "untrusted_endpoint", "errorType": "ValueError"}]}]
        for failure in malformed:
            with self.subTest(failure=failure):
                record = {"status": "deferred", "target": "2026-09-04",
                          "timestamp": "2026-09-06T22:31:00+08:00"}
                if failure is not None:
                    record["sourceFailure"] = failure
                self.last.write_text(json.dumps(record))
                result = self.run_audit()
                issue = next(i for i in result["issues"] if i["code"] == "last_snapshot_not_successful")
                self.assertEqual(result["lastRun"]["status"], "deferred")
                self.assertEqual(issue["severity"], "warning")
                self.assertEqual(issue["target"], "2026-09-04")
                self.assertNotIn("sourceFailure", issue)

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

    def test_stored_optional_mootdx_error_keeps_row_valid(self):
        day = "2026-08-28"
        payload = {"date": day, "qlib": {"date": day},
                   "akshare": {"activity": {"统计日期": day}},
                   "mootdx": {"error": "mootdx quotes API returned empty"}}
        self.sql("UPDATE snapshot SET payload=? WHERE date=?", (json.dumps(payload), day))
        ledger = {row["tradeDate"]: row for row in self.run_audit()["ledger"]}
        self.assertEqual(ledger[day]["snapshotState"], "valid")

    def test_stored_non_optional_source_error_is_still_invalid(self):
        day = "2026-08-28"
        payload = {"date": day, "qlib": {"date": day},
                   "akshare": {"activity": {"统计日期": day}},
                   "mootdx": {"error": "mootdx quotes API returned empty"},
                   "announcements": {"error": "cninfo unavailable"}}
        self.sql("UPDATE snapshot SET payload=? WHERE date=?", (json.dumps(payload), day))
        ledger = {row["tradeDate"]: row for row in self.run_audit()["ledger"]}
        self.assertEqual(ledger[day]["snapshotState"], "invalid")
        self.assertEqual(ledger[day]["reason"], "stored_source_errors")

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

    def incremental_fixture(self):
        extra = ["2026-09-07", "2026-09-08"]
        (self.qlib / "calendars/day.txt").write_text("\n".join(self.days + extra) + "\n")
        for symbol in health.REFERENCES:
            self.features(symbol, len(self.days) + len(extra))
        document = {
            "schemaVersion": 1, "kind": "same_source_dolt_incremental", "status": "validated",
            "sourceRepository": "chenditc/investment_data", "sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq",
            "generationId": "a" * 32, "baseReleaseTag": "2026-09-05",
            "baseDate": "2026-09-04", "targetDate": "2026-09-08", "calendarAdded": extra,
            "sourceRowsSha256": "b" * 64,
            "historicalPrefixes": {"unchanged": True, "files": 17, "bytes": 2000, "sha256": "c" * 64},
            "sourceAbsence": {"sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq", "dates": {}},
            "priceValidation": price_claim(extra),
            "legacyUnquotedCSI": legacy_claim(extra),
            "indexPriceValidation": index_claim("2026-09-08"),
        }
        path = self.qlib / health.INCREMENTAL_MANIFEST
        path.write_text(json.dumps(document))
        return path, document

    def test_incremental_audit_keeps_release_label_and_readonly_scope(self):
        self.incremental_fixture()
        before = self.fingerprint()
        result = self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))
        self.assertEqual(result["qlib"]["releaseTag"], "2026-09-05")
        self.assertEqual(result["qlib"]["referenceCompleteDate"], "2026-09-08")
        self.assertEqual(result["qlib"]["dataGeneration"], "same_source_dolt_incremental")
        self.assertEqual(result["qlib"]["incremental"]["targetDate"], "2026-09-08")
        self.assertEqual(result["qlib"]["fullMarketCoverage"], "not_scanned")
        self.assertEqual(result["qlib"]["dataQuality"], "complete")
        self.assertNotIn("qlib_source_absence", self.codes(result))
        self.assertEqual(before, self.fingerprint())
        self.assertFalse(result["automaticRecoveryAuthorized"])

    def test_partial_incremental_proven_absence_is_information_not_warning(self):
        """已证明的有界源缺失记为 information：保留日期与含义，不暗示需要处置。"""
        path, document = self.incremental_fixture()
        absence = {"SH600825": ["2026-09-07", "2026-09-08"]}
        document["sourceAbsence"]["dates"] = absence
        document["priceValidation"] = price_claim(document["calendarAdded"], absence,
            {"2026-09-07": {"SH600825": ["source_absent_today"]},
             "2026-09-08": {"SH600825": ["source_absent_today", "prior_close_source_absent"]}},
            {"SH600825": "2026-09-04"})
        path.write_text(json.dumps(document))
        before = self.fingerprint()
        result = self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))
        self.assertEqual(result["qlib"]["dataQuality"], "partial")
        entry = next(issue for issue in result["issues"] if issue["code"] == "qlib_source_absence")
        self.assertEqual(entry["severity"], "information")
        self.assertTrue(entry["proven"])
        self.assertEqual(entry["meaning"], "source_absent_not_confirmed_suspension")
        self.assertEqual(entry["affectedDates"], ["2026-09-07", "2026-09-08"])
        # 覆盖门禁与整体状态不受分类影响：仍然是 partial 数据质量
        self.assertEqual(result["qlib"]["incremental"]["dailyCoverage"][-1]["status"], "partial")
        self.assertEqual(result["qlib"]["fullMarketCoverage"], "not_scanned")
        self.assertFalse(result["automaticRecoveryAuthorized"])
        self.assertEqual(before, self.fingerprint())

    def test_unproven_absence_still_fails_closed(self):
        """未被证明的缺失（reason 不在白名单/符号未出现在 sourceAbsence）必须失败，不得降级。"""
        path, document = self.incremental_fixture()
        absence = {"SH600825": ["2026-09-07", "2026-09-08"]}
        document["sourceAbsence"]["dates"] = absence
        document["priceValidation"] = price_claim(document["calendarAdded"], absence,
            {"2026-09-07": {"SH600825": ["source_absent_today"]},
             "2026-09-08": {"SH600825": ["source_absent_today", "prior_close_source_absent"]}},
            {"SH600825": "2026-09-04"})
        # 把符号从 sourceAbsence 里移除 → 证明不完整
        document["sourceAbsence"]["dates"] = {"SH600825": ["2026-09-07"]}
        path.write_text(json.dumps(document))
        with self.assertRaises(health.EvidenceError):
            self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))

    def test_absence_threshold_uses_full_market_row_not_csi_union(self):
        # Fixed validated-result stub isolates audit aggregation; this does not
        # claim that arbitrary counts pass the upstream manifest validator.
        import copy
        self.incremental_fixture()
        calendar, complete, generation = health.qlib_evidence(self.qlib)
        for invalid, csi_count, severity in ((11, 8, "information"), (26, 26, "warning"), (28, 10, "warning")):
            changed = list(copy.deepcopy(generation))
            incremental = list(changed[3])
            quality = incremental[9]
            quality["priceStatus"] = "partial"
            symbols = ["SH" + str(600000+i) for i in range(invalid)]
            quality["dailyCoverage"][-1].update(invalid=invalid, eligible=5567, invalid_symbols=symbols)
            quality["sourceAbsenceDates"] = {symbol: [complete] for symbol in symbols}
            quality["lastQuotedDate"] = {symbol: calendar[calendar.index(complete)-1] for symbol in symbols}
            index = quality["index"]["validation"]
            index.update(missingQuoteMembers=["SH" + str(600000+i) for i in range(csi_count)],
                         missingPreviousCloseMembers=[], absenceMeaning="source_absent_not_confirmed_suspension",
                         uniqueMembers=5085, sourceAbsenceSlotsVerified=invalid)
            changed[3] = tuple(incremental)
            with mock.patch.object(health, "qlib_evidence", return_value=(calendar, complete, tuple(changed))), \
                 mock.patch.object(health, "generation", return_value=tuple(changed)):
                result = self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))
            entry = next(x for x in result["issues"] if x["code"] == "qlib_source_absence")
            self.assertEqual(entry["invalidCount"], invalid)
            self.assertEqual(entry["eligibleCount"], 5567)
            self.assertEqual(entry["csiMissingMemberCount"], csi_count)
            self.assertEqual(entry["severity"], severity)
            self.assertEqual(entry["absenceScopes"]["csi_members"]["eligibleCount"], 5085)
            if invalid == 26:
                self.assertEqual(entry["absenceScopes"]["full_market"]["severity"], "information")
                self.assertEqual(entry["absenceScopes"]["csi_members"]["severity"], "warning")

    def test_absence_severity_escalates_when_bounds_are_exceeded(self):
        """已证明缺失的分级：数量/持续期超上界时重新升级，不做永久豁免。

        proven=true 只说明缺失声明与清单/计数一致，不证明停牌，也不证明对研究无影响。
        """
        self.assertEqual(health.absence_severity(True, 9, ["2026-09-18"], 5567),
                         ("information", None))
        self.assertEqual(health.absence_severity(True, 40, ["2026-09-18"], 5567),
                         ("warning", "count_over_tolerance:40>27.8"))
        self.assertEqual(health.absence_severity(
            True, 9, ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"], 5567),
            ("warning", "duration_over_bound:4>3"))
        self.assertEqual(health.absence_severity(False, 9, ["2026-09-18"], 5567),
                         ("warning", "proof_incomplete"))

    def test_legacy_unquoted_severity_is_bounded_and_re_escalates(self):
        """历史特征边界的分级：成员数扩大或触及必需参考符号时重新升级。

        proven 只说明“最后报价早于基线日、声明与计数一致”，不证明停牌，也不证明无研究影响。
        """
        self.assertEqual(health.legacy_unquoted_severity(True, ["SZ301139"], health.REFERENCES),
                         ("information", None))
        self.assertEqual(health.legacy_unquoted_severity(
            True, ["SZ301139", "SZ301140", "SZ301141", "SZ301142"], health.REFERENCES),
            ("warning", "count_over_bound:4>3"))
        self.assertEqual(health.legacy_unquoted_severity(True, [health.REFERENCES[0]], health.REFERENCES),
                         ("warning", "reference_symbol_affected"))
        self.assertEqual(health.legacy_unquoted_severity(False, ["SZ301139"], health.REFERENCES),
                         ("warning", "meaning_unexpected"))

    def test_legacy_has_unknown_quality_not_fabricated_complete(self):
        self.assertEqual(self.run_audit()["qlib"]["dataQuality"], "unknown")

    def test_legacy_csi_only_partial_has_separate_warning_and_inventory_labels(self):
        path, document = self.incremental_fixture()
        document["legacyUnquotedCSI"] = legacy_claim(document["calendarAdded"], {"SH600929": "2026-09-03"})
        document["indexPriceValidation"] = index_claim(document["targetDate"], legacy=["SH600929"])
        path.write_text(json.dumps(document))
        before = self.fingerprint()
        result = self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))
        self.assertEqual(result["qlib"]["dataQuality"], "partial")
        self.assertEqual(result["qlib"]["incremental"]["currentInventoryQuality"], "complete")
        self.assertEqual(result["qlib"]["incremental"]["csiInventoryQuality"], "partial")
        self.assertIn("qlib_legacy_unquoted_csi", self.codes(result))
        self.assertNotIn("qlib_source_absence", self.codes(result))
        self.assertEqual(result["qlib"]["fullMarketCoverage"], "not_scanned")
        self.assertEqual(before, self.fingerprint())

    def test_legacy_issue_binds_distinct_dates_to_validated_generation(self):
        path, document = self.incremental_fixture()
        document["legacyUnquotedCSI"] = legacy_claim(document["calendarAdded"], {"SH600929": "2026-09-03"})
        document["indexPriceValidation"] = index_claim(document["targetDate"], legacy=["SH600929"])
        path.write_text(json.dumps(document))
        before = self.fingerprint()
        result = self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))
        item = next(i for i in result["issues"] if i["code"] == "qlib_legacy_unquoted_csi")
        self.assertEqual(item["targetDate"], "2026-09-08")
        self.assertEqual(item["affectedDates"], ["2026-09-07", "2026-09-08"])
        self.assertEqual(item["legacyUnquotedBaseDate"], "2026-09-04")
        self.assertEqual(item["lastQuotedDates"], {"SH600929": "2026-09-03"})
        self.assertEqual(item["sourceManifestSha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(item["validationScope"], "local_manifest_calendar_and_reference_only")
        self.assertEqual(before, self.fingerprint())

    def test_legacy_bad_or_missing_last_quote_cannot_be_replaced_by_target(self):
        path, baseline = self.incremental_fixture()
        for last in (None, "2026-09-04", "2026-02-30", "2026-09-08"):
            with self.subTest(last=last):
                document = json.loads(json.dumps(baseline))
                document["legacyUnquotedCSI"] = legacy_claim(document["calendarAdded"], {"SH600929": last})
                document["indexPriceValidation"] = index_claim(document["targetDate"], legacy=["SH600929"])
                path.write_text(json.dumps(document))
                with self.assertRaises(health.EvidenceError):
                    self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))

    def test_manifest_change_after_qlib_read_detected_at_audit_end(self):
        path, document = self.incremental_fixture()
        original = health.snapshot_rows
        def mutate_after_read(db):
            rows = original(db)  # Only this test's TemporaryDirectory database.
            path.write_text(json.dumps(dict(document, sourceRowsSha256="d" * 64)))
            return rows
        with mock.patch.object(health, "snapshot_rows", side_effect=mutate_after_read):
            with self.assertRaisesRegex(health.EvidenceError, "generation_changed_during_audit"):
                self.run_audit(now=dt.datetime(2026, 9, 9, 12, tzinfo=health.CST))

    def test_module_loads_by_path_without_its_directory_on_sys_path(self):
        """Regression: qlib's read-only consumer loads this file with
        spec_from_file_location and no sys.path entry, which used to fail with
        ModuleNotFoundError for the shared source_policy import (2026-09-19)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("_consumer_probe", health.__file__)
        module = importlib.util.module_from_spec(spec)
        original = list(sys.path)
        try:
            sys.path[:] = [entry for entry in sys.path if entry != str(Path(health.__file__).parent)]
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = original
        self.assertTrue(callable(module.is_optional_source_error))
        self.assertTrue(module.is_optional_source_error("mootdx.error"))
        self.assertFalse(module.is_optional_source_error("announcements.error"))
        # The shared policy still comes from the single policy module.
        self.assertIs(module.is_optional_source_error.__module__, "_nng8_source_policy")

    def test_audit_output_is_atomic_and_rejects_symlinks(self):
        destination = self.root / "last-health.json"
        health.write_audit_output(destination, '{"status":"critical"}\n')
        self.assertEqual(destination.read_text(), '{"status":"critical"}\n')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        decoy = self.root / "decoy"
        decoy.write_text("secret")
        linked = self.root / "link.json"
        linked.symlink_to(decoy)
        with self.assertRaisesRegex(health.EvidenceError, "audit_output_not_regular_file"):
            health.write_audit_output(linked, '{"status":"ok"}\n')
        self.assertEqual(decoy.read_text(), "secret")


if __name__ == "__main__":
    unittest.main()
