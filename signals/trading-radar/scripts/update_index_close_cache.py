#!/usr/bin/env python3
"""Atomically refresh the four-index close cache after A-share close."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.core.scan_scheduler import HolidayCalendar
from scripts.update_trading_calendar import _write_atomic
from scripts import index_close_cache_contract as contract

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CACHE_PATH = ROOT / "data" / "index_closes_latest.json"
PROBE_PATH = ROOT / "scripts" / "index_close_backend_probe.py"
BACKEND_CONTAINER = "radar-quantdinger-backend"
REQUIRED_SYMBOLS = ("sh000001", "sh000300", "sz399001", "sz399006")
OUTPUT_PREFIX = "INDEX_CLOSE_JSON="
SCHEMA_VERSION = "quant.index-close-cache.v1"
MARKET_CLOSE = wall_time(15, 0)
CLOSE_GRACE = timedelta(minutes=30)
FUTURE_CLOCK_SKEW = timedelta(minutes=5)
# Snapshot at module startup, not after a potentially long provider request.
WRITER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def expected_market_as_of(now: datetime | None = None) -> str:
    current = now or _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    cutoff = datetime.combine(current.date(), MARKET_CLOSE, tzinfo=SHANGHAI_TZ) + CLOSE_GRACE

    candidate = current.date()
    for _ in range(370):
        if candidate.weekday() < 5:
            probe = datetime.combine(candidate, wall_time(12, 0), tzinfo=SHANGHAI_TZ)
            if not HolidayCalendar.is_holiday(probe) and (
                candidate < current.date() or current >= cutoff
            ):
                return candidate.isoformat()
        candidate -= timedelta(days=1)
    raise RuntimeError("unable to resolve expected A-share trading date")


def _run_backend_probe(
    expected_as_of: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    source = PROBE_PATH.read_text(encoding="utf-8")
    completed = runner(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            BACKEND_CONTAINER,
            "python",
            "-",
            expected_as_of,
        ],
        input=source,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "backend probe failed")[-500:]
        raise RuntimeError(tail.strip())
    marker = next(
        (line for line in reversed(completed.stdout.splitlines()) if line.startswith(OUTPUT_PREFIX)),
        None,
    )
    if marker is None:
        raise RuntimeError("backend probe returned no structured result")
    payload = json.loads(marker[len(OUTPUT_PREFIX) :])
    if not isinstance(payload, dict):
        raise RuntimeError("backend probe result is not an object")
    return payload


def validate_probe_payload(payload: dict[str, Any], expected_as_of: str) -> list[dict[str, Any]]:
    if payload.get("expected_as_of") != expected_as_of:
        raise ValueError("probe expected_as_of mismatch")
    raw_closes = payload.get("closes")
    if not isinstance(raw_closes, list) or len(raw_closes) != len(REQUIRED_SYMBOLS):
        raise ValueError("probe must return exactly four closes")

    by_symbol: dict[str, dict[str, Any]] = {}
    for raw in raw_closes:
        if not isinstance(raw, dict):
            raise ValueError("close item is not an object")
        symbol = str(raw.get("symbol") or "")
        if symbol not in REQUIRED_SYMBOLS or symbol in by_symbol:
            raise ValueError("close symbols are missing, unexpected, or duplicated")
        if str(raw.get("trade_date") or "") != expected_as_of:
            raise ValueError(f"{symbol}: trade_date mismatch")
        close = float(raw.get("close"))
        if isinstance(raw.get("close"), bool) or not math.isfinite(close) or close <= 0:
            raise ValueError(f"{symbol}: close must be finite and positive")
        if round(close, 4) <= 0:
            raise ValueError(f"{symbol}: normalized close must remain positive")
        source = str(raw.get("source") or "")
        if source not in {"tencent", "sina"}:
            raise ValueError(f"{symbol}: unsupported source")
        by_symbol[symbol] = {
            "symbol": symbol,
            "close": round(close, 4),
            "trade_date": expected_as_of,
            "source": source,
        }
    return [by_symbol[symbol] for symbol in REQUIRED_SYMBOLS]


def _load_existing(path: Path) -> dict[str, Any]:
    try:
        return contract.read_json(path)[1]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _canonical_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == raw else None


def _refuse_future_cache(existing: dict[str, Any], expected_as_of: str) -> None:
    expected_date = _canonical_date(expected_as_of)
    if expected_date is None:
        raise RuntimeError("expected market date is not canonical ISO")
    existing_date = _canonical_date(existing.get("trade_date"))
    if existing_date is not None and existing_date > expected_date:
        raise RuntimeError("refusing to regress a future cache")


def build_cache(payload: dict[str, Any], expected_as_of: str, fetched_at: datetime) -> dict[str, Any]:
    closes = validate_probe_payload(payload, expected_as_of)
    sources = {item["source"] for item in closes}
    source = "+".join(name for name in ("tencent", "sina") if name in sources)
    return {
        "schema_version": SCHEMA_VERSION,
        "trade_date": expected_as_of,
        "expected_as_of": expected_as_of,
        "stale": False,
        "source": source,
        "fetched_at": fetched_at.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "closes": closes,
    }


def _fresh_cache_status(
    existing: dict[str, Any], expected_as_of: str, now: datetime
) -> dict[str, Any] | None:
    """Return an explicit no-op status only for a complete exact-date cache."""
    if (
        existing.get("schema_version") != SCHEMA_VERSION
        or existing.get("trade_date") != expected_as_of
        or existing.get("expected_as_of") != expected_as_of
        or existing.get("stale") is not False
    ):
        return None
    raw_fetched_at = existing.get("fetched_at")
    if not isinstance(raw_fetched_at, str):
        return None
    try:
        parsed_fetched_at = datetime.fromisoformat(raw_fetched_at)
    except ValueError:
        return None
    if parsed_fetched_at.tzinfo is None or parsed_fetched_at.utcoffset() is None:
        return None
    expected_date = _canonical_date(expected_as_of)
    if expected_date is None:
        return None
    current = now
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    local_fetched_at = parsed_fetched_at.astimezone(SHANGHAI_TZ)
    earliest = (
        datetime.combine(expected_date, MARKET_CLOSE, tzinfo=SHANGHAI_TZ)
        + CLOSE_GRACE
    )
    if (
        local_fetched_at < earliest
        or local_fetched_at > current + FUTURE_CLOCK_SKEW
    ):
        return None
    try:
        closes = validate_probe_payload(existing, expected_as_of)
    except (KeyError, OverflowError, TypeError, ValueError):
        return None
    sources = {item["source"] for item in closes}
    source = "+".join(name for name in ("tencent", "sina") if name in sources)
    if existing.get("source") != source:
        return None
    return {
        "status": "ok",
        "mode": "repair_noop",
        "trade_date": expected_as_of,
        "expected_as_of": expected_as_of,
        "stale": False,
        "source": source,
        "fetched_at": raw_fetched_at,
        "symbols": [item["symbol"] for item in closes],
    }


class CacheIntegrityError(RuntimeError):
    stage = "cache_evidence_check"

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class CacheCommitUncertain(RuntimeError):
    """Pair publication may have started; reconcile, never auto-rollback."""
    stage = "cache_or_receipt_commit"
    reason = "cache_receipt_commit_uncertain"


@contextmanager
def _writer_lock(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(contract.lock_path(output), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CacheIntegrityError("unsafe_lock_file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CacheIntegrityError("cache_writer_busy") from exc
        yield
    finally:
        os.close(fd)


def _checked_existing(output: Path, expected_as_of: str, now: datetime):
    # Called again inside the writer lock, not from a stale pre-lock snapshot.
    try:
        raw, existing = contract.read_json(output)
    except FileNotFoundError:
        raw, existing = None, {}
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise CacheIntegrityError("cache_unreadable_or_invalid") from exc
    evidence = contract.integrity(raw, existing, output)
    if evidence["status"] not in {"verified", "receipt_missing"}:
        raise CacheIntegrityError(evidence["status"])
    if evidence["receipt"] is not None:
        current = now.replace(tzinfo=SHANGHAI_TZ) if now.tzinfo is None else now.astimezone(SHANGHAI_TZ)
        if contract.aware_time(evidence["receipt"]["fetched_at"]) > current + FUTURE_CLOCK_SKEW:
            raise CacheIntegrityError("receipt_time_in_future")
        if contract.metadata_issues(existing, current):
            raise CacheIntegrityError("receipted_cache_contract_invalid")
        try:
            validate_probe_payload(existing, existing["trade_date"])
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise CacheIntegrityError("receipted_cache_values_invalid") from exc
    _refuse_future_cache(existing, expected_as_of)
    return raw, existing


def _collect(probe, expected_as_of, fetched_at):
    try:
        payload = probe(expected_as_of)
    except Exception as exc:
        exc.stage = "backend_probe"
        raise
    try:
        cache = build_cache(payload, expected_as_of, fetched_at)
        if contract.metadata_issues(cache, fetched_at):
            raise ValueError("new cache metadata invalid")
        return cache
    except Exception as exc:
        exc.stage = "same_date_source_validation"
        raise


def _commit_cache(output, cache, previous, existing):
    raw = (json.dumps(cache, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    prior_date = existing.get("trade_date")
    if _canonical_date(prior_date) is None:
        prior_date = None
    receipt = dict(schema_version=contract.RECEIPT_SCHEMA, status="committed",
                   writer=contract.WRITER, writer_sha256=WRITER_SHA256,
                   run_id=str(uuid.uuid4()), cache_sha256=contract.sha256(raw),
                   trade_date=cache["trade_date"], source=cache["source"], fetched_at=cache["fetched_at"],
                   previous=(dict(cache_sha256=contract.sha256(previous), trade_date=prior_date)
                             if previous is not None else None))
    contract.valid_receipt(receipt)
    try:
        # This is evidence of an attempted cache publication, not a DB backup.
        # Keep it on any interrupted write, even if the first cache had no old
        # receipt. Otherwise a later retry could silently erase that uncertainty.
        _write_atomic(contract.pending_path(output), dict(
            schema_version="quant.index-close-write-intent.v1", status="prepared",
            run_id=receipt["run_id"], next_cache_sha256=receipt["cache_sha256"]))
        _write_atomic(output, cache)
        actual, _data = contract.read_json(output)
        if actual != raw:
            raise ValueError("cache changed before receipt commit")
        _write_atomic(contract.receipt_path(output), receipt)
        contract.pending_path(output).unlink()
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        raise CacheCommitUncertain("cache/receipt publication incomplete; inspect evidence before retry") from exc


def refresh(
    output: Path = CACHE_PATH,
    *,
    now: datetime | None = None,
    source_smoke: bool = False,
    probe: Callable[[str], dict[str, Any]] = _run_backend_probe,
) -> dict[str, Any]:
    fetched_at = now or _now()
    expected_as_of = expected_market_as_of(fetched_at)
    if source_smoke:
        # In particular, do not create a lock or receipt on a read-only smoke.
        previous, existing = _checked_existing(output, expected_as_of, fetched_at)
        cache = _collect(probe, expected_as_of, fetched_at)
    else:
        with _writer_lock(output):
            previous, existing = _checked_existing(output, expected_as_of, fetched_at)
            cache = _collect(probe, expected_as_of, fetched_at)
            _commit_cache(output, cache, previous, existing)
    return {
        "status": "ok",
        "mode": "source_smoke" if source_smoke else "refresh",
        "trade_date": cache["trade_date"],
        "expected_as_of": cache["expected_as_of"],
        "stale": cache["stale"],
        "source": cache["source"],
        "fetched_at": cache["fetched_at"],
        "symbols": [item["symbol"] for item in cache["closes"]],
    }


def repair_if_stale(
    output: Path = CACHE_PATH,
    *,
    now: datetime | None = None,
    probe: Callable[[str], dict[str, Any]] = _run_backend_probe,
) -> dict[str, Any]:
    """Repair only an invalid/stale cache; never fetch for an exact-date LKG."""
    fetched_at = now or _now()
    expected_as_of = expected_market_as_of(fetched_at)
    with _writer_lock(output):
        previous, existing = _checked_existing(output, expected_as_of, fetched_at)
        fresh = _fresh_cache_status(existing, expected_as_of, fetched_at)
        evidence = contract.integrity(previous, existing, output)
        if fresh is not None and evidence["status"] == "verified":
            return fresh
        cache = _collect(probe, expected_as_of, fetched_at)
        _commit_cache(output, cache, previous, existing)
    repaired = dict(status="ok", trade_date=cache["trade_date"],
                    expected_as_of=expected_as_of, stale=False, source=cache["source"],
                    fetched_at=cache["fetched_at"], symbols=[r["symbol"] for r in cache["closes"]])
    repaired["mode"] = "repair"
    return repaired


def _failure_status(output: Path, exc: Exception, now: datetime) -> dict[str, Any]:
    existing = _load_existing(output)
    try:
        expected_as_of = expected_market_as_of(now)
    except Exception:
        expected_as_of = None
    return {
        "status": "error",
        "stale": True,
        "source": existing.get("source") or "none",
        "fetched_at": existing.get("fetched_at"),
        "checked_at": now.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "expected_as_of": expected_as_of,
        "cached_trade_date": existing.get("trade_date"),
        "error": type(exc).__name__,
        "stage": getattr(exc, "stage", "date_or_cache_validation"),
        "reason": getattr(exc, "reason", None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--source-smoke", action="store_true", help="fetch and validate without writing"
    )
    mode.add_argument(
        "--repair-if-stale",
        action="store_true",
        help="fetch only when the exact expected-date cache is not already fresh",
    )
    parser.add_argument("--output", type=Path, default=CACHE_PATH)
    args = parser.parse_args(argv)
    now = _now()
    try:
        if args.repair_if_stale:
            status = repair_if_stale(args.output, now=now)
        else:
            status = refresh(args.output, now=now, source_smoke=args.source_smoke)
    except Exception as exc:
        failure = _failure_status(args.output, exc, now)
        if args.repair_if_stale:
            failure["mode"] = "repair"
        elif args.source_smoke:
            failure["mode"] = "source_smoke"
        else:
            failure["mode"] = "refresh"
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
