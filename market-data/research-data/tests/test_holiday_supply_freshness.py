"""Holiday skip (snapshot not required) must not swallow Qlib supply staleness."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest

import core_contract as contract
import data_health as health
import test_active_run_decision as producer_tests
from test_core_market_contract import closed_calendar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import health_alert as alert


def closed(day, previous):
    value = closed_calendar(day)
    value["document"]["previousTradingDate"] = previous
    value["sha256"] = contract.digest({k: v for k, v in value.items() if k != "sha256"})
    return value


class HolidaySupplyFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.producer = producer_tests.ActiveRunDecisionTests(methodName="runTest")
        self.producer.setUp()
        self.addCleanup(self.producer.doCleanups)
        self.f = self.producer.f
        self.path = self.f.root / "alert-audit.json"
        self.f.sql("DELETE FROM snapshot WHERE date IN ('2026-09-02','2026-09-03')")
        self.prior = self.decide(self.producer.audit())["state"]

    def describe(self, payload):
        self.path.write_text(json.dumps(payload))
        return alert.describe(self.path, now=self.f.now)

    def decide(self, payload, previous=None):
        return alert.decide(self.describe(payload), "inactive", previous, self.f.now)

    def holiday(self, day, previous):
        self.f.now = dt.datetime.fromisoformat(day + "T23:00:00+08:00")
        evidence = closed(day, previous)
        self.f.last.write_text(json.dumps({
            "runId": "c" * 32, "status": "skipped_non_trading", "target": previous,
            "startedAt": day + "T22:30:00+08:00", "timestamp": day + "T22:31:00+08:00",
            "calendarEvidence": evidence}))
        return self.producer.audit(evidence=evidence)

    def test_stale_qlib_on_closed_day_is_separate_from_snapshot_verdict(self):
        payload = self.holiday("2026-09-06", "2026-09-05")
        self.assertEqual(payload["activeRunDecision"]["verification"]["status"], "not_required")
        fresh = payload["supplyFreshness"]
        self.assertEqual(fresh["status"], "stale")
        self.assertEqual(fresh["expectedTargetDate"], "2026-09-05")
        self.assertEqual(fresh["actualTargetDate"], "2026-09-04")
        self.assertFalse(fresh["snapshotRequired"])
        event = self.decide(payload, self.prior)
        self.assertTrue(event["notify"])
        self.assertIn("qlib_supply_stale|2026-09-05", event["new"])
        self.assertFalse(any(a.startswith("active_run_") for a in event["current"]))
        text = alert.message(self.describe(payload), decision=event, now=self.f.now)
        self.assertIn("供应链新鲜度：滞后", text)
        self.assertIn("快照：休市无需运行", text)

    def test_same_stale_target_on_next_holiday_night_is_unchanged(self):
        first = self.decide(self.holiday("2026-09-06", "2026-09-05"), self.prior)
        second = self.decide(self.holiday("2026-09-07", "2026-09-05"), first["state"])
        self.assertFalse(second["notify"])
        self.assertEqual(second["kind"], "unchanged")

    def test_lowering_expected_day_does_not_prove_old_price_target_recovered(self):
        stale = self.decide(self.holiday("2026-09-06", "2026-09-05"), self.prior)
        # This was previously called "catching up", although no fixture price
        # advanced: only the independent expected date moved backwards.
        payload = self.holiday("2026-09-07", "2026-09-04")
        self.assertEqual(payload["supplyFreshness"]["actualTargetDate"], "2026-09-04")
        current = self.decide(payload, stale["state"])
        self.assertNotIn("qlib_supply_stale|2026-09-05", current["recovered"])
        self.assertIn("qlib_supply_stale|2026-09-05", current["state"]["businessAtoms"])
        self.assertIn("qlib_supply_stale|2026-09-05", current["recoveryUnconfirmed"])

    def test_qlib_catching_up_is_reported_as_recovery(self):
        stale = self.decide(self.holiday("2026-09-06", "2026-09-05"), self.prior)
        days = self.f.days + ["2026-09-05"]
        (self.f.qlib / "calendars/day.txt").write_text("\n".join(days) + "\n")
        for symbol in health.REFERENCES:
            self.f.features(symbol, len(days))
        payload = self.holiday("2026-09-07", "2026-09-05")
        self.assertEqual(payload["supplyFreshness"]["status"], "current")
        self.assertEqual(payload["supplyFreshness"]["expectedTargetDate"], "2026-09-05")
        self.assertEqual(payload["supplyFreshness"]["actualTargetDate"], "2026-09-05")
        current = self.decide(payload, stale["state"])
        self.assertTrue(current["notify"])
        self.assertIn("qlib_supply_stale|2026-09-05", current["recovered"])
        # Price recovery cannot fill the separate missing snapshot fixture.
        self.assertIn("unresolved_trading_day_gaps|2026-09-05", current["state"]["businessAtoms"])

    def test_current_qlib_on_closed_day_stays_silent(self):
        payload = self.holiday("2026-09-05", "2026-09-04")
        self.assertEqual(payload["supplyFreshness"]["status"], "current")
        self.assertFalse(self.decide(payload, self.prior)["notify"])

    def test_legacy_payload_without_field_derives_from_issue(self):
        payload = self.holiday("2026-09-06", "2026-09-05")
        del payload["supplyFreshness"]
        info = self.describe(payload)
        self.assertEqual(info["supplyFreshness"]["status"], "stale")
        self.assertIn("qlib_supply_stale|2026-09-05", alert.alert_atoms(info, "inactive", self.f.now))

    def test_malformed_field_is_an_observability_failure_not_silence(self):
        payload = self.holiday("2026-09-06", "2026-09-05")
        payload["supplyFreshness"]["status"] = "fine"
        atoms = alert.alert_atoms(self.describe(payload), "inactive", self.f.now)
        self.assertIn("audit_supply_freshness_invalid|2026-09-06", atoms)

    def test_calendar_unavailable_after_deadline_is_unknown(self):
        self.f.now = dt.datetime.fromisoformat("2026-09-06T23:00:00+08:00")
        payload = self.producer.audit(unavailable=True)
        self.assertEqual(payload["supplyFreshness"]["status"], "unknown")
        atoms = alert.alert_atoms(self.describe(payload), "inactive", self.f.now)
        self.assertIn("qlib_supply_freshness_unknown|2026-09-06", atoms)


if __name__ == "__main__":
    unittest.main()
