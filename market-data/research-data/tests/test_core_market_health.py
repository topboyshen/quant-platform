import datetime as dt
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

import core_market as core
import data_health as health
import test_data_health as legacy_health_tests
from test_core_market_contract import qlib, calendar, closed_calendar


class CoreHealthTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing full-audit fixture, not production observations.
        self.f = legacy_health_tests.AuditTests(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.now = dt.datetime(2026, 9, 4, 23, 0, tzinfo=health.CST)
        self.policy = self.f.root / "contract.json"
        self.policy.write_text(json.dumps({"activeContract": "research.core-market.v1", "effectiveTradeDate": "2026-09-04"}))
        self.core_db = self.f.root / "core-market.db"
        self.core_last = self.f.root / "core-last-run.json"
        q = qlib(); q["date"] = "2026-09-04"
        stage = json.loads((self.f.stages / "2026-09-04.json").read_text())
        self.record = core.build_core(q, run_id="a" * 32, generated_at="2026-09-04T22:30:00+08:00", calendar_evidence=calendar("2026-09-04"))
        core.store_core(self.record, self.core_db)
        self.core_last.write_text(json.dumps({"schema": core.SCHEMA, "scope": "core_market", "fullSnapshot": False,
            "runId": "a" * 32, "status": "inserted", "target": "2026-09-04", "timestamp": "2026-09-04T22:31:00+08:00",
            "payloadSha256": core.digest(self.record), "calendarEvidence": calendar("2026-09-04")}))
        self.f.sql("DELETE FROM snapshot WHERE date='2026-09-04'")

    def audit(self, **kwargs):
        opts = dict(contract_policy=self.policy, core_db=self.core_db, core_last_run=self.core_last)
        opts.update(kwargs)
        return self.f.run_audit(**opts)

    def codes(self, value):
        return {x["code"] for x in value["issues"]}

    def test_default_keeps_full_contract_even_when_core_file_exists(self):
        result = self.audit(contract_policy=None)
        self.assertEqual(result["activeContract"], "research.full-snapshot.v1")
        self.assertEqual(result["coreMarket"]["readState"], "checked")
        self.assertIn("core_history_policy_missing_or_conflicting", self.codes(result))
        self.assertIn("unresolved_trading_day_gaps", self.codes(result))

    def test_preinstalled_unit_with_missing_policy_stays_full(self):
        self.policy.unlink()
        result = self.audit()
        self.assertEqual(result["activeContract"], "research.full-snapshot.v1")
        self.assertEqual(result["coreMarket"]["readState"], "checked")
        self.assertIn("core_history_policy_missing_or_conflicting", self.codes(result))
        self.assertIn("unresolved_trading_day_gaps", self.codes(result))

    def test_effective_day_is_core_not_fake_full_success(self):
        result = self.audit()
        self.assertEqual(result["activeContract"], core.SCHEMA)
        self.assertEqual(result["coreMarket"]["ledger"][0]["coreState"], "valid")
        legacy_row = next(x for x in result["ledger"] if x["tradeDate"] == "2026-09-04")
        self.assertEqual(legacy_row["snapshotState"], "missing")
        self.assertFalse(legacy_row["expectedUnderContract"])
        self.assertFalse(result["coreMarket"]["fullSnapshot"])
        self.assertNotIn("unresolved_trading_day_gaps", self.codes(result))
        self.assertNotIn("scheduled_snapshot_missing", self.codes(result))
        self.assertNotIn("unresolved_core_market_gaps", self.codes(result))
        self.assertNotEqual(result["snapshot"]["latestValidDate"], "2026-09-04")
        self.assertEqual(result["coreMarket"]["quality"]["payloadSha256"], core.digest(self.record))
        self.assertEqual(result["coreMarket"]["quality"]["invalid"], 11)
        self.assertEqual(result["coreMarket"]["quality"]["eligible"], 5567)
        self.assertEqual(result["coreMarket"]["quality"]["priceStatus"], "partial")

    def test_historical_missing_stays_missing_with_same_severity(self):
        self.f.sql("DELETE FROM snapshot WHERE date IN ('2026-09-01','2026-09-02')")
        result = self.audit()
        gap = next(x for x in result["issues"] if x["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(gap["dates"], ["2026-09-01", "2026-09-02"])
        self.assertEqual(gap["severity"], "critical")
        self.assertEqual(result["status"], "critical")

    def test_core_missing_alarms_and_cannot_use_old_full_row(self):
        self.core_db.unlink()
        self.f.sql("INSERT INTO snapshot VALUES (?,?)", ("2026-09-04", json.dumps({"date": "2026-09-04", "qlib": {"date": "2026-09-04"}, "akshare": {"activity": {"统计日期": "2026-09-04 15:00:00"}}})))
        result = self.audit()
        self.assertIn("unresolved_core_market_gaps", self.codes(result))
        self.assertIn("core_market_evidence_unavailable", self.codes(result))
        self.assertIn("core_market_run_missing_or_mismatched", self.codes(result))
        self.assertEqual(next(x for x in result["issues"] if x["code"] == "legacy_full_rows_after_cutover")["severity"], "critical")
        self.assertEqual(result["coreMarket"]["ledger"][0]["coreState"], "missing")

    def test_wrong_schema_or_hash_cannot_pass_core(self):
        import sqlite3
        with sqlite3.connect(self.core_db) as conn:
            conn.execute("UPDATE core_market SET payload_sha256=?", ("0" * 64,))
        result = self.audit()
        self.assertEqual(result["coreMarket"]["ledger"][0]["coreState"], "invalid")
        self.assertIn("unresolved_core_market_gaps", self.codes(result))

    def test_core_last_run_missing_stale_wrong_contract_and_failed(self):
        current = json.loads(self.core_last.read_text())
        for change in ({"schema": "research.full-snapshot.v1"}, {"timestamp": "2026-09-03T22:31:00+08:00"}):
            self.core_last.write_text(json.dumps({**current, **change}))
            self.assertIn("scheduled_core_market_missing", self.codes(self.audit()))
        self.core_last.write_text(json.dumps({**current, "status": "deferred"}))
        self.assertIn("last_core_market_not_successful", self.codes(self.audit()))
        self.core_last.unlink()
        self.assertIn("scheduled_core_market_missing", self.codes(self.audit()))

    def test_future_effective_date_does_not_switch(self):
        self.policy.write_text(json.dumps({"activeContract": core.SCHEMA, "effectiveTradeDate": "2026-09-07"}))
        result = self.audit()
        self.assertEqual(result["activeContract"], "research.full-snapshot.v1")
        self.assertEqual(result["coreMarket"]["ledger"], [])
        self.assertIn("unresolved_trading_day_gaps", self.codes(result))

    def test_invalid_policy_fails_closed(self):
        for value in ({"activeContract": "unknown", "effectiveTradeDate": "2026-09-04"},
                      {"activeContract": core.SCHEMA, "effectiveTradeDate": None},
                      {"activeContract": core.SCHEMA}):
            self.policy.write_text(json.dumps(value))
            with self.assertRaises(health.EvidenceError): self.audit()

    def test_legacy_baseline_still_detects_deleted_old_row(self):
        baseline = self.f.root / "baseline.json"
        baseline.write_text(json.dumps({"rowHashes": {d: r["payloadSha256"] for d, r in health.snapshot_rows(self.f.db).items()}}))
        self.f.sql("DELETE FROM snapshot WHERE date='2026-08-28'")
        self.assertIn("historical_rows_changed_or_missing", self.codes(self.audit(baseline=baseline)))

    def test_core_audit_does_not_import_collectors(self):
        # The helper's dependency is a pure module, not core_market/daily_snapshot.
        self.assertNotIn("daily_snapshot", health._core_contract.__dict__)
        self.assertNotIn("core_market", health._core_contract.__dict__)
        self.assertEqual(health.core_market_rows(self.core_db, self.f.now)[1], "checked")

    def test_optional_stage_absence_cannot_block_core_but_old_full_rule_stays(self):
        for file in self.f.stages.glob("*.json"):
            file.unlink()
        self.assertNotIn("no_sealed_market_evidence", self.codes(self.audit()))
        self.assertIn("no_sealed_market_evidence", self.codes(self.audit(contract_policy=None)))

    def test_old_invalid_stage_remains_historical_issue(self):
        path = self.f.stages / "2026-09-02.json"
        path.write_text("{")
        result = self.audit()
        issue = next(x for x in result["issues"] if x["code"] == "market_stage_missing_or_invalid")
        self.assertTrue(issue["historicalOnly"])
        self.assertEqual(issue["contract"], "research.full-snapshot.v1")

    def test_closed_skip_requires_current_bound_calendar_and_never_claims_core_row(self):
        self.f.now = dt.datetime(2026, 9, 5, 23, tzinfo=health.CST)
        value = {"schema": core.SCHEMA, "scope": "core_market", "fullSnapshot": False,
                 "runId": "b" * 32, "status": "skipped_non_trading", "target": "2026-09-04",
                 "timestamp": "2026-09-05T22:30:00+08:00", "calendarEvidence": closed_calendar("2026-09-05")}
        self.core_last.write_text(json.dumps(value))
        result = self.audit()
        self.assertNotIn("scheduled_core_market_missing", self.codes(result))
        self.assertNotIn("last_core_market_not_successful", self.codes(result))
        self.assertEqual(result["activeLastRun"]["status"], "skipped_non_trading")
        self.assertIsNone(result["coreMarket"]["quality"])
        self.assertTrue(result["activeLastRun"]["calendarClosedVerified"])
        for change in ({"target": "2026-09-03"}, {"calendarEvidence": calendar("2026-09-05")},
                       {"calendarEvidence": closed_calendar("2026-09-04")}, {"calendarEvidence": None},
                       {"payloadSha256": "a" * 64}):
            self.core_last.write_text(json.dumps({**value, **change}))
            self.assertIn("scheduled_core_market_missing", self.codes(self.audit()), change)

    def test_new_time_cannot_make_old_target_a_current_success(self):
        value = json.loads(self.core_last.read_text())
        value.update(target="2026-09-03")
        self.core_last.write_text(json.dumps(value))
        self.assertIn("scheduled_core_market_missing", self.codes(self.audit()))

    def test_first_cutover_is_closed_no_database_is_not_new_fault(self):
        self.f.now = dt.datetime(2026, 9, 5, 23, tzinfo=health.CST)
        self.policy.write_text(json.dumps({"activeContract": core.SCHEMA, "effectiveTradeDate": "2026-09-05"}))
        self.core_db.unlink()
        self.core_last.write_text(json.dumps({"schema": core.SCHEMA, "scope": "core_market", "fullSnapshot": False,
            "runId": "b" * 32, "status": "skipped_non_trading", "target": "2026-09-04",
            "timestamp": "2026-09-05T22:30:00+08:00", "calendarEvidence": closed_calendar("2026-09-05")}))
        self.assertNotIn("core_market_evidence_unavailable", self.codes(self.audit()))
        self.assertNotIn("scheduled_core_market_missing", self.codes(self.audit()))

    def test_optional_post_cutover_stage_not_an_upstream_core_clock(self):
        self.f.now = dt.datetime(2026, 9, 8, 23, tzinfo=health.CST)
        self.f.stage("2026-09-07")
        self.assertNotIn("qlib_behind_observed_market_stage", self.codes(self.audit()))
        self.assertIn("qlib_behind_observed_market_stage", self.codes(self.audit(contract_policy=None)))

    def test_forged_closed_on_known_trading_day_is_critical_conflict(self):
        self.core_last.write_text(json.dumps({"schema": core.SCHEMA, "scope": "core_market", "fullSnapshot": False,
            "runId": "b" * 32, "status": "skipped_non_trading", "target": "2026-09-03",
            "timestamp": "2026-09-04T22:30:00+08:00", "calendarEvidence": closed_calendar("2026-09-04")}))
        result = self.audit()
        self.assertEqual(next(x for x in result["issues"] if x["code"] == "core_calendar_schedule_conflict")["severity"], "critical")


if __name__ == "__main__":
    unittest.main()
