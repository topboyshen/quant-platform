#!/usr/bin/env python3
"""Fixed six-number v7 metric bridge; no arbitrary file/label forwarding.

The CLI's paths are constants. UID/GID bindings are explicit P-installation
identities, not guessed future user IDs. Imports and --help do no publishing.
Only the root CLI is a production entry point; injected paths belong to tests.
"""
import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

SOURCE_DIR = "/var/lib/qd-log-health-metrics"
DEST_DIR = "/home/ubuntu/trading-radar/data/prometheus-textfile"
NAME = "qd_redacted_log.prom"
LOCK_NAME = ".qd_redacted_log.publisher.lock"
MAX_BYTES = 4096
FIELDS = (
    "qd_redacted_log_health_ok",
    "qd_redacted_log_probe_unixtime",
    "qd_redacted_log_notification_acknowledged",
    "qd_redacted_log_notification_delivered",
    "qd_redacted_log_notification_last_attempt_unixtime",
    "qd_redacted_log_notification_failure_total",
)
NUMBER = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class Refusal(ValueError):
    pass


def parse_metrics(raw, now):
    """No HELP/TYPE, labels, duplicate fields, sample timestamps or raw text."""
    if not raw or len(raw) > MAX_BYTES or not math.isfinite(now) or now < 0:
        raise Refusal("invalid_metric_snapshot")
    try:
        text = raw.decode("ascii")
        values = {}
        for line in text.splitlines():
            parts = line.split(" ")
            if len(parts) != 2 or parts[0] not in FIELDS or parts[0] in values:
                raise Refusal("metric_schema_refused")
            if not NUMBER.fullmatch(parts[1]) or len(parts[1]) > 40:
                raise Refusal("metric_value_refused")
            values[parts[0]] = Decimal(parts[1])
    except (UnicodeError, InvalidOperation):
        raise Refusal("metric_schema_refused") from None
    if set(values) != set(FIELDS) or len(text.splitlines()) != len(FIELDS):
        raise Refusal("metric_schema_refused")
    health, probe, ack, delivered, attempt, failures = (values[x] for x in FIELDS)
    if any(x not in (0, 1) for x in (health, ack, delivered)):
        raise Refusal("metric_value_refused")
    if (delivered > ack or attempt > probe or max(probe, attempt) > Decimal("1e12")
            or failures != failures.to_integral_value()
            or failures > Decimal(2**53 - 1)):
        raise Refusal("metric_consistency_refused")
    age = Decimal(str(now)) - probe
    if age < -5 or age > 90:
        raise Refusal("producer_snapshot_not_fresh")
    # Preserve the producer clock value. Copy time/mtime never replaces it.
    canonical = []
    for name in FIELDS:
        value = format(values[name], "f")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        canonical.append(name + " " + value + "\n")
    return "".join(canonical).encode("ascii")


def open_directory(path):
    """Walk every component with O_NOFOLLOW; never enter symlinked parents."""
    parts = Path(path).parts
    if not parts or parts[0] != "/" or any(p in (".", "..") for p in parts):
        raise Refusal("unsafe_directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate_directory(descriptor, uid, gid, *, shared=False):
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid
            or info.st_mode & 0o002 or (not shared and info.st_mode & 0o022)):
        raise Refusal("directory_identity_refused")
    return info


def regular(info, uid, gid=None, *, mode=None):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
            or (gid is not None and info.st_gid != gid)
            or info.st_mode & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise Refusal("file_identity_refused")


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_consistent(descriptor, uid, gid):
    fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    try:
        before = os.fstat(fd)
        regular(before, uid, gid, mode=0o644)
        if before.st_size > MAX_BYTES:
            raise Refusal("metric_snapshot_too_large")
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            block = os.read(fd, min(1024, MAX_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        named = os.stat(NAME, dir_fd=descriptor, follow_symlinks=False)
        if (len(raw) > MAX_BYTES or len(raw) != before.st_size
                or fingerprint(before) != fingerprint(after)
                or fingerprint(before) != fingerprint(named)):
            raise Refusal("source_changed_during_read")
        return bytes(raw)
    finally:
        os.close(fd)


def validate_target(descriptor, publisher_uid):
    try:
        info = os.stat(NAME, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    regular(info, publisher_uid, mode=0o644)
    return fingerprint(info)


def _publish(source_dir, dest_dir, *, source_uid, source_gid, dest_uid,
             dest_gid, publisher_uid=None, now=None):
    """Internal test seam. Production main supplies only the two fixed paths."""
    if publisher_uid is None:
        publisher_uid = os.geteuid()
    source_fd = dest_fd = lock_fd = temp_fd = None
    temporary = None
    renamed = False
    try:
        source_fd = open_directory(source_dir)
        dest_fd = open_directory(dest_dir)
        validate_directory(source_fd, source_uid, source_gid)
        validate_directory(dest_fd, dest_uid, dest_gid, shared=True)
        lock_fd = os.open(LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                          0o600, dir_fd=dest_fd)
        regular(os.fstat(lock_fd), publisher_uid, mode=0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("publisher_busy") from None
        old_target = validate_target(dest_fd, publisher_uid)
        raw = read_consistent(source_fd, source_uid, source_gid)
        output = parse_metrics(raw, time.time() if now is None else now)
        temporary = ".qd_redacted_log." + secrets.token_hex(12) + ".tmp"
        temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=dest_fd)
        offset = 0
        while offset < len(output):
            count = os.write(temp_fd, output[offset:])
            if count <= 0:
                raise Refusal("publisher_write_failed")
            offset += count
        os.fchmod(temp_fd, 0o644)
        os.fsync(temp_fd)
        # The destination is shared. Refuse an unexpected replacement rather
        # than overwriting a new owner/symlink written while input was read.
        if validate_target(dest_fd, publisher_uid) != old_target:
            raise Refusal("destination_changed_during_publish")
        os.replace(temporary, NAME, src_dir_fd=dest_fd, dst_dir_fd=dest_fd)
        renamed = True
        os.fsync(dest_fd)
        return {"ok": True, "published": True, "fields": len(FIELDS),
                "producer_timestamp_preserved": True}
    except Exception as error:
        reason = str(error) if isinstance(error, Refusal) else "publisher_failed"
        return {"ok": False, "published": False, "reason": reason,
                "update_may_have_completed": renamed}
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temporary is not None and not renamed and dest_fd is not None:
            try:
                os.unlink(temporary, dir_fd=dest_fd)
            except FileNotFoundError:
                pass
        for descriptor in (lock_fd, dest_fd, source_fd):
            if descriptor is not None:
                os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser()
    # These are identity expectations only, NEVER source/destination paths.
    for key in ("source-uid", "source-gid", "dest-uid", "dest-gid"):
        parser.add_argument("--" + key, type=int, required=True)
    args = parser.parse_args(argv)
    identities = (args.source_uid, args.source_gid, args.dest_uid, args.dest_gid)
    if os.geteuid() != 0 or any(value < 0 for value in identities):
        print('{"ok":false,"reason":"root_and_bound_identities_required"}')
        return 2
    result = _publish(SOURCE_DIR, DEST_DIR, source_uid=args.source_uid,
                      source_gid=args.source_gid, dest_uid=args.dest_uid,
                      dest_gid=args.dest_gid)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
