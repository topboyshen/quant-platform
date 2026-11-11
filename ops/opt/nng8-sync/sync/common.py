"""Small bounded primitives shared by the exporter and writer (standard library)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import time
import uuid

MAX_SOURCE = 300_000
MAX_BUNDLE = 1_048_576
MAX_STDERR = 16_384
MAX_HEALTH = 8192
MAX_DEPTH = 32
MAX_NODES = 60_000
OPERATION = "nng8-readonly-export-v1"
VERSION = "nng8.sync-writer.v1"
DOCUMENTS = frozenset(("today", "progress", "delivery", "projects", "ledger", "phase2"))
MODES = frozenset(("RUNNING", "FROZEN", "UNKNOWN"))
ERRORS = frozenset(("transport_failed", "timeout", "size_limit", "invalid_bundle", "replay",
                    "capture_conflict", "source_time_regressed", "publication_failed",
                    "receipt_failed", "interrupted", "unknown"))


class SyncError(ValueError):
    def __init__(self, code="unknown"):
        self.code = code if code in ERRORS else "unknown"
        super().__init__(self.code)


class Busy(SyncError):
    """No state was changed because another process owns the lock."""


class PublicationUncertain(SyncError):
    """Rename became visible, but its following directory fsync did not complete."""


class Deadline:
    def __init__(self, seconds=45, *, end=None):
        self.end = time.monotonic() + seconds if end is None else end

    def remaining(self):
        value = self.end - time.monotonic()
        if value <= 0:
            raise SyncError("timeout")
        return value

    def check(self):
        self.remaining()


@contextmanager
def alarm(deadline):
    """Production entrypoints use SIGALRM to interrupt blocking Python I/O."""
    def expired(*_):
        raise SyncError("timeout")
    own_remaining = deadline.remaining()
    previous = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, min(remaining, own_remaining) if remaining else own_remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, remaining - (time.monotonic() - started)), interval)


def utcnow():
    return datetime.now(timezone.utc)


def instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise SyncError("invalid_bundle")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise SyncError("invalid_bundle") from None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def entry(payload):
    return {"payload": payload, "sha256": sha(canonical(payload))}


def strict_json(raw, limit=MAX_BUNDLE):
    if len(raw) > limit:
        raise SyncError("size_limit")
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise SyncError("invalid_bundle")
            result[key] = value
        return result
    def invalid(_):
        raise SyncError("invalid_bundle")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > MAX_DEPTH or nodes > MAX_NODES:
                raise SyncError("invalid_bundle")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise SyncError("invalid_bundle")
            elif isinstance(item, str):
                item.encode("utf-8", errors="strict")
        if not isinstance(value, dict):
            raise SyncError("invalid_bundle")
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, SyncError):
            raise
        raise SyncError("invalid_bundle") from None


def open_directory(path, *, protected=False, owner=None, strict_parents=False):
    """Walk every component using dirfd + NOFOLLOW, avoiding parent-link escapes."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise SyncError("publication_failed")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = following
            info = os.fstat(fd)
            if strict_parents and (info.st_uid not in (0, owner) or info.st_mode & 0o022):
                raise SyncError("publication_failed")
        info = os.fstat(fd)
        if protected and (info.st_mode & 0o022 or (owner is not None and info.st_uid != owner)):
            raise SyncError("publication_failed")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _bounded_fd_pass(fd, limit, deadline):
    """Read at most limit + 1 actual bytes, including the oversize sentinel."""
    pieces, count = [], 0
    while True:
        if deadline:
            deadline.check()
        part = os.read(fd, min(65_536, limit + 1 - count))
        count += len(part)
        if count > limit:
            raise SyncError("size_limit")
        if not part:
            return b"".join(pieces)
        pieces.append(part)


def read_at(directory, name, limit, deadline=None):
    """Two bounded byte observations of one pinned fd; never reopen the pathname.

    At most 2 * (limit + 1) bytes are read, using the same total deadline. Equal
    observations detect many in-place writes but cannot exclude unobserved ABA.
    Producers must publish completed new inodes atomically and never modify a
    published inode in place. Inode-preserving metadata alone is not a snapshot.
    """
    if deadline:
        deadline.check()
    if "/" in name or name in ("", ".", ".."):
        raise SyncError("invalid_bundle")
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SyncError("invalid_bundle")
    if before.st_size > limit:
        raise SyncError("size_limit")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise SyncError("invalid_bundle")
        first = _bounded_fd_pass(fd, limit, deadline)
        middle = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (middle.st_size, middle.st_mtime_ns):
            raise SyncError("invalid_bundle")
        if deadline:
            deadline.check()
        os.lseek(fd, 0, os.SEEK_SET)
        second = _bounded_fd_pass(fd, limit, deadline)
        finished = os.fstat(fd)
        # Atomic replacement unlinks this old inode and can change ctime/nlink;
        # its stable content remains valid. Only content-size/mtime must match.
        if ((after.st_size, after.st_mtime_ns) != (finished.st_size, finished.st_mtime_ns)
                or first != second):
            raise SyncError("invalid_bundle")
        if deadline:
            deadline.check()
        return first
    finally:
        os.close(fd)


def read_file(path, limit, deadline=None):
    path = Path(path)
    directory = open_directory(path.parent)
    try:
        return read_at(directory, path.name, limit, deadline)
    finally:
        os.close(directory)


@contextmanager
def lock(directory, name):
    """Never truncate, unlink, replace, or steal the persistent lock inode."""
    fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise SyncError("publication_failed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy() from None
        yield
    finally:
        os.close(fd)


def atomic_write(directory, name, raw, deadline, *, mode=0o640):
    """Each boundary is deadline checked. Only this call's unique temp is cleaned."""
    deadline.check()
    if name not in ("current.json", "sync_health.json", "receipt.json"):
        raise SyncError("publication_failed")
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SyncError("publication_failed")
    except FileNotFoundError:
        pass
    temporary = "." + name + "." + uuid.uuid4().hex + ".tmp"
    fd = None
    renamed = False
    try:
        deadline.check()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=directory)
        remaining = memoryview(raw)
        while remaining:
            deadline.check()
            written = os.write(fd, remaining[:65_536])
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        deadline.check()
        os.fsync(fd)
        os.close(fd)
        fd = None
        deadline.check()
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        renamed = True
        try:
            deadline.check()
            os.fsync(directory)
        except (SyncError, OSError) as exc:
            raise PublicationUncertain(exc.code if isinstance(exc, SyncError) else "publication_failed") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not renamed:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                # Best effort only: cleanup must not obscure the primary failure.
                # Never broaden cleanup to somebody else's temporary files.
                pass


def validate_bundle(raw, now=None):
    """Wire invariants plus the exact existing shared semantic normalizer."""
    from dashboard.nng8_readonly import BundleUnavailable, normalize_bundle
    bundle = strict_json(raw)
    now = now or utcnow()
    try:
        if set(bundle) != {"schema", "captured_at", "documents", "runtime_modes"}:
            raise SyncError("invalid_bundle")
        if bundle["schema"] != "nng8.readonly-bundle.v1":
            raise SyncError("invalid_bundle")
        if not isinstance(bundle["documents"], dict) or set(bundle["documents"]) != DOCUMENTS:
            raise SyncError("invalid_bundle")
        if not isinstance(bundle["runtime_modes"], dict) or set(bundle["runtime_modes"]) != {"ledger", "phase2"}:
            raise SyncError("invalid_bundle")
        captured = instant(bundle["captured_at"])
        if (captured - now).total_seconds() > 5:
            raise SyncError("invalid_bundle")
        for row in bundle["documents"].values():
            if not isinstance(row, dict) or set(row) != {"payload", "sha256"} or not isinstance(row["payload"], dict):
                raise SyncError("invalid_bundle")
            if len(canonical(row["payload"])) > MAX_SOURCE:
                raise SyncError("size_limit")
            for key in ("sourceTime", "exportedAt"):
                if key in row["payload"] and row["payload"][key] is not None:
                    source = instant(row["payload"][key])
                    if (source - now).total_seconds() > 300:
                        raise SyncError("invalid_bundle")
        for row in bundle["runtime_modes"].values():
            if not isinstance(row, dict) or set(row) != {"payload", "sha256"}:
                raise SyncError("invalid_bundle")
            payload = row["payload"]
            if not isinstance(payload, dict) or set(payload) != {"mode", "observed_at"}:
                raise SyncError("invalid_bundle")
            if payload["mode"] not in MODES or instant(payload["observed_at"]) != captured:
                raise SyncError("invalid_bundle")
        # A returned 503 / unavailable is valid business evidence and MUST publish.
        normalize_bundle(bundle, now=now)
        _validate_source_times(bundle, now)  # Explicit nested timestamps must parse.
        return bundle
    except (BundleUnavailable, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        if isinstance(exc, SyncError):
            raise
        raise SyncError("invalid_bundle") from None


def source_times(bundle):
    """Only stable source identities; array positions never establish identity."""
    result = {}
    for name, row in bundle["documents"].items():
        payload = row["payload"]
        if payload.get("sourceTime") is not None:
            result[(name,)] = instant(payload["sourceTime"])
        if name == "delivery" and isinstance(payload.get("layers"), dict):
            for layer in ("seal", "relay", "telegram"):
                value = payload["layers"].get(layer)
                if isinstance(value, dict) and value.get("sourceTime") is not None:
                    result[(name, "layers", layer)] = instant(value["sourceTime"])
    return result


def _validate_source_times(bundle, now):
    pending = [((), bundle["documents"])]
    while pending:
        path, item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "sourceTime" and value is not None:
                    if (instant(value) - now).total_seconds() > 300:
                        raise SyncError("invalid_bundle")
                elif isinstance(value, (dict, list)):
                    pending.append((path + (key,), value))
        elif isinstance(item, list):
            pending.extend((path + (str(index),), value) for index, value in enumerate(item))
