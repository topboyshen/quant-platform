"""Batch 1 (2026-09-26): notify on change only; explicit confirmed-retained list."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import health_alert as alert  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))
DAY1 = dt.datetime(2026, 9, 25, 22, 45, 6, tzinfo=CST)
DAY2 = dt.datetime(2026, 9, 26, 22, 45, 6, tzinfo=CST)
DAY3 = dt.datetime(2026, 9, 27, 22, 45, 6, tzinfo=CST)
BASE8 = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-11",
         "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
LEGACY10 = ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
            "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
REPO = Path(__file__).resolve().parents[1]


def audit(generated, gaps=None, extra=(), run_id="run-a"):
    """Shape of the real 2026-09-25 22:45 audit (issues only)."""
    gaps = list(BASE8 + ["2026-09-22"]) if gaps is None else gaps
    issues = [
        {"code": "qlib_source_absence", "severity": "information", "affectedDates": [generated[:10]],
         "invalidCount": 8},
        {"code": "legacy_gaps_or_invalid_rows", "severity": "warning", "dates": list(LEGACY10)},
        {"code": "last_snapshot_material_warning", "severity": "warning", "target": "2026-09-24",
         "runAt": generated, "runId": run_id},
    ]
    if gaps:
        issues.append({"code": "unresolved_trading_day_gaps",
                       "severity": "critical" if len(gaps) >= 2 else "warning", "dates": gaps})
    issues.extend(extra)
    status = "critical" if any(i["severity"] == "critical" for i in issues) else "warning"
    return {"schemaVersion": 1, "generatedAt": generated, "status": status, "issues": issues}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def info(self, payload, now):
        path = self.dir / "last.json"
        path.write_text(json.dumps(payload))
        return alert.describe(path, now=now, retained_path=REPO / "config/health-alert-retained.json")


class RetainedListTests(Base):
    def test_real_shape_counts_8_plus_10_and_0922_separately(self):
        info = self.info(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        r = info["retained"]
        self.assertEqual((r["unresolvedTradingDayGaps"], r["legacyGapDays"]), (8, 10))
        self.assertEqual(r["laterRegisteredDates"], ["2026-09-22"])
        self.assertEqual(r["unconfirmedDates"], [])
        line = alert._retained_line(info)
        self.assertIn("原确认缺口日 8 个 + 遗留问题日 10 个", line)
        self.assertIn("后续单列登记缺口日 2026-09-22", line)
        self.assertIn("非新增故障", line)

    def test_a_future_gap_is_never_called_not_new(self):
        info = self.info(audit("2026-09-29T22:45:00+08:00", gaps=BASE8 + ["2026-09-22", "2026-09-28"]),
                         dt.datetime(2026, 9, 29, 22, 45, 6, tzinfo=CST))
        r = info["retained"]
        self.assertEqual(r["unconfirmedDates"], ["2026-09-28"])
        line = alert._retained_line(info)
        self.assertIn("不在已确认保留清单：2026-09-28", line)
        self.assertNotIn("非新增故障", line)

    def test_unreadable_list_confirms_nothing(self):
        path = self.dir / "last.json"
        path.write_text(json.dumps(audit("2026-09-25T22:45:00+08:00")))
        info = alert.describe(path, now=DAY1, retained_path=self.dir / "missing.json")
        self.assertEqual(info["retained"]["unconfirmedDays"], 19)
        self.assertNotIn("非新增故障", alert._retained_line(info))

    def test_shipped_list_is_exactly_the_evidence_backed_scope(self):
        data = json.loads((REPO / "config/health-alert-retained.json").read_text())
        self.assertEqual(data["originalBaseline"]["unresolved_trading_day_gaps"], BASE8)
        self.assertEqual(data["originalBaseline"]["legacy_gaps_or_invalid_rows"], LEGACY10)
        self.assertEqual([(i["code"], i["date"]) for i in data["laterRegistrations"]],
                         [("unresolved_trading_day_gaps", "2026-09-22")])
        self.assertNotIn("2026-09-22", data["originalBaseline"]["unresolved_trading_day_gaps"])


class ChangeDecisionTests(Base):
    def day1_state(self):
        info = self.info(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        return alert.decide(info, "inactive", None, DAY1)["state"]

    def test_same_old_critical_two_days_second_day_silent_but_still_critical(self):
        previous = self.day1_state()
        info = self.info(audit("2026-09-26T22:45:00.100000+08:00", run_id="run-b"), DAY2)
        decision = alert.decide(info, "inactive", previous, DAY2)
        self.assertFalse(decision["notify"])
        self.assertEqual(decision["kind"], "unchanged")
        self.assertEqual(decision["fingerprint"], previous["fingerprint"])
        self.assertEqual(info["status"], "critical")  # true severity kept
        self.assertFalse(alert.should_alert(info, "inactive", previous, DAY2))

    def test_fingerprint_ignores_run_id_and_audit_time(self):
        a = self.info(audit("2026-09-25T22:45:00+08:00", run_id="x"), DAY1)
        b = self.info(audit("2026-09-26T22:45:09+08:00", run_id="y"), DAY2)
        self.assertEqual(alert.decide(a, "inactive", None, DAY1)["fingerprint"],
                         alert.decide(b, "inactive", None, DAY2)["fingerprint"])

    def test_one_new_gap_day_notifies_as_worsened(self):
        previous = self.day1_state()
        info = self.info(audit("2026-09-28T22:45:00+08:00", gaps=BASE8 + ["2026-09-22", "2026-09-28"]),
                         dt.datetime(2026, 9, 28, 22, 45, 6, tzinfo=CST))
        decision = alert.decide(info, "inactive", previous, dt.datetime(2026, 9, 28, 22, 45, 6, tzinfo=CST))
        self.assertTrue(decision["notify"])
        self.assertEqual(decision["kind"], "worsened")
        self.assertEqual(decision["worsened"], ["unresolved_trading_day_gaps|2026-09-28"])
        text = alert.message(info, decision=decision)
        self.assertIn("变化：恶化", text)
        self.assertIn("unresolved_trading_day_gaps@2026-09-28", text)
        self.assertNotIn("非新增故障", text)

    def test_new_critical_issue_type_is_a_new_failure(self):
        previous = self.day1_state()
        extra = [{"code": "historical_rows_changed_or_missing", "severity": "critical", "dates": ["2026-08-28"]}]
        info = self.info(audit("2026-09-26T22:45:00+08:00", extra=extra), DAY2)
        decision = alert.decide(info, "inactive", previous, DAY2)
        self.assertTrue(decision["notify"])
        self.assertEqual(decision["kind"], "new_failure")
        self.assertEqual(decision["new"], ["historical_rows_changed_or_missing|2026-08-28"])

    def test_real_recovery_notifies(self):
        previous = self.day1_state()
        # 09-22 row genuinely appears valid: gap set shrinks.
        info = self.info(audit("2026-09-26T22:45:00+08:00", gaps=list(BASE8)), DAY2)
        decision = alert.decide(info, "inactive", previous, DAY2)
        self.assertTrue(decision["notify"])
        self.assertEqual(decision["kind"], "recovered")
        self.assertEqual(decision["recovered"], ["unresolved_trading_day_gaps|2026-09-22"])
        self.assertIn("变化：恢复", alert.message(info, decision=decision))

    def test_full_recovery_to_warning_notifies_then_goes_quiet(self):
        previous = self.day1_state()
        info = self.info(audit("2026-09-26T22:45:00+08:00", gaps=[]), DAY2)
        first = alert.decide(info, "inactive", previous, DAY2)
        self.assertTrue(first["notify"])
        self.assertEqual(first["current"], [])
        later = dt.datetime(2026, 9, 27, 22, 45, 6, tzinfo=CST)
        info = self.info(audit("2026-09-27T22:45:00+08:00", gaps=[]), later)
        self.assertFalse(alert.decide(info, "inactive", first["state"], later)["notify"])

    def test_should_run_but_did_not_is_per_scheduled_day(self):
        previous = self.day1_state()
        stale = self.info(audit("2026-09-25T22:45:00+08:00"), DAY2)  # no new audit on day 2
        self.assertTrue(stale["stale"])
        d2 = alert.decide(stale, "inactive", previous, DAY2)
        self.assertTrue(d2["notify"])
        self.assertEqual(d2["kind"], "should_run_but_did_not")
        self.assertEqual(d2["new"], ["audit_not_current|2026-09-26"])
        # a retry for the same day collapses; the next missed day is a new event
        self.assertFalse(alert.decide(stale, "inactive", d2["state"], DAY2 + dt.timedelta(minutes=5))["notify"])
        d3 = alert.decide(stale, "inactive", d2["state"], DAY2 + dt.timedelta(days=1))
        self.assertTrue(d3["notify"])
        self.assertEqual(d3["kind"], "should_run_but_did_not")
        self.assertIn("audit_not_current|2026-09-27", d3["new"] + d3["worsened"])
        self.assertIn("audit_not_current|2026-09-26", d3["recovered"])

    def test_failed_audit_unit_and_missing_result_alert(self):
        previous = self.day1_state()
        info = self.info(audit("2026-09-26T22:45:00+08:00"), DAY2)
        self.assertTrue(alert.decide(info, "failed", previous, DAY2)["notify"])
        missing = alert.describe(self.dir / "absent.json", now=DAY2)
        decision = alert.decide(missing, "inactive", previous, DAY2)
        self.assertTrue(decision["notify"])
        self.assertIn("audit_result_missing|2026-09-26", decision["new"])

    def test_no_state_or_corrupt_state_fails_toward_alerting(self):
        info = self.info(audit("2026-09-26T22:45:00+08:00"), DAY2)
        self.assertTrue(alert.decide(info, "inactive", None, DAY2)["notify"])
        bad = self.dir / "state.json"
        state = self.day1_state()
        state["atoms"] = state["atoms"][:-1]  # fingerprint no longer matches
        bad.write_text(json.dumps(state))
        self.assertIsNone(alert.read_state(bad))

    def test_warning_or_information_changes_do_not_page(self):
        previous = self.day1_state()
        payload = audit("2026-09-26T22:45:00+08:00")
        payload["issues"][0]["affectedDates"] = ["2026-09-26"]  # information moves daily
        payload["issues"][2]["target"] = "2026-09-25"             # warning target moves
        self.assertFalse(alert.decide(self.info(payload, DAY2), "inactive", previous, DAY2)["notify"])


class CliStateTests(Base):
    def test_describe_pending_then_commit_round_trip(self):
        last = self.dir / "last.json"
        last.write_text(json.dumps(audit(dt.datetime.now(CST).isoformat())))
        state, pending = self.dir / "state.json", self.dir / "pending.json"
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            alert.main(["describe", "--json", str(last), "--state-file", str(state),
                        "--pending-file", str(pending), "--unit-state", "inactive"])
        lines = dict(l.split("=", 1) for l in out.getvalue().splitlines())
        self.assertEqual((lines["ALERT"], lines["DECISION"]), ("yes", "first_observation"))
        self.assertTrue(pending.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(alert.main(["commit-state", "--pending-file", str(pending), "--state-file", str(state)]), 0)
        self.assertFalse(pending.exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            alert.main(["describe", "--json", str(last), "--state-file", str(state), "--unit-state", "inactive"])
        lines = dict(l.split("=", 1) for l in out.getvalue().splitlines())
        self.assertEqual((lines["ALERT"], lines["DECISION"], lines["STATUS"]), ("no", "unchanged", "critical"))


if __name__ == "__main__":
    unittest.main()


class DueRunAndBlindAuditTests(Base):
    """R01/R03 (2026-09-27): due-run events page; a blind audit never proves recovery."""

    def _state(self, payload, now):
        info = self.info(payload, now)
        return alert.decide(info, "inactive", None, now)["state"], info

    def test_new_scheduled_snapshot_missing_notifies_while_critical_set_is_unchanged(self):
        state, _ = self._state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        current = self.info(audit("2026-09-26T22:45:00.767231+08:00", extra=[
            {"code": "scheduled_snapshot_missing", "severity": "warning", "dueDate": "2026-09-26"}]),
            DAY2)
        decision = alert.decide(current, "inactive", state, DAY2)
        self.assertTrue(decision["notify"], decision)
        self.assertEqual(decision["kind"], "should_run_but_did_not")
        self.assertIn("scheduled_snapshot_missing|2026-09-26", decision["new"])

    def test_repeated_due_run_event_for_the_same_due_date_stays_silent(self):
        payload = audit("2026-09-26T22:45:00.767231+08:00", extra=[
            {"code": "scheduled_snapshot_missing", "severity": "warning", "dueDate": "2026-09-26"}])
        state, _ = self._state(payload, DAY2)
        again = self.info(audit("2026-09-26T22:50:00.000000+08:00", extra=[
            {"code": "scheduled_snapshot_missing", "severity": "warning", "dueDate": "2026-09-26"}]), DAY2)
        decision = alert.decide(again, "inactive", state, DAY2)
        self.assertFalse(decision["notify"], decision)
        self.assertEqual(decision["kind"], "unchanged")

    def test_a_new_due_date_is_a_new_event(self):
        payload = audit("2026-09-26T22:45:00.767231+08:00", extra=[
            {"code": "scheduled_snapshot_missing", "severity": "warning", "dueDate": "2026-09-26"}])
        state, _ = self._state(payload, DAY2)
        next_day = self.info(audit("2026-09-27T22:45:00.000000+08:00", extra=[
            {"code": "scheduled_snapshot_missing", "severity": "warning", "dueDate": "2026-09-27"}]), DAY3)
        decision = alert.decide(next_day, "inactive", state, DAY3)
        self.assertTrue(decision["notify"], decision)
        # 同 code、新 dueDate 属“恶化”，不是“新增”；两者都必须通知。
        self.assertIn("scheduled_snapshot_missing|2026-09-27",
                      decision["new"] + decision["worsened"])

    def test_ordinary_warning_alone_does_not_page(self):
        state, _ = self._state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        current = self.info(audit("2026-09-26T22:45:00.767231+08:00", extra=[
            {"code": "concept_source_degraded", "severity": "warning", "affectedDates": ["2026-09-26"]}]), DAY2)
        decision = alert.decide(current, "inactive", state, DAY2)
        self.assertFalse(decision["notify"], decision)

    def test_missing_audit_does_not_confirm_business_recovery(self):
        state, _ = self._state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        blind = alert.describe(self.dir / "absent.json", now=DAY2,
                               retained_path=REPO / "config/health-alert-retained.json")
        decision = alert.decide(blind, "inactive", state, DAY2)
        self.assertTrue(decision["notify"], decision)
        self.assertEqual(decision["kind"], "should_run_but_did_not")
        self.assertFalse([a for a in decision["recovered"] if a.startswith("unresolved_trading_day_gaps")],
                         decision["recovered"])
        self.assertFalse(decision["state"]["auditVisible"])
        self.assertIn("unresolved_trading_day_gaps|2026-09-07", decision["state"]["businessAtoms"])

    def test_business_fault_is_not_reported_new_after_a_blind_audit(self):
        state, _ = self._state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        blind = alert.describe(self.dir / "absent.json", now=DAY2,
                               retained_path=REPO / "config/health-alert-retained.json")
        state = alert.decide(blind, "inactive", state, DAY2)["state"]
        readable = self.info(audit("2026-09-27T22:45:00.000000+08:00"), DAY3)
        decision = alert.decide(readable, "inactive", state, DAY3)
        # 审计恢复本身要通知（可观测性故障消失），但业务故障不得被当成新增。
        self.assertEqual(decision["kind"], "recovered")
        self.assertEqual([a for a in decision["recovered"] if not a.startswith("audit_")], [])
        self.assertEqual([a for a in decision["new"] if not a.startswith("audit_")], [])
        self.assertTrue(decision["state"]["auditVisible"])

    def test_unchanged_critical_set_still_stays_silent(self):
        state, _ = self._state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        same = self.info(audit("2026-09-26T22:45:00.767231+08:00"), DAY2)
        decision = alert.decide(same, "inactive", state, DAY2)
        self.assertFalse(decision["notify"], decision)
        self.assertEqual(decision["kind"], "unchanged")


class LegacyStateMigrationTests(Base):
    """R03 migration: live states written before businessAtoms must be inherited, not emptied."""

    def _legacy_state(self, payload, now):
        """Shape of a state committed by the pre-R03 code: atoms + fingerprint only."""
        info = self.info(payload, now)
        state = alert.decide(info, "inactive", None, now)["state"]
        legacy = {key: state[key] for key in ("schemaVersion", "atoms", "fingerprint")}
        legacy["auditStatus"] = state["auditStatus"]
        return legacy

    def test_missing_business_atoms_is_inherited_not_treated_as_empty(self):
        legacy = self._legacy_state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        self.assertNotIn("businessAtoms", legacy)
        blind = alert.describe(self.dir / "absent.json", now=DAY2,
                               retained_path=REPO / "config/health-alert-retained.json")
        decision = alert.decide(blind, "inactive", legacy, DAY2)
        self.assertFalse([a for a in decision["recovered"] if not a.startswith("audit_")],
                         decision["recovered"])
        self.assertIn("unresolved_trading_day_gaps|2026-09-22", decision["state"]["businessAtoms"])
        self.assertFalse(decision["state"]["auditVisible"])

    def test_migration_then_readable_fault_still_present_is_not_new_and_not_recovered(self):
        legacy = self._legacy_state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        blind = alert.describe(self.dir / "absent.json", now=DAY2,
                               retained_path=REPO / "config/health-alert-retained.json")
        state = alert.decide(blind, "inactive", legacy, DAY2)["state"]
        readable = self.info(audit("2026-09-27T22:45:00.000000+08:00"), DAY3)
        decision = alert.decide(readable, "inactive", state, DAY3)
        self.assertEqual([a for a in decision["new"] if not a.startswith("audit_")], [])
        self.assertEqual([a for a in decision["recovered"] if not a.startswith("audit_")], [])
        self.assertEqual(decision["kind"], "recovered")  # only the audit observability fault recovered

    def test_migration_then_readable_fault_gone_notifies_the_real_recovery(self):
        legacy = self._legacy_state(audit("2026-09-25T22:45:00.767231+08:00"), DAY1)
        blind = alert.describe(self.dir / "absent.json", now=DAY2,
                               retained_path=REPO / "config/health-alert-retained.json")
        state = alert.decide(blind, "inactive", legacy, DAY2)["state"]
        resolved = self.info(audit("2026-09-27T22:45:00.000000+08:00", gaps=[]), DAY3)
        decision = alert.decide(resolved, "inactive", state, DAY3)
        self.assertTrue(decision["notify"], decision)
        self.assertIn("unresolved_trading_day_gaps|2026-09-22", decision["recovered"])
        self.assertIn("unresolved_trading_day_gaps|2026-09-07", decision["recovered"])
