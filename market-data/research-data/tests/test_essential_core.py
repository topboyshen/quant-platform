"""Offline contract tests: real validation/storage; only Qlib input and clocks fixed."""
import copy
import datetime as dt
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import core_market as core
import core_storage
import snapshot_runtime as runtime
import snapshot_contract as policy
import snapshot_supervisor as supervisor
import contract_transition as transition
import review

DAY = "2026-10-08"
REAL_DATETIME = dt.datetime


def calendar(day=DAY, closed=False):
    target = dt.date.fromisoformat(day)
    value = {"document": {"schemaVersion": 1, "targetDate": day, "status": "closed" if closed else "open",
        "previousTradingDate": (target - dt.timedelta(days=1)).isoformat(),
        "nextTradingDate": (target + dt.timedelta(days=1)).isoformat(),
        "sourceIds": ["akshare:sina:trade-calendar"], "observedAt": day + "T07:50:00+08:00",
        "validUntil": day + "T23:59:59+08:00"},
        "binding": {"archiveDate": day, "inputSha256": "a" * 64,
                    "cutoffInstant": day + "T08:00:00+08:00", "publishedStatus": "sealed"}}
    if closed:
        value["binding"] = {"kind": "validated_non_trading_skip", "targetDate": day,
            "contextSha256": "a" * 64, "lastRunSha256": "b" * 64, "stateSha256": "c" * 64,
            "completedAt": day + "T08:00:01+08:00"}
    value["sha256"] = core.digest(value)
    return value


def qlib(day=DAY):
    return {"date": day, "generation": {"tag": "fixture-generation", "inode": 1},
        "coverage": {"valid": 5000, "eligible": 5000, "invalid": 0, "ratio": 1.0},
        "breadth": {"up": 3000, "down": 2000, "flat": 0, "total": 5000}}


def record(day=DAY):
    return core.build_core(qlib(day), run_id="a" * 32, generated_at=day + "T22:30:00+08:00",
                           calendar_evidence=calendar(day))


def interval_policy():
    return {"schema": policy.POLICY_V2, "transitions": [
        {"activeContract": policy.CORE, "effectiveTradeDate": DAY},
        {"activeContract": policy.LEGACY, "effectiveTradeDate": "2026-10-09"}]}


class FixedDatetime(REAL_DATETIME):
    current = REAL_DATETIME.fromisoformat(DAY + "T22:30:00+08:00")
    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class EssentialCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = self.root / "core.db"

    def test_core_import_has_no_supplier_or_proxy_side_effect(self):
        with mock.patch.dict(sys.modules, {"daily_snapshot": None, "requests": None, "bs4": None}), \
                mock.patch.dict(os.environ, {"HTTPS_PROXY": "fixture-no-network"}):
            importlib.reload(core)
            importlib.reload(supervisor)
            self.assertEqual(os.environ["HTTPS_PROXY"], "fixture-no-network")
            self.assertIsNone(sys.modules["daily_snapshot"])

    def test_stale_then_current_preserves_core_contract_and_never_needs_stage(self):
        with mock.patch.object(core, "qlib_market", side_effect=[qlib("2026-09-30"), qlib()]):
            with self.assertRaisesRegex(runtime.SnapshotDeferredError, "core_qlib_not_current") as caught:
                core.run_core(db_path=self.db, now=DAY + "T22:30:00+08:00", calendar_evidence=calendar(), effective_trade_date=DAY)
            self.assertEqual(caught.exception.target, DAY)
            self.assertFalse(self.db.exists())
            with mock.patch.dict(os.environ, {"RESEARCH_DATA_RETRY_CORE_DAY": DAY,
                    "RESEARCH_DATA_RETRY_CORE_CALENDAR_SHA256": calendar()["sha256"]}):
                result = core.run_core(db_path=self.db, now=DAY + "T22:31:00+08:00", calendar_evidence=calendar())
        self.assertEqual(result["status"], "inserted")
        value = core.read_core(DAY, self.db)
        self.assertFalse(value["fullSnapshot"])
        self.assertEqual(value["requiredSources"], ["qlib"])
        self.assertEqual(value["appendix"]["status"], "not_requested")
        self.assertEqual(result["payloadSha256"], core.digest(value))

    def test_retry_changed_calendar_or_day_rejects_before_qlib(self):
        for day, sha in (("2026-10-07", calendar()["sha256"]), (DAY, "b" * 64)):
            with mock.patch.dict(os.environ, {"RESEARCH_DATA_RETRY_CORE_DAY": day,
                    "RESEARCH_DATA_RETRY_CORE_CALENDAR_SHA256": sha}), mock.patch.object(core, "qlib_market") as scan:
                with self.assertRaisesRegex(runtime.SnapshotDeferredError, "calendar_missing_or_changed"):
                    core.run_core(db_path=self.db, now=DAY + "T22:31:00+08:00", calendar_evidence=calendar())
                scan.assert_not_called()

    def test_holiday_does_not_scan_or_write(self):
        with mock.patch.object(core, "qlib_market") as scan:
            result = core.run_core(db_path=self.db, now=DAY + "T22:30:00+08:00", calendar_evidence=calendar(closed=True))
        self.assertEqual(result["status"], "skipped_non_trading")
        scan.assert_not_called(); self.assertFalse(self.db.exists())

    def test_retry_open_calendar_replaced_by_valid_closed_calendar_rejects(self):
        with mock.patch.dict(os.environ, {"RESEARCH_DATA_RETRY_CORE_DAY": DAY,
                "RESEARCH_DATA_RETRY_CORE_CALENDAR_SHA256": calendar()["sha256"]}), \
                mock.patch.object(core, "qlib_market") as scan:
            with self.assertRaisesRegex(runtime.SnapshotDeferredError, "calendar_missing_or_changed"):
                core.run_core(db_path=self.db, now=DAY + "T22:31:00+08:00", calendar_evidence=calendar(closed=True))
        scan.assert_not_called(); self.assertFalse(self.db.exists())

    def test_state_directory_override_preserves_full_stage_isolation(self):
        environment = {k: v for k, v in os.environ.items() if k not in {
            "RESEARCH_DATA_FULL_STATE_DIR", "RESEARCH_DATA_AKSHARE_STAGE_DIR"}}
        environment["RESEARCH_DATA_STATE_DIR"] = str(self.root / "scratch-state")
        spec = importlib.util.spec_from_file_location("isolated_runtime_fixture", runtime.__file__)
        isolated = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, environment, clear=True):
            spec.loader.exec_module(isolated)
            self.assertEqual(isolated.FULL_STATE_DIR, environment["RESEARCH_DATA_STATE_DIR"])
            def attempt(**kw):
                self.assertEqual(kw["stage_dir"], str(self.root / "scratch-state/akshare-market"))
                self.assertEqual(kw["environment"]["RESEARCH_DATA_FULL_STATE_DIR"], isolated.STATE_DIR)
                return {**kw["record"], "status": "skipped_existing"}, 0
            with mock.patch.object(supervisor, "snapshot", isolated), \
                    mock.patch.object(supervisor, "_run_attempt", side_effect=attempt):
                result, code = supervisor.run_supervised(db_path=str(self.db),
                    now=REAL_DATETIME.fromisoformat(DAY + "T22:30:00+08:00"))
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "scratch-state/akshare-market").exists())

    def test_rollover_at_commit_rolls_back_without_deleting_history(self):
        core.store_core(record(), self.db)
        before = self.db.read_bytes()
        tomorrow = record("2026-10-09")
        with mock.patch.object(core_storage, "_write_gate", side_effect=[None, None, ValueError("historical_core_write_forbidden")]):
            with self.assertRaisesRegex(ValueError, "historical_core_write_forbidden"):
                core.store_core(tomorrow, self.db)
        self.assertEqual(before, self.db.read_bytes())
        self.assertIsNone(core.read_core("2026-10-09", self.db))

    def test_reconcile_core_is_readonly_hash_bound_and_no_full_import(self):
        core.store_core(record(), self.db); before = self.db.read_bytes()
        with mock.patch.dict(sys.modules, {"daily_snapshot": None}):
            result = supervisor._reconcile_read_only({"schema": core.SCHEMA, "target": DAY,
                "snapshotCandidateSha256": core.digest(record())}, self.db, self.root / "nonexistent")
        self.assertEqual(result, {"stage": "not_required", "snapshot": "matching_candidate_committed"})
        self.assertEqual(before, self.db.read_bytes())

    def test_supervisor_retry_preserves_lock_run_and_caps_without_stage(self):
        FixedDatetime.current = REAL_DATETIME.fromisoformat(DAY + "T22:30:00+08:00")
        elapsed, observed = [0.], []
        def attempt(**kw):
            observed.append({**kw, "environment": kw["environment"].copy()})
            if len(observed) == 1:
                return {**kw["record"], "status": "deferred", "target": DAY,
                    "reason": "core_qlib_not_current_for_expected_trading_day", "calendarEvidence": calendar()}, 69
            return {**kw["record"], "status": "inserted", "target": DAY}, 0
        def wait(seconds):
            with open(self.root / "state/snapshot.lock", "a+") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elapsed[0] += seconds; FixedDatetime.current += dt.timedelta(seconds=seconds)
        with mock.patch.object(supervisor.dt, "datetime", FixedDatetime), \
                mock.patch.object(supervisor.time, "time", side_effect=lambda: FixedDatetime.current.timestamp()), \
                mock.patch.object(supervisor.time, "monotonic", side_effect=lambda: elapsed[0]), \
                mock.patch.object(supervisor.time, "sleep", side_effect=wait), \
                mock.patch.object(supervisor, "_run_attempt", side_effect=attempt), \
                mock.patch.object(supervisor, "_retry_stage_matches", side_effect=AssertionError("stage forbidden")):
            result, code = supervisor.run_supervised(state_dir=str(self.root / "state"), db_path=str(self.db),
                log_path=str(self.root / "log"), contract=core.SCHEMA)
        self.assertEqual((code, result["attemptCount"]), (0, 2))
        self.assertEqual(observed[0]["record"]["runId"], observed[1]["record"]["runId"])
        for key in ("hard", "monotonic_stop", "lock_fd"):
            self.assertEqual(observed[0][key], observed[1][key])
        self.assertEqual(observed[1]["environment"]["RESEARCH_DATA_RETRY_CORE_CALENDAR_SHA256"], calendar()["sha256"])
        self.assertFalse(any(k.startswith("RESEARCH_DATA_RETRY_STAGE_") for k in observed[1]["environment"]))

    def test_core_retry_requires_open_same_day_identity_and_original_budget(self):
        now = REAL_DATETIME.fromisoformat(DAY + "T22:30:00+08:00")
        final = {"schema": core.SCHEMA, "fullSnapshot": False, "status": "deferred", "target": DAY,
            "reason": "core_qlib_not_current_for_expected_trading_day", "calendarEvidence": calendar()}
        with mock.patch.object(supervisor.dt, "datetime", FixedDatetime), mock.patch.object(supervisor.time, "monotonic", return_value=0):
            for current, attempt, stop in ((now, 20, 600), (now, 1, 0),
                    (now.replace(hour=22, minute=40), 1, 600), (now + dt.timedelta(days=1), 1, 600)):
                FixedDatetime.current = current
                self.assertFalse(supervisor._qlib_retry_allowed(final, 69, started=now, hard=now + dt.timedelta(minutes=11),
                    monotonic_stop=stop, attempt=attempt, contract=core.SCHEMA))


class TerminalCalendarV2Tests(unittest.TestCase):
    """Producer-shaped local files; exercise reader, all hashes and core validator."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = REAL_DATETIME.fromisoformat(DAY + "T22:30:00+08:00")

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def fixture(self, status="failed"):
        source = "akshare:sina:trade-calendar"
        document = {"targetDate": DAY, "status": "trading_day", "previousTradingDate": "2026-09-30",
            "nextTradingDate": "2026-10-09", "sourceIds": [source], "dataAsOf": DAY + "T08:00:00+08:00"}
        context = {"schemaVersion": "1.0", "meta": {"targetDate": DAY, "timezone": "Asia/Shanghai",
            "asOf": DAY + "T08:00:40+08:00"}, "data": {"tradingCalendar": document},
            "sources": [{"id": source, "dataAsOf": document["dataAsOf"],
                "url": "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"}]}
        path = self.write("work/morning-context.json", context)
        last = {"schemaVersion": "1.0", "targetDate": DAY, "status": status, "phase": "publish",
            "startedAt": DAY + "T00:00:31.000Z", "frozenCutoffInstant": DAY + "T00:00:00.000Z",
            "contextFile": "work/morning-context.json", "contextSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "publicationGate": "passed" if status == "success" else "blocked"}
        last["failedAt" if status == "failed" else "completedAt"] = DAY + "T00:00:44.000Z"
        state = {"schemaVersion": "2.0", "task": "morning-report", "targetDate": DAY,
            "invocationId": "terminal-v2-fixed", "status": status, "frozenCutoffInstant": last["frozenCutoffInstant"],
            "startedAt": DAY + "T00:00:30.000Z", "completedAt": DAY + "T00:00:45.000Z",
            "childExitCode": {"failed": 1, "blocked": 2, "success": 0}[status], "timedOut": False, "interrupted": False}
        self.save(state, last)
        return state, last

    def save(self, state, last):
        self.write("work/orchestrator/states/morning-report-" + DAY + ".json", state)
        self.write("work/last-run.json", last)

    def test_delayed_v2_failed_blocked_and_success_are_hash_bound_without_publication(self):
        for status in ("failed", "blocked", "success"):
            with self.subTest(status=status):
                self.fixture(status)
                evidence = core.load_calendar_evidence(self.root, self.now)
                self.assertEqual(evidence["binding"]["stateSchemaVersion"], "2.0")
                for field, name in (("contextSha256", "work/morning-context.json"),
                        ("lastRunSha256", "work/last-run.json"),
                        ("stateSha256", "work/orchestrator/states/morning-report-" + DAY + ".json")):
                    self.assertEqual(evidence["binding"][field], hashlib.sha256((self.root / name).read_bytes()).hexdigest())
                with mock.patch.object(core, "qlib_market", return_value=qlib()):
                    result = core.run_core(db_path=self.root / (status + ".db"), now=self.now.isoformat(), calendar_evidence=evidence)
                stored = core.read_core(DAY, self.root / (status + ".db"))
                self.assertEqual(result["payloadSha256"], core.digest(stored))
                self.assertFalse((self.root / "work/report-archives").exists())

    def test_v2_missing_noncanonical_mismatched_or_future_cutoff_rejected(self):
        changes = [("state", lambda v: v.pop("frozenCutoffInstant")),
            ("last", lambda v: v.pop("frozenCutoffInstant")),
            ("state", lambda v: v.update(frozenCutoffInstant=DAY + "T08:00:00+08:00")),
            ("last", lambda v: v.update(frozenCutoffInstant=DAY + "T00:00:30.000Z")),
            ("state", lambda v: v.update(startedAt=DAY + "T07:59:59+08:00")),
            ("state", lambda v: v.update(schemaVersion="3.0")),
            ("last", lambda v: v.update(schemaVersion="2.0"))]
        for part, change in changes:
            state, last = self.fixture(); change(state if part == "state" else last); self.save(state, last)
            with self.subTest(part=part), self.assertRaises(runtime.SnapshotDeferredError):
                core.load_calendar_evidence(self.root, self.now)

    def test_legacy_v1_keeps_exact_start_cutoff_and_rejects_version_downgrade(self):
        state, last = self.fixture()
        state.update(schemaVersion="1.0"); state.pop("frozenCutoffInstant")
        self.save(state, last)
        with self.assertRaises(runtime.SnapshotDeferredError): core.load_calendar_evidence(self.root, self.now)
        state["startedAt"] = last["frozenCutoffInstant"]
        self.save(state, last)
        evidence = core.load_calendar_evidence(self.root, self.now)
        self.assertNotIn("stateSchemaVersion", evidence["binding"])
        state["frozenCutoffInstant"] = last["frozenCutoffInstant"]
        self.save(state, last)
        with self.assertRaises(runtime.SnapshotDeferredError): core.load_calendar_evidence(self.root, self.now)

    def test_blocked_terminal_tuple_cannot_claim_failure_or_wrong_exit(self):
        for change in ({"status": "success"}, {"childExitCode": 1}, {"childExitCode": 0}):
            state, last = self.fixture("blocked"); state.update(change); self.save(state, last)
            with self.assertRaises(runtime.SnapshotDeferredError): core.load_calendar_evidence(self.root, self.now)
        state, last = self.fixture("failed"); state.update(status="blocked", childExitCode=2); self.save(state, last)
        with self.assertRaises(runtime.SnapshotDeferredError): core.load_calendar_evidence(self.root, self.now)

    def test_v2_persisted_binding_rejects_rehashed_downgrade_or_partial_identity(self):
        self.fixture(); original = core.load_calendar_evidence(self.root, self.now)
        for keys in (("stateSchemaVersion",), ("stateFrozenCutoffInstant",),
                     ("stateSchemaVersion", "stateFrozenCutoffInstant")):
            modified = copy.deepcopy(original)
            for key in keys: modified["binding"].pop(key)
            modified["sha256"] = core.digest({k: v for k, v in modified.items() if k != "sha256"})
            with self.assertRaises(ValueError): core.validate_calendar_evidence(modified, self.now)
        original["binding"]["stateFrozenCutoffInstant"] = DAY + "T00:00:30.000Z"
        with self.assertRaises(ValueError): core.validate_calendar_evidence(original, self.now)


class IntervalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_full_core_full_assignment_and_legacy_compatibility(self):
        p = policy.parse_policy(json.dumps(interval_policy()))
        for day, expected in (("2026-10-07", policy.LEGACY), (DAY, policy.CORE), ("2026-10-09", policy.LEGACY)):
            self.assertEqual(policy.contract_for_day(p, day), expected)
        legacy = {"activeContract": policy.CORE, "effectiveTradeDate": DAY}
        self.assertEqual(policy.contract_for_day(legacy, DAY), policy.CORE)
        self.assertEqual(policy.first_core_date(p), DAY)

    def test_repeated_reversed_noncanonical_or_unknown_transitions_rejected(self):
        p = interval_policy()
        for items in ([p["transitions"][0]] * 2, p["transitions"][::-1],
                      [{"activeContract": policy.CORE, "effectiveTradeDate": "2026-1-8"}],
                      [{"activeContract": policy.LEGACY, "effectiveTradeDate": DAY}]):
            with self.assertRaises(ValueError): policy.parse_policy(json.dumps({"schema": policy.POLICY_V2, "transitions": items}))

    def test_append_only_future_cas_forward_rollback_preserves_databases(self):
        full, cdb, path = self.root / "snapshot.db", self.root / "core.db", self.root / "policy.json"
        with sqlite3.connect(full) as conn:
            conn.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY,payload TEXT)")
            conn.execute("INSERT INTO snapshot VALUES ('2026-09-30','{}')")
        before = full.read_bytes()
        first = transition.transition(path, expected_sha256="missing", active_contract=policy.CORE, effective_day=DAY,
            full_db=full, core_db=cdb, state_root=self.root / "state", now=REAL_DATETIME(2026,10,7,12,tzinfo=policy.CST))
        core.store_core(record(), cdb); core_bytes = cdb.read_bytes()
        second = transition.transition(path, expected_sha256=first["policySha256"], active_contract=policy.LEGACY,
            effective_day="2026-10-09", full_db=full, core_db=cdb, state_root=self.root / "state",
            now=REAL_DATETIME(2026,10,8,23,tzinfo=policy.CST))
        self.assertEqual(second["policy"], interval_policy())
        self.assertEqual(before, full.read_bytes()); self.assertEqual(core_bytes, cdb.read_bytes())
        with self.assertRaisesRegex(ValueError, "compare_and_set"):
            transition.transition(path, expected_sha256=first["policySha256"], active_contract=policy.CORE,
                effective_day="2026-10-10", full_db=full, core_db=cdb, state_root=self.root / "state")
        selected, value = review.read_review(DAY, db_path=full, core_db=cdb, policy_path=path)
        self.assertEqual(selected, policy.CORE); self.assertEqual(value, record())

    def test_historical_reassignment_cannot_turn_gaps_green(self):
        for day in ("2026-10-01", "2026-10-07"):
            with self.assertRaisesRegex(ValueError, "must_be_future"):
                transition.append_transition({"activeContract": policy.LEGACY, "effectiveTradeDate": None},
                    active_contract=policy.CORE, effective_day=day, now=REAL_DATETIME(2026,10,7,tzinfo=policy.CST),
                    full_rows={}, core_rows={})
        with self.assertRaisesRegex(ValueError, "existing_row_contract_conflict"):
            transition.append_transition({"activeContract": policy.LEGACY, "effectiveTradeDate": None},
                active_contract=policy.CORE, effective_day=DAY, now=REAL_DATETIME(2026,10,7,tzinfo=policy.CST),
                full_rows={}, core_rows={"2026-10-06": "fixture"})

    def test_active_core_missing_does_not_fall_back_to_full(self):
        p = self.root / "policy.json"; p.write_text(json.dumps(interval_policy()))
        selected, value = review.read_review(db_path=self.root / "full", core_db=self.root / "core", policy_path=p,
            now=REAL_DATETIME(2026,10,8,23,tzinfo=policy.CST))
        self.assertEqual(selected, policy.CORE); self.assertIsNone(value)

    def test_lost_or_legacy_policy_cannot_hide_existing_core_rows(self):
        cdb, p = self.root / "core.db", self.root / "policy.json"
        core.store_core(record(), cdb)
        for value in (None, {"activeContract": policy.LEGACY, "effectiveTradeDate": None}):
            if value is None:
                if p.exists(): p.unlink()
            else:
                p.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "core_history_policy"):
                policy.assert_core_history(policy.read_policy(p), cdb)
            with self.assertRaises(ValueError):
                review.read_review(db_path=self.root / "full", core_db=cdb, policy_path=p)
        p.write_text(json.dumps(interval_policy()))
        policy.assert_core_history(policy.read_policy(p), cdb)


class IntervalAuditTests(unittest.TestCase):
    def test_forward_full_rollback_retains_core_hash_and_separate_missing_days(self):
        import test_data_health
        f = test_data_health.AuditTests(methodName="runTest"); f.setUp(); self.addCleanup(f.doCleanups)
        days = ["2026-09-30", DAY, "2026-10-09"]
        (f.qlib / "calendars/day.txt").write_text("\n".join(days) + "\n")
        (f.qlib.parent / ".tag").write_text("2026-10-10\n")
        (f.qlib / ".qlib-release").write_text("2026-10-10\n")
        for symbol in test_data_health.health.REFERENCES: f.features(symbol, len(days))
        f.sql("DELETE FROM snapshot")
        for day in (days[0], days[2]):
            f.sql("INSERT INTO snapshot VALUES (?,?)", (day, json.dumps({"date":day,"qlib":{"date":day},
                "akshare":{"activity":{"统计日期": day + " 15:00:00"}}})))
        f.stage(days[2]); f.now = REAL_DATETIME(2026,10,9,23,tzinfo=policy.CST)
        p, cdb = f.root / "policy", f.root / "core.db"
        p.write_text(json.dumps(interval_policy())); core.store_core(record(), cdb)
        current_hash = test_data_health.health.snapshot_rows(f.db)[days[2]]["payloadSha256"]
        f.last.write_text(json.dumps({"runId": "b" * 32, "status": "inserted", "target": days[2],
            "startedAt": days[2] + "T22:30:00+08:00", "timestamp": days[2] + "T22:31:00+08:00",
            "payloadSha256": current_hash}))
        self.enterContext(mock.patch.object(test_data_health.health._calendar_evidence, "load_calendar_evidence",
                                          return_value=calendar(days[2])))
        opts = dict(contract_policy=p, core_db=cdb, since=days[0], monitor_since="2026-10-07", calendar_project=f.root)
        result = f.run_audit(**opts)
        self.assertEqual(result["activeContract"], policy.LEGACY)
        self.assertEqual(result["activeRunDecision"]["verification"]["status"], "pass")
        self.assertEqual(result["coreMarket"]["rowHashes"][DAY], core.digest(record()))
        self.assertEqual([r["tradeDate"] for r in result["coreMarket"]["ledger"]], [DAY])
        self.assertEqual([(r["tradeDate"], r["expectedUnderContract"]) for r in result["ledger"]],
                         [(days[0], True), (DAY, False), (days[2], True)])
        self.assertNotIn("legacy_full_rows_after_cutover", {i["code"] for i in result["issues"]})
        self.assertIsNone(result["legacyFullSnapshot"]["scheduledBefore"])
        p.unlink()
        missing_policy = f.run_audit(**opts)
        self.assertEqual(next(i["severity"] for i in missing_policy["issues"]
            if i["code"] == "core_history_policy_missing_or_conflicting"), "critical")
        self.assertNotEqual(missing_policy["activeRunDecision"]["verification"]["status"], "pass")
        self.assertEqual(missing_policy["coreMarket"]["rowHashes"][DAY], core.digest(record()))
        p.write_text(json.dumps(interval_policy()))
        f.sql("DELETE FROM snapshot WHERE date='2026-10-09'")
        result = f.run_audit(**opts)
        self.assertEqual(next(i["dates"] for i in result["issues"] if i["code"] == "unresolved_trading_day_gaps"), [days[2]])
        with sqlite3.connect(cdb) as conn: conn.execute("UPDATE core_market SET payload_sha256=?", ("0"*64,))
        result = f.run_audit(**opts)
        self.assertEqual(next(i["dates"] for i in result["issues"] if i["code"] == "unresolved_core_market_gaps"), [DAY])
