"""M4.3 swing EOD orchestration (SHADOW).

One run per decision date/attempt: collect the macro slice, load/refresh the macro-risk
state, evaluate the frozen 40-ETF universe, apply the risk gate, derive per-target
decisions and the bounded run audit, then persist ``signals.*`` inside the qp database.
No notification is sent and no legacy file is written.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.http import fetch as http_fetch
from qp.market.kline import DailyBar, KlineFetcher
from qp.models.market import TradingDay
from qp.models.signals import (
    EodRun,
    MacroSlice,
    RiskSnapshot,
    RiskState,
    TargetDecision,
    TrendSignal,
)
from qp.signals.audit import audit_result
from qp.signals.indicators import Bar, volume_breakout, weekly_golden_cross
from qp.signals.macro import (
    RISK_STATE_MAX_AGE_SEC,
    collect_macro_slice,
    evaluate_risk,
    fetch_risk_values,
    merge_risk_metadata,
    risk_state_is_fresh,
)
from qp.signals.risk import gate_action
from qp.signals.universe import (
    DAILY_BREAKOUT_ENABLED,
    MA_LONG,
    TARGETS,
    VOLUME_RATIO,
    VOLUME_WINDOW,
    WEEKLY_MACD_ENABLED,
    Target,
)

CST = ZoneInfo("Asia/Shanghai")
UTC = dt.UTC
DEFAULT_OVERRIDE_FILE = "/var/lib/qp/radar_risk_override.json"
FACTORS = ("vix", "us_10y", "crude_oil")


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


# --------------------------------------------------------------------------- value objects
@dataclass
class Signal:
    code: str
    name: str
    signal_type: str
    period: str
    title: str
    detail: dict[str, Any]
    confidence: float


@dataclass
class TargetEvaluation:
    target: Target
    data_ok: bool
    data_date: dt.date | None = None
    reference_close: float | None = None
    source: str = ""
    reason: str = ""
    signals: list[Signal] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)


@dataclass
class StoragePlan:
    records: list[dict]
    blocked_count: int
    unique_allowed: list[Signal]
    notification_candidates: list[Signal]
    deduplicated: int


# --------------------------------------------------------------------------- calendar
def trading_day_status(session: Session, day: dt.date) -> tuple[bool, str]:
    """Return (is_open, source) with a conservative weekday fallback.

    A calendar that already covers ``day`` is authoritative: a missing row means the
    exchange is closed.  Only a calendar that ends before ``day`` falls back to weekday.
    """
    row = session.execute(select(TradingDay.is_open).where(
        TradingDay.origin == "qp", TradingDay.date == day)).first()
    if row is not None:
        return bool(row[0]), "qp_calendar"
    max_date = session.scalar(select(func.max(TradingDay.date)).where(TradingDay.origin == "qp"))
    if max_date is not None and max_date >= day:
        return False, "qp_calendar_no_row"
    return day.weekday() < 5, "weekday_fallback"


def previous_open_day(session: Session, day: dt.date) -> dt.date | None:
    value = session.scalar(select(func.max(TradingDay.date)).where(
        TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date < day))
    if value is not None:
        return value
    cursor = day - dt.timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= dt.timedelta(days=1)
    return cursor


# --------------------------------------------------------------------------- evaluation
def evaluate_target(target: Target, fetcher: Any, *, decision_date: dt.date,
                    previous_session: dt.date | None = None,
                    ma_long: int = MA_LONG, volume_window: int = VOLUME_WINDOW,
                    volume_ratio: float = VOLUME_RATIO,
                    daily_breakout_enabled: bool = DAILY_BREAKOUT_ENABLED,
                    weekly_macd_enabled: bool = WEEKLY_MACD_ENABLED) -> TargetEvaluation:
    try:
        result = fetcher.get(target.code, required_date=decision_date,
                             previous_session=previous_session)
    except Exception as exc:
        return TargetEvaluation(target, False, reason=f"fetch_failed:{type(exc).__name__}")
    attempts = list(getattr(result, "attempts", []) or [])
    bars: list[DailyBar] = list(result.bars or [])
    if not bars:
        reason = "data_date_mismatch" if getattr(result, "publication_lag", False) else "trend_data_unavailable"
        return TargetEvaluation(target, False, source=getattr(result, "source", ""),
                                reason=reason, attempts=attempts)
    last = bars[-1]
    reference = float(last.close)
    if last.date != decision_date:
        return TargetEvaluation(target, False, last.date, reference, getattr(result, "source", ""),
                                "data_date_mismatch", attempts=attempts)
    if len(bars) < ma_long + 5:
        # Legacy leaves ``last_close`` unset when the row minimum is not met.
        return TargetEvaluation(target, False, None, None, "", "insufficient_daily_rows",
                                attempts=attempts)

    converted = [Bar(b.date, b.close, b.open, b.high, b.low, b.volume) for b in bars]
    signals: list[Signal] = []
    daily_triggered, daily_diag = volume_breakout(
        converted, ma_long=ma_long, volume_window=volume_window, ratio=volume_ratio)
    if daily_breakout_enabled and daily_triggered:
        signals.append(Signal(
            target.code, target.name, "volume_breakout_ma60", "D",
            f"{target.name} 放量突破 MA{ma_long}",
            {
                "data_date": last.date.isoformat(),
                "close": round(float(daily_diag["close"]), 4),
                f"ma{ma_long}": round(float(daily_diag["ma"]), 4),
                "volume_ratio": daily_diag["volume_ratio"],
                "volume_threshold": volume_ratio,
            },
            0.55,
        ))
    weekly_triggered, weekly_diag = weekly_golden_cross(converted)
    if weekly_macd_enabled and weekly_triggered:
        signals.append(Signal(
            target.code, target.name, "weekly_macd_golden_cross", "W",
            f"{target.name} 周线 MACD 金叉",
            {
                "data_date": weekly_diag["data_date"],
                "dif": round(float(weekly_diag["dif"]), 4),
                "dea": round(float(weekly_diag["dea"]), 4),
                "weekly_close": round(float(weekly_diag["weekly_close"]), 4),
            },
            0.6,
        ))
    return TargetEvaluation(target, True, last.date, reference, getattr(result, "source", ""),
                            "", signals, attempts)


def evaluate_all(targets: Sequence[Target], fetcher: Any, *, decision_date: dt.date,
                 previous_session: dt.date | None = None, **kwargs: Any) -> list[TargetEvaluation]:
    return [evaluate_target(t, fetcher, decision_date=decision_date,
                            previous_session=previous_session, **kwargs) for t in targets]


# --------------------------------------------------------------------------- risk gate + decisions
def apply_risk_gate(evaluations: Sequence[TargetEvaluation], risk_level: str | None) -> set[int]:
    """Return ids of blocked signals; missing/unknown levels block everything."""
    blocked: set[int] = set()
    for evaluation in evaluations:
        for signal in evaluation.signals:
            if gate_action(risk_level, evaluation.target.sector, evaluation.target.name) == "blocked":
                blocked.add(id(signal))
    return blocked


def _signal_core(signal: Signal) -> dict[str, Any]:
    return {
        "signal_type": signal.signal_type,
        "period": signal.period,
        "title": signal.title,
        "confidence": signal.confidence,
        "detail": signal.detail,
    }


def signal_record(signal: Signal, *, blocked: bool, level: str, message: str) -> dict[str, Any]:
    detail = dict(signal.detail)
    detail["risk_gate"] = {"blocked": blocked, "level": level, "message": str(message)[:200]}
    return {
        "code": signal.code,
        "name": signal.name,
        "signal_type": signal.signal_type,
        "period": signal.period,
        "title": signal.title,
        "detail": detail,
        "confidence": signal.confidence,
    }


def signal_key(record: dict[str, Any]) -> tuple[str, str, str, str] | None:
    detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    data_date = detail.get("data_date") or record.get("data_date")
    if not data_date:
        return None
    return (str(record.get("code", "")), str(record.get("signal_type", "")),
            str(record.get("period", "")), str(data_date))


def target_decisions(evaluations: Sequence[TargetEvaluation], blocked_ids: set[int],
                     risk_level: str) -> list[dict[str, Any]]:
    rows = []
    for evaluation in evaluations:
        raw = evaluation.signals
        blocked_count = sum(id(signal) in blocked_ids for signal in raw)
        if not evaluation.data_ok:
            state, action, reason = "data_failed", "not_applicable", "trend_data_unavailable"
        elif not raw:
            state, action, reason = "no_trigger", "not_applicable", "signal_not_triggered"
        elif blocked_count == len(raw):
            state, action, reason = "blocked", "blocked", "macro_risk_blocked"
        else:
            state, action, reason = "allowed", "allowed", "signal_allowed"
        rows.append({
            "code": evaluation.target.code,
            "name": evaluation.target.name,
            "sector": evaluation.target.sector,
            "data_status": "ok" if evaluation.data_ok else "failed",
            "decision_state": state,
            "raw_trigger_count": len(raw),
            "raw_signals": [_signal_core(signal) for signal in raw],
            "risk_action": action,
            "risk_level": risk_level,
            "reason_code": reason,
            "reference_close": evaluation.reference_close,
            "data_date": evaluation.data_date,
            "validation_split": "live_oos",
        })
    return rows


def plan_storage(evaluations: Sequence[TargetEvaluation], blocked_ids: set[int], *,
                 risk_level: str, risk_message: str,
                 existing_other_dates: set[tuple[str, str, str, str]],
                 notified_keys: set[tuple[str, str, str, str]]) -> StoragePlan:
    records: list[dict] = []
    unique_allowed: list[Signal] = []
    notification_candidates: list[Signal] = []
    seen: set[tuple[str, str, str, str]] = set()
    blocked_count = 0
    for evaluation in evaluations:
        for signal in evaluation.signals:
            blocked = id(signal) in blocked_ids
            record = signal_record(signal, blocked=blocked, level=risk_level, message=risk_message)
            key = signal_key(record)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            # A different date already persisted this market bar: keep it out of today's
            # audit only when that identity was already notified. Unnotified rows migrate.
            if key is None or key not in existing_other_dates or key not in notified_keys:
                records.append(record)
                if blocked:
                    blocked_count += 1
            if not blocked:
                unique_allowed.append(signal)
                if key is None or key not in notified_keys:
                    notification_candidates.append(signal)
    return StoragePlan(records, blocked_count, unique_allowed, notification_candidates,
                       deduplicated=0)


# --------------------------------------------------------------------------- risk state
def _state_dict(level: str, message: str, **extra: Any) -> dict[str, Any]:
    state = {"level": level, "message": message, "vix": None, "us_10y": None, "crude_oil": None,
             "sources": {}, "observed_at": {}, "source_timestamps": {}, "available_at": {},
             "cached_fields": [], "missing_fields": list(FACTORS), "override": False}
    state.update(extra)
    return state


def load_risk_override(path: str | Path | None = None, *, now: float | None = None) -> dict[str, Any] | None:
    target = Path(path or os.getenv("QP_RISK_OVERRIDE_FILE", DEFAULT_OVERRIDE_FILE))
    now = time.time() if now is None else now
    try:
        if not target.exists():
            return None
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        expires_at = float(data.get("expires_at") or 0)
        if expires_at and now > expires_at:
            return None
        state = data.get("state") if isinstance(data.get("state"), dict) else data
        level = str(state.get("level") or "unavailable").lower()
        message = str(state.get("message") or state.get("reason") or "risk override")
        return _state_dict(level, message, vix=state.get("vix"), us_10y=state.get("us_10y"),
                           crude_oil=state.get("crude_oil"), override=True,
                           sources={"override": "file"}, missing_fields=[])
    except Exception:
        return None


def _latest_state_row(session: Session, *, now: float | None = None) -> RiskState | None:
    """Newest published state that is not dated in the future (small skew tolerated)."""
    now = time.time() if now is None else now
    horizon = dt.datetime.fromtimestamp(now + 1, tz=UTC)
    return session.scalar(select(RiskState).where(RiskState.observed_at <= horizon)
                          .order_by(RiskState.observed_at.desc(), RiskState.id.desc()).limit(1))


def load_risk_state(session: Session, *, now: float | None = None,
                    fetch_fn: Callable[..., Any] = http_fetch,
                    override_path: str | Path | None = None) -> dict[str, Any]:
    """Override -> fresh published state -> one live refresh -> unavailable (fail-closed)."""
    now = time.time() if now is None else now
    override = load_risk_override(override_path, now=now)
    if override is not None:
        return override

    row = _latest_state_row(session, now=now)
    previous = row.raw if row is not None and isinstance(row.raw, dict) else None
    if row is not None:
        observed = row.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if risk_state_is_fresh(observed.timestamp(), now=now, max_age=RISK_STATE_MAX_AGE_SEC):
            state = dict(previous or {})
            state.setdefault("level", row.level)
            state.setdefault("message", row.message)
            state.setdefault("override", False)
            return state

    try:
        fetched = fetch_risk_values(fetch_fn, now=now)
        state = evaluate_risk(fetched["values"], previous_state=previous, now=now)
        state = merge_risk_metadata(state, fetched)
        session.add(RiskState(
            observed_at=dt.datetime.fromtimestamp(now, tz=UTC),
            level=str(state["level"]),
            values={name: state.get(name) for name in FACTORS},
            sources=state.get("sources") or {},
            override=False,
            message=str(state.get("message") or ""),
            raw=state,
        ))
        session.flush()
        return state
    except Exception as exc:
        return _state_dict("unavailable", f"RISK_REFRESH_FAILED:{type(exc).__name__}")


# --------------------------------------------------------------------------- persistence
def create_run(session: Session, decision_date: dt.date, *, data_date: dt.date | None = None,
               status: str = "running") -> EodRun:
    attempt_no = int(session.scalar(select(func.max(EodRun.attempt_no)).where(
        EodRun.decision_date == decision_date)) or 0) + 1
    run = EodRun(decision_date=decision_date, attempt_no=attempt_no, data_date=data_date,
                 status=status, target_count=len(TARGETS), reason_codes=[], details={})
    session.add(run)
    session.flush()
    return run


def finish_run(run: EodRun, audit: dict[str, Any], *, details: dict[str, Any]) -> None:
    run.status = audit["status"]
    run.decision_outcome = audit["outcome"]
    run.target_count = audit["target_count"]
    run.evaluated_count = audit["evaluated_count"]
    run.failed_count = audit["failed_count"]
    run.signal_count = audit["signal_count"]
    run.allowed_signal_count = audit["allowed_signal_count"]
    run.blocked_signal_count = audit["blocked_signal_count"]
    run.reason_codes = list(audit["reason_codes"])
    run.details = details
    run.finished_at = now_utc()


_MACRO_FACTOR_COLUMNS = {
    "total_amount": ("total_amount",),
    "margin_balance": ("margin_balance",),
    "margin_growth_pct": ("margin_growth_pct",),
    "limit_ratio": ("limit_up", "limit_down", "limit_ratio"),
}


def _source_tokens(value: str) -> list[str]:
    return [token for token in str(value or "").split("+") if token and token != "none"]


def persist_macro(session: Session, decision_date: dt.date, macro: dict[str, Any]) -> bool:
    """Merge one live macro slice without erasing earlier successful factors.

    Mirrors legacy ``SwingStore.upsert_partial``: a retry whose individual upstream
    failed may not overwrite an earlier non-NULL value with NULL.  The failed status
    is kept under ``collect_status.last_collect_status``; a repair's
    ``collect_status.factor_imports`` record survives later partial collections.
    """
    values = {name: macro.get(name) for name in
              ("total_amount", "margin_balance", "margin_growth_pct", "limit_up", "limit_down")}
    if all(value is None for value in values.values()):
        return False
    incoming_status = macro.get("collect_status")
    incoming_status = incoming_status if isinstance(incoming_status, dict) else {}
    incoming_source = str(macro.get("source") or "none")
    existing = session.get(MacroSlice, decision_date, with_for_update=True)
    if existing is None:
        session.add(MacroSlice(
            decision_date=decision_date, total_amount=macro.get("total_amount"),
            margin_balance=macro.get("margin_balance"),
            margin_growth_pct=macro.get("margin_growth_pct"),
            limit_up=macro.get("limit_up"), limit_down=macro.get("limit_down"),
            limit_ratio=macro.get("limit_ratio"), source=incoming_source,
            collect_status=dict(incoming_status)))
        return True
    merged_status = dict(existing.collect_status or {})
    last_attempt: dict[str, Any] = {}
    for factor, status in incoming_status.items():
        if factor == "last_collect_status":
            continue
        columns = _MACRO_FACTOR_COLUMNS.get(factor, (factor,))
        complete_new = all(macro.get(column) is not None for column in columns)
        has_old = any(getattr(existing, column) is not None for column in columns
                      if hasattr(existing, column))
        if complete_new or not has_old:
            merged_status[factor] = status
        else:
            last_attempt[factor] = status
    if last_attempt:
        merged_status["last_collect_status"] = last_attempt
    else:
        merged_status.pop("last_collect_status", None)
    existing.collect_status = merged_status
    for column in ("total_amount", "margin_balance", "margin_growth_pct",
                   "limit_up", "limit_down", "limit_ratio"):
        if macro.get(column) is not None:
            setattr(existing, column, macro[column])
    sources = list(dict.fromkeys(_source_tokens(existing.source) + _source_tokens(incoming_source)))
    existing.source = "+".join(sources) or "none"
    return True


def persist_risk_snapshot(session: Session, run_id: int, risk: dict[str, Any], *,
                          decision_at: dt.datetime) -> None:
    values = {name: risk.get(name) for name in FACTORS}
    if risk.get("override"):
        values["override"] = True
        values["reason"] = str(risk.get("message") or "")
    session.add(RiskSnapshot(
        run_id=run_id,
        decision_at=decision_at,
        level=str(risk.get("level") or "unavailable"),
        values=values,
        sources=risk.get("sources") or {},
        observed_at=risk.get("observed_at") or {},
        available_at=risk.get("available_at") or {},
        missing_fields=list(risk.get("missing_fields") or []),
    ))


def persist_decisions(session: Session, run_id: int, decision_date: dt.date,
                      rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        session.add(TargetDecision(
            run_id=run_id,
            decision_date=decision_date,
            code=row["code"],
            name=row["name"],
            sector=row["sector"],
            data_status=row["data_status"],
            decision_state=row["decision_state"],
            risk_action=row["risk_action"],
            risk_level=row["risk_level"],
            reason_code=row["reason_code"],
            reference_close=row["reference_close"],
            raw_trigger_count=row["raw_trigger_count"],
            raw_signals=row["raw_signals"],
            validation_split=row["validation_split"],
        ))


def existing_signal_keys(session: Session, *, exclude_date: dt.date | None = None) -> set[tuple[str, str, str, str]]:
    stmt = select(TrendSignal.code, TrendSignal.signal_type, TrendSignal.period, TrendSignal.data_date)
    if exclude_date is not None:
        stmt = stmt.where(TrendSignal.decision_date != exclude_date)
    stmt = stmt.where(TrendSignal.data_date.is_not(None))
    return {(str(code), str(kind), str(period), data_date.isoformat())
            for code, kind, period, data_date in session.execute(stmt)}


def notified_signal_keys(session: Session) -> set[tuple[str, str, str, str]]:
    stmt = (select(TrendSignal.code, TrendSignal.signal_type, TrendSignal.period, TrendSignal.data_date)
            .where(TrendSignal.notified_at.is_not(None), TrendSignal.data_date.is_not(None)))
    return {(str(code), str(kind), str(period), data_date.isoformat())
            for code, kind, period, data_date in session.execute(stmt)}


def persist_signals(session: Session, decision_date: dt.date, records: Sequence[dict[str, Any]]) -> None:
    # Rerun idempotency: only unnotified rows for this date are replaceable.
    session.execute(delete(TrendSignal).where(
        TrendSignal.decision_date == decision_date, TrendSignal.notified_at.is_(None)))
    for record in records:
        key = signal_key(record)
        detail = record.get("detail") or {}
        data_date = detail.get("data_date") or record.get("data_date")
        if key is not None:
            session.execute(delete(TrendSignal).where(
                TrendSignal.code == key[0], TrendSignal.signal_type == key[1],
                TrendSignal.period == key[2], TrendSignal.data_date == dt.date.fromisoformat(key[3]),
                TrendSignal.decision_date != decision_date, TrendSignal.notified_at.is_(None)))
        payload = {
            "decision_date": decision_date,
            "code": record["code"],
            "name": record.get("name") or "",
            "signal_type": record["signal_type"],
            "period": record["period"],
            "data_date": dt.date.fromisoformat(str(data_date)) if data_date else None,
            "title": record.get("title") or "",
            "detail": detail,
            "confidence": record.get("confidence") or 0,
        }
        stmt = pg_insert(TrendSignal).values(**payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=[TrendSignal.decision_date, TrendSignal.code, TrendSignal.signal_type],
            set_={"name": payload["name"], "period": payload["period"],
                  "data_date": payload["data_date"], "title": payload["title"],
                  "detail": payload["detail"], "confidence": payload["confidence"]})
        session.execute(stmt)


# --------------------------------------------------------------------------- orchestration
def _empty_macro(decision_date: dt.date) -> dict[str, Any]:
    missing = {"available": False, "source": "", "message": "collect_failed",
               "observed_date": None, "stale": False, "anomalous": False}
    return {
        "decision_date": decision_date.isoformat(),
        "total_amount": None, "margin_balance": None, "margin_growth_pct": None,
        "limit_up": None, "limit_down": None, "limit_ratio": None, "source": "none",
        "collect_status": {name: dict(missing) for name in
                           ("total_amount", "margin_balance", "margin_growth_pct", "limit_ratio")},
    }


def run_eod(session: Session, *, decision_date: dt.date, fetcher: Any | None = None,
            macro_slice: dict[str, Any] | None = None,
            risk_state: dict[str, Any] | None = None,
            risk_loader: Callable[[], dict[str, Any]] | None = None,
            targets: Sequence[Target] = TARGETS,
            previous_session: dt.date | None = None,
            daily_breakout_enabled: bool = DAILY_BREAKOUT_ENABLED,
            now: float | None = None, dry_run: bool = False,
            batch_source: str = "timer") -> dict[str, Any]:
    """Execute one EOD attempt. The caller owns the transaction."""
    now = time.time() if now is None else now
    fetcher = fetcher or KlineFetcher()
    if macro_slice is None:
        try:
            macro_slice = collect_macro_slice(decision_date.isoformat())
        except Exception:
            macro_slice = _empty_macro(decision_date)
    if risk_state is None:
        risk_state = (risk_loader or (lambda: load_risk_state(session, now=now)))()
    risk_state = risk_state or _state_dict("unavailable", "risk state missing")
    level = str(risk_state.get("level") or "unavailable").lower()
    message = str(risk_state.get("message") or "")

    run = None if dry_run else create_run(session, decision_date, data_date=decision_date)
    evaluations = evaluate_all(targets, fetcher, decision_date=decision_date,
                               previous_session=previous_session,
                               daily_breakout_enabled=daily_breakout_enabled)
    blocked_ids = apply_risk_gate(evaluations, level)
    decisions = target_decisions(evaluations, blocked_ids, level)

    existing = set() if dry_run else existing_signal_keys(session, exclude_date=decision_date)
    notified = set() if dry_run else notified_signal_keys(session)
    plan = plan_storage(evaluations, blocked_ids, risk_level=level, risk_message=message,
                        existing_other_dates=existing, notified_keys=notified)
    generated = sum(len(evaluation.signals) for evaluation in evaluations)
    plan.deduplicated = max(0, generated - len(plan.records))

    collect_status = macro_slice.get("collect_status") or {}
    failed = [f"{evaluation.target.name}({evaluation.target.code})"
              for evaluation in evaluations if not evaluation.data_ok]
    audit = audit_result({
        "trend_targets": len(targets),
        "trend_data_ok": sum(1 for evaluation in evaluations if evaluation.data_ok),
        "trend_data_failed": failed,
        "decision_signals": len(plan.records),
        "decision_risk_blocked_signals": plan.blocked_count,
        "collect_status": collect_status,
    })
    details = {
        "risk_state": {"level": level, "message": message[:200],
                       "override": bool(risk_state.get("override")),
                       "cached_fields": list(risk_state.get("cached_fields") or []),
                       "missing_fields": list(risk_state.get("missing_fields") or [])},
        "macro": {"source": macro_slice.get("source") or "none", "collect_status": collect_status},
        "failed_targets": [{"code": evaluation.target.code, "reason": evaluation.reason,
                            "attempts": evaluation.attempts}
                           for evaluation in evaluations if not evaluation.data_ok],
        "generated_signals": generated,
        "deduplicated_signals": plan.deduplicated,
    }

    run_id = None
    if run is not None:
        persist_macro(session, decision_date, macro_slice)
        persist_risk_snapshot(session, run.run_id, risk_state,
                              decision_at=dt.datetime.fromtimestamp(now, tz=UTC))
        persist_decisions(session, run.run_id, decision_date, decisions)
        persist_signals(session, decision_date, plan.records)
        finish_run(run, audit, details=details)
        session.flush()
        run_id = run.run_id
        from qp.signals import chain
        chain_result = chain.register_run(session, run=run, decisions=decisions,
                                          run_status=audit["status"], source=batch_source)

    return {
        "ok": True,
        "dry_run": dry_run,
        "run_id": run_id,
        "decision_date": decision_date.isoformat(),
        "status": audit["status"],
        "outcome": audit["outcome"],
        "reason_codes": audit["reason_codes"],
        "target_count": audit["target_count"],
        "evaluated_count": audit["evaluated_count"],
        "failed_count": audit["failed_count"],
        "signal_count": audit["signal_count"],
        "allowed_signal_count": audit["allowed_signal_count"],
        "blocked_signal_count": audit["blocked_signal_count"],
        "generated_signals": generated,
        "deduplicated_signals": plan.deduplicated,
        "notification_candidates": len(plan.notification_candidates),
        "risk_level": level,
        "macro_source": macro_slice.get("source") or "none",
        "record_chain": chain_result if run is not None else {"enabled": False},
    }


def record_skipped_run(session: Session, decision_date: dt.date, *, source: str,
                       now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    run = create_run(session, decision_date, data_date=decision_date, status="skipped")
    audit = audit_result({"skipped": True, "trend_targets": len(TARGETS)})
    finish_run(run, audit, details={"calendar_source": source})
    session.flush()
    return {"ok": True, "run_id": run.run_id, "decision_date": decision_date.isoformat(),
            "status": "skipped", "outcome": "skipped", "reason_codes": audit["reason_codes"],
            "calendar_source": source}
