"""Fixed local artifacts exercise the real full/core audit, without collectors."""
import datetime as dt
import json
import sqlite3
import unittest
from unittest import mock

import core_market as core
import data_health as health
import test_data_health as legacy_tests
from test_core_market_contract import calendar, closed_calendar, qlib


DAY = "2026-09-04"


class ActiveRunDecisionTests(unittest.TestCase):
    def setUp(self):
        self.f = legacy_tests.AuditTests(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.now = dt.datetime.fromisoformat(DAY + "T23:00:00+08:00")
        self.full_run = {"runId": "a" * 32, "status": "inserted", "target": DAY,
                         "startedAt": DAY + "T22:30:00+08:00", "timestamp": DAY + "T22:31:00+08:00",
                         "payloadSha256": health.snapshot_rows(self.f.db)[DAY]["payloadSha256"]}
        self.f.last.write_text(json.dumps(self.full_run))
        self.policy = self.f.root / "contract.json"
        self.core_db = self.f.root / "core.db"
        self.core_last = self.f.root / "core-last.json"
        self.core_run = None

    def activate_core(self):
        self.policy.write_text(json.dumps({"activeContract": core.SCHEMA, "effectiveTradeDate": DAY}))
        q = qlib()
        q["date"] = DAY
        record = core.build_core(q, run_id="b" * 32, generated_at=DAY + "T22:30:00+08:00",
                                 calendar_evidence=calendar(DAY))
        core.store_core(record, self.core_db)
        self.core_run = {**self.full_run, "runId": "b" * 32, "schema": core.SCHEMA,
                         "scope": "core_market", "fullSnapshot": False,
                         "payloadSha256": core.digest(record), "calendarEvidence": calendar(DAY)}
        self.core_last.write_text(json.dumps(self.core_run))
        self.f.sql("DELETE FROM snapshot WHERE date=?", (DAY,))

    def audit(self, evidence=None, unavailable=False, **opts):
        if self.core_run is not None:
            opts.update(contract_policy=self.policy, core_db=self.core_db, core_last_run=self.core_last)
        calendar_opts = {"side_effect": ValueError("unavailable")} if unavailable else {"return_value": evidence or calendar(DAY)}
        with mock.patch.object(health._calendar_evidence, "load_calendar_evidence", **calendar_opts):
            return self.f.run_audit(calendar_project=self.f.root / "calendar", **opts)

    def test_full_current_pass_keeps_frozen_historical_gaps_visible(self):
        self.f.sql("DELETE FROM snapshot WHERE date IN ('2026-09-02','2026-09-03')")
        result = self.audit()
        decision = result["activeRunDecision"]
        # Accepted FROZEN (the mode since 2026-08-22) keeps the gaps visible as
        # limitations but no longer pages on them; the current run still passes.
        self.assertTrue(result["acceptedFreeze"])
        self.assertEqual(result["status"], "warning")
        gap = next(item for item in result["issues"] if item["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(gap["severity"], "information")
        self.assertTrue(gap["acceptedFreeze"])
        self.assertEqual(gap["dates"], ["2026-09-02", "2026-09-03"])
        self.assertEqual(decision["verification"]["status"], "pass")
        self.assertEqual(decision["schema"], "research.active-run-decision.v1")
        self.assertTrue(decision["fullSnapshot"])
        self.assertEqual(decision["run"]["runId"], self.full_run["runId"])
        self.assertEqual(decision["artifact"]["payloadSha256"], self.full_run["payloadSha256"])
        self.assertIn("unresolved_trading_day_gaps", decision["verification"]["historicalIssueCodes"])
        self.assertNotIn("unresolved_trading_day_gaps", decision["verification"]["currentIssueCodes"])
        self.assertEqual(decision["scheduledDate"], DAY)
        self.assertEqual(decision["run"]["startedAt"], self.full_run["startedAt"])

    def test_non_frozen_historical_gaps_stay_critical(self):
        self.f.mode.write_text("RUNNING\n")
        self.f.sql("DELETE FROM snapshot WHERE date IN ('2026-09-02','2026-09-03')")
        result = self.audit()
        self.assertFalse(result["acceptedFreeze"])
        self.assertEqual(result["status"], "critical")
        gap = next(item for item in result["issues"] if item["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(gap["severity"], "critical")
        self.assertNotIn("acceptedFreeze", gap)
        self.assertIn("unresolved_trading_day_gaps",
                      result["activeRunDecision"]["verification"]["historicalIssueCodes"])

    def test_core_current_pass_declares_summary_and_its_bound_quality(self):
        self.activate_core()
        result = self.audit()
        decision = result["activeRunDecision"]
        self.assertEqual(decision["verification"]["status"], "pass")
        self.assertEqual(decision["contract"], core.SCHEMA)
        self.assertFalse(decision["fullSnapshot"])
        self.assertEqual(decision["artifactKind"], "qlib_market_summary")
        self.assertEqual(decision["artifact"]["quality"]["invalid"], 11)
        self.assertEqual(decision["artifact"]["quality"]["payloadSha256"], self.core_run["payloadSha256"])

    def test_current_success_cannot_pass_without_independent_calendar(self):
        for contract in ("full", "core"):
            if contract == "core":
                self.activate_core()
            with self.subTest(contract=contract):
                value = self.audit(unavailable=True)["activeRunDecision"]
                self.assertEqual(value["verification"]["status"], "insufficient")
                self.assertIn("calendar_evidence_unavailable", value["verification"]["reasons"])
                self.assertIsNone(value["calendar"])

    def test_same_day_run_before_schedule_is_not_natural_success(self):
        record = {**self.full_run, "startedAt": DAY + "T07:00:00+08:00", "timestamp": DAY + "T07:01:00+08:00"}
        self.f.last.write_text(json.dumps(record))
        self.assertEqual(self.audit()["activeRunDecision"]["verification"]["status"], "insufficient")

    def test_before_deadline_waits_for_today_and_does_not_reuse_yesterday(self):
        record = {**self.full_run, "startedAt": "2026-09-03T22:30:00+08:00", "timestamp": "2026-09-03T22:31:00+08:00"}
        self.f.last.write_text(json.dumps(record))
        self.f.now = dt.datetime.fromisoformat(DAY + "T22:20:00+08:00")
        value = self.audit()["activeRunDecision"]
        self.assertEqual(value["scheduledDate"], DAY)
        self.assertEqual(value["verification"]["status"], "pending")
        self.f.now = dt.datetime.fromisoformat(DAY + "T23:00:00+08:00")
        self.assertEqual(self.audit()["activeRunDecision"]["verification"]["status"], "insufficient")

    def test_missing_corrupt_and_incomplete_run_identity_cannot_pass(self):
        original = self.f.last.read_text()
        for value in (None, "{", json.dumps({**self.full_run, "runId": "invalid"}),
                      json.dumps({k: v for k, v in self.full_run.items() if k != "startedAt"})):
            if value is None:
                self.f.last.unlink()
            else:
                self.f.last.write_text(value)
            with self.subTest(value=value):
                result = self.audit()
                self.assertEqual(result["activeRunDecision"]["verification"]["status"], "insufficient")
            self.f.last.write_text(original)

    def test_incomplete_start_keeps_valid_completion_but_cannot_pass(self):
        self.f.last.write_text(json.dumps({k: v for k, v in self.full_run.items() if k != "startedAt"}))
        result = self.audit()
        decision = result["activeRunDecision"]
        self.assertIsNone(decision["run"]["startedAt"])
        self.assertEqual(decision["run"]["completedAt"], result["activeLastRun"]["timestamp"])
        self.assertEqual(decision["verification"]["status"], "insufficient")
        for field, invalid, intact in (("startedAt", "invalid", "completedAt"),
                                      ("timestamp", "invalid", "startedAt")):
            value = {**self.full_run, field: invalid}
            decision = health.active_run_decision(contract=health._core_contract.LEGACY_SCHEMA,
                last=value, rows=health.snapshot_rows(self.f.db), stages={}, now=self.f.now,
                calendar=None, evidence=None, issues=[], known_trading_dates=set())
            with self.subTest(field=field):
                self.assertIsNone(decision["run"]["startedAt" if field == "startedAt" else "completedAt"])
                self.assertEqual(decision["run"][intact], self.full_run["startedAt" if intact == "startedAt" else "timestamp"])
                self.assertEqual(decision["verification"]["status"], "insufficient")

    def test_explicit_current_failure_has_its_own_verdict(self):
        for contract in ("full", "core"):
            if contract == "core":
                self.activate_core()
            path, original = ((self.f.last, self.full_run) if contract == "full" else (self.core_last, self.core_run))
            path.write_text(json.dumps({**original, "status": "deferred"}))
            with self.subTest(contract=contract):
                value = self.audit()["activeRunDecision"]
                self.assertEqual(value["verification"]["status"], "fail")
                self.assertIn("current_run_not_successful", value["verification"]["reasons"])

    def test_matching_run_hash_still_requires_valid_persisted_artifact(self):
        self.f.sql("DELETE FROM snapshot WHERE date=?", (DAY,))
        value = self.audit()["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "fail")
        self.assertIn("current_run_artifact_missing_or_mismatched", value["verification"]["reasons"])
        self.activate_core()
        with sqlite3.connect(self.core_db) as conn:
            conn.execute("UPDATE core_market SET payload_sha256=?", ("0" * 64,))
        self.assertEqual(self.audit()["activeRunDecision"]["verification"]["status"], "fail")

    def test_full_requires_current_valid_stage_core_does_not(self):
        (self.f.stages / (DAY + ".json")).unlink()
        full = self.audit()["activeRunDecision"]
        self.assertEqual(full["verification"]["status"], "fail")
        self.assertIn("current_full_stage_missing_or_invalid", full["verification"]["reasons"])
        self.activate_core()
        self.assertEqual(self.audit()["activeRunDecision"]["verification"]["status"], "pass")

    def test_closed_skips_are_not_required_without_claiming_new_rows(self):
        closed_day = "2026-09-05"
        self.f.now = dt.datetime.fromisoformat(closed_day + "T23:00:00+08:00")
        closed = closed_calendar(closed_day)
        skip = {"runId": "c" * 32, "status": "skipped_non_trading", "target": DAY,
                "startedAt": closed_day + "T22:30:00+08:00", "timestamp": closed_day + "T22:31:00+08:00",
                "calendarEvidence": closed}
        self.f.last.write_text(json.dumps(skip))
        value = self.audit(evidence=closed)["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "not_required")
        self.assertIsNone(value["artifact"]["payloadSha256"])
        self.assertEqual(value["calendar"]["sourceDate"], closed_day)
        self.activate_core()
        self.core_last.write_text(json.dumps({**skip, "schema": core.SCHEMA, "scope": "core_market", "fullSnapshot": False}))
        core_decision = self.audit(evidence=closed)["activeRunDecision"]
        self.assertEqual(core_decision["verification"]["status"], "not_required")
        self.assertIsNone(core_decision["artifact"]["quality"])

    def test_closed_interval_uses_original_identity_despite_different_derivation_times(self):
        source = calendar(DAY)
        source["document"]["nextTradingDate"] = "2026-09-07"
        source["sha256"] = core.digest({k: v for k, v in source.items() if k != "sha256"})
        closed_day = "2026-09-05"
        def interval(at):
            result = {"document": {**source["document"], "targetDate": closed_day, "status": "closed",
                        "previousTradingDate": DAY, "validUntil": "2026-09-06T23:59:59.999999+08:00"},
                      "binding": {"kind": "validated_closed_interval", "sourceEvidence": source, "derivedAt": at}}
            result["sha256"] = core.digest(result)
            return result
        self.f.now = dt.datetime.fromisoformat(closed_day + "T23:00:00+08:00")
        writer_evidence = interval(closed_day + "T22:30:00+08:00")
        audit_evidence = interval(closed_day + "T23:00:00+08:00")
        self.f.last.write_text(json.dumps({"runId": "c" * 32, "status": "skipped_non_trading", "target": DAY,
            "startedAt": closed_day + "T22:30:00+08:00", "timestamp": closed_day + "T22:31:00+08:00",
            "calendarEvidence": writer_evidence}))
        value = self.audit(evidence=audit_evidence)["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "not_required")
        self.assertEqual(value["calendar"]["sourceDate"], DAY)
        self.assertEqual(value["calendar"]["observedAt"], source["document"]["observedAt"])
        self.assertEqual(value["calendar"]["sourceEvidenceSha256"], source["sha256"])

    def test_late_success_and_changed_historical_hash_are_not_current_pass(self):
        self.f.last.write_text(json.dumps({**self.full_run, "timestamp": DAY + "T22:46:00+08:00"}))
        value = self.audit()["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "fail")
        self.assertIn("current_run_completed_after_deadline", value["verification"]["reasons"])
        self.f.last.write_text(json.dumps(self.full_run))
        baseline = self.f.root / "baseline.json"
        baseline.write_text(json.dumps({"rowHashes": {d: r["payloadSha256"] for d, r in health.snapshot_rows(self.f.db).items()}}))
        self.f.sql("DELETE FROM snapshot WHERE date='2026-08-28'")
        value = self.audit(baseline=baseline)["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "fail")
        self.assertIn("historical_rows_changed_or_missing", value["verification"]["reasons"])

    def test_material_warning_remains_visible_alongside_verified_artifact(self):
        self.f.last.write_text(json.dumps({**self.full_run, "warnings": ["current_stage_retry_warning"]}))
        value = self.audit()["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "pass")
        self.assertEqual(value["verification"]["materialWarningCodes"], ["last_snapshot_material_warning"])
        self.assertIn("last_snapshot_material_warning", value["verification"]["currentIssueCodes"])

    def test_closed_claim_cannot_skip_a_known_trading_day_under_full_contract(self):
        self.f.last.write_text(json.dumps({"runId": "c" * 32, "status": "skipped_non_trading",
            "target": "2026-09-03", "startedAt": DAY + "T22:30:00+08:00",
            "timestamp": DAY + "T22:31:00+08:00", "calendarEvidence": closed_calendar(DAY)}))
        value = self.audit(evidence=closed_calendar(DAY))["activeRunDecision"]
        self.assertEqual(value["verification"]["status"], "fail")
        self.assertIn("current_closed_skip_binding_mismatch", value["verification"]["reasons"])

    def test_decision_is_read_only_and_compatible_fields_remain(self):
        before = self.f.fingerprint()
        result = self.audit()
        self.assertEqual(before, self.f.fingerprint())
        self.assertEqual(result["activeLastRun"], result["lastRun"])
        self.assertEqual(result["activeRunDecision"]["run"]["targetDate"], result["activeLastRun"]["target"])


if __name__ == "__main__":
    unittest.main()
