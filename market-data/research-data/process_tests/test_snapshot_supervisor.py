"""Synthetic hard-deadline, same-run identity, and read-only reconciliation."""
import fcntl
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import snapshot_supervisor as supervisor
import daily_snapshot as snapshot
import data_health


WORKER = str(Path(__file__).parents[1] / "tests" / "fixtures" / "snapshot_worker_stub.py")
WATCHDOG = str(Path(__file__).parents[1] / "tests" / "fixtures" / "snapshot_watchdog_stub.py")
DRIVER = str(Path(__file__).parents[1] / "tests" / "fixtures" / "snapshot_supervisor_driver_stub.py")


def linux_process_live(pid):
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return False
    try:
        return "State:\tZ" not in status.read_text(encoding="ascii")
    except FileNotFoundError:
        return False


def linux_process_starttime(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
        return fields[19]  # field 22: kernel starttime, after state field 3
    except FileNotFoundError:
        return None


def kill_same_linux_process(pid, starttime):
    # Fault-injection cleanup must not signal a numerically reused PID/PGID.
    if starttime is not None and linux_process_starttime(pid) == starttime:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def sample_market(trade_date):
    return {
        "sectors": [{"name": f"sector-{index}", "chg": 1.0,
                     "netInflow": 1.0, "upCount": 20,
                     "downCount": 10, "leader": "sample"}
                    for index in range(60)],
        "activity": {"上涨": 3000, "下跌": 2000, "平盘": 100,
                     "涨停": 50, "跌停": 5,
                     "统计日期": trade_date + " 15:00:00"},
    }


class SnapshotSupervisorTests(unittest.TestCase):
    def execute(self, directory, mode, *, deadline=2):
        root = Path(directory)
        with mock.patch.dict(os.environ, {"SNAPSHOT_WORKER_TEST_MODE": mode}):
            return supervisor.run_supervised(
                worker_path=WORKER, state_dir=str(root / "state"),
                watchdog_path=WATCHDOG,
                db_path=str(root / "snapshot.db"),
                stage_dir=str(root / "stage"), log_path=str(root / "snapshot.log"),
                deadline_seconds_override=deadline,
            )

    def test_success_uses_this_run_not_stale_shared_record(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            (state / "last-run.json").write_text(
                '{"runId":"old","status":"failed","target":"2026-09-22"}\n',
                encoding="utf-8",
            )
            with mock.patch("snapshot_supervisor.os.killpg", wraps=os.killpg) as signal_group:
                result, code = self.execute(directory, "success")
            signal_group.assert_not_called()
            current = json.loads((state / "last-run.json").read_text())
            self.assertEqual((state / "watchdog.disarmed").read_text(), "clean\n")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(result["runId"], current["runId"])
        self.assertNotEqual(result["runId"], "old")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PGID-pinning check")
    def test_worker_reaped_before_disarm_still_has_live_group_leader(self):
        original_disarm = supervisor._disarm_watchdog
        observed = []

        def delayed_disarm(watchdog, control_write, ack_read):
            # The worker has already been reaped by process.wait(). The
            # watchdog, not the worker PID, must still pin the group number.
            observed.append((watchdog.pid, os.getpgid(watchdog.pid)))
            time.sleep(0.2)
            return original_disarm(watchdog, control_write, ack_read)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            supervisor, "_disarm_watchdog", side_effect=delayed_disarm
        ):
            result, code = self.execute(directory, "success")
        self.assertEqual((result["status"], code), ("inserted", 0))
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], observed[0][1])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PGID-pinning check")
    def test_deadline_group_signals_while_watchdog_pid_is_unreaped(self):
        real_killpg = os.killpg
        observed = []

        def identity_checked_killpg(pgid, sig):
            # Even after worker.wait() and TERM, the group leader's /proc
            # identity must still exist when this numeric PGID is signalled.
            leader = Path(f"/proc/{pgid}/stat")
            self.assertTrue(leader.exists(), "group leader reaped before killpg")
            fields = leader.read_text(encoding="ascii").rsplit(") ", 1)[1].split()
            self.assertEqual(int(fields[2]), pgid)
            observed.append((pgid, sig, fields[0]))
            return real_killpg(pgid, sig)

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "snapshot_supervisor.os.killpg", side_effect=identity_checked_killpg
        ):
            result, code = self.execute(directory, "hang", deadline=0.3)
            watchdog_pid = int((Path(directory) / "state" / "watchdog.pid").read_text())
        self.assertEqual((result["status"], code), ("failed", 1))
        self.assertEqual([entry[1] for entry in observed], [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(all(entry[0] == watchdog_pid for entry in observed))

    def test_source_deferred_remains_deferred_with_same_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            result, code = self.execute(directory, "deferred")
            current = json.loads(
                (Path(directory) / "state" / "last-run.json").read_text()
            )
        self.assertEqual(code, 69)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(current["runId"], result["runId"])

    def test_reaped_watchdog_identity_never_signals_old_numeric_group(self):
        worker = mock.Mock()
        watchdog = mock.Mock(pid=12345)
        with mock.patch.object(supervisor, "_watchdog_identity_pinned",
                               return_value=False), mock.patch(
            "snapshot_supervisor.os.killpg"
        ) as signal_group:
            terminated = supervisor._terminate_group(worker, watchdog)
            members = supervisor._live_nonwatchdog_members(watchdog, worker)
        self.assertFalse(terminated)
        self.assertIsNone(members)
        signal_group.assert_not_called()
        worker.kill.assert_called_once_with()

    def test_group_cleanup_observes_delayed_exit_with_one_kill_budget(self):
        for leaves in (True, False):
            with self.subTest(leaves=leaves):
                worker = mock.Mock(pid=54321)
                watchdog = mock.Mock(pid=12345)
                clock = [0.0]
                scans = [0]

                def members(*_):
                    scans[0] += 1
                    return [] if leaves and scans[0] == 3 else [42]

                def sleep(seconds):
                    clock[0] += seconds

                with mock.patch.object(supervisor, "_watchdog_identity_pinned", return_value=True), \
                        mock.patch.object(supervisor, "_live_nonwatchdog_members", side_effect=members), \
                        mock.patch("snapshot_supervisor.os.killpg") as signal_group, \
                        mock.patch("snapshot_supervisor.time.monotonic", side_effect=lambda: clock[0]), \
                        mock.patch("snapshot_supervisor.time.sleep", side_effect=sleep):
                    terminated = supervisor._terminate_group(worker, watchdog)
                self.assertEqual(terminated, leaves)
                self.assertGreater(scans[0], 1)
                self.assertLessEqual(clock[0], 5.0)
                pgid = 12345 if sys.platform.startswith("linux") else 54321
                self.assertEqual(signal_group.call_args_list,
                                 [mock.call(pgid, signal.SIGTERM), mock.call(pgid, signal.SIGKILL)])
                watchdog.wait.assert_not_called()
                watchdog.poll.assert_not_called()

    def test_group_cleanup_does_not_accept_unknown_or_lost_identity(self):
        for pinned, members, expected_signals in (([True, True], None, 2),
                                                 ([True, False], [], 1)):
            with self.subTest(pinned=pinned, members=members):
                worker = mock.Mock()
                watchdog = mock.Mock(pid=12345)
                with mock.patch.object(supervisor, "_watchdog_identity_pinned", side_effect=pinned), \
                        mock.patch.object(supervisor, "_live_nonwatchdog_members", return_value=members), \
                        mock.patch("snapshot_supervisor.os.killpg") as signal_group:
                    terminated = supervisor._terminate_group(worker, watchdog)
                self.assertFalse(terminated)
                self.assertEqual(signal_group.call_count, expected_signals)
                watchdog.wait.assert_not_called()
                watchdog.poll.assert_not_called()

    def test_intentional_timeout_and_unexpected_watchdog_loss_keep_distinct_reasons(self):
        for already_dead, expected in ((False, "deadline_exceeded"), (True, "watchdog_lost")):
            with self.subTest(already_dead=already_dead), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                worker = mock.Mock(returncode=-signal.SIGKILL)
                watchdog = mock.Mock(pid=12345)
                dead = [already_dead]

                def terminate(*_):
                    dead[0] = True
                    return True

                with mock.patch.object(supervisor, "_launch_guarded_worker", return_value=(worker, watchdog, None, None)), \
                        mock.patch.object(supervisor, "_watchdog_exited", side_effect=lambda *_: dead[0]), \
                        mock.patch.object(supervisor, "_terminate_group", side_effect=terminate), \
                        mock.patch.object(supervisor, "_reconcile_bounded", return_value={"stage": "unknown", "snapshot": "unknown"}), \
                        mock.patch("snapshot_supervisor.time.time", return_value=10**12):
                    result, code = supervisor.run_supervised(
                        state_dir=str(root / "state"), db_path=str(root / "unused.db"),
                        stage_dir=str(root / "unused-stage"), log_path=str(root / "worker.log"),
                        now=dt.datetime(2026, 9, 29, 22, 30, tzinfo=supervisor.ZONE),
                        deadline_seconds_override=1,
                    )
                self.assertEqual((result["status"], result["reasonCode"], code), ("failed", expected, 1))
                self.assertFalse((root / "unused.db").exists())

    def test_crash_between_final_journal_and_shared_publish_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            original = snapshot._atomic_run_json

            def interrupted(path, record):
                if (path == str(state / "last-run.json")
                        and record.get("status") == "inserted"):
                    raise OSError("simulated supervisor crash before shared final")
                return original(path, record)

            with mock.patch.object(snapshot, "_atomic_run_json", side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.execute(directory, "success")
            shared = json.loads((state / "last-run.json").read_text())
            final = json.loads(next((state / "runs").glob("*.json")).read_text())
        self.assertEqual(shared["status"], "temporary_failure")
        self.assertEqual(shared["reasonCode"], "supervisor_terminal_not_published")
        self.assertEqual(final["status"], "inserted")
        self.assertEqual(shared["runId"], final["runId"])

    def test_reconciliation_timeout_is_bounded_and_never_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock"
            with open(lock, "a+") as locked, mock.patch.object(
                supervisor, "_reconcile_read_only", side_effect=lambda *_: time.sleep(5)
            ):
                started = time.monotonic()
                result = supervisor._reconcile_bounded(
                    {"target": "2026-09-24"}, str(Path(directory) / "snapshot.db"),
                    str(Path(directory) / "stage"), locked.fileno(), timeout=0.2,
                )
                elapsed = time.monotonic() - started
        self.assertEqual(result["snapshot"], "reconciliation_timed_out")
        self.assertLess(elapsed, 2)

    def test_partial_reconciliation_pipe_frame_cannot_block_recv(self):
        def partial_then_hang(sender, *_):
            os.write(sender.fileno(), b"\x00\x00")
            time.sleep(5)

        with tempfile.TemporaryDirectory() as directory:
            with open(Path(directory) / "lock", "a+") as locked, mock.patch.object(
                supervisor, "_reconcile_child", side_effect=partial_then_hang
            ):
                started = time.monotonic()
                result = supervisor._reconcile_bounded(
                    {"target": "2026-09-24"}, str(Path(directory) / "snapshot.db"),
                    str(Path(directory) / "stage"), locked.fileno(), timeout=0.2,
                )
                elapsed = time.monotonic() - started
        self.assertEqual(result["snapshot"], "reconciliation_timed_out")
        self.assertLess(elapsed, 2)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc group-injection check")
    def test_worker_success_with_live_helper_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            result, code = self.execute(directory, "success_with_live_helper")
            helper_pid = int((Path(directory) / "state" / "helper.pid").read_text())
            try:
                os.kill(helper_pid, 0)
            except ProcessLookupError:
                helper_alive = False
            else:
                # Linux may briefly retain a reaped zombie; it cannot fetch.
                status = Path(f"/proc/{helper_pid}/status")
                helper_alive = not (status.exists() and "State:\tZ" in status.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn(result["reasonCode"],
                      {"worker_descendants_remained", "termination_unconfirmed"})
        self.assertFalse(helper_alive)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux supervisor SIGKILL injection")
    def test_supervisor_sigkill_reaps_blocked_worker_and_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            env = os.environ.copy()
            env.update({
                "SNAPSHOT_SUPERVISOR_TEST_ROOT": str(root),
                "SNAPSHOT_WORKER_TEST_MODE": "hang_with_helper",
                "RESEARCH_DATA_STATE_DIR": str(state),
                "RESEARCH_DATA_SNAPSHOT_DB": str(root / "snapshot.db"),
                "RESEARCH_DATA_AKSHARE_STAGE_DIR": str(root / "stage"),
            })
            driver = subprocess.Popen(
                [sys.executable, "-B", DRIVER], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pids = {}
            starttimes = {}
            try:
                stop = time.monotonic() + 7
                while time.monotonic() < stop:
                    if all((state / f"{name}.pid").exists()
                           for name in ("worker", "helper", "watchdog")):
                        pids = {name: int((state / f"{name}.pid").read_text())
                                for name in ("worker", "helper", "watchdog")}
                        starttimes = {name: linux_process_starttime(pid)
                                      for name, pid in pids.items()}
                        break
                    time.sleep(0.05)
                self.assertTrue(pids, "test worker/watchdog did not start")
                self.assertTrue(all(linux_process_live(pid) for pid in pids.values()))
                os.kill(driver.pid, signal.SIGKILL)
                driver.wait(timeout=3)
                stop = time.monotonic() + 5
                while time.monotonic() < stop and any(
                    linux_process_live(pid) for pid in pids.values()
                ):
                    time.sleep(0.05)
                self.assertFalse(any(linux_process_live(pid) for pid in pids.values()),
                                 "supervisor death left worker/helper/watchdog alive")
                shared = json.loads((state / "last-run.json").read_text())
                self.assertEqual(shared["status"], "temporary_failure")
                self.assertEqual(shared["reasonCode"],
                                 "supervisor_terminal_not_published")
            finally:
                if driver.poll() is None:
                    driver.kill()
                    driver.wait(timeout=3)
                for name, pid in pids.items():
                    kill_same_linux_process(pid, starttimes.get(name))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux watchdog-loss injection")
    def test_watchdog_loss_fails_closed_and_kills_worker_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            env = os.environ.copy()
            env.update({
                "SNAPSHOT_SUPERVISOR_TEST_ROOT": str(root),
                "SNAPSHOT_WORKER_TEST_MODE": "hang_with_helper",
                "RESEARCH_DATA_STATE_DIR": str(state),
                "RESEARCH_DATA_SNAPSHOT_DB": str(root / "snapshot.db"),
                "RESEARCH_DATA_AKSHARE_STAGE_DIR": str(root / "stage"),
            })
            driver = subprocess.Popen(
                [sys.executable, "-B", DRIVER], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pids = {}
            starttimes = {}
            try:
                stop = time.monotonic() + 7
                while time.monotonic() < stop:
                    if all((state / f"{name}.pid").exists()
                           for name in ("worker", "helper", "watchdog")):
                        pids = {name: int((state / f"{name}.pid").read_text())
                                for name in ("worker", "helper", "watchdog")}
                        starttimes = {name: linux_process_starttime(pid)
                                      for name, pid in pids.items()}
                        break
                    time.sleep(0.05)
                self.assertTrue(pids)
                self.assertTrue(all(linux_process_live(pid) for pid in pids.values()))
                kill_same_linux_process(pids["watchdog"], starttimes["watchdog"])
                driver.wait(timeout=8)
                stop = time.monotonic() + 5
                while time.monotonic() < stop and any(
                    linux_process_live(pids[name]) for name in ("worker", "helper")
                ):
                    time.sleep(0.05)
                self.assertFalse(any(
                    linux_process_live(pids[name]) for name in ("worker", "helper")
                ))
                shared = json.loads((state / "last-run.json").read_text())
                self.assertEqual(shared["status"], "failed")
                self.assertEqual(shared["reasonCode"], "watchdog_lost")
            finally:
                if driver.poll() is None:
                    driver.kill()
                    driver.wait(timeout=3)
                for name, pid in pids.items():
                    kill_same_linux_process(pid, starttimes.get(name))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux SQLite SIGKILL injection")
    def test_supervisor_sigkill_during_sqlite_transaction_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            env = os.environ.copy()
            env.update({
                "SNAPSHOT_SUPERVISOR_TEST_ROOT": str(root),
                "SNAPSHOT_WORKER_TEST_MODE": "sqlite_transaction_hang",
                "RESEARCH_DATA_STATE_DIR": str(state),
                "RESEARCH_DATA_SNAPSHOT_DB": str(root / "snapshot.db"),
                "RESEARCH_DATA_AKSHARE_STAGE_DIR": str(root / "stage"),
            })
            driver = subprocess.Popen(
                [sys.executable, "-B", DRIVER], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pids = {}
            starttimes = {}
            try:
                stop = time.monotonic() + 7
                while time.monotonic() < stop:
                    if all((state / f"{name}.pid").exists()
                           for name in ("worker", "watchdog")):
                        pids = {name: int((state / f"{name}.pid").read_text())
                                for name in ("worker", "watchdog")}
                        starttimes = {name: linux_process_starttime(pid)
                                      for name, pid in pids.items()}
                        break
                    time.sleep(0.05)
                self.assertTrue(pids, "SQLite fault-injection worker did not start")
                os.kill(driver.pid, signal.SIGKILL)
                driver.wait(timeout=3)
                stop = time.monotonic() + 5
                while time.monotonic() < stop and any(
                    linux_process_live(pid) for pid in pids.values()
                ):
                    time.sleep(0.05)
                self.assertFalse(any(linux_process_live(pid) for pid in pids.values()))
                with sqlite3.connect((root / "snapshot.db").as_uri() + "?mode=ro",
                                     uri=True, timeout=1) as db:
                    self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertEqual(db.execute("SELECT count(*) FROM snapshot").fetchone()[0], 0)
                shared = json.loads((state / "last-run.json").read_text())
                self.assertEqual(shared["status"], "temporary_failure")
            finally:
                if driver.poll() is None:
                    driver.kill()
                    driver.wait(timeout=3)
                for name, pid in pids.items():
                    kill_same_linux_process(pid, starttimes.get(name))

    def test_hard_deadline_fails_and_does_not_read_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            (state / "last-run.json").write_text(
                '{"runId":"old","status":"inserted","target":"2026-09-22"}\n',
                encoding="utf-8",
            )
            started = time.monotonic()
            result, code = self.execute(directory, "hang", deadline=0.3)
            elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reasonCode"], "deadline_exceeded")
        self.assertNotEqual(result["runId"], "old")
        self.assertLess(elapsed, 8)

    def test_uncooperative_worker_is_killed_before_audit_window(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result, code = self.execute(directory, "hang_ignore_term", deadline=0.3)
            elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn(result["reasonCode"],
                      {"deadline_exceeded", "termination_unconfirmed"})
        self.assertLess(elapsed, 11)

    def test_committed_candidate_after_timeout_is_recorded_but_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            result, code = self.execute(directory, "commit_then_hang", deadline=0.4)
            db = sqlite3.connect(
                (Path(directory) / "snapshot.db").as_uri() + "?mode=ro", uri=True
            )
            with db:
                count = db.execute("SELECT count(*) FROM snapshot").fetchone()[0]
            db.close()
        self.assertEqual(count, 1)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reasonCode"], "reconciliation_required")
        self.assertEqual(result["reconciliation"]["snapshot"],
                         "matching_candidate_committed")

    def test_lock_contention_does_not_launch_worker_or_replace_last_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            previous = '{"runId":"old","status":"inserted"}\n'
            (state / "last-run.json").write_text(previous, encoding="utf-8")
            with open(state / "snapshot.lock", "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result, code = self.execute(directory, "success")
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            self.assertEqual((state / "last-run.json").read_text(), previous)
        self.assertEqual(code, 75)
        self.assertEqual(result["reasonCode"], "snapshot_lock_busy")

    def test_worker_checkpoints_never_publish_shared_last_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            (state / "last-run.json").write_text(
                '{"runId":"previous","status":"inserted"}\n', encoding="utf-8"
            )
            run_id = "a" * 32
            with open(state / "snapshot.lock", "a+") as lock, mock.patch.dict(
                os.environ, {
                    "RESEARCH_DATA_SUPERVISED_RUN_ID": run_id,
                    "RESEARCH_DATA_SUPERVISOR_LOCK_FD": str(lock.fileno()),
                }
            ), mock.patch.object(snapshot, "STATE_DIR", str(state)):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                snapshot._verify_supervisor_lock()
                snapshot.write_run_checkpoint(run_id, target="2026-09-24")
                snapshot.write_last_run({"runId": run_id, "status": "deferred"})
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            previous = json.loads((state / "last-run.json").read_text())
            run = json.loads((state / "runs" / f"{run_id}.json").read_text())
        self.assertEqual(previous["runId"], "previous")
        self.assertEqual(run["status"], "deferred")

    def test_reconciliation_separates_unchanged_history_from_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "snapshot.db"
            target = "2026-09-24"
            payload = json.dumps({
                "date": target, "qlib": {"date": target},
                "akshare": {"activity": {"统计日期": target}},
            }, sort_keys=True, separators=(",", ":"))
            original_hash = hashlib.sha256(payload.encode()).hexdigest()
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)")
                db.execute("INSERT INTO snapshot VALUES(?,?,?)", (target, target, payload))
            evidence = supervisor._reconcile_read_only(
                {"target": target, "preexistingRowSha256": original_hash},
                str(db_path), str(Path(directory) / "stage")
            )
            self.assertEqual(evidence["snapshot"], "preexisting_row_unchanged")
            changed = json.dumps({
                "date": target, "qlib": {"date": target},
                "akshare": {"activity": {"统计日期": target}},
                "note": "changed",
            }, sort_keys=True, separators=(",", ":"))
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE snapshot SET payload=? WHERE date=?", (changed, target))
            evidence = supervisor._reconcile_read_only(
                {"target": target, "preexistingRowSha256": original_hash},
                str(db_path), str(Path(directory) / "stage")
            )
            self.assertEqual(evidence["snapshot"], "historical_row_changed")

    def test_soft_deadline_prevents_new_source_stage_and_sqlite_work(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"RESEARCH_DATA_SOFT_DEADLINE_EPOCH": "1"}
        ):
            db_path = Path(directory) / "snapshot.db"
            with mock.patch.object(snapshot, "akshare_market") as source:
                with self.assertRaises(snapshot.SnapshotTemporaryError):
                    snapshot.collect_source(source)
            source.assert_not_called()
            with self.assertRaises(snapshot.SnapshotTemporaryError):
                snapshot.seal_akshare_market_stage({}, "", "", str(Path(directory) / "stage"))
            with self.assertRaises(snapshot.SnapshotTemporaryError):
                snapshot.store_snapshot({"date": "2026-09-24"}, str(db_path))
            self.assertFalse(db_path.exists())
            self.assertFalse((Path(directory) / "stage").exists())

    def test_late_start_writes_failure_without_launching_worker(self):
        late = dt.datetime(2026, 9, 24, 22, 40, 1,
                           tzinfo=supervisor.ZONE)
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "snapshot_supervisor.subprocess.Popen"
        ) as launch:
            result, code = supervisor.run_supervised(
                worker_path=WORKER,
                state_dir=str(Path(directory) / "state"),
                db_path=str(Path(directory) / "snapshot.db"),
                stage_dir=str(Path(directory) / "stage"),
                log_path=str(Path(directory) / "snapshot.log"),
                now=late,
            )
            current = json.loads(
                (Path(directory) / "state" / "last-run.json").read_text()
            )
            audit, audit_time = data_health.recent_run(
                str(Path(directory) / "state" / "last-run.json"),
                dt.datetime.now(supervisor.ZONE),
            )
        launch.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(result["reasonCode"], "start_after_safe_window")
        self.assertEqual(current["runId"], result["runId"])
        self.assertEqual(audit["status"], "failed")
        self.assertIsNotNone(audit_time)

    def test_worker_launch_failure_is_readable_by_health_audit(self):
        now = dt.datetime.now(supervisor.ZONE).replace(
            hour=22, minute=30, second=0, microsecond=0
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "snapshot_supervisor._launch_guarded_worker",
            side_effect=OSError("simulated launch failure"),
        ), mock.patch.object(supervisor.dt, "datetime", wraps=dt.datetime) as clock:
            # The simulated 22:30 start must precede its simulated completion,
            # even when the suite runs before 22:30 on the host.
            clock.now.return_value = now + dt.timedelta(seconds=1)
            root = Path(directory)
            result, code = supervisor.run_supervised(
                worker_path=WORKER, watchdog_path=WATCHDOG,
                state_dir=str(root / "state"), db_path=str(root / "snapshot.db"),
                stage_dir=str(root / "stage"), log_path=str(root / "snapshot.log"),
                now=now,
            )
            audit, audit_time = data_health.recent_run(
                str(root / "state" / "last-run.json"),
                dt.datetime.now(supervisor.ZONE),
            )
        self.assertEqual(code, 1)
        self.assertEqual(result["reasonCode"], "worker_launch_failed:OSError")
        self.assertEqual(audit["status"], "failed")
        self.assertIsNotNone(audit_time)

    def test_interrupted_stage_is_only_observed_if_complete_and_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = Path(directory) / "stage"
            target = "2026-09-24"
            progress = {
                "target": target,
                "stageCandidateTradeDate": target,
                "stageCandidatePayloadSha256": "b" * 64,
                "stageCandidateRecordSha256": "c" * 64,
            }
            before = supervisor._reconcile_read_only(
                progress, str(Path(directory) / "missing.db"), str(stage_dir)
            )
            self.assertEqual(before["stage"], "missing_or_invalid")
            sealed = snapshot.seal_akshare_market_stage(
                sample_market(target),
                target + "T22:30:00+08:00", target + "T22:30:02+08:00",
                str(stage_dir),
            )
            progress.update({
                "stageCandidatePayloadSha256": sealed["payloadSha256"],
                "stageCandidateRecordSha256": sealed["recordSha256"],
            })
            after = supervisor._reconcile_read_only(
                progress, str(Path(directory) / "missing.db"), str(stage_dir)
            )
            self.assertEqual(after["stage"], "matching_immutable_stage")
            progress["stageCandidateRecordSha256"] = "d" * 64
            mismatch = supervisor._reconcile_read_only(
                progress, str(Path(directory) / "missing.db"), str(stage_dir)
            )
            self.assertEqual(mismatch["stage"], "candidate_mismatch")


if __name__ == "__main__":
    unittest.main()
