#!/usr/bin/env python3
"""Allowlist-only nginx syslog classification; never persist a received message.

Raw datagrams exist only in process/kernel memory. Output schema has no place for
URI, host, header, request line, exception text, syslog tag or sender-provided time.
Unix datagram transport is best effort, NOT a lossless forensic audit channel.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import tempfile
import time

CANARY = b"QD_REDACTED_CANARY_V1"
MAX_DATAGRAM = 16384
SEVERITIES = ("emerg", "alert", "crit", "error", "warn", "notice", "info", "debug")
CLASSES = (
    "upstream_connect", "upstream_timeout", "upstream_response",
    "rate_limited", "request_too_large", "malformed_request", "client_timeout",
    "client_closed", "tls_error", "worker_lifecycle", "log_transport", "other",
    "unparseable", "truncated",
)
RULES = (
    ("upstream_connect", (b"connect() failed", b"no live upstreams")),
    ("upstream_timeout", (b"upstream timed out",)),
    ("upstream_response", (b"upstream prematurely closed", b"upstream sent",)),
    ("rate_limited", (b"limiting requests", b"delaying request")),
    ("request_too_large", (b"too long uri", b"too long header", b"too large body")),
    ("malformed_request", (b"client sent invalid", b"client sent plain http")),
    ("client_timeout", (b"client timed out",)),
    ("client_closed", (b"client prematurely closed", b"client closed connection")),
    ("tls_error", (b"ssl_do_handshake() failed", b"ssl_read() failed")),
    ("worker_lifecycle", (b"start worker", b"signal process started", b"reconfiguring",
                          b"gracefully shutting down", b"exiting", b"exit")),
    ("log_transport", (b"send() to syslog failed", b"syslog server")),
)


def classify(raw, truncated=False):
    """Return ONLY a fixed severity and fixed class; never return input text."""
    if truncated:
        return "unknown", "truncated"
    match = re.match(rb"^<([0-9]{1,3})>", raw)
    if match is None or int(match.group(1)) > 191:
        return "unknown", "unparseable"
    severity = SEVERITIES[int(match.group(1)) % 8]
    # Client context is not needed for classification and can contain fake words.
    message = raw[match.end():].lower().split(b", client:", 1)[0]
    for category, needles in RULES:
        if any(needle in message for needle in needles):
            return severity, category
    return severity, "other"


def _secure_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError("unsafe_output_directory")
    if info.st_mode & 0o077:
        raise RuntimeError("unsafe_output_permissions")
    return path


def _atomic_json(path, value):
    descriptor, tmp = tempfile.mkstemp(prefix=".redacted-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_status(directory):
    try:
        path = Path(directory) / "status.json"
        if path.is_symlink() or path.stat().st_size > 65536:
            return {}
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) and value.get("schema") == 1 else {}
    except (OSError, ValueError, TypeError):
        return {}


class Collector:
    def __init__(self, state_dir, max_summary_bytes=2 * 1024 * 1024):
        self.directory = _secure_directory(state_dir)
        previous = read_status(self.directory)
        self.started = time.time()
        prior_heartbeat = previous.get("heartbeat_unix")
        self.startup_gap = (max(0.0, self.started - prior_heartbeat)
                            if isinstance(prior_heartbeat, (int, float)) else None)
        self.max_summary_bytes = max_summary_bytes
        self.received = self.canaries = self.truncated = 0
        self.last_canary = self.last_event = None
        self.classes, self.severities, self.pending = Counter(), Counter(), Counter()

    def ingest(self, raw, truncated=False):
        if raw == CANARY and not truncated:
            self.canaries += 1
            self.last_canary = time.time()
            return
        severity, category = classify(raw, truncated)
        self.received += 1
        self.truncated += int(truncated)
        self.last_event = time.time()
        self.classes[category] += 1
        self.severities[severity] += 1
        self.pending[(severity, category)] += 1

    def status(self):
        return {
            "schema": 1, "collector_started_unix": self.started,
            "heartbeat_unix": time.time(), "received_total": self.received,
            "canary_total": self.canaries, "last_canary_unix": self.last_canary,
            "last_event_unix": self.last_event, "truncated_total": self.truncated,
            "class_counts": dict(self.classes), "severity_counts": dict(self.severities),
            "delivery_guarantee": "best_effort", "raw_messages_persisted": False,
            "startup_unobserved_seconds": self.startup_gap,
        }

    def flush(self):
        if self.pending:
            path = self.directory / "events.jsonl"
            if path.exists() and path.stat().st_size >= self.max_summary_bytes:
                if path.is_symlink():
                    raise RuntimeError("unsafe_summary_path")
                os.replace(path, self.directory / "events.previous.jsonl")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                now = time.time()
                for (severity, category), count in sorted(self.pending.items()):
                    stream.write(json.dumps({"schema": 1, "observed_unix": now,
                                             "severity": severity, "class": category,
                                             "count": count}, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.pending.clear()
        _atomic_json(self.directory / "status.json", self.status())


def health_check(socket_path, state_dir, timeout=2, max_age=3):
    """Prove active consumer progress, not merely that a socket file exists."""
    before = read_status(state_dir)
    start = time.monotonic()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
            probe.settimeout(min(timeout, 0.5))
            probe.sendto(CANARY, str(socket_path))
    except (OSError, ValueError):
        return {"ok": False, "reason": "collector_socket_unavailable"}
    while time.monotonic() - start < timeout:
        current = read_status(state_dir)
        heartbeat = current.get("heartbeat_unix")
        changed_instance = current.get("collector_started_unix") != before.get("collector_started_unix")
        progressed = current.get("canary_total", 0) > before.get("canary_total", 0)
        recent = isinstance(heartbeat, (int, float)) and 0 <= time.time() - heartbeat <= max_age
        if recent and (progressed or (changed_instance and current.get("canary_total", 0) > 0)):
            return {"ok": True, "reason": "collector_canary_acknowledged",
                    "delivery_guarantee": "best_effort"}
        time.sleep(min(0.05, max(0, timeout - (time.monotonic() - start))))
    return {"ok": False, "reason": "collector_canary_unacknowledged"}


def serve(args):
    collector = Collector(args.state_dir, args.max_summary_bytes)
    owned_inode = None
    if args.activated:
        if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
            raise RuntimeError("socket_activation_required")
        listener = socket.socket(fileno=3)
        if listener.family != socket.AF_UNIX or listener.type != socket.SOCK_DGRAM:
            raise RuntimeError("unexpected_activation_socket")
    else:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(args.socket)  # Refuse existing sockets; never unlink someone else's file.
        os.chmod(args.socket, 0o620)
        owned_inode = os.stat(args.socket).st_ino
    listener.settimeout(min(args.flush_interval, 0.2))
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_flush = time.monotonic()
    try:
        collector.flush()
        while not stopping:
            try:
                raw, _ancillary, flags, _address = listener.recvmsg(MAX_DATAGRAM)
                collector.ingest(raw, bool(flags & socket.MSG_TRUNC))
            except socket.timeout:
                pass
            if time.monotonic() - last_flush >= args.flush_interval:
                collector.flush()
                last_flush = time.monotonic()
        collector.flush()
    finally:
        listener.close()
        if owned_inode is not None:
            try:
                if os.stat(args.socket).st_ino == owned_inode:
                    os.unlink(args.socket)
            except FileNotFoundError:
                pass


def summarize_stdin(state_dir):
    """Sanitize bounded lines from nginx -t/control commands; never echo stderr."""
    collector = Collector(state_dir)
    while True:
        raw = sys.stdin.buffer.readline(MAX_DATAGRAM)
        if not raw:
            break
        collector.ingest(raw, len(raw) >= MAX_DATAGRAM)
    collector.flush()
    print(json.dumps({"ok": True, "control_output_classified": collector.received}))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    srv = sub.add_parser("serve")
    srv.add_argument("--socket", default="/run/approved-signals-log/syslog.sock")
    srv.add_argument("--state-dir", required=True)
    srv.add_argument("--activated", action="store_true")
    srv.add_argument("--flush-interval", type=float, default=1.0)
    srv.add_argument("--max-summary-bytes", type=int, default=2 * 1024 * 1024)
    chk = sub.add_parser("check")
    chk.add_argument("--socket", required=True)
    chk.add_argument("--state-dir", required=True)
    chk.add_argument("--timeout", type=float, default=2.0)
    chk.add_argument("--max-age", type=float, default=3.0)
    inp = sub.add_parser("stdin")
    inp.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            if not 0.01 <= args.flush_interval <= 10 or args.max_summary_bytes < 4096:
                raise RuntimeError("invalid_limits")
            serve(args)
        elif args.command == "check":
            if not 0 < args.timeout <= 10 or not 0 < args.max_age <= 60:
                raise RuntimeError("invalid_limits")
            result = health_check(args.socket, args.state_dir, args.timeout, args.max_age)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["ok"] else 2
        else:
            summarize_stdin(args.state_dir)
    except Exception:
        # Exception text can contain the offending filename/message. Never print it.
        print('{"ok":false,"reason":"redacted_collector_failed"}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
