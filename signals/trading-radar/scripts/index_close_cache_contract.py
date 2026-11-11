"""Read-only cache provenance checks shared by the writer and digest.

Receipts are local write evidence, not supplier attestations or tamper-proof logs.
Missing evidence is never reconstructed from mtime, Git, or the current clock.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import stat
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SYMBOLS = ("sh000001", "sh000300", "sz399001", "sz399006")
CACHE_SCHEMA = "quant.index-close-cache.v1"
RECEIPT_SCHEMA = "quant.index-close-write.v1"
WRITER = "scripts/update_index_close_cache.py"
MAX_BYTES = 128 * 1024


def receipt_path(cache: Path) -> Path:
    return cache.with_name(cache.name + ".receipt.json")


def lock_path(cache: Path) -> Path:
    return cache.with_name(cache.name + ".lock")


def pending_path(cache: Path) -> Path:
    return cache.with_name(cache.name + ".pending.json")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> tuple[bytes, dict]:
    """Bounded regular-file read; never create files or follow a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("not a single-link regular file")
        raw = b""
        while len(raw) <= MAX_BYTES:
            block = os.read(fd, min(8192, MAX_BYTES + 1 - len(raw)))
            if not block:
                break
            raw += block
        after = os.fstat(fd)
        if len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ValueError("cache too large or changed during read")
    finally:
        os.close(fd)
    def reject_constant(_value):
        raise ValueError("non-finite JSON constant")
    payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")
    return raw, payload


def canonical_date(raw) -> date:
    if not isinstance(raw, str):
        raise ValueError("missing date")
    parsed = date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("date not canonical")
    return parsed


def aware_time(raw) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(TZ)


def _digest(raw) -> bool:
    return isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{64}", raw) is not None


def valid_receipt(value: dict) -> dict:
    if value.get("schema_version") != RECEIPT_SCHEMA or value.get("status") != "committed":
        raise ValueError("invalid receipt schema/state")
    if value.get("writer") != WRITER or not _digest(value.get("writer_sha256")):
        raise ValueError("invalid writer identity")
    run = value.get("run_id")
    if not isinstance(run, str) or str(uuid.UUID(run)) != run:
        raise ValueError("invalid run identity")
    if not _digest(value.get("cache_sha256")):
        raise ValueError("invalid receipt hash")
    day = canonical_date(value.get("trade_date"))
    fetched = aware_time(value.get("fetched_at"))
    earliest = datetime.combine(day, time(15, 30), tzinfo=TZ)
    if fetched < earliest or value.get("source") not in {"tencent", "sina", "tencent+sina"}:
        raise ValueError("invalid receipt provenance")
    if "previous" not in value:
        raise ValueError("missing prior cache evidence")
    previous = value["previous"]
    if previous is not None:
        if not isinstance(previous, dict) or not _digest(previous.get("cache_sha256")):
            raise ValueError("invalid prior cache evidence")
        if previous.get("trade_date") is not None and canonical_date(previous["trade_date"]) > day:
            raise ValueError("receipt regresses previous date")
    return value


def integrity(raw: bytes | None, payload: dict, path: Path) -> dict:
    """Compare cache bytes to the last successful independent writer receipt."""
    # A prepared write without terminal cleanup is not permission to retry.
    try:
        pending_path(path).lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return {"status": "publication_unverified", "receipt": None}
    else:
        return {"status": "publication_incomplete", "receipt": None}
    try:
        _receipt_raw, receipt = read_json(receipt_path(path))
        valid_receipt(receipt)
    except FileNotFoundError:
        return {"status": "receipt_missing", "receipt": None}
    except (OSError, ValueError, TypeError, OverflowError):
        return {"status": "receipt_invalid", "receipt": None}
    if raw is None:
        return {"status": "cache_missing_or_invalid", "receipt": receipt}
    if sha256(raw) != receipt["cache_sha256"]:
        try:
            regressed = canonical_date(payload.get("trade_date")) < canonical_date(receipt["trade_date"])
        except (TypeError, ValueError):
            regressed = False
        return {"status": "cache_regressed" if regressed else "cache_mismatch", "receipt": receipt}
    if any(payload.get(key) != receipt[key] for key in ("trade_date", "source", "fetched_at")):
        return {"status": "receipt_mismatch", "receipt": receipt}
    return {"status": "verified", "receipt": receipt}


def metadata_issues(data: dict, now: datetime) -> list[str]:
    issues = []
    if data.get("schema_version") != CACHE_SCHEMA:
        issues.append("schema_missing_or_invalid")
    try:
        day = canonical_date(data.get("trade_date"))
        if data.get("expected_as_of") != day.isoformat() or data.get("stale") is not False:
            issues.append("cache_contract_invalid")
        fetched = aware_time(data.get("fetched_at"))
        if fetched < datetime.combine(day, time(15, 30), tzinfo=TZ) or fetched > now + timedelta(minutes=5):
            issues.append("fetched_at_out_of_range")
    except (ValueError, TypeError, OverflowError):
        issues.append("fetched_at_or_date_unverified")
    raw_sources = [r.get("source") for r in data.get("closes", []) if isinstance(r, dict)]
    sources = {s for s in raw_sources if isinstance(s, str)}
    if len(raw_sources) != len(SYMBOLS) or any(not isinstance(s, str) for s in raw_sources):
        issues.append("row_source_unverified")
    if not sources or not sources <= {"tencent", "sina"}:
        issues.append("row_source_unverified")
    aggregate = "+".join(name for name in ("tencent", "sina") if name in sources)
    if not aggregate or data.get("source") != aggregate:
        issues.append("source_unverified")
    return issues


def _read_state(path: Path, expected_as_of: str | None, now: datetime) -> dict:
    """No supplier calls, writes, lock-file creation, or implicit repair."""
    current = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    state = dict(available=False, values_present=False, provenance_verified=False,
                 quality="unavailable", trade_date=None, expected_as_of=expected_as_of,
                 stale=True, source="none", fetched_at=None, closes=[], issues=[])
    raw, data = None, {}
    try:
        raw, data = read_json(path)
        rows = data.get("closes")
        if not isinstance(rows, list) or len(rows) != len(SYMBOLS):
            raise ValueError("exact four closes required")
        parsed = {}
        day = canonical_date(data.get("trade_date")).isoformat()
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") not in SYMBOLS or row["symbol"] in parsed:
                raise ValueError("invalid market identity")
            if canonical_date(row.get("trade_date")).isoformat() != day:
                raise ValueError("mixed dates")
            close = row.get("close")
            if (isinstance(close, bool) or not math.isfinite(float(close))
                    or round(float(close), 4) <= 0):
                raise ValueError("invalid close")
            parsed[row["symbol"]] = dict(symbol=row["symbol"], close=round(float(close), 4),
                                         trade_date=day, source=row.get("source") or "unknown")
        state.update(values_present=True, trade_date=day, source=data.get("source") or "unknown",
                     fetched_at=data.get("fetched_at"), closes=[parsed[s] for s in SYMBOLS])
    except (OSError, ValueError, TypeError, OverflowError, KeyError):
        state["issues"].append("cache_missing_or_invalid")
    evidence = integrity(raw, data, path)
    state["integrity"] = evidence["status"]
    receipt = evidence["receipt"]
    state["last_success"] = ({key: receipt[key] for key in
        ("run_id", "trade_date", "cache_sha256", "fetched_at", "writer", "writer_sha256")}
        if receipt else None)
    if not state["values_present"]:
        if receipt:
            state["quality"] = "cache_evidence_mismatch"
        return state
    issues = metadata_issues(data, current)
    state["issues"].extend(issues)
    if evidence["status"] != "verified":
        state["issues"].append(evidence["status"])
    if evidence["status"] == "cache_regressed":
        state["quality"] = "cache_regressed"
    elif evidence["status"] not in {"verified", "receipt_missing"}:
        state["quality"] = "cache_evidence_mismatch"
    elif issues or evidence["status"] != "verified":
        state["quality"] = "legacy_unverified" if data.get("schema_version") is None else "unverified"
    else:
        state["provenance_verified"] = True
        state["stale"] = expected_as_of is None or state["trade_date"] != expected_as_of
        state["quality"] = "stale" if state["stale"] else "current"
        # "Available" means the provenanced values can be read, not that they are
        # current; freshness is carried separately by "stale"/"quality" so a
        # declared-stale digest stays structurally valid to the runtime observer.
        state["available"] = True
    return state


def _snapshot_identity(path: Path):
    values = []
    for p in (path, receipt_path(path), pending_path(path), lock_path(path)):
        try:
            info = p.lstat()
            values.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                           info.st_ctime_ns, info.st_mode, info.st_nlink))
        except FileNotFoundError:
            values.append(None)
    return tuple(values)


def _unverified_state(expected_as_of, reason):
    return dict(available=False, values_present=False, provenance_verified=False,
                quality="concurrent_update_unverified", integrity=reason, last_success=None,
                trade_date=None, expected_as_of=expected_as_of, stale=True,
                source="none", fetched_at=None, closes=[], issues=[reason])


def read_state(path: Path, expected_as_of: str | None, now: datetime) -> dict:
    """Read one consistent pair without creating a lock or retrying suppliers.

    A nonblocking shared lock excludes cooperating writers. The bounded identity
    check also covers the first-writer race when the lock does not yet exist.
    An unstable pair is never described as a proven cache regression.
    """
    for _attempt in range(2):
        fd = None
        try:
            try:
                fd = os.open(lock_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                pass
            if fd is not None:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return _unverified_state(expected_as_of, "unsafe_lock_file")
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return _unverified_state(expected_as_of, "publication_in_progress")
            before = _snapshot_identity(path)
            result = _read_state(path, expected_as_of, now)
            if before == _snapshot_identity(path):
                return result
        except OSError:
            return _unverified_state(expected_as_of, "snapshot_unreadable")
        finally:
            if fd is not None:
                os.close(fd)
    return _unverified_state(expected_as_of, "concurrent_update_unverified")
