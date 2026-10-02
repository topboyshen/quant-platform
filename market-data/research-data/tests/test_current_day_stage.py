"""Fixed collectors exercise real stage sealing, full validation, and SQLite."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest import mock

import calendar_evidence
import daily_snapshot as daily
import snapshot_supervisor as supervisor
from test_core_market_contract import calendar, closed_calendar
from test_current_sources import values
from test_daily_snapshot import sample_akshare_market, sample_qlib_market


DAY = "2026-09-24"
NOW = dt.datetime.fromisoformat(DAY + "T22:30:00+08:00")
REAL_DATETIME = dt.datetime


class FixedDatetime(REAL_DATETIME):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


class CurrentDayStageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.db = self.root / "snapshot.db"
        self.stage_dir = self.root / "stage"
        FixedDatetime.current = NOW
        for name, value in (("DB", str(self.db)), ("STATE_DIR", str(self.root / "state")),
                            ("AKSHARE_STAGE_DIR", str(self.stage_dir))):
            self.stack.enter_context(mock.patch.object(daily, name, value))
        self.stack.enter_context(mock.patch.object(daily.datetime, "datetime", FixedDatetime))
        self.stack.enter_context(mock.patch.object(daily.sys, "argv", ["daily_snapshot.py"]))
        self.stack.enter_context(mock.patch.object(daily, "collect_source", side_effect=lambda fn, *args, **kwargs: fn(*args)))
        self.stack.enter_context(mock.patch.object(daily, "emit_status"))
        self.stack.enter_context(mock.patch.object(calendar_evidence, "load_calendar_evidence", return_value=calendar(DAY)))

    def full_sources(self, *, concepts_failed=False):
        config = self.root / "stocks.json"
        config.write_text(json.dumps({"corePool": [{"code": "002617"}]}))
        expand = os.path.expanduser
        self.stack.enter_context(mock.patch.object(daily.os.path, "expanduser", side_effect=lambda path:
            str(config) if path == "~/research-data/config/stocks.json" else expand(path)))
        concepts, insiders = values()
        if concepts_failed:
            concepts = {"002617": {"error": "fixed source failure"}}
        sources = {
            "akshare_lhb": {"date": DAY, "count": 0, "rows": []},
            "spot_market": {"date": "current", "breadth": {"total": 2000}, "limitUp": []},
            "fund_flow_sina": {"002617": [1]},
            "lhb_official": {"date": DAY, "szse": [], "sse": ""},
            "mootdx_quotes": {"002617": {"price": 10}},
            "tencent_quote": {"002617": {"price": "10"}},
            "baostock_valuation": {"002617": [{"date": DAY}]},
            "collect_cninfo_announcements": {"002617": []},
            "cls_telegraph": [{"title": "fixed news"}],
            "northbound_flow": {"hgt": 1.0, "sgt": 2.0, "total": 3.0},
            "collect_concepts": concepts, "collect_shareholders": insiders,
            "hot_stocks": [{"code": "002617"}],
            "global_markets": {"道指": {}, "纳指": {}, "标普": {}},
        }
        for name, value in sources.items():
            self.stack.enter_context(mock.patch.object(daily, name, return_value=value))

    def test_stale_qlib_preserves_stage_and_same_day_recovery_reuses_exact_bytes(self):
        self.full_sources()
        events = []
        market = self.stack.enter_context(mock.patch.object(daily, "akshare_market", side_effect=lambda:
            (events.append("market") or sample_akshare_market(DAY))))
        qlib_days = iter(("2026-09-23", DAY))
        self.stack.enter_context(mock.patch.object(daily, "qlib_market", side_effect=lambda _:
            (events.append("qlib") or sample_qlib_market(next(qlib_days)))))
        with self.assertRaisesRegex(daily.SnapshotDeferredError, "qlib_not_current") as caught:
            daily.run_current_day("first", time.monotonic())
        self.assertEqual(events, ["market", "qlib"])
        stage_path = self.stage_dir / (DAY + ".json")
        sealed_bytes = stage_path.read_bytes()
        stage = daily.load_akshare_market_stage(DAY)
        self.assertEqual(caught.exception.target, DAY)
        self.assertEqual(caught.exception.akshare_stage["recordSha256"], stage["recordSha256"])
        self.assertFalse(self.db.exists(), "stale Qlib must not create a mixed-day database")
        FixedDatetime.current = NOW + dt.timedelta(minutes=2)
        self.assertEqual(daily.run_current_day("second", time.monotonic()), 0)
        market.assert_called_once()
        self.assertEqual(stage_path.read_bytes(), sealed_bytes)
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT date, payload FROM snapshot").fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][1])
        self.assertEqual((rows[0][0], payload["date"], payload["qlib"]["date"],
                          daily.activity_trade_date(payload["akshare"])), (DAY,) * 4)
        self.assertEqual(payload["assemblyKind"], "same_day_observation")
        self.assertEqual(payload["sourceAsOf"]["akshareStage"]["recordSha256"], stage["recordSha256"])

    def test_stage_survives_qlib_read_failure(self):
        with mock.patch.object(daily, "akshare_market", return_value=sample_akshare_market(DAY)), \
                mock.patch.object(daily, "qlib_market", side_effect=daily.SnapshotValidationError("coverage")):
            with self.assertRaisesRegex(daily.SnapshotValidationError, "coverage"):
                daily.run_current_day("read-failure", time.monotonic())
        self.assertIsNotNone(daily.load_akshare_market_stage(DAY))
        self.assertFalse(self.db.exists())

    def test_failed_stage_is_not_published_and_does_not_start_qlib(self):
        for payload in ({"error": "fixed source failure"}, sample_akshare_market("2026-09-23")):
            with self.subTest(payload=payload), \
                    mock.patch.object(daily, "akshare_market", return_value=payload), \
                    mock.patch.object(daily, "qlib_market") as qlib:
                with self.assertRaises((daily.SnapshotValidationError, daily.SnapshotDeferredError)):
                    daily.run_current_day("bad-stage", time.monotonic())
                qlib.assert_not_called()
                self.assertFalse(self.stage_dir.exists())
                self.assertFalse(self.db.exists())

    def test_retained_stage_does_not_relax_full_source_contract(self):
        self.full_sources(concepts_failed=True)
        with mock.patch.object(daily, "akshare_market", return_value=sample_akshare_market(DAY)), \
                mock.patch.object(daily, "qlib_market", return_value=sample_qlib_market(DAY)):
            with self.assertRaisesRegex(daily.SnapshotValidationError, "concepts.002617.error"):
                daily.run_current_day("source-failed", time.monotonic())
        self.assertIsNotNone(daily.load_akshare_market_stage(DAY))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM snapshot").fetchone()[0], 0)

    def test_holiday_has_no_market_qlib_or_snapshot_collection(self):
        FixedDatetime.current = REAL_DATETIME.fromisoformat("2026-09-26T22:30:00+08:00")
        with mock.patch.object(calendar_evidence, "load_calendar_evidence", return_value=closed_calendar("2026-09-26")), \
                mock.patch.object(daily, "akshare_market") as market, \
                mock.patch.object(daily, "qlib_market") as qlib:
            self.assertEqual(daily.run_current_day("holiday", time.monotonic()), 0)
        market.assert_not_called()
        qlib.assert_not_called()
        self.assertFalse(self.db.exists())
        self.assertFalse(self.stage_dir.exists())

    def test_rollover_before_stage_publication_cannot_write_yesterday(self):
        real_fsync = os.fsync
        def roll_over(fd):
            real_fsync(fd)
            FixedDatetime.current = NOW + dt.timedelta(days=1)
        with mock.patch.object(daily.os, "fsync", side_effect=roll_over):
            with self.assertRaisesRegex(daily.SnapshotDeferredError, "historical_stage_write_forbidden"):
                daily.seal_akshare_market_stage(sample_akshare_market(DAY), NOW.isoformat(),
                    NOW.isoformat(), str(self.stage_dir), expected_day=DAY)
        self.assertEqual(list(self.stage_dir.iterdir()), [])

    def test_rollover_before_assembly_cannot_reuse_previous_day_stage(self):
        daily.seal_akshare_market_stage(sample_akshare_market(DAY), NOW.isoformat(),
            NOW.isoformat(), str(self.stage_dir), expected_day=DAY)
        before = (self.stage_dir / (DAY + ".json")).read_bytes()
        FixedDatetime.current = NOW + dt.timedelta(days=1)
        with mock.patch.object(daily, "qlib_market") as qlib:
            with self.assertRaisesRegex(daily.SnapshotDeferredError, "rolled_over"):
                daily.run_snapshot("rollover", time.monotonic(), expected_day=DAY)
        qlib.assert_not_called()
        with self.assertRaisesRegex(daily.SnapshotValidationError, "historical_snapshot_write_forbidden"):
            daily.store_snapshot({"date": DAY}, str(self.db), expected_day=DAY)
        self.assertFalse(self.db.exists())
        self.assertEqual((self.stage_dir / (DAY + ".json")).read_bytes(), before)

    def test_rollover_during_sqlite_transaction_rolls_back_and_keeps_history(self):
        real_connect = sqlite3.connect
        with real_connect(self.db) as conn:
            daily._ensure_snapshot_table(conn)
            conn.execute("INSERT INTO snapshot VALUES(?,?,?)", ("2026-09-23", "old", "historical bytes"))
        class RolloverConnection(sqlite3.Connection):
            checks = 0
            def execute(self, sql, *args, **kwargs):
                result = super().execute(sql, *args, **kwargs)
                if sql == "PRAGMA quick_check":
                    self.checks += 1
                    if self.checks == 2:
                        FixedDatetime.current = NOW + dt.timedelta(days=1)
                return result
        with mock.patch.object(daily.sqlite3, "connect", side_effect=lambda *a, **kw:
                real_connect(*a, **kw, factory=RolloverConnection)):
            with self.assertRaisesRegex(daily.SnapshotValidationError, "historical_snapshot_write_forbidden"):
                daily.store_snapshot({"date": DAY, "generatedAt": NOW.isoformat()},
                                     str(self.db), expected_day=DAY)
        with real_connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT date, payload FROM snapshot").fetchall(),
                             [("2026-09-23", "historical bytes")])

    def test_retry_stage_missing_or_replaced_cannot_collect_or_use_new_seal(self):
        for replace in (False, True):
            stage = daily.seal_akshare_market_stage(sample_akshare_market(DAY), NOW.isoformat(),
                NOW.isoformat(), str(self.stage_dir), expected_day=DAY)
            (self.stage_dir / (DAY + ".json")).unlink()
            if replace:
                daily.seal_akshare_market_stage(sample_akshare_market(DAY, "changed"), NOW.isoformat(),
                    NOW.isoformat(), str(self.stage_dir), expected_day=DAY)
            identity = {"RESEARCH_DATA_RETRY_STAGE_" + suffix: stage[field]
                        for field, suffix in (("tradeDate", "TRADE_DATE"),
                                              ("payloadSha256", "PAYLOAD_SHA256"),
                                              ("recordSha256", "RECORD_SHA256"))}
            with self.subTest(replace=replace), mock.patch.dict(os.environ, identity), \
                    mock.patch.object(daily, "akshare_market") as market, \
                    mock.patch.object(daily, "qlib_market") as qlib:
                with self.assertRaisesRegex(daily.SnapshotDeferredError, "same_day_retry_stage_missing_or_changed"):
                    daily.run_snapshot("retry", time.monotonic(), expected_day=DAY)
            market.assert_not_called()
            qlib.assert_not_called()
            self.assertFalse(self.db.exists())


class SameDayRetryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.root / "state"
        self.stages = self.root / "stage"
        FixedDatetime.current = NOW
        self.elapsed = 0.0
        self.stack.enter_context(mock.patch.object(supervisor.dt, "datetime", FixedDatetime))
        self.stack.enter_context(mock.patch.object(supervisor.time, "time", side_effect=lambda: FixedDatetime.current.timestamp()))
        self.stack.enter_context(mock.patch.object(supervisor.time, "monotonic", side_effect=lambda: self.elapsed))
        self.stage = daily.seal_akshare_market_stage(sample_akshare_market(DAY), NOW.isoformat(),
            NOW.isoformat(), str(self.stages), expected_day=DAY)

    def sleep(self, seconds):
        self.elapsed += seconds
        FixedDatetime.current += dt.timedelta(seconds=seconds)

    def deferred(self, record):
        return ({**record, "status": "deferred", "target": DAY,
                 "reason": "qlib_not_current_for_expected_trading_day",
                 "akshareStage": {key: self.stage[key] for key in
                                  ("tradeDate", "payloadSha256", "recordSha256")}}, 69)

    def run_supervisor(self):
        return supervisor.run_supervised(state_dir=str(self.state),
            db_path=str(self.root / "snapshot.db"), stage_dir=str(self.stages),
            log_path=str(self.root / "snapshot.log"))

    def test_retry_keeps_one_lock_identity_and_original_deadline(self):
        observed = []
        def attempt(**kwargs):
            observed.append(kwargs)
            if len(observed) == 1:
                return self.deferred(kwargs["record"])
            self.assertEqual(kwargs["environment"]["RESEARCH_DATA_RETRY_STAGE_RECORD_SHA256"],
                             self.stage["recordSha256"])
            return {**kwargs["record"], "status": "inserted", "target": DAY}, 0
        def wait_locked(seconds):
            # Another entry cannot replace last-run or enter source work
            # while the first invocation waits for Qlib readiness.
            before = (self.state / "last-run.json").read_bytes()
            busy, code = self.run_supervisor()
            self.assertEqual((busy["reasonCode"], code), ("snapshot_lock_busy", 75))
            self.assertEqual((self.state / "last-run.json").read_bytes(), before)
            self.sleep(seconds)
        with mock.patch.object(supervisor, "_run_attempt", side_effect=attempt), \
                mock.patch.object(supervisor.time, "sleep", side_effect=wait_locked):
            final, code = self.run_supervisor()
        self.assertEqual((final["status"], code, final["attemptCount"]), ("inserted", 0, 2))
        for field in ("hard", "monotonic_stop", "lock_fd"):
            self.assertEqual(observed[0][field], observed[1][field])
        self.assertEqual(observed[0]["record"]["runId"], observed[1]["record"]["runId"])
        self.assertEqual(observed[0]["environment"]["RESEARCH_DATA_SOFT_DEADLINE_EPOCH"],
                         str(REAL_DATETIME.fromisoformat(DAY + "T22:40:30+08:00").timestamp()))
        self.assertEqual(final["attempts"][0]["status"], "deferred")
        self.assertEqual(json.loads((self.state / "last-run.json").read_text())["status"], "inserted")

    def test_last_start_cutoff_and_rollover_block_retry_after_wait(self):
        for start, jump in (("2026-09-24T22:39:45+08:00", False),
                            ("2026-09-24T22:30:00+08:00", True)):
            FixedDatetime.current = REAL_DATETIME.fromisoformat(start)
            def sleep(seconds):
                self.sleep(seconds)
                if jump:
                    FixedDatetime.current += dt.timedelta(days=1)
            with self.subTest(start=start), \
                    mock.patch.object(supervisor, "_run_attempt", side_effect=lambda **kw: self.deferred(kw["record"])) as attempt, \
                    mock.patch.object(supervisor.time, "sleep", side_effect=sleep):
                final, code = self.run_supervisor()
            self.assertEqual((final["status"], code, attempt.call_count), ("deferred", 69, 1))

    def test_retry_count_and_global_budget_are_bounded(self):
        with mock.patch.object(supervisor, "QLIB_MAX_ATTEMPTS", 3), \
                mock.patch.object(supervisor, "_run_attempt", side_effect=lambda **kw: self.deferred(kw["record"])) as attempt, \
                mock.patch.object(supervisor.time, "sleep", side_effect=self.sleep):
            final, code = self.run_supervisor()
        self.assertEqual((code, final["attemptCount"], attempt.call_count), (69, 3, 3))
        self.assertEqual(self.elapsed, 60)

    def test_missing_or_changed_stage_generic_deferred_and_failure_never_retry(self):
        for reason in ("source unavailable", "qlib_not_current_for_expected_trading_day"):
            def attempt(**kw):
                final, code = self.deferred(kw["record"])
                final["reason"] = reason
                final["akshareStage"]["recordSha256"] = "0" * 64
                return final, code
            with self.subTest(reason=reason), mock.patch.object(supervisor, "_run_attempt", side_effect=attempt) as worker, \
                    mock.patch.object(supervisor.time, "sleep") as sleep:
                final, code = self.run_supervisor()
            self.assertEqual((code, worker.call_count), (69, 1))
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
