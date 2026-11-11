"""The alert projects real audit full/core verdicts; no collectors or sends."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest

import test_active_run_decision as producer_tests
from test_core_market_contract import closed_calendar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import health_alert as alert


class ActiveRunAlertTests(unittest.TestCase):
    def setUp(self):
        self.producer = producer_tests.ActiveRunDecisionTests(methodName="runTest")
        self.producer.setUp()
        self.addCleanup(self.producer.doCleanups)
        self.f = self.producer.f
        self.path = self.f.root / "alert-audit.json"
        self.f.sql("DELETE FROM snapshot WHERE date IN ('2026-09-02','2026-09-03')")

    def describe(self, payload=None):
        self.path.write_text(json.dumps(payload if payload is not None else self.producer.audit()))
        return alert.describe(self.path, now=self.f.now)

    def decide(self, previous=None, payload=None):
        return alert.decide(self.describe(payload), "inactive", previous, self.f.now)

    def old_success(self):
        self.f.last.write_text(json.dumps({**self.producer.full_run,
            "startedAt": "2026-09-03T22:30:00+08:00", "timestamp": "2026-09-03T22:31:00+08:00"}))

    def test_old_success_current_insufficient_notifies_and_preserves_historical_level(self):
        prior = self.decide()["state"]
        self.old_success()
        payload = self.producer.audit()
        info = self.describe(payload)
        self.assertEqual(payload["activeLastRun"]["status"], "inserted")
        self.assertEqual(info["activeRunDecision"]["status"], "insufficient")
        # Accepted FROZEN keeps the historical gaps as visible limitations, so
        # the artifact is warning-level; the current run still pages on its own.
        self.assertTrue(info["acceptedFreeze"])
        self.assertEqual(info["status"], "warning")
        self.assertIn("2026-09-02", info["freezeAbsenceDates"])
        self.assertIn("2026-09-03", info["freezeAbsenceDates"])
        event = alert.decide(info, "inactive", prior, self.f.now)
        self.assertTrue(event["notify"])
        self.assertIn("active_run_insufficient|2026-09-04", event["new"])
        self.assertEqual(event["recovered"], [])

    def test_old_success_current_insufficient_is_critical_when_not_frozen(self):
        prior = self.decide()["state"]
        self.old_success()
        # RUNNING also pages as business_mode_not_frozen; the active-run atom
        # below remains the property this test pins down.
        self.producer.f.mode.write_text("RUNNING\n")
        info = self.describe(self.producer.audit())
        self.assertIn("business_mode_not_frozen", info["codes"])
        self.assertFalse(info["acceptedFreeze"])
        self.assertEqual(info["status"], "critical")
        event = alert.decide(info, "inactive", prior, self.f.now)
        self.assertTrue(event["notify"])
        self.assertIn("active_run_insufficient|2026-09-04", event["new"])

    def test_same_scheduled_date_deduplicates_and_new_date_is_new_occurrence(self):
        self.old_success()
        first = self.decide()
        self.f.now += dt.timedelta(seconds=20)
        same = self.decide(first["state"])
        self.assertFalse(same["notify"])
        self.assertEqual(same["fingerprint"], first["fingerprint"])
        self.f.now = dt.datetime.fromisoformat("2026-09-05T23:00:00+08:00")
        next_day = self.decide(same["state"])
        self.assertTrue(next_day["notify"])
        self.assertIn("active_run_insufficient|2026-09-05", next_day["worsened"])

    def test_before_deadline_pending_does_not_create_failure(self):
        prior = self.decide()["state"]
        self.old_success()
        self.f.now = dt.datetime.fromisoformat("2026-09-04T22:20:00+08:00")
        info = self.describe()
        self.assertEqual(info["activeRunDecision"]["status"], "pending")
        event = alert.decide(info, "inactive", prior, self.f.now)
        self.assertFalse(event["notify"])
        self.assertFalse(any(atom.startswith("active_run_") for atom in event["current"]))

    def test_full_and_core_current_failed_runs_notify(self):
        for contract in ("full", "core"):
            if contract == "core":
                self.producer.activate_core()
            path, record = ((self.f.last, self.producer.full_run) if contract == "full"
                            else (self.producer.core_last, self.producer.core_run))
            path.write_text(json.dumps({**record, "status": "deferred"}))
            with self.subTest(contract=contract):
                info = self.describe()
                self.assertEqual(info["activeRunDecision"]["status"], "fail")
                self.assertIn("active_run_fail|2026-09-04", alert.alert_atoms(info, "inactive", self.f.now))
                self.assertTrue(alert.decide(info, "inactive", None, self.f.now)["notify"])

    def test_verified_closed_full_and_core_are_no_new_failure(self):
        prior = self.decide()["state"]
        self.f.now = dt.datetime.fromisoformat("2026-09-05T23:00:00+08:00")
        closed = closed_calendar("2026-09-05")
        record = {"runId": "c" * 32, "status": "skipped_non_trading", "target": "2026-09-04",
                  "startedAt": "2026-09-05T22:30:00+08:00", "timestamp": "2026-09-05T22:31:00+08:00",
                  "calendarEvidence": closed}
        for contract in ("full", "core"):
            if contract == "full":
                self.f.last.write_text(json.dumps(record))
            else:
                self.producer.activate_core()
                self.producer.core_last.write_text(json.dumps({**record, "schema": "research.core-market.v1",
                                                              "scope": "core_market", "fullSnapshot": False}))
            with self.subTest(contract=contract):
                payload = self.producer.audit(evidence=closed)
                info = self.describe(payload)
                self.assertEqual(info["activeRunDecision"]["status"], "not_required")
                event = alert.decide(info, "inactive", prior, self.f.now)
                self.assertFalse(event["notify"])
                self.assertFalse(any(atom.startswith("active_run_") for atom in event["current"]))

    def test_schema_and_same_object_binding_conflicts_are_observability_failures(self):
        original = self.producer.audit()
        previous = self.decide(payload=original)["state"]
        changes = [
            ("schema", "research.active-run-decision.v2"),
            ("evaluatedAt", "2026-09-04T22:59:00+08:00"),
            ("contract", "research.core-market.v1"),
            ("scheduledDate", "2026-09-03"),
            ("run.runId", "0" * 32),
            ("run.targetDate", "2026-09-03"),
            ("run.status", "success"),
            ("run.startedAt", "2026-09-04T22:30:01+08:00"),
            ("run.completedAt", "2026-09-04T22:31:01+08:00"),
            ("artifact.payloadSha256", "0" * 64),
            ("verification.status", "confirmed"),
        ]
        for field, value in changes:
            payload = copy.deepcopy(original)
            target = payload["activeRunDecision"]
            parts = field.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            # A malformed verdict must not treat even a falsely empty issues list
            # as proof that the last known business faults recovered.
            payload["issues"] = []
            with self.subTest(field=field):
                info = self.describe(payload)
                self.assertTrue(info["activeRunDecisionInvalid"])
                event = alert.decide(info, "inactive", previous, self.f.now)
                self.assertTrue(event["notify"])
                self.assertFalse(event["state"]["auditVisible"])
                self.assertEqual(event["state"]["businessAtoms"], previous["businessAtoms"])
                self.assertEqual(event["recovered"], [])
                self.assertIn("audit_run_decision_invalid|2026-09-04", event["new"])
                self.assertEqual(info["status"], original["status"])

    def test_present_null_or_incomplete_schema_does_not_fall_back_to_old_success(self):
        original = self.producer.audit()
        for value in (None, {}, {k: v for k, v in original["activeRunDecision"].items() if k != "run"}):
            payload = {**original, "activeRunDecision": value}
            with self.subTest(value=value):
                info = self.describe(payload)
                self.assertTrue(info["activeRunDecisionInvalid"])
                self.assertIn("audit_run_decision_invalid|2026-09-04", alert.alert_atoms(info, "inactive", self.f.now))

    def test_success_cannot_declare_missing_identity_even_if_both_sides_match(self):
        original = self.producer.audit()
        for run_field, source_field in (("runId", "runId"), ("targetDate", "target"),
                                        ("startedAt", "startedAt"), ("completedAt", "timestamp")):
            payload = copy.deepcopy(original)
            payload["activeRunDecision"]["run"][run_field] = None
            payload["activeLastRun"].pop(source_field, None)
            with self.subTest(run_field=run_field):
                self.assertTrue(self.describe(payload)["activeRunDecisionInvalid"])
        payload = copy.deepcopy(original)
        payload["schemaVersion"] = True
        self.assertTrue(self.describe(payload)["activeRunDecisionInvalid"])

    def test_calendar_and_core_quality_identity_cannot_mix_other_day_or_scope(self):
        self.producer.activate_core()
        original = self.producer.audit()
        previous = self.decide(payload=original)["state"]
        changes = [("calendar.targetDate", "2026-09-03"), ("calendar.sourceDate", "invalid"),
                   ("calendar.observedAt", "2026-09-04T08:00:00"), ("calendar.proofKind", "unchecked"),
                   ("calendar.evidenceSha256", "invalid"), ("calendar.sourceEvidenceSha256", "invalid"),
                   ("artifact.rowState", {"token": "SECRET"}), ("artifact.quality", "invalid"),
                   ("artifact.quality.targetDate", "2026-09-03"),
                   ("artifact.quality.payloadSha256", "0" * 64),
                   ("artifact.quality.scope", "full_snapshot"), ("artifact.quality.invalid", True),
                   ("artifact.quality.ratio", "0.9"), ("artifact.quality.priceStatus", "green"),
                   ("artifact.quality.generationTag", "https://user:pass@host/?token=SECRET")]
        for field, value in changes:
            payload = copy.deepcopy(original)
            target = payload["activeRunDecision"]
            parts = field.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            with self.subTest(field=field):
                info = self.describe(payload)
                self.assertTrue(info["activeRunDecisionInvalid"])
                event = alert.decide(info, "inactive", previous, self.f.now)
                self.assertFalse(event["state"]["auditVisible"])
                self.assertEqual(event["recovered"], [])
                self.assertNotIn("SECRET", alert.message(info, now=self.f.now, decision=event))

    def test_absent_new_field_is_legacy_compatible(self):
        payload = self.producer.audit()
        del payload["activeRunDecision"]
        info = self.describe(payload)
        self.assertNotIn("activeRunDecision", info)
        self.assertNotIn("activeRunDecisionInvalid", info)
        self.assertEqual(alert.alert_atoms(info, "inactive", self.f.now), info["criticalAtoms"])

    def test_incomplete_start_retains_completion_binding_and_reports_insufficient(self):
        self.f.last.write_text(json.dumps({k: v for k, v in self.producer.full_run.items() if k != "startedAt"}))
        payload = self.producer.audit()
        self.assertIsNone(payload["activeRunDecision"]["run"]["startedAt"])
        self.assertEqual(payload["activeRunDecision"]["run"]["completedAt"], payload["activeLastRun"]["timestamp"])
        info = self.describe(payload)
        self.assertNotIn("activeRunDecisionInvalid", info)
        self.assertIn("active_run_insufficient|2026-09-04", alert.alert_atoms(info, "inactive", self.f.now))

    def test_core_scope_and_material_warning_remain_truthful_in_projection(self):
        self.producer.activate_core()
        self.producer.core_last.write_text(json.dumps({**self.producer.core_run, "warnings": ["fixture_warning"]}))
        info = self.describe()
        self.assertEqual(info["activeRunDecision"]["contract"], "research.core-market.v1")
        self.assertEqual(info["activeRunDecision"]["status"], "pass")
        self.assertEqual(info["activeRunDecision"]["materialWarningCodes"], ["core_market_material_warning"])
        self.assertIn("core_market_material_warning", alert.message(info, now=self.f.now))
        self.assertFalse(any(atom.startswith("active_run_") for atom in alert.alert_atoms(info, "inactive", self.f.now)))


if __name__ == "__main__":
    unittest.main()
