"""D02–D05 fixed evidence; no collection, no live database or sender."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import absence_policy
import core_market
import snapshot_runtime
import calendar_evidence
import daily_snapshot as daily
import data_health
from test_core_market_contract import calendar, closed_calendar
from test_daily_snapshot import sample_qlib_market
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import health_alert


class CurrentDayTests(unittest.TestCase):
    def test_non_akshare_boundary_is_not_mislabelled_as_akshare_or_timeout(self):
        import bounded_source
        with mock.patch.object(daily, "_supervised_run_id", return_value="a" * 32), \
             mock.patch.object(daily, "_supervised_soft_deadline"), \
             mock.patch.object(daily, "write_run_checkpoint") as checkpoint, \
             mock.patch.dict(os.environ, {"RESEARCH_DATA_SOFT_DEADLINE_EPOCH": str(time.time() + 60)}), \
             mock.patch.object(bounded_source, "call", side_effect=bounded_source.CollectionBoundaryError("collector_result_unavailable")):
            with self.assertRaises(daily.SnapshotTemporaryError) as caught:
                daily.collect_source(lambda: None)
            self.assertEqual(caught.exception.source_failure["provider"], "source_collection")
            self.assertEqual(caught.exception.source_failure["attempts"], [{"attempt": 1, "errorType": "RuntimeError"}])
            self.assertIsNone(checkpoint.call_args.kwargs["activeEndpoint"])

    def test_core_supervised_worker_keeps_full_and_core_state_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            full = str(Path(directory) / "full-state")
            core = str(Path(directory) / "core-state")
            policy = Path(directory) / "contract.json"
            policy.write_text(json.dumps({"activeContract": core_market.SCHEMA, "effectiveTradeDate": "2000-01-01"}))
            with mock.patch.object(snapshot_runtime, "STATE_DIR", core), \
                 mock.patch.object(snapshot_runtime, "_supervised_run_id", return_value="a" * 32), \
                 mock.patch.dict(os.environ, {"RESEARCH_DATA_FULL_STATE_DIR": full}), \
                 mock.patch.object(snapshot_runtime, "_await_supervisor_gate"), \
                 mock.patch.object(snapshot_runtime, "_verify_supervisor_lock"), \
                 mock.patch.object(core_market, "load_calendar_evidence", return_value={}), \
                 mock.patch.object(core_market, "run_core", return_value={"status": "skipped_non_trading"}), \
                 mock.patch.object(snapshot_runtime, "write_last_run") as record, mock.patch("builtins.print"):
                self.assertEqual(core_market.main(["--state-dir", core, "--contract-policy", str(policy)], _cli=True), 0)
                self.assertEqual(record.call_args.args[1], core)
                with self.assertRaises(SystemExit):
                    core_market.main(["--state-dir", full, "--contract-policy", str(policy)], _cli=True)

    def test_stale_qlib_preserves_stage_before_stopping_full_assembly(self):
        stage = {"status": "reused", "tradeDate": "2026-09-28",
                 "payloadSha256": "a" * 64, "recordSha256": "b" * 64}
        with mock.patch.object(sys, "argv", ["daily_snapshot.py"]), \
             mock.patch.object(daily, "qlib_market", return_value=sample_qlib_market("2026-09-24")), \
             mock.patch.object(daily, "existing_snapshot") as prior, \
             mock.patch.object(daily, "_current_day_stage", return_value=stage) as acquire, \
             mock.patch.object(daily, "capture_current_akshare_market") as collector:
            with self.assertRaisesRegex(daily.SnapshotDeferredError, "qlib_not_current") as caught:
                daily.run_snapshot("fixed", time.monotonic(), expected_day="2026-09-28")
            self.assertEqual(caught.exception.target, "2026-09-28")
            self.assertEqual(caught.exception.akshare_stage, stage)
            acquire.assert_called_once_with("fixed", "2026-09-28")
            prior.assert_not_called(); collector.assert_not_called()

    def test_missing_independent_day_cannot_run(self):
        with mock.patch.object(calendar_evidence, "load_calendar_evidence", side_effect=ValueError("missing")), \
             mock.patch.object(daily, "run_snapshot") as writer, mock.patch.object(sys, "argv", ["daily_snapshot.py"]):
            with self.assertRaisesRegex(daily.SnapshotDeferredError, "independent_expected_day_unavailable"):
                daily.run_current_day("fixed", time.monotonic())
            writer.assert_not_called()

    def test_supervised_old_day_cannot_create_stage_or_database(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(daily, "_supervised_run_id", return_value="a" * 32):
            path = Path(root) / "no.db"
            with self.assertRaisesRegex(daily.SnapshotValidationError, "historical_snapshot_write_forbidden"):
                daily.store_snapshot({"date": "2026-09-22"}, str(path))
            self.assertFalse(path.exists())

    def test_closed_live_entry_ends_before_any_collection(self):
        day = "2026-09-25"
        for hour, allowed in ((22, True), (7, False)):
            fixed = dt.datetime.fromisoformat(f"{day}T{hour:02d}:30:00+08:00")
            class FixedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
            with self.subTest(hour=hour), \
                 mock.patch.object(daily.datetime, "datetime", FixedDateTime), \
                 mock.patch.object(calendar_evidence, "load_calendar_evidence", return_value=closed_calendar(day)), \
                 mock.patch.object(daily, "run_snapshot") as writer, \
                 mock.patch.object(daily, "emit_status") as emit, mock.patch.object(sys, "argv", ["daily_snapshot.py"]):
                if allowed:
                    self.assertEqual(daily.run_current_day("fixed", time.monotonic()), 0)
                    self.assertEqual(emit.call_args.args[0]["status"], "skipped_non_trading")
                else:
                    with self.assertRaisesRegex(daily.SnapshotDeferredError, "independent_expected_day_unavailable"):
                        daily.run_current_day("fixed", time.monotonic())
                    emit.assert_not_called()
                writer.assert_not_called()


class AbsenceTests(unittest.TestCase):
    def test_scope_thresholds_are_independent(self):
        symbols = [f"SH{600000+i}" for i in range(26)]
        scopes = absence_policy.scopes(["2026-09-23", "2026-09-24"], "2026-09-24",
            {"invalid": 26, "eligible": 5570, "invalid_symbols": symbols},
            {"missingQuoteMembers": symbols, "missingPreviousCloseMembers": symbols, "uniqueMembers": 5085},
            {s: ["2026-09-24"] for s in symbols}, {s: "2026-09-23" for s in symbols}, True)
        self.assertEqual(scopes["full_market"]["severity"], "information")
        self.assertEqual(scopes["csi_members"]["severity"], "warning")
        self.assertEqual(scopes["csi_members"]["invalidCount"], 26)
        self.assertEqual(scopes["csi_members"]["absenceTolerance"], 25.425)

    def test_single_increment_does_not_reset_four_day_absence(self):
        days = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
        result = absence_policy.scopes(days, days[-1],
            {"invalid": 1, "eligible": 5570, "invalid_symbols": ["SH600001"]},
            {"missingQuoteMembers": ["SH600001"], "uniqueMembers": 5085},
            {"SH600001": days[1:]}, {"SH600001": days[0]}, True)
        self.assertEqual(result["full_market"]["escalation"], "duration_over_bound:4>3")

    def test_resumed_quote_with_missing_prior_close_has_zero_current_streak(self):
        result = absence_policy.duration(["2026-09-23", "2026-09-24"], "2026-09-24", ["SH600001"],
                                         {"SH600001": ["2026-09-23"]}, {"SH600001": "2026-09-24"})
        self.assertTrue(result["proven"])
        self.assertEqual(result["maxConsecutiveMissingTradingDays"], 0)

    def test_uncovered_calendar_or_hole_does_not_prove_bounded_duration(self):
        for quoted, missing in (("2026-09-21", ["2026-09-24"]), ("2026-09-22", ["2026-09-24"])):
            result = absence_policy.duration(["2026-09-22", "2026-09-23", "2026-09-24"], "2026-09-24",
                                             ["SH600001"], {"SH600001": missing}, {"SH600001": quoted})
            self.assertFalse(result["proven"])

    def test_rolling_three_day_tail_cannot_reset_a_long_streak(self):
        days = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
        result = absence_policy.scopes(days, days[-1],
            {"invalid": 1, "eligible": 5570, "invalid_symbols": ["SH600001"]},
            {"missingQuoteMembers": ["SH600001"], "uniqueMembers": 5085},
            {"SH600001": days[-3:]}, {"SH600001": days[0]}, True)
        self.assertEqual(result["full_market"]["severity"], "warning")
        self.assertEqual(result["full_market"]["escalation"], "proof_incomplete")
        self.assertEqual(result["full_market"]["maxConsecutiveMissingTradingDays"], 4)


class WatchdogTests(unittest.TestCase):
    def test_pre_window_audit_is_stale_even_if_recent(self):
        now = dt.datetime.fromisoformat("2026-09-28T23:00:00+08:00")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "health.json"
            path.write_text(json.dumps({"status": "ok", "generatedAt": "2026-09-28T22:30:00+08:00", "issues": []}))
            info = health_alert.describe(path, now=now)
        self.assertTrue(info["stale"])
        self.assertEqual(info["reason"], "scheduled_audit_missing")
        self.assertIn("audit_not_current|2026-09-28", health_alert.alert_atoms(info, "inactive", now))

    def test_watchdog_reuses_original_sender_and_state_machine(self):
        root = Path(__file__).parents[1]
        timer = (root / "systemd/research-data-health-alert.timer").read_text()
        self.assertIn("Unit=research-data-health-alert.service", timer)
        self.assertIn("23:00:00 Asia/Shanghai", timer)
        self.assertNotIn("Persistent=true", timer)
        wrapper = (root / "scripts/run_daily_snapshot_with_alert.sh").read_text()
        self.assertNotIn('send_telegram "$msg"', wrapper[wrapper.index('set +e'):])
        effective = (root / "systemd/research-data-health.service.d/output.conf").read_text()
        self.assertIn("--calendar-project /home/ubuntu/AStock_AI_System", effective)
        self.assertIn("--core-last-run", effective)

    def test_audit_detects_expected_open_day_even_with_stale_qlib_and_existing_row(self):
        from test_data_health import AuditTests
        fixture = AuditTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        now = dt.datetime.fromisoformat("2026-09-07T23:00:00+08:00")
        fixture.last.write_text(json.dumps({"status": "skipped_existing", "target": "2026-09-04",
                                           "timestamp": "2026-09-07T22:31:00+08:00"}))
        before = fixture.fingerprint()
        with mock.patch.object(data_health._calendar_evidence, "load_calendar_evidence", return_value=calendar("2026-09-07")):
            result = fixture.run_audit(now=now, calendar_project=fixture.root)
        self.assertEqual(result["independentExpectedMarketDate"], "2026-09-07")
        self.assertIn("qlib_not_current_for_expected_trading_day", fixture.codes(result))
        gaps = next(issue for issue in result["issues"] if issue["code"] == "unresolved_trading_day_gaps")
        self.assertEqual(gaps["dates"], ["2026-09-07"])
        self.assertEqual(before, fixture.fingerprint())

    def test_missing_calendar_produces_explicit_operational_warning(self):
        from test_data_health import AuditTests
        fixture = AuditTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        now = dt.datetime.fromisoformat("2026-09-07T23:00:00+08:00")
        result = fixture.run_audit(now=now, calendar_project=fixture.root / "no-project")
        self.assertIn("independent_expected_day_unavailable", fixture.codes(result))
        self.assertIsNone(result["independentExpectedMarketDate"])

    def test_full_old_success_and_predeadline_do_not_prove_todays_run(self):
        from test_data_health import AuditTests
        fixture = AuditTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        with mock.patch.object(data_health._calendar_evidence, "load_calendar_evidence", return_value=calendar("2026-09-07")):
            before = fixture.run_audit(now=dt.datetime.fromisoformat("2026-09-07T22:35:00+08:00"), calendar_project=fixture.root)
            after = fixture.run_audit(now=dt.datetime.fromisoformat("2026-09-07T23:00:00+08:00"), calendar_project=fixture.root)
        self.assertFalse(any(i["code"] == "scheduled_snapshot_missing" and i.get("dueDate") == "2026-09-07" for i in before["issues"]))
        self.assertTrue(next(r for r in before["ledger"] if r["tradeDate"] == "2026-09-07")["pendingWindow"])
        self.assertTrue(any(i["code"] == "scheduled_snapshot_missing" and i.get("dueDate") == "2026-09-07" for i in after["issues"]))

    def test_core_old_success_and_predeadline_do_not_prove_todays_run(self):
        from test_core_market_health import CoreHealthTests
        fixture = CoreHealthTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        with mock.patch.object(data_health._calendar_evidence, "load_calendar_evidence", return_value=calendar("2026-09-07")):
            before = fixture.audit(now=dt.datetime.fromisoformat("2026-09-07T22:35:00+08:00"), calendar_project=fixture.f.root)
            after = fixture.audit(now=dt.datetime.fromisoformat("2026-09-07T23:00:00+08:00"), calendar_project=fixture.f.root)
        self.assertTrue(next(r for r in before["coreMarket"]["ledger"] if r["tradeDate"] == "2026-09-07")["pendingWindow"])
        self.assertFalse(any(i["code"] == "scheduled_core_market_missing" and i.get("dueDate") == "2026-09-07" for i in before["issues"]))
        self.assertTrue(any(i["code"] == "scheduled_core_market_missing" and i.get("dueDate") == "2026-09-07" for i in after["issues"]))
        self.assertEqual(next(r for r in after["coreMarket"]["ledger"] if r["tradeDate"] == "2026-09-07")["coreState"], "missing")

    def test_new_expected_day_failure_is_event_then_same_day_silent(self):
        now = dt.datetime.fromisoformat("2026-09-28T23:00:00+08:00")
        atoms = health_alert._due_run_atoms({"issues": [{"code": "qlib_not_current_for_expected_trading_day",
                                                        "dueDate": "2026-09-28", "severity": "warning"}]})
        self.assertEqual(atoms, ["qlib_not_current_for_expected_trading_day|2026-09-28"])
        info = {"status": "warning", "stale": False, "criticalAtoms": [], "dueRunAtoms": atoms}
        first = health_alert.decide(info, "inactive", now=now)
        self.assertTrue(first["notify"])
        second = health_alert.decide(info, "inactive", previous=first["state"], now=now)
        self.assertFalse(second["notify"])
