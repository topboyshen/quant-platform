#!/usr/bin/env python3
"""Stale-only repair for a missing same-day Swing total_amount factor.

The repair is deliberately narrower than an EOD replay.  It only appends a
verified factor import and fills an empty ``macro_slice.total_amount``.  The
completed EOD run, its risk snapshot and per-target decisions stay immutable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sqlite3
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from radar.core.scan_scheduler import HolidayCalendar
from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError
from radar.secrets import merge_env_into_config_data
from radar.swing.evidence_store import FactorImportPreconditionChanged
from radar.swing.macro_snapshot import MacroSnapshotCollector
from radar.swing.store import SwingStore

BEIJING = ZoneInfo("Asia/Shanghai")
EARLIEST_REPAIR = time(15, 45)
LATEST_REPAIR = time(16, 0)
TENCENT_DATE_EVIDENCE = (
    MacroSnapshotCollector.TENCENT_TOTAL_AMOUNT_DATE_EVIDENCE
)
logger = logging.getLogger("swing_total_amount_repair")

SOURCE_REFS = {
    "tencent": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return merge_env_into_config_data(json.load(handle))


def _normalise_now(now: Optional[datetime]) -> datetime:
    current = now or datetime.now(BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    return current.astimezone(BEIJING)


def _no_op(reason: str, *, decision_date: str, dry_run: bool) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "no_op",
        "reason": reason,
        "decision_date": decision_date,
        "dry_run": dry_run,
    }


def _error(
    reason: str,
    *,
    decision_date: str,
    dry_run: bool,
    **details: Any,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "action": "error",
        "reason": reason,
        "decision_date": decision_date,
        "dry_run": dry_run,
        **details,
    }


def _database_path(config_path: str, config: Dict[str, Any]) -> Path:
    raw = Path(str(config["swing"].get("store_file", "data/swing_macro.db")))
    if raw.is_absolute():
        return raw
    return Path(config_path).resolve().parent / raw


def _read_eligibility(database: Path, decision_date: str) -> Dict[str, Any]:
    """Read the repair predicates without creating, migrating or chmodding DB."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        latest = connection.execute(
            "SELECT run_id,status,reason_codes FROM swing_eod_run "
            "WHERE decision_date=? ORDER BY id DESC LIMIT 1",
            (decision_date,),
        ).fetchone()
        if latest is None:
            return {"eligible": False, "reason": "eod_run_missing"}
        if str(latest["status"] or "") != "partial":
            return {"eligible": False, "reason": "latest_eod_not_partial"}
        try:
            reason_codes = json.loads(latest["reason_codes"] or "[]")
        except (TypeError, ValueError):
            reason_codes = []
        if not isinstance(reason_codes, list) or "macro_data_partial" not in reason_codes:
            return {"eligible": False, "reason": "macro_data_partial_absent"}
        slices = connection.execute(
            "SELECT total_amount FROM macro_slice WHERE date=?", (decision_date,)
        ).fetchall()
        if len(slices) != 1:
            return {"eligible": False, "reason": "macro_slice_missing"}
        if slices[0]["total_amount"] is not None:
            return {"eligible": False, "reason": "total_amount_present"}
        return {
            "eligible": True,
            "reason": "eligible_partial_macro_data",
            "run_id": str(latest["run_id"]),
        }


def _collect_live_total_amount(decision_date: str) -> Dict[str, Any]:
    """Collect Tencent total_amount with its two-index qfqday date proof.

    The generic EOD collector may fall back to snapshot sources that currently
    label ``collection_date`` as ``observed_date``.  That is useful for live
    degradation, but not strong enough for an accepted immutable repair.
    """
    # Tencent turnover collection does not access the store.  Keeping it absent
    # here guarantees qualification and dry-run cannot initialize the DB.
    collector = MacroSnapshotCollector(None, logger)  # type: ignore[arg-type]
    collector._collection_date = decision_date
    collector._total_amount_observed_date = None
    collector._total_amount_date_evidence = None
    value = collector._tencent_total_amount()
    observed_date = collector._total_amount_observed_date
    date_evidence = collector._total_amount_date_evidence
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float("nan")
    stale = collector._observation_is_stale(
        "total_amount", observed_date, decision_date
    )
    usable = (
        math.isfinite(number)
        and MacroSnapshotCollector.is_valid_factor_value("total_amount", number)
        and observed_date == decision_date
        and not stale
    )
    status = {
        "available": usable,
        "source": "tencent",
        "observed_date": observed_date,
        "stale": stale,
        "anomalous": value is not None
        and not MacroSnapshotCollector.is_valid_factor_value("total_amount", value),
        "message": "" if usable else "腾讯成交额缺失、越界或真实交易日不匹配",
        "date_evidence": date_evidence,
    }
    return {
        "usable": usable,
        "value": round(number, 2) if usable else None,
        "source": "tencent",
        "status": status,
    }


def _result_is_usable(result: Dict[str, Any], decision_date: str) -> bool:
    """Revalidate the collector contract at the write boundary."""
    status = result.get("status")
    if not isinstance(status, dict):
        return False
    try:
        value = float(result.get("value"))
    except (TypeError, ValueError):
        return False
    source = str(result.get("source") or "")
    return bool(
        result.get("usable") is True
        and MacroSnapshotCollector.is_valid_factor_value("total_amount", value)
        and source in SOURCE_REFS
        and status.get("available") is True
        and status.get("source") == source
        and status.get("observed_date") == decision_date
        and status.get("date_evidence") == TENCENT_DATE_EVIDENCE
        and not status.get("stale")
        and not status.get("anomalous")
    )


def _build_import(
    *,
    decision_date: str,
    run_id: str,
    collected_at: datetime,
    result: Dict[str, Any],
) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
    source = str(result["source"])
    source_ref = SOURCE_REFS.get(source)
    if not source_ref:
        raise ValueError(f"未登记的成交额来源: {source or 'unknown'}")
    source_market_time = datetime.combine(
        collected_at.date(), time(15, 0), tzinfo=BEIJING
    ).isoformat()
    components = {
        "total_amount_yi": float(result["value"]),
        "unit": "CNY 100 million",
        "collection_status": result.get("status") or {},
        "repair_policy": "same_day_partial_macro_data_stale_only",
    }
    core = {
        "factor_name": "total_amount",
        "observation_date": decision_date,
        "value": float(result["value"]),
        "source": source,
        "source_ref": source_ref,
        "source_market_time": source_market_time,
        "available_at": collected_at.isoformat(),
        "components": components,
    }
    observation = {**core, "checksum": _sha256(core)}
    observations = [observation]
    # One deterministic batch identity per market day closes the race between
    # the 15:47 and 15:57 invocations (or an operator starting a duplicate).
    batch_id = f"swing-total-amount-repair-{decision_date}"
    batch = {
        "batch_id": batch_id,
        "factor_name": "total_amount",
        "source": source,
        "source_ref": source_ref,
        "requested_start": decision_date,
        "requested_end": decision_date,
        "observed_at": collected_at.isoformat(),
        "record_count": 1,
        "checksum": _sha256(observations),
        "validation_status": "accepted",
        "details": {
            "import_kind": "same_day_stale_only_repair",
            "repair_policy": "same_day_partial_macro_data_stale_only",
            "decision_date": decision_date,
            "qualifying_run_id": run_id,
            "mutates_eod_evidence": False,
            "recomputes_strategy": False,
            "sends_notification": False,
        },
    }
    return batch, observations


def run(
    config_path: str,
    *,
    now: Optional[datetime] = None,
    after_fetch_now: Optional[datetime] = None,
    dry_run: bool = False,
    fetch_total_amount: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    current = _normalise_now(now)
    decision_date = current.date().isoformat()
    if current.weekday() >= 5 or HolidayCalendar.is_holiday(current):
        return _no_op(
            "non_trading_day", decision_date=decision_date, dry_run=dry_run
        )
    if current.time() < EARLIEST_REPAIR:
        return _no_op("too_early", decision_date=decision_date, dry_run=dry_run)
    if current.time() >= LATEST_REPAIR:
        return _no_op("too_late", decision_date=decision_date, dry_run=dry_run)

    config = _read_config(config_path)
    database = _database_path(config_path, config)
    if not database.is_file():
        return _error(
            "database_missing", decision_date=decision_date, dry_run=dry_run
        )
    try:
        eligibility = _read_eligibility(database, decision_date)
    except (OSError, sqlite3.Error) as exc:
        return _error(
            "eligibility_read_failed",
            decision_date=decision_date,
            dry_run=dry_run,
            error_type=type(exc).__name__,
        )
    if not eligibility["eligible"]:
        return _no_op(
            str(eligibility["reason"]),
            decision_date=decision_date,
            dry_run=dry_run,
        )

    fetch = fetch_total_amount or _collect_live_total_amount
    collected = fetch(decision_date)
    post_fetch = _normalise_now(
        after_fetch_now
        if after_fetch_now is not None
        else now
        if now is not None
        else None
    )
    if post_fetch.date().isoformat() != decision_date:
        return _error(
            "date_changed_after_fetch",
            decision_date=decision_date,
            dry_run=dry_run,
            observed_at=post_fetch.isoformat(),
        )
    if post_fetch.weekday() >= 5 or HolidayCalendar.is_holiday(post_fetch):
        return _error(
            "non_trading_day_after_fetch",
            decision_date=decision_date,
            dry_run=dry_run,
            observed_at=post_fetch.isoformat(),
        )
    if not (EARLIEST_REPAIR <= post_fetch.time() < LATEST_REPAIR):
        return _error(
            "window_closed_after_fetch",
            decision_date=decision_date,
            dry_run=dry_run,
            observed_at=post_fetch.isoformat(),
        )
    if not _result_is_usable(collected, decision_date):
        return _error(
            "source_unusable",
            decision_date=decision_date,
            dry_run=dry_run,
            source=str(collected.get("source") or ""),
            source_status=collected.get("status") or {},
        )
    batch, observations = _build_import(
        decision_date=decision_date,
        run_id=str(eligibility["run_id"]),
        collected_at=post_fetch,
        result=collected,
    )
    summary: Dict[str, Any] = {
        "ok": True,
        "action": "would_repair" if dry_run else "repaired",
        "reason": "eligible_partial_macro_data",
        "decision_date": decision_date,
        "run_id": str(eligibility["run_id"]),
        "batch_id": batch["batch_id"],
        "value": observations[0]["value"],
        "source": batch["source"],
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    store = SwingStore(str(database))
    try:
        written = store.record_factor_import(batch=batch, observations=observations)
    except FactorImportPreconditionChanged as exc:
        return {
            **_no_op(
                "precondition_changed",
                decision_date=decision_date,
                dry_run=dry_run,
            ),
            "precondition_reason": exc.reason,
        }
    except sqlite3.IntegrityError:
        # A deterministic batch id makes a concurrent duplicate harmless.  Only
        # suppress it after confirming that the intended empty field was filled.
        current_slice = store.get_for_date(decision_date)
        if current_slice is not None and current_slice.total_amount is not None:
            return _no_op(
                "already_repaired", decision_date=decision_date, dry_run=dry_run
            )
        raise
    if written != {"inserted": 1, "filled": 1, "conflicts": 0}:
        raise RuntimeError(f"成交额修复写入结果异常: {written}")
    summary.update(written)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stale-only repair for today's missing Swing total_amount"
    )
    parser.add_argument("--config", default="config.swing.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        lock = SingleInstanceLock("radar-swing-eod").acquire()
    except SingleInstanceLockError as exc:
        logger.error("Swing EOD/成交额修复锁冲突: %s", exc)
        raise SystemExit(75) from exc
    try:
        summary = run(args.config, dry_run=args.dry_run)
    finally:
        lock.release()
    logger.info("成交额 stale-only 修复: %s", json.dumps(summary, ensure_ascii=False))
    if not summary.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
