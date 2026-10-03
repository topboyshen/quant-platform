"""Vendor-free run identity, strict JSON and lock/state primitives. No network imports."""
import fcntl
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
STATE_DIR = os.environ.get("RESEARCH_DATA_STATE_DIR", os.path.expanduser("~/.local/state/research-data"))
FULL_STATE_DIR = os.environ.get("RESEARCH_DATA_FULL_STATE_DIR", STATE_DIR)
DB = os.environ.get("RESEARCH_DATA_SNAPSHOT_DB", os.path.expanduser("~/research-data/snapshot.db"))
FULL_DB = os.path.expanduser("~/research-data/snapshot.db")
SQLITE_TIMEOUT = 30
SNAPSHOT_DEFERRED_EXIT = 69
_RUN_PROGRESS = {}


class QlibDateError(ValueError):
    """The requested trade date is not present in the local Qlib calendar."""

class SnapshotValidationError(RuntimeError):
    """The candidate snapshot is unsafe to persist."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}

class SnapshotUsageError(RuntimeError):
    """The command-line invocation is unsupported."""

class SnapshotTemporaryError(RuntimeError):
    """A transient lock or database-busy condition blocked the run."""

class SnapshotDeferredError(RuntimeError):
    """Required same-trade-date source material has not arrived yet."""

    def __init__(self, message, *, target=None, qlib_tag=None, source_failure=None,
                 akshare_stage=None):
        super().__init__(message)
        self.target = target
        self.qlib_tag = qlib_tag
        self.source_failure = source_failure
        self.akshare_stage = akshare_stage

def _supervised_run_id():
    value = os.environ.get("RESEARCH_DATA_SUPERVISED_RUN_ID")
    if value is None:
        return None
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise SnapshotUsageError("supervised run ID is invalid")
    return value

def _await_supervisor_gate():
    """Do not begin any source work until the watchdog acknowledges the PGID."""
    if _supervised_run_id() is None:
        return
    fd_text = os.environ.pop("RESEARCH_DATA_SUPERVISOR_GATE_FD", None)
    parent_text = os.environ.get("RESEARCH_DATA_SUPERVISOR_PID")
    if fd_text is None or parent_text is None:
        raise SnapshotUsageError("supervisor start gate is missing")
    try:
        gate_fd = int(fd_text)
        expected_parent = int(parent_text)
        released = os.read(gate_fd, 1)
    except (OSError, ValueError) as exc:
        raise SnapshotTemporaryError("supervisor start gate unavailable") from exc
    finally:
        try:
            os.close(gate_fd)
        except (OSError, UnboundLocalError):
            pass
    if released != b"G" or os.getppid() != expected_parent:
        raise SnapshotTemporaryError("supervisor exited before worker release")

def _supervised_soft_deadline(phase):
    value = os.environ.get("RESEARCH_DATA_SOFT_DEADLINE_EPOCH")
    if value is None:
        return
    try:
        deadline = float(value)
    except ValueError as exc:
        raise SnapshotUsageError("supervised deadline is invalid") from exc
    if time.time() >= deadline:
        raise SnapshotTemporaryError(f"safe snapshot window ended before {phase}")

def write_run_checkpoint(run_id, *, state_dir=None, **fields):
    """The worker records only its own progress; the supervisor owns last-run."""
    if _supervised_run_id() is None:
        return
    if run_id != _supervised_run_id():
        raise SnapshotUsageError("supervised run ID differs from worker")
    _RUN_PROGRESS.update(fields)
    _RUN_PROGRESS.update({"runId": run_id, "status": "running"})
    state_dir = state_dir or STATE_DIR
    os.makedirs(os.path.join(state_dir, "runs"), mode=0o700, exist_ok=True)
    path = os.path.join(state_dir, "runs", f"{run_id}.json")
    _atomic_run_json(path, _RUN_PROGRESS)

def _atomic_run_json(path, record):
    temporary_path = f"{path}.{os.getpid()}.tmp"
    payload = _snapshot_payload(record) + "\n"
    with open(temporary_path, "w", encoding="utf-8") as state_file:
        state_file.write(payload)
        state_file.flush()
        os.fsync(state_file.fileno())
    os.replace(temporary_path, path)

def _verify_supervisor_lock(state_dir=None):
    descriptor = os.environ.get("RESEARCH_DATA_SUPERVISOR_LOCK_FD")
    if descriptor is None:
        raise SnapshotUsageError("supervised snapshot lock descriptor missing")
    try:
        fd = int(descriptor)
        inherited = os.fstat(fd)
        lock = os.stat(os.path.join(state_dir or STATE_DIR, "snapshot.lock"))
    except (ValueError, OSError) as exc:
        raise SnapshotUsageError("supervised snapshot lock invalid") from exc
    if (inherited.st_dev, inherited.st_ino) != (lock.st_dev, lock.st_ino):
        raise SnapshotUsageError("supervised snapshot lock mismatch")

def _snapshot_payload(snapshot):
    try:
        return json.dumps(
            snapshot,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"payload is not strict JSON: {exc}") from exc

def _payload_sha256(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")

def _strict_json(raw):
    """Match the audit's finite-number and unique-object-key JSON contract."""
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"non-finite JSON number: {value}")
        return number

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw, parse_constant=_reject_json_constant,
                      parse_float=finite, object_pairs_hook=unique)

def write_last_run(record, state_dir=None):
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    supervised = _supervised_run_id()
    if supervised is None:
        path = os.path.join(state_dir, "last-run.json")
    else:
        if record.get("runId") != supervised:
            raise SnapshotUsageError("supervised final record has wrong run ID")
        os.makedirs(os.path.join(state_dir, "runs"), mode=0o700, exist_ok=True)
        path = os.path.join(state_dir, "runs", f"{supervised}.json")
    _atomic_run_json(path, record)

@contextmanager
def snapshot_run_lock(state_dir=None):
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    lock_path = os.path.join(state_dir, "snapshot.lock")
    lock_file = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise SnapshotTemporaryError("another snapshot run holds the lock") from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

def source_error_paths(value, path=""):
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (key == "error" or key.endswith("_error")) and child:
                errors.append(child_path)
            else:
                errors.extend(source_error_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            errors.extend(source_error_paths(child, child_path))
    return errors
