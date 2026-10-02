#!/usr/bin/env python3
"""One natural snapshot run with a same-run terminal record and hard deadline.

The worker does all business validation and writing. This supervisor holds the
existing snapshot lock, terminates the worker group at the deadline, and only
reads stage/SQLite when a terminal result is ambiguous.
"""
import datetime as dt
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

import daily_snapshot as snapshot
import snapshot_watchdog as group_guard


ZONE = ZoneInfo("Asia/Shanghai")
SUCCESS = {"inserted", "idempotent", "skipped_existing", "skipped_non_trading"}
TERMINAL = SUCCESS | {"deferred", "rejected", "temporary_failure", "usage_error", "failed"}
START_LATEST = dt.time(22, 40)
SOFT_LATEST = dt.time(22, 40, 30)
HARD_LATEST = dt.time(22, 42)
RECONCILE_MAX_SECONDS = 20
QLIB_RETRY_SECONDS = 30
QLIB_MAX_ATTEMPTS = 20


def _qlib_retry_allowed(final, code, *, started, hard, monotonic_stop, attempt):
    """Retry only the known readiness deferral, within this run's deadlines."""
    current = dt.datetime.now(ZONE)
    stage = final.get("akshareStage") or {}
    return (
        code == snapshot.SNAPSHOT_DEFERRED_EXIT
        and final.get("status") == "deferred"
        and final.get("reason") == "qlib_not_current_for_expected_trading_day"
        and isinstance(stage, dict)
        and stage.get("tradeDate") == final.get("target") == current.date().isoformat()
        and current.date() == started.astimezone(ZONE).date()
        and current.time() < START_LATEST
        and current.timestamp() < hard.timestamp()
        and time.monotonic() < monotonic_stop
        and attempt < QLIB_MAX_ATTEMPTS
    )


def _retry_stage_matches(final, stage_dir, lock_fd, timeout):
    """A terminal deferral alone cannot authorize a retry without its seal."""
    if timeout <= 0:
        return False
    claimed = final.get("akshareStage") or {}
    progress = {"target": final["target"],
                "stageCandidateTradeDate": claimed.get("tradeDate"),
                "stageCandidatePayloadSha256": claimed.get("payloadSha256"),
                "stageCandidateRecordSha256": claimed.get("recordSha256")}
    # Stage files can be replaced or filesystem reads can wedge. The parent
    # must keep its clock/lock control even between guarded worker attempts.
    evidence = _reconcile_bounded(progress, None, stage_dir, lock_fd,
                                  timeout=timeout, stage_only=True)
    return evidence.get("stage") == "matching_immutable_stage"


def _run_record_path(state_dir, run_id):
    return Path(state_dir) / "runs" / f"{run_id}.json"


def _read_record(path, run_id):
    try:
        record = snapshot._strict_json(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return record if isinstance(record, dict) and record.get("runId") == run_id else None


def _publish_record(state_dir, record):
    snapshot._atomic_run_json(
        str(_run_record_path(state_dir, record["runId"])), record
    )
    snapshot._atomic_run_json(
        str(Path(state_dir) / "last-run.json"), record
    )


def _reconcile_read_only(progress, db_path, stage_dir, stage_only=False):
    """Describe persisted evidence after interruption; never promote to pass."""
    evidence = {"stage": "unknown", "snapshot": "unknown"}
    target = progress.get("target")
    if not isinstance(target, str):
        return evidence
    if progress.get("stageCandidateTradeDate") == target:
        try:
            stage = snapshot.load_akshare_market_stage(target, stage_dir)
        except (snapshot.SnapshotValidationError, OSError):
            evidence["stage"] = "missing_or_invalid"
        else:
            if stage is None:
                evidence["stage"] = "missing_or_invalid"
            elif (stage.get("payloadSha256") == progress.get("stageCandidatePayloadSha256")
                    and stage.get("recordSha256") == progress.get("stageCandidateRecordSha256")):
                evidence["stage"] = "matching_immutable_stage"
            else:
                evidence["stage"] = "candidate_mismatch"
    if stage_only:
        return evidence
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        with connection:
            connection.execute("PRAGMA query_only=ON")
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                evidence["snapshot"] = "database_integrity_uncertain"
            else:
                row = connection.execute(
                    "SELECT payload FROM snapshot WHERE date=?", (target,)
                ).fetchone()
                if row is None:
                    evidence["snapshot"] = "no_row"
                else:
                    previous = progress.get("preexistingRowSha256")
                    current = snapshot._payload_sha256(row[0])
                    candidate = progress.get("snapshotCandidateSha256")
                    try:
                        parsed = snapshot._strict_json(row[0])
                        dates_match = (parsed.get("date") == target
                                       and (parsed.get("qlib") or {}).get("date") == target
                                       and snapshot.activity_trade_date(parsed.get("akshare") or {}) == target)
                        source_clean = not snapshot.required_source_errors(
                            snapshot.source_error_paths(parsed)
                        )
                    except (TypeError, ValueError, AttributeError):
                        dates_match = source_clean = False
                    if not dates_match or not source_clean:
                        evidence["snapshot"] = "row_invalid"
                    elif previous is not None and current != previous:
                        evidence["snapshot"] = "historical_row_changed"
                    elif previous is not None:
                        evidence["snapshot"] = "preexisting_row_unchanged"
                    elif candidate is not None and candidate == current:
                        evidence["snapshot"] = "matching_candidate_committed"
                    else:
                        evidence["snapshot"] = "candidate_missing_or_mismatched"
    except (sqlite3.Error, OSError):
        evidence["snapshot"] = "database_read_unavailable"
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass
    return evidence


def _reconcile_child(sender, progress, db_path, stage_dir, inherited_lock_fd, stage_only=False):
    # A read-only reconciliation must not keep the supervisor's lock alive.
    os.close(inherited_lock_fd)
    try:
        sender.send(_reconcile_read_only(progress, db_path, stage_dir, stage_only))
    except BaseException:
        sender.send({"stage": "unknown", "snapshot": "reconciliation_unavailable"})
    finally:
        sender.close()


def _reconcile_bounded(progress, db_path, stage_dir, inherited_lock_fd,
                       timeout=RECONCILE_MAX_SECONDS, stage_only=False):
    """Kill a wedged SQLite/stage read; never infer success from timeout."""
    receiver, sender = multiprocessing.get_context("fork").Pipe(duplex=False)
    process = multiprocessing.get_context("fork").Process(
        target=_reconcile_child,
        args=(sender, progress, db_path, stage_dir, inherited_lock_fd, stage_only),
    )
    process.start()
    sender.close()
    try:
        # poll() only proves that *some* pipe bytes arrived; recv() could then
        # block forever on a partial pickle frame. Reap the child first.
        process.join(timeout=timeout)
        if process.is_alive():
            result = {"stage": "unknown", "snapshot": "reconciliation_timed_out"}
        elif receiver.poll(0):
            try:
                result = receiver.recv()
            except (EOFError, OSError):
                result = {"stage": "unknown", "snapshot": "reconciliation_unavailable"}
        else:
            result = {"stage": "unknown", "snapshot": "reconciliation_unavailable"}
    finally:
        receiver.close()
        process.join(timeout=0.1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
    if process.is_alive():
        return {"stage": "unknown", "snapshot": "reconciliation_termination_unconfirmed"}
    return result


def _watchdog_exited(watchdog):
    """Observe without wait/reap so its PID keeps the PGID from being reused."""
    if hasattr(os, "waitid"):
        try:
            return os.waitid(
                os.P_PID, watchdog.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            ) is not None
        except ChildProcessError:
            return True
    # Darwin is only a local test platform; the Linux path above is required
    # for release. Do not poll()/wait() here, as they reap the group leader.
    try:
        os.kill(watchdog.pid, 0)
    except ProcessLookupError:
        return True
    return False


def _watchdog_identity_pinned(watchdog):
    """Only a still-owned, unreaped Linux child may anchor a numeric PGID.

    A child accidentally reaped by another waitpid user must never cause us
    to signal its old numeric group ID. There is no competing wait thread in
    this supervisor between this check and the following group operation.
    """
    if sys.platform.startswith("linux"):
        try:
            os.waitid(
                os.P_PID, watchdog.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            return os.getpgid(watchdog.pid) == watchdog.pid
        except (ChildProcessError, ProcessLookupError, OSError):
            return False
    # Non-Linux is exercised only by synthetic local tests; it is not a
    # process-identity proof suitable for production release.
    try:
        return os.getpgid(watchdog.pid) == watchdog.pid
    except ProcessLookupError:
        return False


def _live_nonwatchdog_members(watchdog, worker):
    if not _watchdog_identity_pinned(watchdog):
        return None
    members = group_guard.live_group_members(
        watchdog.pid, exclude=(watchdog.pid,)
    )
    if members is not None:
        return members
    if sys.platform.startswith("linux"):
        return None  # /proc unreadable: cannot prove clean, fail closed.
    return [] if worker.returncode is not None else [worker.pid]


def _terminate_group(process, watchdog):
    # The watchdog is the still-unreaped group leader. Numeric PGID cannot be
    # reused until *after* both signals and all group checks are complete.
    if not _watchdog_identity_pinned(watchdog):
        # We may kill only our still-owned direct worker; descendants are
        # uncontained, so the run must remain failed/termination_unconfirmed.
        try:
            process.kill()
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        return False
    pgid = watchdog.pid if sys.platform.startswith("linux") else process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    if not _watchdog_identity_pinned(watchdog):
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # wait() only reaps the direct worker. KILL delivery to its descendants
    # can still be pending, so prove group emptiness within the same bounded
    # termination budget before releasing the lock or reaping the leader.
    stop = time.monotonic() + 5
    try:
        process.wait(timeout=max(0, stop - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    while True:
        members = _live_nonwatchdog_members(watchdog, process)
        if members is None:
            return False
        if members == []:
            return True
        remaining = stop - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def _close_fd(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _launch_guarded_worker(worker_path, watchdog_path, environment, log, lock_fd, worker_args=()):
    """Arm the reaper before releasing the worker's one-byte start gate."""
    control_read, control_write = os.pipe()
    ack_read, ack_write = os.pipe()
    gate_read, gate_write = os.pipe()
    watchdog = worker = None
    handed_off = False
    try:
        watchdog = subprocess.Popen(
            [sys.executable, "-B", watchdog_path,
             str(control_read), str(ack_write)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            env=environment,
            process_group=0, pass_fds=(control_read, ack_write),
        )
        _close_fd(control_read)
        control_read = None
        _close_fd(ack_write)
        ack_write = None
        environment["RESEARCH_DATA_SUPERVISOR_GATE_FD"] = str(gate_read)
        environment["RESEARCH_DATA_SUPERVISOR_PID"] = str(os.getpid())
        worker = subprocess.Popen(
            [sys.executable, "-B", worker_path, *worker_args],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=environment,
            **({"process_group": watchdog.pid} if sys.platform.startswith("linux")
               else {"start_new_session": True}),
            pass_fds=(lock_fd, gate_read),
        )
        _close_fd(gate_read)
        gate_read = None
        os.write(control_write, f"{worker.pid}\n".encode("ascii"))
        if not select.select([ack_read], [], [], 3)[0]:
            raise RuntimeError("watchdog did not arm")
        if os.read(ack_read, 6) != b"READY\n" or _watchdog_exited(watchdog):
            raise RuntimeError("watchdog acknowledgement invalid")
        os.write(gate_write, b"G")
        _close_fd(gate_write)
        gate_write = None
        handed_off = True
        return worker, watchdog, control_write, ack_read
    except BaseException:
        _close_fd(gate_write)
        _close_fd(gate_read)
        if worker is not None:
            _terminate_group(worker, watchdog)
        _close_fd(control_write)
        if watchdog is not None:
            try:
                watchdog.wait(timeout=2)
            except subprocess.TimeoutExpired:
                watchdog.kill()
                watchdog.wait(timeout=2)
        raise
    finally:
        _close_fd(control_read)
        if not handed_off:
            _close_fd(ack_read)
        _close_fd(ack_write)


def _disarm_watchdog(watchdog, control_write, ack_read):
    """Only clean normal completion may disarm; never use EOF as success."""
    if _watchdog_exited(watchdog):
        return False
    try:
        os.write(control_write, b"DISARM\n")
        if not select.select([ack_read], [], [], 2)[0]:
            return False
        return os.read(ack_read, 9) == b"DISARMED\n"
    except OSError:
        return False


def _run_attempt(*, worker_path, watchdog_path, environment, log_path, lock_fd,
                 worker_args, hard, monotonic_stop, record, state_dir,
                 db_path, stage_dir):
    """Complete and reap one guarded worker before considering any retry."""
    run_id = record["runId"]
    try:
        with open(log_path, "ab") as log:
            process, watchdog, control_write, ack_read = _launch_guarded_worker(
                worker_path, watchdog_path, environment, log, lock_fd, worker_args
            )
            try:
                timed_out = watchdog_lost = descendants_remained = False
                while True:
                    if _watchdog_exited(watchdog):
                        watchdog_lost = True
                        terminated = _terminate_group(process, watchdog)
                        break
                    remaining = min(hard.timestamp() - time.time(),
                                    monotonic_stop - time.monotonic())
                    if remaining <= 0:
                        timed_out = True
                        terminated = _terminate_group(process, watchdog)
                        break
                    try:
                        process.wait(timeout=min(1.0, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                    terminated = True
                    break
                # Intentional deadline cleanup must retain its timeout reason.
                if not timed_out and _watchdog_exited(watchdog):
                    watchdog_lost = True
                if not timed_out and not watchdog_lost:
                    members = _live_nonwatchdog_members(watchdog, process)
                    descendants_remained = members is None or bool(members)
                    if descendants_remained:
                        terminated = _terminate_group(process, watchdog)
                    elif not _disarm_watchdog(watchdog, control_write, ack_read):
                        watchdog_lost = True
                        terminated = _terminate_group(process, watchdog)
            finally:
                _close_fd(control_write)
                _close_fd(ack_read)
                try:
                    watchdog.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Never signal a numeric group after reaping its leader.
                    watchdog.kill()
                    watchdog.wait(timeout=2)
    except (OSError, RuntimeError) as exc:
        return ({**record, "status": "failed",
                 "reasonCode": f"worker_launch_failed:{type(exc).__name__}",
                 "timestamp": dt.datetime.now(ZONE).isoformat()}, 1)
    child = _read_record(_run_record_path(state_dir, run_id), run_id)
    if (not timed_out and not watchdog_lost and not descendants_remained
            and terminated and child is not None
            and child.get("status") in TERMINAL):
        status = child["status"]
        expected_exit = (status in SUCCESS and process.returncode == 0)
        expected_deferred = (status == "deferred"
                             and process.returncode == snapshot.SNAPSHOT_DEFERRED_EXIT)
        expected_failure = (status not in SUCCESS | {"deferred"}
                            and process.returncode != 0)
        if expected_exit or expected_deferred or expected_failure:
            return {**record, **child}, process.returncode
        return ({**record, **child, "status": "failed",
                 "reasonCode": "exit_record_conflict"}, 1)
    progress = {**record, **(child or {})}
    evidence = _reconcile_bounded(
        progress, db_path, stage_dir, lock_fd
    ) if terminated else {
        "stage": "unknown", "snapshot": "worker_termination_unconfirmed"
    }
    return ({
        **progress, "runId": run_id, "status": "failed",
        "reasonCode": ("termination_unconfirmed" if not terminated else
                       "watchdog_lost" if watchdog_lost else
                       "worker_descendants_remained" if descendants_remained else
                       "reconciliation_required" if evidence["snapshot"] in {
                           "matching_candidate_committed", "historical_row_changed",
                           "candidate_missing_or_mismatched", "row_invalid",
                       } else "deadline_exceeded" if timed_out
                       else "worker_terminal_record_missing"),
        "reconciliation": evidence,
    }, 1)


def run_supervised(*, worker_path=None, state_dir=None, db_path=None,
                   stage_dir=None, log_path=None, watchdog_path=None, now=None,
                   deadline_seconds_override=None, worker_args=()):
    state_dir = state_dir or snapshot.STATE_DIR
    db_path = db_path or snapshot.DB
    stage_dir = stage_dir or snapshot._akshare_stage_directory()
    worker_path = worker_path or str(Path(snapshot.__file__).resolve())
    watchdog_path = watchdog_path or str(Path(__file__).with_name("snapshot_watchdog.py"))
    log_path = log_path or str(Path(snapshot.__file__).parent / "snapshot.log")
    run_id = uuid.uuid4().hex
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    os.makedirs(Path(state_dir) / "runs", mode=0o700, exist_ok=True)
    lock_path = Path(state_dir) / "snapshot.lock"
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ({"runId": run_id, "status": "temporary_failure",
                     "reasonCode": "snapshot_lock_busy",
                     "timestamp": dt.datetime.now(ZONE).isoformat()}, 75)
        started = now or dt.datetime.now(ZONE)
        if started.tzinfo is None:
            raise ValueError("supervisor start time requires timezone")
        local = started.astimezone(ZONE)
        soft = dt.datetime.combine(local.date(), SOFT_LATEST, ZONE)
        hard = min(started + dt.timedelta(minutes=11),
                   dt.datetime.combine(local.date(), HARD_LATEST, ZONE))
        if deadline_seconds_override is not None:
            if not 0 < deadline_seconds_override <= 660:
                raise ValueError("test deadline override is invalid")
            hard = dt.datetime.now(ZONE) + dt.timedelta(
                seconds=deadline_seconds_override
            )
        record = {
            "runId": run_id, "status": "running",
            "startedAt": started.isoformat(), "target": None,
        }
        # Publish a fail-closed placeholder BEFORE a worker can touch stage or
        # SQLite. If this supervisor crashes between the per-run final and the
        # shared final write, health cannot mistake yesterday's success for
        # today's outcome. The per-run journal remains useful for forensics.
        pending = {**record, "status": "temporary_failure",
                   "timestamp": started.isoformat(),
                   "reasonCode": "supervisor_terminal_not_published"}
        snapshot._atomic_run_json(str(Path(state_dir) / "last-run.json"), pending)
        snapshot._atomic_run_json(str(_run_record_path(state_dir, run_id)), record)
        if local.time() >= START_LATEST:
            final = {**record, "status": "failed", "reasonCode": "start_after_safe_window",
                     "timestamp": dt.datetime.now(ZONE).isoformat()}
            _publish_record(state_dir, final)
            return final, 1
        environment = os.environ.copy()
        for suffix in ("TRADE_DATE", "PAYLOAD_SHA256", "RECORD_SHA256"):
            environment.pop("RESEARCH_DATA_RETRY_STAGE_" + suffix, None)
        environment.update({
            "RESEARCH_DATA_SUPERVISED_RUN_ID": run_id,
            "RESEARCH_DATA_SUPERVISOR_LOCK_FD": str(lock_file.fileno()),
            "RESEARCH_DATA_SOFT_DEADLINE_EPOCH": str(soft.timestamp()),
            "RESEARCH_DATA_STATE_DIR": str(state_dir),
            # Core workers have their own state directory. Preserve the full
            # contract's identity before overriding STATE_DIR for the worker.
            "RESEARCH_DATA_FULL_STATE_DIR": str(snapshot.STATE_DIR),
            "RESEARCH_DATA_SNAPSHOT_DB": str(db_path),
            "RESEARCH_DATA_AKSHARE_STAGE_DIR": str(stage_dir),
        })
        # One lock, run ID, and deadline budget span all readiness attempts.
        # A fresh guarded process prevents source helpers leaking into retries.
        monotonic_stop = time.monotonic() + max(0, hard.timestamp() - time.time())
        attempts = []
        while True:
            attempt = len(attempts) + 1
            attempt_record = {**record, "attemptCount": attempt}
            # Clear the previous attempt's terminal record before launch: a
            # crashed retry must never inherit an earlier terminal claim.
            snapshot._atomic_run_json(str(_run_record_path(state_dir, run_id)), attempt_record)
            final, code = _run_attempt(
                worker_path=worker_path, watchdog_path=watchdog_path,
                environment=environment, log_path=log_path, lock_fd=lock_file.fileno(),
                worker_args=worker_args, hard=hard, monotonic_stop=monotonic_stop,
                record=attempt_record, state_dir=state_dir, db_path=db_path, stage_dir=stage_dir,
            )
            attempts.append({"attempt": attempt, "status": final["status"],
                             "reason": final.get("reason", final.get("reasonCode")),
                             "completedAt": dt.datetime.now(ZONE).isoformat()})
            if not _qlib_retry_allowed(final, code, started=started, hard=hard,
                                       monotonic_stop=monotonic_stop, attempt=attempt):
                break
            retry_stop = min(
                dt.datetime.combine(local.date(), START_LATEST, ZONE).timestamp(),
                hard.timestamp(),
            )
            verify_budget = min(RECONCILE_MAX_SECONDS, retry_stop - time.time(),
                                monotonic_stop - time.monotonic())
            if not _retry_stage_matches(final, stage_dir, lock_file.fileno(), verify_budget):
                break
            delay = min(QLIB_RETRY_SECONDS, retry_stop - time.time(),
                        monotonic_stop - time.monotonic())
            if delay <= 0:
                break
            snapshot._atomic_run_json(str(_run_record_path(state_dir, run_id)), {
                **final, "status": "running", "activePhase": "waiting_for_qlib",
                "attemptCount": attempt, "attempts": attempts,
            })
            time.sleep(delay)
            if not _qlib_retry_allowed(final, code, started=started, hard=hard,
                                       monotonic_stop=monotonic_stop, attempt=attempt):
                break
            for field, suffix in (("tradeDate", "TRADE_DATE"),
                                  ("payloadSha256", "PAYLOAD_SHA256"),
                                  ("recordSha256", "RECORD_SHA256")):
                environment["RESEARCH_DATA_RETRY_STAGE_" + suffix] = final["akshareStage"][field]
        final["attemptCount"] = len(attempts)
        if len(attempts) > 1:
            final["attempts"] = attempts
        final["timestamp"] = dt.datetime.now(ZONE).isoformat()
        _publish_record(state_dir, final)
        return final, code
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    result, code = run_supervised()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    raise SystemExit(code)
