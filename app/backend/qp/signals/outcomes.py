"""M4.4 forward-only outcome collection (SHADOW).

For every persisted target decision this module records horizons 1/3/5/10/20 trading
days later.  The execution model follows the legacy ``SwingOutcomeEvaluator`` because
the shadow compare matrix (C6) compares dates, fill status and returns against the
legacy rows:

* the entry session is the first benchmark session after the decision date and the
  entry price is that session's open plus slippage;
* the exit price is the horizon session's close minus slippage;
* benchmark 510300 uses the same entry open / exit close convention;
* commission is 0.03% (minimum 5 CNY), slippage 0.05%, stamp duty 0 for ETFs;
* a missing entry/exit bar or an unexecutable limit-up gap is recorded as
  ``unfilled`` with a reason - never as a zero return.

The contract line "入场/出场用对应交易日收盘" conflicts with the legacy evaluator and
with C6 row-level parity; this module follows the legacy behaviour and documents the
divergence instead of silently inventing a different P&L series.

Rows are append-only: ``(decision_id, horizon_sessions)`` is unique and re-runs only
fill horizons that are still missing.  Multiple horizons of one decision are one trade
observed at several holding periods, never several trades.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.kline import DailyBar, KlineFetcher, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import DecisionOutcome, TargetDecision
from qp.signals import chain
from qp.signals.eod import now_cst

HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)
DEFAULT_BENCHMARK = "510300"
DEFAULT_HISTORY_DAYS = 420


@dataclass(frozen=True)
class OutcomeSettings:
    benchmark_code: str = DEFAULT_BENCHMARK
    notional: float = 100_000.0
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    slippage_rate: float = 0.0005
    stamp_duty_rate: float = 0.0  # ETF default: stamp duty exempt
    limit_up_unfilled_ratio: float = 0.095


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def normalise_bars(bars: Iterable[DailyBar], *, as_of: dt.date) -> list[DailyBar]:
    """Keep observable, positive-close bars, unique by date (last wins), ascending."""
    seen: dict[dt.date, DailyBar] = {}
    for bar in bars:
        if bar.date > as_of or not math.isfinite(bar.close) or bar.close <= 0:
            continue
        if not all(math.isfinite(value) and value > 0 for value in (bar.open, bar.high, bar.low)):
            continue
        seen[bar.date] = bar
    return [seen[day] for day in sorted(seen)]


def _unfilled(decision: dict[str, Any], *, horizon: int, as_of: dt.date, entry_date: dt.date,
              reason: str, settings: OutcomeSettings) -> dict[str, Any]:
    return {
        "decision_id": decision["decision_id"],
        "horizon_sessions": horizon,
        "evaluation_as_of": as_of,
        "entry_date": entry_date,
        "exit_date": None,
        "entry_price": None,
        "exit_price": None,
        "gross_return": None,
        "net_return": None,
        "benchmark_return": None,
        "excess_return": None,
        "fill_status": "unfilled",
        "fill_reason": reason,
        "details": {
            "evaluation_as_of": as_of.isoformat(),
            "benchmark_code": settings.benchmark_code,
            "fill_reason": reason,
        },
    }


def _filled(decision: dict[str, Any], *, code_bars: Sequence[DailyBar], entry_bar: DailyBar,
            exit_bar: DailyBar, benchmark_entry: DailyBar, benchmark_exit: DailyBar,
            prior_close: float, horizon: int, as_of: dt.date,
            settings: OutcomeSettings) -> dict[str, Any]:
    entry_open = float(entry_bar.open)
    exit_close = float(exit_bar.close)
    entry_fill = entry_open * (1 + settings.slippage_rate)
    exit_fill = exit_close * (1 - settings.slippage_rate)
    shares = math.floor(settings.notional / entry_fill / 100) * 100
    if shares <= 0:
        return _unfilled(decision, horizon=horizon, as_of=as_of, entry_date=entry_bar.date,
                         reason="notional_below_one_board_lot", settings=settings)
    buy_amount = entry_fill * shares
    sell_amount = exit_fill * shares
    buy_fee = max(buy_amount * settings.commission_rate, settings.min_commission)
    sell_fee = (max(sell_amount * settings.commission_rate, settings.min_commission)
                + sell_amount * settings.stamp_duty_rate)
    cash_in = buy_amount + buy_fee
    cash_out = sell_amount - sell_fee
    gross_return = exit_close / entry_open - 1
    net_return = cash_out / cash_in - 1
    benchmark_return = float(benchmark_exit.close) / float(benchmark_entry.open) - 1
    window = [bar for bar in code_bars if entry_bar.date <= bar.date <= exit_bar.date]
    mfe = max(float(bar.high) for bar in window) / entry_fill - 1
    mae = min(float(bar.low) for bar in window) / entry_fill - 1
    fee_return = (buy_fee + sell_fee) / cash_in
    slippage_return = max(0.0, gross_return - net_return - fee_return)
    return {
        "decision_id": decision["decision_id"],
        "horizon_sessions": horizon,
        "evaluation_as_of": as_of,
        "entry_date": entry_bar.date,
        "exit_date": exit_bar.date,
        "entry_price": entry_open,
        "exit_price": exit_close,
        "gross_return": gross_return,
        "net_return": net_return,
        "benchmark_return": benchmark_return,
        "excess_return": net_return - benchmark_return,
        "fill_status": "filled",
        "fill_reason": "next_session_open",
        "details": {
            "evaluation_as_of": as_of.isoformat(),
            "benchmark_code": settings.benchmark_code,
            "entry_open": entry_open,
            "entry_fill": entry_fill,
            "exit_close": exit_close,
            "exit_fill": exit_fill,
            "shares": shares,
            "prior_close": prior_close,
            "gap_return": entry_open / prior_close - 1,
            "mfe": mfe,
            "mae": mae,
            "fees_return": fee_return,
            "slippage_return": slippage_return,
            "fill_reason": "next_session_open",
        },
    }


def evaluate_decision(decision: dict[str, Any], *, code_bars: Sequence[DailyBar],
                      benchmark_bars: Sequence[DailyBar], as_of: dt.date,
                      settings: OutcomeSettings | None = None) -> list[dict[str, Any]]:
    """Return the matured, not-yet-recorded horizons for one decision (legacy parity)."""
    settings = settings or OutcomeSettings()
    decision_date = _as_date(decision["decision_date"])
    completed = {int(value) for value in (decision.get("completed_horizons") or set())}
    sessions = [bar.date for bar in benchmark_bars
                if decision_date < bar.date <= as_of]
    if not sessions:
        return []
    code_by_date = {bar.date: bar for bar in code_bars}
    benchmark_by_date = {bar.date: bar for bar in benchmark_bars}
    entry_date = sessions[0]
    entry_bar = code_by_date.get(entry_date)
    reference = decision.get("reference_close")
    prior_rows = [bar for bar in code_bars if bar.date <= decision_date]
    if reference is not None:
        prior_close: float | None = float(reference)
    elif prior_rows:
        prior_close = float(prior_rows[-1].close)
    else:
        prior_close = None

    terminal_unfilled_reason = ""
    if entry_bar is None:
        terminal_unfilled_reason = "suspended_or_no_next_session_bar"
    elif prior_close is None or prior_close <= 0:
        terminal_unfilled_reason = "decision_reference_close_missing"
    elif (float(entry_bar.open) / prior_close - 1 >= settings.limit_up_unfilled_ratio
          and float(entry_bar.low) >= float(entry_bar.open) * 0.999):
        terminal_unfilled_reason = "limit_up_no_executable_liquidity"

    results: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        if horizon in completed or len(sessions) < horizon:
            continue
        exit_date = sessions[horizon - 1]
        if terminal_unfilled_reason:
            results.append(_unfilled(
                decision, horizon=horizon, as_of=as_of,
                entry_date=entry_bar.date if entry_bar is not None else entry_date,
                reason=terminal_unfilled_reason, settings=settings))
            continue
        exit_bar = code_by_date.get(exit_date)
        benchmark_entry = benchmark_by_date.get(entry_date)
        benchmark_exit = benchmark_by_date.get(exit_date)
        if exit_bar is None:
            results.append(_unfilled(
                decision, horizon=horizon, as_of=as_of, entry_date=entry_date,
                reason="exit_bar_missing_or_suspended", settings=settings))
            continue
        if benchmark_entry is None or benchmark_exit is None:
            continue
        assert prior_close is not None and entry_bar is not None
        results.append(_filled(
            decision, code_bars=code_bars, entry_bar=entry_bar, exit_bar=exit_bar,
            benchmark_entry=benchmark_entry, benchmark_exit=benchmark_exit,
            prior_close=prior_close, horizon=horizon, as_of=as_of, settings=settings))
    return results


def _persist(session: Session, rows: Sequence[dict[str, Any]]) -> dict[int, str]:
    """Insert missing horizons; return ``{horizon: fill_status}`` for inserted rows."""
    inserted: dict[int, str] = {}
    for row in rows:
        payload = {
            "decision_id": row["decision_id"],
            "horizon_sessions": row["horizon_sessions"],
            "entry_date": row["entry_date"],
            "exit_date": row["exit_date"],
            "entry_price": row["entry_price"],
            "exit_price": row["exit_price"],
            "gross_return": row["gross_return"],
            "net_return": row["net_return"],
            "benchmark_return": row["benchmark_return"],
            "excess_return": row["excess_return"],
            "fill_status": row["fill_status"],
            "fill_reason": row["fill_reason"],
            "details": row["details"],
        }
        stmt = (pg_insert(DecisionOutcome).values(**payload)
                .on_conflict_do_nothing(index_elements=["decision_id", "horizon_sessions"])
                .returning(DecisionOutcome.horizon_sessions))
        if session.execute(stmt).first() is not None:
            inserted[int(row["horizon_sessions"])] = str(row["fill_status"])
    return inserted


def _pending_decisions(session: Session, *, limit: int,
                       decision_date: dt.date | None = None, allowed_only: bool = False,
                       as_of: dt.date | None = None) -> list[TargetDecision]:
    counts = (select(DecisionOutcome.decision_id.label("decision_id"),
                     func.count().label("n"))
              .group_by(DecisionOutcome.decision_id).subquery())
    stmt = (select(TargetDecision)
            .outerjoin(counts, counts.c.decision_id == TargetDecision.id)
            .where(func.coalesce(counts.c.n, 0) < len(HORIZONS)))
    if decision_date is not None:
        stmt = stmt.where(TargetDecision.decision_date == decision_date)
    if allowed_only:
        # record-chain semantics: only allowed signals materialise results
        stmt = stmt.where(TargetDecision.decision_state == "allowed")
    if allowed_only and as_of is not None:
        # due-date queue: never let old incomplete decisions consume the limit
        stmt = stmt.order_by(TargetDecision.decision_date, TargetDecision.code).limit(5000)
        return _filter_due(session, list(session.execute(stmt).scalars()), as_of)[:max(1, int(limit))]
    stmt = (stmt.order_by(TargetDecision.decision_date, TargetDecision.code)
            .limit(max(1, int(limit))))
    return list(session.execute(stmt).scalars())


def _filter_due(session: Session, decisions: Sequence[TargetDecision],
                as_of: dt.date) -> list[TargetDecision]:
    if not decisions:
        return []
    ids = [decision.id for decision in decisions]
    completed: dict[int, set[int]] = {}
    for decision_id, horizon in session.execute(
            select(DecisionOutcome.decision_id, DecisionOutcome.horizon_sessions)
            .where(DecisionOutcome.decision_id.in_(ids))):
        completed.setdefault(int(decision_id), set()).add(int(horizon))
    start = min(decision.decision_date for decision in decisions)
    days = list(session.scalars(
        select(TradingDay.date).where(TradingDay.origin == "qp", TradingDay.is_open.is_(True),
                                      TradingDay.date > start, TradingDay.date <= as_of)
        .order_by(TradingDay.date)).all())
    due: list[TargetDecision] = []
    for decision in decisions:
        sessions = sum(1 for day in days if day > decision.decision_date)
        if any(horizon <= sessions and horizon not in completed.get(decision.id, set())
               for horizon in HORIZONS):
            due.append(decision)
    return due


def _completed_horizons(session: Session, decision_ids: Sequence[int]) -> dict[int, set[int]]:
    if not decision_ids:
        return {}
    result: dict[int, set[int]] = {}
    for decision_id, horizon in session.execute(
            select(DecisionOutcome.decision_id, DecisionOutcome.horizon_sessions)
            .where(DecisionOutcome.decision_id.in_(decision_ids))):
        result.setdefault(int(decision_id), set()).add(int(horizon))
    return result


def _bars(fetcher: Any, code: str, *, as_of: dt.date) -> list[DailyBar]:
    result = fetcher.get(code)
    if not isinstance(result, KlineResult):
        raise TypeError("outcome fetcher must return KlineResult")
    return normalise_bars(result.bars, as_of=as_of)


def evaluate_pending(session: Session, *, as_of: dt.date | None = None, limit: int = 500,
                     fetcher: Any | None = None, decision_date: dt.date | None = None,
                     settings: OutcomeSettings | None = None) -> dict[str, Any]:
    """Collect matured outcomes for pending decisions; idempotent per horizon."""
    settings = settings or OutcomeSettings()
    as_of = as_of or now_cst().date()
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    chain_on = chain.enabled()
    if chain_on:
        chain.reconcile_expired(session, as_of=as_of)
    decisions = _pending_decisions(session, limit=limit, decision_date=decision_date,
                                   allowed_only=chain_on, as_of=as_of)
    summary: dict[str, Any] = {
        "checked": 0, "inserted": 0, "not_mature": 0, "unfilled": 0,
        "failed": 0, "benchmark_ok": False, "as_of": as_of.isoformat(),
        "benchmark_code": settings.benchmark_code, "benchmark_source": None,
        "benchmark_attempts": [], "benchmark_error": None,
    }
    if not decisions:
        summary["benchmark_ok"] = True
        return summary
    try:
        result = fetcher.get(settings.benchmark_code)
        if not isinstance(result, KlineResult):
            raise TypeError("outcome fetcher must return KlineResult")
        benchmark = normalise_bars(result.bars, as_of=as_of)
        summary["benchmark_source"] = result.source or None
        summary["benchmark_attempts"] = result.attempts or []
    except Exception as exc:
        benchmark = []
        summary["benchmark_error"] = type(exc).__name__
    if not benchmark:
        summary["failed"] = len(decisions)
        return summary
    summary["benchmark_ok"] = True
    completed = _completed_horizons(session, [decision.id for decision in decisions])
    frames: dict[str, list[DailyBar]] = {}
    for decision in decisions:
        summary["checked"] += 1
        try:
            if decision.code not in frames:
                frames[decision.code] = _bars(fetcher, decision.code, as_of=as_of)
            payload = {
                "decision_id": decision.id,
                "decision_date": decision.decision_date,
                "reference_close": (float(decision.reference_close)
                                    if decision.reference_close is not None else None),
                "completed_horizons": completed.get(decision.id, set()),
            }
            outcomes = evaluate_decision(
                payload, code_bars=frames[decision.code], benchmark_bars=benchmark,
                as_of=as_of, settings=settings)
            if not outcomes:
                summary["not_mature"] += 1
                continue
            inserted = _persist(session, outcomes)
            summary["inserted"] += len(inserted)
            summary["unfilled"] += sum(1 for status in inserted.values() if status == "unfilled")
            if chain_on and inserted:
                uid = chain.decision_uid_for(session, decision.decision_date, decision.code)
                if uid:
                    by_horizon = {row["horizon_sessions"]: row for row in outcomes}
                    for horizon in inserted:
                        chain.append_outcome_revision(
                            session, decision_uid=uid, horizon=int(horizon),
                            row=by_horizon[int(horizon)],
                            expected=chain.expected_maturity(session, uid, int(horizon)),
                            as_of=as_of)
        except Exception:
            summary["failed"] += 1
    return summary
