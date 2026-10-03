"""Synthetic calendar intervals; no market collection or live database."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock

import calendar_evidence as adapter
import core_contract as contract
import core_market as core
import daily_snapshot as full
import data_health as health
import test_data_health as audit_tests


class ClosedIntervalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")).start()

    def now(self, day, clock="22:30:00"):
        return dt.datetime.fromisoformat(day + "T" + clock + "+08:00")

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def source(self, day="2026-11-06", following="2026-11-09", previous="2026-11-05"):
        calendar = {"targetDate": day, "status": "trading_day", "previousTradingDate": previous,
                    "nextTradingDate": following, "sourceIds": ["akshare:sina:trade-calendar"],
                    "dataAsOf": day + "T07:50:00+08:00"}
        path = self.write("work/report-archives/" + day + "/input.json", {
            "targetDate": day, "cutoffInstant": day + "T08:00:00+08:00",
            "compilerContext": {"data": {"tradingCalendar": calendar}},
            "rawContext": {"data": {"tradingCalendar": calendar}}})
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        (path.parent / "input.sha256").write_text(sha + "\n")
        (path.parent / "published.json").write_text(json.dumps({"status": "sealed", "inputSha256": sha}))
        return path

    def evidence(self, day="2026-11-07", clock="22:30:00"):
        return adapter.load_calendar_evidence(self.root, self.now(day, clock))

    def test_friday_open_weekend_closed_monday_requires_new_evidence(self):
        self.source()
        self.assertEqual(self.evidence("2026-11-06")["document"]["status"], "open")
        for day in ("2026-11-07", "2026-11-08"):
            evidence = self.evidence(day)
            self.assertEqual(evidence["document"]["status"], "closed")
            self.assertEqual(evidence["document"]["previousTradingDate"], "2026-11-06")
            self.assertEqual(evidence["document"]["observedAt"], "2026-11-06T07:50:00+08:00")
            self.assertEqual(evidence["binding"]["sourceEvidence"]["document"]["targetDate"], "2026-11-06")
        with self.assertRaises(ValueError): self.evidence("2026-11-09", "00:00:00")
        self.source("2026-11-09", "2026-11-10", "2026-11-06")
        self.assertEqual(self.evidence("2026-11-09")["document"]["status"], "open")

    def test_long_holiday_closed_weekday_and_end_boundary(self):
        self.source("2026-09-30", "2026-10-09", "2026-09-29")
        for day in ("2026-10-01", "2026-10-04", "2026-10-08"):
            self.assertEqual(self.evidence(day)["document"]["status"], "closed")
        with self.assertRaises(ValueError): self.evidence("2026-10-09")

    def terminal_source(self, day, status, previous="2026-11-06", following="2026-11-09"):
        calendar = {"targetDate": day, "status": "non_trading_day" if status == "skipped" else "trading_day",
                    "previousTradingDate": previous, "nextTradingDate": following,
                    "sourceIds": ["akshare:sina:trade-calendar"], "dataAsOf": day + "T08:00:00+08:00"}
        context = {"meta": {"targetDate": day, "timezone": "Asia/Shanghai", "asOf": day + "T08:00:10+08:00"},
                   "data": {"tradingCalendar": calendar}, "sources": [{"id": calendar["sourceIds"][0],
                   "dataAsOf": calendar["dataAsOf"], "url": "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"}]}
        path = self.write("work/morning-context.json", context)
        last = {"schemaVersion": "1.0", "targetDate": day, "status": status, "phase": "collect",
                "startedAt": day + "T08:00:00+08:00", "frozenCutoffInstant": day + "T08:00:00+08:00",
                "contextFile": "work/morning-context.json", "contextSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "publicationGate": "not_applicable" if status == "skipped" else "blocked"}
        last["completedAt" if status == "skipped" else "failedAt"] = day + "T08:00:11+08:00"
        if status == "skipped": last["tradingStatus"] = "non_trading_day"
        state = {"schemaVersion": "1.0", "task": "morning-report", "targetDate": day, "status": status,
                 "invocationId": "synthetic-occurrence", "startedAt": day + "T08:00:00+08:00",
                 "completedAt": day + "T08:00:12+08:00", "childExitCode": 0 if status == "skipped" else 1,
                 "timedOut": False, "interrupted": False}
        self.write("work/last-run.json", last)
        self.write("work/orchestrator/states/morning-report-" + day + ".json", state)

    def test_closed_source_retains_previous_trading_day_instead_of_closed_source_day(self):
        self.terminal_source("2026-11-07", "skipped")
        value = self.evidence("2026-11-08")
        self.assertEqual(value["document"]["previousTradingDate"], "2026-11-06")
        self.assertEqual(value["binding"]["sourceEvidence"]["document"]["targetDate"], "2026-11-07")

    def test_same_day_closed_proof_rechecks_all_mutable_files(self):
        original = adapter._assert_publication_absent
        for relative in ("work/last-run.json", "work/morning-context.json",
                         "work/orchestrator/states/morning-report-2026-11-07.json"):
            self.terminal_source("2026-11-07", "skipped")
            calls = []
            def concurrent_write(*args):
                original(*args)
                calls.append(1)
                if len(calls) == 2:
                    path = self.root / relative
                    path.write_bytes(path.read_bytes() + b" ")
            with self.subTest(relative=relative), mock.patch.object(adapter, "_assert_publication_absent", side_effect=concurrent_write):
                with self.assertRaises(ValueError): self.evidence()

    def test_failed_report_with_valid_bound_calendar_still_proves_closed_interval(self):
        self.terminal_source("2026-11-06", "failed", previous="2026-11-05")
        value = self.evidence()
        self.assertEqual(value["document"]["status"], "closed")
        self.assertEqual(value["binding"]["sourceEvidence"]["binding"]["runStatus"], "failed")

    def test_missing_next_date_unknown_year_and_unbounded_interval_fail(self):
        path = self.source()
        frozen = json.loads(path.read_text())
        for part in ("compilerContext", "rawContext"):
            frozen[part]["data"]["tradingCalendar"].pop("nextTradingDate")
        path.write_text(json.dumps(frozen))
        with self.assertRaises(ValueError): self.evidence()
        self.source("2026-11-06", "2026-12-20")
        with self.assertRaises(ValueError): self.evidence()
        # A same-year proof does not silently establish the next year's calendar.
        self.source("2026-12-31", "2027-01-04", "2026-12-30")
        with self.assertRaises(ValueError): self.evidence("2027-01-01")

    def test_bad_today_archive_or_occurrence_never_falls_back(self):
        self.source()
        today = self.root / "work/report-archives/2026-11-07"
        today.mkdir()
        for value in ({"status": "failed"}, {"status": "sealed"}):
            (today / "published.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError): self.evidence()
        (today / "published.json").unlink()
        today.rmdir()
        self.write("work/orchestrator/states/morning-report-2026-11-07.json", {"status": "failed"})
        with self.assertRaises(ValueError): self.evidence()

    def test_damaged_latest_source_not_skipped_in_favour_of_older_proof(self):
        self.source()
        self.write("work/report-archives/2026-11-07/input.json", {"broken": True})
        with self.assertRaises(ValueError): self.evidence("2026-11-08")

    def test_newer_unbound_shared_context_blocks_older_archive(self):
        self.source()
        self.write("work/morning-context.json", {"meta": {"targetDate": "2026-11-07"},
                   "data": {"tradingCalendar": {"targetDate": "2026-11-07"}}})
        with self.assertRaises(ValueError): self.evidence("2026-11-08")
        self.write("work/morning-context.json", {"meta": {"targetDate": "2026-11-06"},
                   "data": {"tradingCalendar": {"targetDate": "2026-11-07"}}})
        with self.assertRaises(ValueError): self.evidence("2026-11-08")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_nonregular_calendar_files_rejected_without_blocking_open(self):
        path = self.source()
        path.unlink()
        os.mkfifo(path)
        start = time.monotonic()
        with self.assertRaises(ValueError): self.evidence("2026-11-06")
        self.assertLess(time.monotonic() - start, 1)
        path.unlink()
        self.source()
        state = self.root / "work/orchestrator/states/morning-report-2026-11-07.json"
        state.parent.mkdir(parents=True)
        os.mkfifo(state)
        start = time.monotonic()
        with self.assertRaises(ValueError): self.evidence()
        self.assertLess(time.monotonic() - start, 1)

    def test_tampered_source_and_symlink_and_permission_fail(self):
        path = self.source()
        original = path.read_bytes()
        path.write_text('{}')
        with self.assertRaises(ValueError): self.evidence()
        path.write_bytes(original)
        path.unlink()
        path.symlink_to(self.root / "missing-input")
        with self.assertRaises(ValueError): self.evidence()
        path.unlink()
        self.source()
        read = adapter._optional_file
        def denied(root, name, limit):
            if name == "work/orchestrator/states/morning-report-2026-11-07.json":
                raise PermissionError("synthetic permission denial")
            return read(root, name, limit)
        with mock.patch.object(adapter, "_optional_file", side_effect=denied):
            with self.assertRaises(ValueError): self.evidence()

    def test_source_change_or_today_appearance_during_read_fails(self):
        self.source()
        with mock.patch.object(adapter, "_current_material", side_effect=[False, True]):
            with self.assertRaises(ValueError): self.evidence()
        read = adapter._load_current_evidence
        calls = 0
        def changed(path, now):
            nonlocal calls
            value = read(path, now)
            calls += 1
            if calls == 2: value["binding"]["inputSha256"] = "f" * 64
            return value
        with mock.patch.object(adapter, "_load_current_evidence", side_effect=changed):
            with self.assertRaises(ValueError): self.evidence()

    def test_rehashed_derived_claims_and_nested_source_cannot_authorize_open(self):
        self.source()
        value = self.evidence()
        for key, change in (("status", "open"), ("previousTradingDate", "2026-11-05"),
                            ("observedAt", "2026-11-07T08:00:00+08:00"),
                            ("nextTradingDate", "2026-11-10")):
            modified = copy.deepcopy(value)
            modified["document"][key] = change
            modified["sha256"] = contract.digest({k: v for k, v in modified.items() if k != "sha256"})
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_calendar_evidence(modified, self.now("2026-11-07"))
        nested = copy.deepcopy(value)
        nested["binding"]["sourceEvidence"] = value
        with self.assertRaises(ValueError): contract.validate_calendar_evidence(nested, self.now("2026-11-07"))

    def test_timezone_midnight_and_interval_expiration(self):
        self.source()
        moment = dt.datetime.fromisoformat("2026-11-06T16:00:00+00:00")
        value = adapter.load_calendar_evidence(self.root, moment)
        self.assertEqual(value["document"]["targetDate"], "2026-11-07")
        contract.validate_calendar_evidence(value, moment)
        with self.assertRaises(ValueError):
            contract.validate_calendar_evidence(value, self.now("2026-11-09", "00:00:00"))

    def test_full_and_core_closed_branches_do_not_collect_or_write_database(self):
        self.source()
        evidence = self.evidence()
        with mock.patch.object(full, "qlib_market", side_effect=AssertionError("no collector")):
            core_db = self.root / "core.db"
            result = core.run_core(db_path=core_db, now=self.now("2026-11-07").isoformat(), calendar_evidence=evidence)
            self.assertEqual(result["status"], "skipped_non_trading")
            self.assertFalse(core_db.exists())
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = self.now("2026-11-07")
        with mock.patch.object(full.datetime, "datetime", clock), mock.patch.dict(
                "os.environ", {"RESEARCH_DATA_CORE_CALENDAR_PROJECT_ROOT": str(self.root)}), \
                mock.patch("sys.argv", ["daily_snapshot.py"]), \
                mock.patch.object(full, "run_snapshot", side_effect=AssertionError("no snapshot")), \
                mock.patch.object(full, "emit_status") as emitted:
            self.assertEqual(full.run_current_day("c" * 32, time.monotonic()), 0)
        self.assertEqual(emitted.call_args.args[0]["status"], "skipped_non_trading")
        self.assertEqual(emitted.call_args.args[0]["target"], "2026-11-06")

    def test_audit_reuses_same_closed_proof_and_preserves_historical_gap(self):
        self.source("2026-09-04", "2026-09-07", "2026-09-03")
        evidence = self.evidence("2026-09-05")
        fixture = audit_tests.AuditTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.now = self.now("2026-09-05", "23:00:00")
        fixture.last.write_text(json.dumps({"runId": "a" * 32, "status": "skipped_non_trading",
            "target": "2026-09-04", "timestamp": "2026-09-05T22:30:00+08:00", "calendarEvidence": evidence}))
        before = fixture.db.read_bytes()
        result = fixture.run_audit(calendar_project=self.root)
        codes = fixture.codes(result)
        self.assertNotIn("independent_expected_day_unavailable", codes)
        self.assertNotIn("last_snapshot_not_successful", codes)
        self.assertTrue(result["activeLastRun"]["calendarClosedVerified"])
        self.assertEqual(fixture.db.read_bytes(), before)
        fixture.sql("DELETE FROM snapshot WHERE date='2026-09-02'")
        result = fixture.run_audit(calendar_project=self.root)
        gap = next(x for x in result["issues"] if x["code"] == "unresolved_trading_day_gaps")
        self.assertIn("2026-09-02", gap["dates"])


if __name__ == "__main__":
    unittest.main()
