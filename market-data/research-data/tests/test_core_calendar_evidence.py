import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import core_market as core
import daily_snapshot as legacy
import data_health
from test_core_market_contract import calendar, closed_calendar

DAY = "2026-09-28"
NOW = dt.datetime.fromisoformat(DAY + "T22:30:00+08:00")


class CalendarAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write_json(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def frozen_calendar(self, status="trading_day"):
        return {"targetDate": DAY, "status": status, "previousTradingDate": "2026-09-24",
                "nextTradingDate": "2026-09-29", "sourceIds": ["akshare:sina:trade-calendar"],
                "dataAsOf": DAY + "T08:00:00+08:00"}

    def archive(self):
        c = self.frozen_calendar()
        value = {"targetDate": DAY, "cutoffInstant": DAY + "T08:00:03+08:00",
                 "compilerContext": {"data": {"tradingCalendar": c}}, "rawContext": {"data": {"tradingCalendar": c}}}
        path = self.write_json("work/report-archives/" + DAY + "/input.json", value)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        (path.parent / "input.sha256").write_text(sha + "\n")
        (path.parent / "published.json").write_text(json.dumps({"status": "sealed", "inputSha256": sha}))
        return path.parent

    def skip(self):
        path = self.write_json("work/morning-context.json", {"data": {"tradingCalendar": self.frozen_calendar("non_trading_day")}})
        last = {"targetDate": DAY, "status": "skipped", "phase": "collect", "tradingStatus": "non_trading_day",
                "publicationGate": "not_applicable", "contextFile": "work/morning-context.json",
                "contextSha256": hashlib.sha256(path.read_bytes()).hexdigest(), "completedAt": DAY + "T08:00:11+08:00"}
        state = {"task": "morning-report", "targetDate": DAY, "status": "skipped", "childExitCode": 0,
                 "timedOut": False, "interrupted": False, "completedAt": DAY + "T08:00:12+08:00"}
        return self.write_json("work/last-run.json", last), self.write_json("work/orchestrator/states/morning-report-" + DAY + ".json", state)

    def test_same_day_sealed_archive_is_bound_without_recompilation(self):
        path = self.archive()
        first = core.load_calendar_evidence(path, NOW)
        self.assertEqual(first, core.load_calendar_evidence(self.root, NOW))
        self.assertEqual(first["document"]["status"], "open")
        self.assertEqual(first["binding"]["inputSha256"], hashlib.sha256((path / "input.json").read_bytes()).hexdigest())

    def test_tampered_archive_cannot_fall_back_to_skip(self):
        self.skip()
        path = self.archive()
        (path / "input.json").write_text("{}")
        with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)

    def test_explicit_nonsealed_publication_record_is_not_a_fallback(self):
        # An invalid published claim is contradictory evidence, unlike an
        # unfinished archive which has never produced published.json.
        path = self.archive()
        published = json.loads((path / "published.json").read_text())
        published["status"] = "generation_failed"
        (path / "published.json").write_text(json.dumps(published))
        with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)
        self.assertFalse((self.root / "core-market.db").exists())

    def test_symlinked_archive_parent_is_rejected(self):
        path = self.archive()
        archive_parent = path.parent
        relocated = self.root / "elsewhere"
        archive_parent.rename(relocated)
        archive_parent.symlink_to(relocated, target_is_directory=True)
        with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)

    def test_closed_skip_does_not_read_qlib_or_write_database(self):
        self.skip()
        evidence = core.load_calendar_evidence(self.root, NOW)
        db = self.root / "no-core.db"
        with mock.patch.object(legacy, "qlib_market", side_effect=AssertionError("must not scan")):
            result = core.run_core(db_path=db, now=NOW.isoformat(), calendar_evidence=evidence)
        self.assertEqual(result["status"], "skipped_non_trading")
        self.assertEqual(result["target"], "2026-09-24")
        self.assertFalse(db.exists())

    def test_skip_requires_success_identity_context_hash_and_real_source(self):
        for part, change in (("last", {"targetDate": "2026-09-25"}), ("last", {"contextSha256": "0" * 64}),
                             ("last", {"publicationGate": "passed"}), ("last", {"contextFile": "work/../secret.json"}),
                             ("state", {"childExitCode": 1}), ("state", {"interrupted": True}),
                             ("state", {"completedAt": DAY + "T23:59:59+08:00"})):
            last, state = self.skip()
            path = last if part == "last" else state
            value = json.loads(path.read_text()); value.update(change); path.write_text(json.dumps(value))
            with self.subTest(change=change), self.assertRaises(legacy.SnapshotDeferredError):
                core.load_calendar_evidence(self.root, NOW)

    def test_missing_stale_or_wrong_source_never_means_closed(self):
        with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)
        self.skip()
        with self.assertRaises(legacy.SnapshotDeferredError):
            core.load_calendar_evidence(self.root, NOW + dt.timedelta(days=1))
        evidence = closed_calendar(DAY)
        evidence["document"]["sourceIds"] = ["manual"]
        evidence["sha256"] = core.digest({k:v for k,v in evidence.items() if k != "sha256"})
        with self.assertRaises(legacy.SnapshotDeferredError): core.run_core(now=NOW.isoformat(), calendar_evidence=evidence)

    def test_actual_cli_parsers_help(self):
        # Exercise real argparse entrypoints, not just audit() and run_core().
        for target, argv in ((data_health.main, ["data_health.py", "--help"]), (core.main, ["core_market.py", "--help"])):
            with mock.patch("sys.argv", argv), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                with self.assertRaises(SystemExit) as stopped: target()
                self.assertEqual(stopped.exception.code, 0)
                self.assertIn("--contract-policy", out.getvalue())


if __name__ == "__main__":
    unittest.main()
