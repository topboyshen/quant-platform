"""Date rollover is not recovery; all inputs and delivery files are local stubs."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import health_alert as alert

CST = dt.timezone(dt.timedelta(hours=8))
FULL_DATES = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-11",
              "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
              "2026-09-22", "2026-09-30"]
CORE_DATES = [f"2026-10-{day}" for day in range(12, 17)]


def info(day="2026-10-16", *, run="fail", fresh="stale", actual="2026-10-09",
         core_dates=CORE_DATES):
    return {"status": "critical", "stale": False, "generatedAt": day + "T22:45:00+08:00",
            "criticalAtoms": ([f"unresolved_trading_day_gaps|{d}" for d in FULL_DATES]
                              + [f"unresolved_core_market_gaps|{d}" for d in core_dates]),
            "activeRunDecision": {"status": run, "scheduledDate": day, "targetDate": day,
                                  "materialWarningCodes": []},
            "supplyFreshness": {"status": fresh, "expectedTargetDate": day,
                                "actualTargetDate": actual, "dueDate": day}}


def decision(value, previous=None):
    now = dt.datetime.fromisoformat(value["generatedAt"]) + dt.timedelta(seconds=7)
    return alert.decide(value, "inactive", previous, now)


class DateRolloverTests(unittest.TestCase):
    def prior(self, legacy=False):
        state = decision(info("2026-10-15", core_dates=[]))["state"]
        if legacy:
            state.pop("businessAtoms")
            state.pop("auditVisible")
        return state

    def test_real_1015_to_1016_new_gaps_notify_without_two_false_recoveries(self):
        event = decision(info(), self.prior())
        self.assertEqual(event["recovered"], [])
        self.assertEqual(event["rolledForward"], ["active_run_fail|2026-10-15",
                                                 "qlib_supply_stale|2026-10-15"])
        self.assertEqual(event["new"], [f"unresolved_core_market_gaps|{day}" for day in CORE_DATES])
        self.assertEqual(event["worsened"], ["active_run_fail|2026-10-16", "qlib_supply_stale|2026-10-16"])
        self.assertEqual(len(event["state"]["businessAtoms"]), 17)
        self.assertTrue(event["notify"])
        self.assertNotIn("恢复：", "\n".join(alert._change_lines(event)))
        self.assertIn("非恢复", "\n".join(alert._change_lines(event)))

    def test_repeat_same_day_is_silent_and_fingerprint_stable(self):
        first = decision(info(), self.prior())
        again = decision(info(), first["state"])
        self.assertFalse(again["notify"])
        self.assertEqual(again["kind"], "unchanged")
        self.assertEqual(again["fingerprint"], first["fingerprint"])
        self.assertEqual(again["rolledForward"], [])

    def test_price_reaching_old_target_can_partially_recover_while_new_target_stale(self):
        event = decision(info(actual="2026-10-15"), self.prior())
        self.assertEqual(event["recovered"], ["qlib_supply_stale|2026-10-15"])
        self.assertIn("qlib_supply_stale|2026-10-16", event["worsened"])
        self.assertEqual(event["rolledForward"], ["active_run_fail|2026-10-15"])

    def test_same_day_pass_and_current_recover_only_current_faults(self):
        prior = decision(info())["state"]
        event = decision(info(run="pass", fresh="current", actual="2026-10-16"), prior)
        self.assertEqual(event["recovered"], ["active_run_fail|2026-10-16", "qlib_supply_stale|2026-10-16"])
        self.assertEqual(event["rolledForward"], [])
        self.assertTrue(set(f"unresolved_core_market_gaps|{d}" for d in CORE_DATES)
                        <= set(event["state"]["businessAtoms"]))

    def test_later_day_success_does_not_claim_earlier_snapshot_restored(self):
        prior = decision(info())["state"]
        event = decision(info("2026-10-19", run="pass", fresh="current", actual="2026-10-19"), prior)
        self.assertEqual(event["recovered"], ["qlib_supply_stale|2026-10-16"])
        self.assertEqual(event["rolledForward"], ["active_run_fail|2026-10-16"])
        self.assertIn("unresolved_core_market_gaps|2026-10-15", event["state"]["businessAtoms"])

    def test_holiday_pending_and_missing_run_do_not_confirm_recovery(self):
        for run in ("pending", "not_required", "missing"):
            current = info()
            current["activeRunDecision"]["status"] = run
            if run == "missing":
                del current["activeRunDecision"]
            with self.subTest(run=run):
                event = decision(current, self.prior())
                self.assertNotIn("active_run_fail|2026-10-15", event["recovered"])
                self.assertIn("active_run_fail|2026-10-15", event["state"]["businessAtoms"])
                self.assertIn("active_run_fail|2026-10-15", event["recoveryUnconfirmed"])

    def test_unknown_missing_or_not_evaluated_freshness_preserves_prior_target(self):
        for fresh in ("unknown", "not_evaluated", "missing"):
            current = info()
            current["supplyFreshness"]["status"] = fresh
            if fresh == "missing":
                del current["supplyFreshness"]
            with self.subTest(fresh=fresh):
                event = decision(current, self.prior())
                self.assertNotIn("qlib_supply_stale|2026-10-15", event["recovered"])
                self.assertIn("qlib_supply_stale|2026-10-15", event["state"]["businessAtoms"])

    def test_freshness_unknown_date_rollover_is_not_recovery(self):
        prior = decision(info("2026-10-15", fresh="unknown"))["state"]
        event = decision(info(fresh="unknown"), prior)
        self.assertEqual(event["recovered"], [])
        self.assertIn("qlib_supply_freshness_unknown|2026-10-15", event["rolledForward"])
        self.assertIn("qlib_supply_freshness_unknown|2026-10-16", event["worsened"])
        self.assertFalse(decision(info(fresh="unknown"), event["state"])["notify"])

    def test_unknown_freshness_can_recover_observability_without_prices_recovering(self):
        prior = decision(info("2026-10-15", fresh="unknown"))["state"]
        for day in ("2026-10-15", "2026-10-16"):
            for fresh, actual in (("stale", "2026-10-09"), ("current", day)):
                with self.subTest(day=day, fresh=fresh):
                    event = decision(info(day, fresh=fresh, actual=actual), prior)
                    self.assertIn("qlib_supply_freshness_unknown|2026-10-15", event["recovered"])
                    if fresh == "stale":
                        self.assertIn(f"qlib_supply_stale|{day}", event["new"])
                    self.assertIn("不代表价格追平", "\n".join(alert._change_lines(event)))

    def test_unknown_freshness_not_recovered_by_missing_or_not_evaluated_field(self):
        prior = decision(info("2026-10-15", fresh="unknown"))["state"]
        for status in ("missing", "not_evaluated"):
            current = info()
            current["supplyFreshness"]["status"] = status
            if status == "missing":
                current.pop("supplyFreshness")
            with self.subTest(status=status):
                event = decision(current, prior)
                self.assertNotIn("qlib_supply_freshness_unknown|2026-10-15", event["recovered"])
                self.assertIn("qlib_supply_freshness_unknown|2026-10-15", event["state"]["businessAtoms"])

    def test_same_day_failure_to_insufficient_is_not_recovery(self):
        prior = decision(info())["state"]
        event = decision(info(run="insufficient"), prior)
        self.assertEqual(event["recovered"], [])
        self.assertIn("active_run_fail|2026-10-16", event["state"]["businessAtoms"])

    def test_same_day_insufficient_to_failure_supersedes_uncertainty_without_recovery(self):
        prior = decision(info(run="insufficient"))["state"]
        event = decision(info(), prior)
        self.assertEqual(event["recovered"], [])
        self.assertEqual(event["rolledForward"], ["active_run_insufficient|2026-10-16"])
        self.assertNotIn("active_run_insufficient|2026-10-16", event["state"]["businessAtoms"])
        self.assertIn("active_run_fail|2026-10-16", event["new"])

    def test_legacy_state_and_blind_audit_preserve_business_then_roll_forward(self):
        old = self.prior(legacy=True)
        snapshot = copy.deepcopy(old)
        blind = {"status": "missing", "stale": True, "generatedAt": "2026-10-16T22:45:00+08:00"}
        lost = decision(blind, old)
        self.assertEqual(lost["recovered"], [])
        self.assertEqual(lost["state"]["businessAtoms"], old["atoms"])
        self.assertEqual(old, snapshot)
        restored = decision(info(), lost["state"])
        self.assertEqual(restored["recovered"], ["audit_result_missing|2026-10-16"])
        self.assertEqual(restored["rolledForward"], ["active_run_fail|2026-10-15", "qlib_supply_stale|2026-10-15"])

    def test_new_gap_and_scheduled_missing_still_notify_and_gap_recovery_is_distinct(self):
        prior = decision(info())["state"]
        value = info("2026-10-19", core_dates=CORE_DATES + ["2026-10-19"])
        value["dueRunAtoms"] = ["scheduled_core_market_missing|2026-10-19"]
        event = decision(value, prior)
        self.assertEqual(event["kind"], "should_run_but_did_not")
        self.assertIn("unresolved_core_market_gaps|2026-10-19", event["worsened"])
        self.assertIn("scheduled_core_market_missing|2026-10-19", event["new"])
        # A fresh audit explicitly removing a gap remains a separate recovery.
        value = info(core_dates=CORE_DATES[1:])
        event = decision(value, prior)
        self.assertEqual(event["recovered"], ["unresolved_core_market_gaps|2026-10-12"])

if __name__ == "__main__":
    unittest.main()
