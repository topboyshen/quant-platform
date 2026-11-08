import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

import calendar_evidence
import core_market
import data_health as health
import test_data_health as legacy_health
from test_core_market_contract import qlib, calendar
from test_health_alert_changes import alert


class HistoricalCalendarGapTests(unittest.TestCase):
    def setUp(self):
        self.f = legacy_health.AuditTests(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.now = dt.datetime(2026, 10, 15, 23, tzinfo=health.CST)
        self.project = self.f.root / "astock"
        self.project.mkdir()
        self.policy = self.f.root / "policy.json"
        self.policy.write_text(json.dumps({"activeContract": "research.core-market.v1", "effectiveTradeDate": "2026-10-08"}))
        self.core_db = self.f.root / "core.db"
        self.core_last = self.f.root / "core-last.json"
        self.days = self.f.days + ["2026-10-08", "2026-10-09"]
        self.set_qlib_days(self.days)
        for day in ("2026-10-08", "2026-10-09"):
            quote = qlib()
            quote["date"] = day
            record = core_market.build_core(quote, run_id="b" * 32, generated_at=day + "T22:30:00+08:00",
                                            calendar_evidence=calendar(day))
            core_market.store_core(record, self.core_db)
        for day in ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"):
            self.archive(day)
        evidence = calendar_evidence.load_calendar_evidence(self.project, self.f.now)
        self.core_last.write_text(json.dumps({"schema": "research.core-market.v1", "scope": "core_market",
            "fullSnapshot": False, "runId": "a" * 32, "startedAt": "2026-10-15T22:30:00+08:00",
            "timestamp": "2026-10-15T22:40:00+08:00", "status": "deferred", "target": "2026-10-15",
            "reason": "core_qlib_not_current_for_expected_trading_day", "calendarEvidence": evidence}))

    def set_qlib_days(self, days):
        (self.f.qlib / "calendars/day.txt").write_text("\n".join(days) + "\n")
        (self.f.qlib.parent / ".tag").write_text(days[-1] + "\n")
        (self.f.qlib / ".qlib-release").write_text(days[-1] + "\n")
        for symbol in health.REFERENCES:
            self.f.features(symbol, len(days))

    def archive(self, day, status="trading_day"):
        previous = {"2026-10-12": "2026-10-09", "2026-10-13": "2026-10-12",
                    "2026-10-14": "2026-10-13", "2026-10-15": "2026-10-14",
                    "2026-10-16": "2026-10-15"}.get(day, "2026-10-09")
        following = ("2026-10-19" if day == "2026-10-16" else
                     (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat())
        calendar = {"targetDate": day, "status": status, "previousTradingDate": previous,
                    "nextTradingDate": following, "sourceIds": ["akshare:sina:trade-calendar"],
                    "dataAsOf": day + "T08:00:00+08:00"}
        frozen = {"targetDate": day, "cutoffInstant": day + "T00:00:00.000Z",
                  "compilerContext": {"data": {"tradingCalendar": calendar}},
                  "rawContext": {"data": {"tradingCalendar": copy.deepcopy(calendar)}}}
        path = self.project / "work/report-archives" / day
        path.mkdir(parents=True, exist_ok=True)
        (path / "input.json").write_text(json.dumps(frozen))
        self.rebind(path)
        return path

    def rebind(self, path):
        sha = hashlib.sha256((path / "input.json").read_bytes()).hexdigest()
        (path / "input.sha256").write_text(sha + "\n")
        (path / "published.json").write_text(json.dumps({"status": "sealed", "inputSha256": sha}))

    def audit(self):
        return self.f.run_audit(contract_policy=self.policy, core_db=self.core_db,
                                core_last_run=self.core_last, calendar_project=self.project)

    def gaps(self, result):
        return next(i for i in result["issues"] if i["code"] == "unresolved_core_market_gaps")

    def test_stalled_qlib_keeps_frozen_open_day_gaps_visible_without_writes(self):
        before = self.f.fingerprint()
        result = self.audit()
        gap = self.gaps(result)
        self.assertEqual(gap["dates"], ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"])
        # Accepted FROZEN keeps all sealed open-day gaps visible as limitations.
        self.assertEqual(gap["severity"], "information")
        self.assertTrue(gap["acceptedFreeze"])
        self.assertEqual(result["historicalCalendarEvidence"]["openDays"], ["2026-10-12", "2026-10-13", "2026-10-14"])
        self.assertEqual(before, self.f.fingerprint())
        self.assertTrue(self.core_db.exists())

    def test_stalled_qlib_gaps_stay_critical_when_not_frozen(self):
        # RUNNING is itself a critical integrity issue (business_mode_not_frozen);
        # the core-gap severity below is the property under test.
        self.f.mode.write_text("RUNNING\n")
        before = self.f.fingerprint()
        result = self.audit()
        self.assertIn("business_mode_not_frozen", {i["code"] for i in result["issues"]})
        gap = self.gaps(result)
        self.assertEqual(gap["dates"], ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertEqual(gap["severity"], "critical")
        self.assertEqual(result["status"], "critical")
        self.assertEqual(before, self.f.fingerprint())

    def test_invalid_archives_never_establish_open_days(self):
        for defect in ("sidecar", "unsealed", "wrong_day", "context_disagrees", "forged_source"):
            with self.subTest(defect=defect):
                path = self.archive("2026-10-12")
                if defect == "sidecar":
                    (path / "input.sha256").write_text("0" * 64)
                elif defect == "unsealed":
                    published = json.loads((path / "published.json").read_text())
                    published["status"] = "failed"
                    (path / "published.json").write_text(json.dumps(published))
                else:
                    frozen = json.loads((path / "input.json").read_text())
                    if defect == "wrong_day":
                        frozen["targetDate"] = "2026-10-11"
                    elif defect == "context_disagrees":
                        frozen["rawContext"]["data"]["tradingCalendar"]["status"] = "non_trading_day"
                    else:
                        for key in ("rawContext", "compilerContext"):
                            frozen[key]["data"]["tradingCalendar"]["sourceIds"] = ["manual"]
                    (path / "input.json").write_text(json.dumps(frozen))
                    self.rebind(path)
                with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_evidence_unavailable_or_invalid"):
                    self.audit()

    def test_missing_publication_record_does_not_erase_hash_bound_calendar(self):
        path = self.project / "work/report-archives/2026-10-14"
        (path / "published.json").unlink()
        result = self.audit()
        self.assertIn("2026-10-14", result["historicalCalendarEvidence"]["openDays"])
        self.assertEqual(result["historicalCalendarEvidence"]["proofs"]["2026-10-14"]["inputSha256"],
                         hashlib.sha256((path / "input.json").read_bytes()).hexdigest())
        self.assertIn("2026-10-14", self.gaps(result)["dates"])

    def test_hash_mismatch_without_publication_is_still_invalid(self):
        path = self.project / "work/report-archives/2026-10-14"
        (path / "published.json").unlink()
        (path / "input.sha256").write_text("0" * 64 + "\n")
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_evidence_unavailable_or_invalid"):
            self.audit()

    def test_old_run_record_cannot_replace_missing_sealed_archive(self):
        path = self.project / "work/report-archives/2026-10-12"
        for file in path.iterdir():
            file.unlink()
        path.rmdir()
        state = self.project / "work/orchestrator/states/morning-report-2026-10-12.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"status": "success", "targetDate": "2026-10-12", "tradingStatus": "trading_day"}))
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_evidence_unavailable_or_invalid"):
            self.audit()

    def test_valid_closed_and_future_archives_do_not_create_gaps(self):
        self.archive("2026-10-11", "non_trading_day")  # Outside the proven predecessor chain.
        self.archive("2026-10-16")
        result = self.audit()
        self.assertNotIn("2026-10-11", self.gaps(result)["dates"])
        self.assertNotIn("2026-10-16", self.gaps(result)["dates"])

    def test_current_qlib_calendar_needs_no_historical_additions(self):
        self.set_qlib_days(self.days + ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"])
        result = self.audit()
        self.assertEqual(result["historicalCalendarEvidence"]["openDays"], [])
        self.assertEqual(self.gaps(result)["dates"], ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertNotIn("historical_calendar_evidence_invalid", {i["code"] for i in result["issues"]})

    def test_historical_days_obey_their_contract_intervals(self):
        self.policy.write_text(json.dumps({"schema": "research.contract-policy.v2", "transitions": [
            {"activeContract": "research.core-market.v1", "effectiveTradeDate": "2026-10-08"},
            {"activeContract": "research.full-snapshot.v1", "effectiveTradeDate": "2026-10-14"}]}))
        result = self.audit()
        self.assertEqual(self.gaps(result)["dates"], ["2026-10-12", "2026-10-13"])
        full = next(i for i in result["issues"] if i["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(full["dates"], ["2026-10-14", "2026-10-15"])

    def test_pending_today_does_not_hide_prior_due_gaps(self):
        self.f.now = self.f.now.replace(hour=21)
        self.assertEqual(self.gaps(self.audit())["dates"], ["2026-10-12", "2026-10-13", "2026-10-14"])

    def test_verified_weekend_keeps_preceding_trading_day_chain(self):
        self.archive("2026-10-16")
        self.f.now = dt.datetime(2026, 10, 17, 23, tzinfo=health.CST)
        result = self.audit()
        self.assertEqual(result["independentExpectedMarketDate"], "2026-10-16")
        self.assertEqual(self.gaps(result)["dates"], ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"])
        self.assertNotIn("2026-10-17", self.gaps(result)["dates"])

    def test_broken_predecessor_link_is_not_a_shorter_successful_history(self):
        path = self.project / "work/report-archives/2026-10-13/input.json"
        frozen = json.loads(path.read_text())
        for key in ("rawContext", "compilerContext"):
            frozen[key]["data"]["tradingCalendar"]["nextTradingDate"] = "2026-10-15"
        path.write_text(json.dumps(frozen))
        self.rebind(path.parent)
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_evidence_unavailable_or_invalid"):
            self.audit()

    def test_chain_cannot_jump_over_known_last_trading_day(self):
        path = self.project / "work/report-archives/2026-10-12/input.json"
        frozen = json.loads(path.read_text())
        for key in ("rawContext", "compilerContext"):
            frozen[key]["data"]["tradingCalendar"]["previousTradingDate"] = "2026-10-08"
        path.write_text(json.dumps(frozen))
        self.rebind(path.parent)
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_evidence_unavailable_or_invalid"):
            self.audit()

    def test_empty_chain_cannot_skip_known_last_trading_day(self):
        path = self.project / "work/report-archives/2026-10-15/input.json"
        frozen = json.loads(path.read_text())
        for key in ("rawContext", "compilerContext"):
            frozen[key]["data"]["tradingCalendar"]["previousTradingDate"] = "2026-10-08"
        path.write_text(json.dumps(frozen))
        self.rebind(path.parent)
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_evidence_unavailable_or_invalid"):
            self.audit()

    def test_invalid_history_uses_unknown_audit_and_retains_business_faults(self):
        # Non-frozen mode keeps the core gaps as business faults, so the test
        # still proves that an unknown audit cannot silently drop them. RUNNING
        # itself also contributes a business_mode_not_frozen atom.
        self.f.mode.write_text("RUNNING\n")
        payload = self.audit()
        report = self.f.root / "audit.json"
        report.write_text(json.dumps(payload))
        retained = Path(__file__).resolve().parents[1] / "config/health-alert-retained.json"
        before = alert.decide(alert.describe(report, now=self.f.now, retained_path=retained), "inactive", now=self.f.now)["state"]
        self.assertIn("unresolved_core_market_gaps|2026-10-12", before["businessAtoms"])
        path = self.project / "work/report-archives/2026-10-12/input.sha256"
        path.write_text("0" * 64)
        with self.assertRaises(health.EvidenceError) as raised:
            self.audit()
        self.assert_unknown_retains_faults(raised.exception, before, report, retained)

    def assert_unknown_retains_faults(self, error, before, report, retained):
        argv = ["data_health.py", "--db", str(self.f.db), "--qlib", str(self.f.qlib), "--stages", str(self.f.stages),
                "--last-run", str(self.f.last), "--mode", str(self.f.mode), "--since", "2026-08-28",
                "--monitor-since", "2026-08-31", "--output", str(report)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(health, "audit", side_effect=error), redirect_stdout(io.StringIO()):
            self.assertEqual(health.main(), 2)
        decision = alert.decide(alert.describe(report, now=self.f.now, retained_path=retained), "failed", previous=before, now=self.f.now)
        self.assertFalse(decision["state"]["auditVisible"])
        self.assertEqual(decision["state"]["businessAtoms"], before["businessAtoms"])
        self.assertEqual(decision["recovered"], [])

    def test_missing_current_calendar_cannot_recover_known_core_faults(self):
        report = self.f.root / "audit.json"
        report.write_text(json.dumps(self.audit()))
        retained = Path(__file__).resolve().parents[1] / "config/health-alert-retained.json"
        before = alert.decide(alert.describe(report, now=self.f.now, retained_path=retained), "inactive", now=self.f.now)["state"]
        (self.project / "work/report-archives/2026-10-15/input.sha256").write_text("0" * 64)
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_chain_head_unavailable") as raised:
            self.audit()
        self.assert_unknown_retains_faults(raised.exception, before, report, retained)

    def test_missing_current_calendar_before_deadline_preserves_known_faults(self):
        self.f.now = self.f.now.replace(hour=20)
        report = self.f.root / "audit.json"
        report.write_text(json.dumps(self.audit()))
        retained = Path(__file__).resolve().parents[1] / "config/health-alert-retained.json"
        before = alert.decide(alert.describe(report, now=self.f.now, retained_path=retained), "inactive", now=self.f.now)["state"]
        (self.project / "work/report-archives/2026-10-15/input.sha256").write_text("0" * 64)
        self.f.now = self.f.now.replace(hour=21)
        with self.assertRaisesRegex(health.EvidenceError, "historical_calendar_chain_head_unavailable") as raised:
            self.audit()
        self.assert_unknown_retains_faults(raised.exception, before, report, retained)

    def test_missing_current_calendar_preserves_full_only_behavior(self):
        (self.project / "work/report-archives/2026-10-15/input.sha256").write_text("0" * 64)
        self.f.now = self.f.now.replace(hour=23)
        self.policy.write_text(json.dumps({"activeContract": "research.full-snapshot.v1", "effectiveTradeDate": None}))
        result = self.audit()
        self.assertIn("independent_expected_day_unavailable", {i["code"] for i in result["issues"]})

    def test_no_core_history_does_not_require_sealed_history_chain(self):
        (self.project / "work/report-archives/2026-10-12/input.sha256").write_text("0" * 64)
        for policy in ({"activeContract": "research.full-snapshot.v1", "effectiveTradeDate": None},
                       {"activeContract": "research.core-market.v1", "effectiveTradeDate": "2026-10-20"}):
            with self.subTest(policy=policy):
                self.policy.write_text(json.dumps(policy))
                self.assertEqual(self.audit()["historicalCalendarEvidence"]["openDays"], [])


if __name__ == "__main__":
    unittest.main()
