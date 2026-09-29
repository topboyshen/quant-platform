#!/usr/bin/env python3
"""Retired legacy generator; explicit bounded inspection of saved metadata only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

OUT = Path("/home/ubuntu/signal-bus/out")
MAX_BYTES = 1024 * 1024


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return value if dt.date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _nonfinite(value):
    raise ValueError("non-finite number")


def inspect_saved(day):
    """No current status, report body, messages, URLs or credentials are emitted."""
    if _date(day) is None or OUT.is_symlink():
        raise ValueError("invalid inspection identity")
    directory = os.open(OUT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(f"research-digest-{day}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError("invalid saved artifact")
        chunks = []
        left = MAX_BYTES + 1
        while left:
            chunk = os.read(fd, min(left, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            raise ValueError("saved artifact changed during read")
    finally:
        os.close(fd)
    record = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    if not isinstance(record, dict) or _date(record.get("as_of")) != day:
        raise ValueError("saved artifact identity mismatch")
    produced = record.get("produced_at")
    try:
        stamp = dt.datetime.fromisoformat(produced.replace("Z", "+00:00")) if isinstance(produced, str) else None
        produced = stamp.isoformat() if stamp and stamp.tzinfo else None
    except ValueError:
        produced = None
    signals = record.get("signals")
    health = record.get("health")
    rd = health.get("research_data") if isinstance(health, dict) else None
    recorded_status = rd.get("status") if isinstance(rd, dict) else None
    statuses = {"inserted", "skipped_existing", "idempotent", "success", "deferred", "failed",
                "rejected", "temporary_failure", "usage_error", "unknown"}
    return {"status": "historical_metadata_only", "contract": "legacy_unverified",
            "as_of": day, "report_date": _date(record.get("target_date")), "produced_at": produced,
            "fileSha256": hashlib.sha256(raw).hexdigest(), "fileBytes": len(raw),
            "signalsCount": len(signals) if isinstance(signals, list) else None,
            "recordedFullSnapshotStatus": recorded_status if isinstance(recorded_status, str) and recorded_status in statuses else None,
            "currentStateConfirmed": False, "sent": 0,
            "limitations": ["Historical legacy metadata, not a verified delivery receipt or current full/core health.",
                            "Body, signals, URLs and credentials are not displayed; no generation or transmission performed."]}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        print("Legacy generation is disabled. --inspect YYYY-MM-DD reads saved historical metadata only; never sends.")
        return 0
    if len(args) != 2 or args[0] != "--inspect" or _date(args[1]) is None:
        print(json.dumps({"status": "held", "reason": "legacy_digest_path_disabled", "sent": 0}), file=sys.stderr)
        return 78
    try:
        result = inspect_saved(args[1])
    except (OSError, ValueError, UnicodeError, RecursionError):
        print(json.dumps({"status": "insufficient", "reason": "legacy_history_unreadable_or_invalid", "sent": 0}), file=sys.stderr)
        return 65
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
