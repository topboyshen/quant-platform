"""M4.4 stale-only repair of a missing same-day Swing total_amount (SHADOW).

The repair is deliberately narrower than an EOD replay.  It may only fill an empty
``signals.macro_slice.total_amount`` for a same-day run whose latest attempt is
``partial`` with ``macro_data_partial`` in ``reason_codes`` and whose macro slice row
already exists.  It never rewrites a present value, never touches the run, risk
snapshot, decisions or signals, and never sends a notification.

Each successful repair appends one deterministic factor-import record to
``macro_slice.collect_status.factor_imports`` (the qp shadow schema has no separate
factor-import table; the record is therefore content-addressed and idempotent by
``batch_id``).  The 15:47 and 15:57 timers may both run: the second invocation sees a
non-NULL total_amount and exits as ``total_amount_present``.  A row lock plus the
``total_amount IS NULL`` re-check closes the race with a concurrent operator.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.market import http
from qp.models.signals import EodRun, MacroSlice
from qp.signals.eod import CST, now_cst, trading_day_status
from qp.signals.macro import TOTAL_AMOUNT_BOUNDS, FactorStatus, fetch_tencent_total

EARLIEST_REPAIR = dt.time(15, 45)
LATEST_REPAIR = dt.time(16, 0)
REPAIR_POLICY = "same_day_partial_macro_data_stale_only"


def _normalise_now(now: dt.datetime | None) -> dt.datetime:
    current = now or now_cst()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    return current.astimezone(CST)


def _no_op(reason: str, *, decision_date: dt.date, dry_run: bool, **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "action": "no_op",
        "reason": reason,
        "decision_date": decision_date.isoformat(),
        "dry_run": dry_run,
        **extra,
    }


def _error(reason: str, *, decision_date: dt.date, dry_run: bool, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "action": "error",
        "reason": reason,
        "decision_date": decision_date.isoformat(),
        "dry_run": dry_run,
        **extra,
    }


def repair_eligibility(session: Session, decision_date: dt.date) -> dict[str, Any]:
    """Read the repair predicates without mutating anything."""
    latest = (
        session.execute(
            select(EodRun)
            .where(EodRun.decision_date == decision_date)
            .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if latest is None:
        return {"eligible": False, "reason": "eod_run_missing", "run_id": None}
    if str(latest.status or "") != "partial":
        return {"eligible": False, "reason": "latest_eod_not_partial", "run_id": latest.run_id}
    reasons = latest.reason_codes if isinstance(latest.reason_codes, list) else []
    if "macro_data_partial" not in {str(item) for item in reasons}:
        return {"eligible": False, "reason": "macro_data_partial_absent", "run_id": latest.run_id}
    macro = session.get(MacroSlice, decision_date)
    if macro is None:
        return {"eligible": False, "reason": "macro_slice_missing", "run_id": latest.run_id}
    if macro.total_amount is not None:
        return {"eligible": False, "reason": "total_amount_present", "run_id": latest.run_id}
    return {"eligible": True, "reason": "eligible_partial_macro_data", "run_id": latest.run_id}


def collect_repair_total(decision_date: dt.date, fetch_fn: Callable[..., Any] = http.fetch) -> dict[str, Any]:
    """Collect Tencent total_amount and require its real observation date to be today.

    The generic EOD collector may accept a stale Tencent snapshot for live degradation.
    That is not strong enough for an immutable repair, so this path mirrors the legacy
    ``_collect_live_total_amount`` proof: same-day observation only, bounded value.
    """
    try:
        value, observed = fetch_tencent_total(fetch_fn)
    except Exception as exc:  # noqa: BLE001 - source failure is a normal no-repair outcome
        status = FactorStatus(False, "tencent", f"tencent:{type(exc).__name__}", None)
        return {"usable": False, "value": None, "source": "tencent", "observed_date": None, "status": status.as_dict()}
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float("nan")
    low, high = TOTAL_AMOUNT_BOUNDS
    finite = math.isfinite(number)
    in_bounds = finite and low <= number <= high
    stale = observed != decision_date.isoformat()
    anomalous = not in_bounds
    usable = bool(in_bounds and not stale)
    if usable:
        message = ""
    elif not finite:
        message = "腾讯成交额缺失或非数值"
    elif not in_bounds:
        message = f"腾讯成交额越界（{number}）"
    else:
        message = f"腾讯成交额观测日不是当日（{observed or 'unknown'}）"
    status = FactorStatus(usable, "tencent", message, observed, stale=stale, anomalous=anomalous)
    return {
        "usable": usable,
        "value": round(number, 2) if usable else None,
        "source": "tencent",
        "observed_date": observed,
        "status": status.as_dict(),
    }


def _result_is_usable(result: dict[str, Any], decision_date: dt.date) -> bool:
    """Revalidate the collector contract at the write boundary."""
    status = result.get("status")
    if not isinstance(status, dict):
        return False
    try:
        value = float(result.get("value"))
    except (TypeError, ValueError):
        return False
    low, high = TOTAL_AMOUNT_BOUNDS
    return bool(
        result.get("usable") is True
        and math.isfinite(value)
        and low <= value <= high
        and str(result.get("source") or "") == "tencent"
        and status.get("available") is True
        and status.get("source") == "tencent"
        and status.get("observed_date") == decision_date.isoformat()
        and not status.get("stale")
        and not status.get("anomalous")
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def build_import_record(
    *, decision_date: dt.date, run_id: int, collected_at: dt.datetime, result: dict[str, Any]
) -> dict[str, Any]:
    """One deterministic factor-import record per market day."""
    core = {
        "batch_id": f"qp-total-amount-repair-{decision_date.isoformat()}",
        "factor_name": "total_amount",
        "observation_date": decision_date.isoformat(),
        "value": float(result["value"]),
        "source": "tencent",
        "observed_date": decision_date.isoformat(),
        "collected_at": collected_at.isoformat(),
        "qualifying_run_id": int(run_id),
        "repair_policy": REPAIR_POLICY,
        "mutates_eod_evidence": False,
        "recomputes_strategy": False,
        "sends_notification": False,
    }
    return {**core, "checksum": _sha256(core)}


def _write_repair(session: Session, *, decision_date: dt.date, value: float, record: dict[str, Any]) -> str:
    """Lock, re-check and fill only an empty total_amount.

    Returns ``repaired`` or a no-op reason.  ``total_amount IS NULL`` is enforced under
    ``FOR UPDATE`` so a concurrent 15:57 operator can never overwrite a value.
    """
    macro = (
        session.execute(select(MacroSlice).where(MacroSlice.decision_date == decision_date).with_for_update())
        .scalars()
        .first()
    )
    if macro is None:
        return "macro_slice_missing"
    if macro.total_amount is not None:
        return "already_repaired"
    collect = dict(macro.collect_status or {})
    imports = list(collect.get("factor_imports") or [])
    if not any(item.get("batch_id") == record["batch_id"] for item in imports if isinstance(item, dict)):
        imports.append(record)
    collect["factor_imports"] = imports
    total_status = dict(collect.get("total_amount") or {})
    total_status.update(
        {
            "available": True,
            "source": "tencent",
            "message": "",
            "observed_date": decision_date.isoformat(),
            "stale": False,
            "anomalous": False,
            "repair": {
                "batch_id": record["batch_id"],
                "value": record["value"],
                "collected_at": record["collected_at"],
                "checksum": record["checksum"],
            },
        }
    )
    collect["total_amount"] = total_status
    macro.total_amount = value
    macro.collect_status = collect
    session.flush()
    return "repaired"


def run_repair(
    session: Session,
    *,
    now: dt.datetime | None = None,
    post_fetch_now: dt.datetime | None = None,
    decision_date: dt.date | None = None,
    dry_run: bool = False,
    fetch_total: Callable[[dt.date], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = _normalise_now(now)
    decision_date = decision_date or current.date()
    if decision_date != current.date():
        return _no_op("not_same_day", decision_date=decision_date, dry_run=dry_run)
    is_open, _ = trading_day_status(session, decision_date)
    if not is_open:
        return _no_op("non_trading_day", decision_date=decision_date, dry_run=dry_run)
    if current.time() < EARLIEST_REPAIR:
        return _no_op("too_early", decision_date=decision_date, dry_run=dry_run)
    if current.time() >= LATEST_REPAIR:
        return _no_op("too_late", decision_date=decision_date, dry_run=dry_run)

    eligibility = repair_eligibility(session, decision_date)
    if not eligibility["eligible"]:
        return _no_op(
            str(eligibility["reason"]), decision_date=decision_date, dry_run=dry_run, run_id=eligibility.get("run_id")
        )
    run_id = int(eligibility["run_id"])

    fetch = fetch_total or collect_repair_total
    collected = fetch(decision_date)
    post_fetch = _normalise_now(post_fetch_now if post_fetch_now is not None else now if now is not None else None)
    if post_fetch.date() != decision_date:
        return _error(
            "date_changed_after_fetch", decision_date=decision_date, dry_run=dry_run, observed_at=post_fetch.isoformat()
        )
    is_open, _ = trading_day_status(session, post_fetch.date())
    if not is_open:
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

    record = build_import_record(decision_date=decision_date, run_id=run_id, collected_at=post_fetch, result=collected)
    value = float(collected["value"])
    summary: dict[str, Any] = {
        "ok": True,
        "action": "would_repair" if dry_run else "repaired",
        "reason": "eligible_partial_macro_data",
        "decision_date": decision_date.isoformat(),
        "run_id": run_id,
        "batch_id": record["batch_id"],
        "value": value,
        "source": "tencent",
        "dry_run": dry_run,
    }
    if dry_run:
        return summary
    action = _write_repair(session, decision_date=decision_date, value=value, record=record)
    if action != "repaired":
        return _no_op(action, decision_date=decision_date, dry_run=dry_run, run_id=run_id, batch_id=record["batch_id"])
    summary.update({"inserted": 1, "filled": 1, "conflicts": 0})
    return summary
