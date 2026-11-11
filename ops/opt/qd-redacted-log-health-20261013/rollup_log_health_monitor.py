#!/usr/bin/env python3
"""Independent canary/metrics/notifier adapter; never reads raw nginx logs.

No external recipient is embedded. A root-owned Unix-socket notification bridge
must implement this contract and be verified against an actual existing channel
before production is approved. Accepted != delivered. Missing/broken bridge is
visible in both the exit status and metrics, even when the collector is dead.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import time

from redacted_log_collector import health_check
from notification_bridge import validate_event

UNITS = ("qd-redacted-log", "qd-redacted-log-health", "qd-review-maintenance")
REASONS = ("collector_socket_unavailable", "collector_canary_unacknowledged",
           "collector_canary_acknowledged", "unit_failed", "probe_failed")


def finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def valid_notification(value):
    if (not isinstance(value, dict) or type(value.get("accepted")) is not bool
            or type(value.get("delivered")) is not bool
            or value["delivered"] and not value["accepted"]):
        return False
    allowed = {"accepted", "delivered", "reason", "receipt_sha256"}
    if set(value) - allowed:
        return False
    if "reason" in value and value["reason"] not in (
            "notification_unconfirmed", "notification_accepted", "notification_delivered"):
        return False
    digest = value.get("receipt_sha256")
    return digest is None or (isinstance(digest, str) and len(digest) == 64
                              and all(char in "0123456789abcdef" for char in digest))


def read_state(path, maximum=16384):
    """Bounded owner-only regular file read; never follows a symbolic link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > maximum):
            raise ValueError("unsafe_monitor_state")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("unsafe_monitor_state")
        return json.loads(raw)
    finally:
        os.close(descriptor)


def valid_health_state(value, unit, timestamp):
    fields = {"schema", "healthy", "reason", "probe_unix", "last_attempt_unix",
              "event_id", "notification", "notification_failures", "event"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int
            or value["schema"] != 1 or type(value["healthy"]) is not bool
            or value["reason"] not in REASONS
            or not finite_time(value["probe_unix"]) or value["probe_unix"] > timestamp
            or not finite_time(value["last_attempt_unix"])
            or value["last_attempt_unix"] > value["probe_unix"]
            or type(value["notification_failures"]) is not int or value["notification_failures"] < 0
            or not valid_notification(value["notification"])):
        return False
    event = value["event"]
    if event is None:
        return (value["healthy"] is True and value["event_id"] == ""
                and value["last_attempt_unix"] == 0
                and value["notification"]["accepted"] is False
                and value["notification"]["delivered"] is False)
    try:
        validate_event(event)
    except (TypeError, ValueError):
        return False
    return (event["unit"] == unit and event["event_id"] == value["event_id"]
            and event["observed_unix"] <= value["probe_unix"]
            and event["status"] == ("resolved" if value["healthy"] else "firing"))


def atomic(path, raw, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("unsafe_output")
    fd, temporary = tempfile.mkstemp(prefix=".qd-health-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_state(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_health_state")
    return directory


def notify(socket_path, event, expected_uid, timeout=8):
    # Local bridge, explicit owner, bounded exchange, no retries here. All event
    # strings are fixed enum values or random IDs, never paths/exception text.
    failure = {"accepted": False, "delivered": False, "reason": "notification_unconfirmed"}
    deadline = time.monotonic() + timeout
    try:
        info = Path(socket_path).lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != expected_uid or info.st_mode & 0o007:
            return failure
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(.001, deadline - time.monotonic()))
            client.connect(str(socket_path))
            if hasattr(socket, "SO_PEERCRED"):
                _pid, uid, _gid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != expected_uid:
                    return failure
            client.sendall(json.dumps(event, separators=(",", ":")).encode() + b"\n")
            raw = bytearray()
            while b"\n" not in raw:
                client.settimeout(max(.001, deadline - time.monotonic()))
                if time.monotonic() >= deadline or len(raw) > 4096:
                    return failure
                part = client.recv(4096)
                if not part:
                    return failure
                raw.extend(part)
            ack = json.loads(raw.split(b"\n", 1)[0])
            if not isinstance(ack, dict) or ack.get("event_id") != event["event_id"]:
                return failure
            state = ack.get("state")
            receipt = ack.get("receipt")
            if state not in ("accepted", "delivered") or not isinstance(receipt, str) or not 1 <= len(receipt) <= 512:
                return failure
            return {"accepted": True, "delivered": state == "delivered",
                    "reason": "notification_" + state,
                    "receipt_sha256": hashlib.sha256(receipt.encode()).hexdigest()}
    except Exception:
        return failure


def observe(*, state_dir, metrics_file, collector_socket, collector_state,
            notify_socket, notify_uid, unit="qd-redacted-log-health", forced=False,
            probe_timeout=2, cooldown=300, now=None, incident_dir=None, health_result=None):
    if unit not in UNITS or type(cooldown) is not int or not 1 <= cooldown <= 3600:
        raise ValueError("invalid_health_request")
    timestamp = time.time() if now is None else now
    if not finite_time(timestamp):
        raise ValueError("invalid_health_clock")
    # The incident anchor is a separate durable store, shared by normal probe
    # and OnFailure. Forced never opens the normal monitor's state or lock.
    if incident_dir is None:
        incident_dir = safe_state(state_dir) / "incidents"
    incidents = safe_state(incident_dir)
    anchor = incidents / (unit + ".json")
    lock_fd = os.open(incidents / (unit + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = None if forced else safe_state(state_dir) / "state.json"
        previous = {}
        anchor_present = anchor.exists() or anchor.is_symlink()
        if anchor_present:
            previous = read_state(anchor)
            if not valid_health_state(previous, unit, timestamp):
                raise ValueError("incident_state_invalid")
        if state_path is not None:
            normal_present = state_path.exists() or state_path.is_symlink()
            if normal_present:
                normal = read_state(state_path)
                if not valid_health_state(normal, unit, timestamp):
                    raise ValueError("monitor_state_invalid")
                # Full valid v6 states can seed the new incident store. A
                # partial v6 write-ahead state is not silently migrated.
                if not anchor_present:
                    previous = normal
            elif anchor_present:
                raise ValueError("monitor_state_missing")
        try:
            health = ({"ok": False, "reason": "unit_failed"} if forced else
                      (health_result if health_result is not None else
                       health_check(collector_socket, collector_state, timeout=probe_timeout, max_age=3)))
        except Exception:
            health = {"ok": False, "reason": "probe_failed"}
        reason = health.get("reason") if health.get("reason") in REASONS else "probe_failed"
        healthy = health.get("ok") is True
        transition = previous.get("healthy") is not healthy
        last_attempt = previous.get("last_attempt_unix", 0)
        notification = previous.get("notification", {"accepted": False, "delivered": False})
        pending_receipt = bool(previous.get("event")) and notification.get("accepted") is not True
        due = transition or ((not healthy or pending_receipt) and timestamp - last_attempt >= cooldown)
        event_id = previous.get("event_id", "")
        failures = int(previous.get("notification_failures", 0))
        event = previous.get("event")
        if due and (not healthy or previous.get("healthy") is False or pending_receipt):
            if transition or not event:
                event_id = os.urandom(16).hex()
                event = {"schema": 1, "event_id": event_id, "unit": unit,
                         "status": "resolved" if healthy else "firing",
                         "reason": reason, "observed_unix": timestamp}
            # Persist the incident identity before asking the bridge to send.
            # A caller crash must not create a fresh event on the next probe.
            pending = {"schema": 1, "healthy": healthy, "reason": reason, "probe_unix": timestamp,
                       "event": event, "event_id": event_id,
                       "last_attempt_unix": timestamp, "notification_failures": failures,
                       "notification": {"accepted": False, "delivered": False}}
            atomic(anchor, (json.dumps(pending, sort_keys=True) + "\n").encode())
            if state_path is not None:
                atomic(state_path, (json.dumps(pending, sort_keys=True) + "\n").encode())
            notification = notify(notify_socket, event, notify_uid)
            if not valid_notification(notification):
                raise ValueError("notification_state_invalid")
            last_attempt = timestamp
            failures += int(not notification["accepted"])
        current = {"schema": 1, "healthy": healthy, "reason": reason,
                   "probe_unix": timestamp, "last_attempt_unix": last_attempt,
                   "event_id": event_id, "notification": notification,
                   "notification_failures": failures, "event": event}
        atomic(anchor, (json.dumps(current, sort_keys=True) + "\n").encode())
        if state_path is not None:
            atomic(state_path, (json.dumps(current, sort_keys=True) + "\n").encode())
        # File contains numeric gauges only. No event IDs, recipient names,
        # response contents, error text or source-controlled arbitrary labels.
        metrics = {
            "qd_redacted_log_health_ok": int(healthy),
            "qd_redacted_log_probe_unixtime": timestamp,
            "qd_redacted_log_notification_acknowledged": int(notification.get("accepted") is True),
            "qd_redacted_log_notification_delivered": int(notification.get("delivered") is True),
            "qd_redacted_log_notification_last_attempt_unixtime": last_attempt,
            "qd_redacted_log_notification_failure_total": failures,
        }
        atomic(metrics_file, "".join(name + " " + str(value) + "\n" for name, value in metrics.items()).encode(), 0o644)
        return {"ok": healthy, "reason": reason, "metrics_written": True,
                "notification_accepted": notification.get("accepted") is True,
                "notification_delivered": notification.get("delivered") is True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--incident-dir")
    parser.add_argument("--metrics-file", required=True)
    parser.add_argument("--collector-socket", required=True)
    parser.add_argument("--collector-state", required=True)
    parser.add_argument("--notify-socket", required=True)
    parser.add_argument("--notify-uid", type=int, default=0)
    parser.add_argument("--unit", choices=UNITS, default="qd-redacted-log-health")
    parser.add_argument("--forced", action="store_true")
    args = vars(parser.parse_args())
    try:
        result = observe(**args)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 2
    except Exception:
        print('{"ok":false,"reason":"monitor_failed","metrics_written":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

