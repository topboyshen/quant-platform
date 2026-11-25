"""M4.4 forward-only outcome collection (SHADOW).

For every persisted target decision this module records horizons 1/3/5/10/20 trading
days later.  The execution model follows the legacy ``SwingOutcomeEvaluator`` because
the shadow compare matrix (C6) compares dates, fill status and returns against the
legacy rows:

* the entry session is the first trading session after the decision date and the
  entry price is that session's open plus slippage;
* the exit price is the horizon session's close minus slippage;
* benchmark 510300 uses the same entry open / exit close convention;
* commission is 0.03% (minimum 5 CNY), slippage 0.05%, stamp duty 0 for ETFs;
* a missing entry/exit bar or an unexecutable limit-up gap is recorded as
  ``unfilled`` with a reason - never as a zero return.

The session sequence comes from the ``qp`` trading calendar when the caller supplies
one (``evaluate_pending`` loads it).  Benchmark bars only supply prices: a missing or
invalid benchmark bar is reported as a data gap and the horizon stays unrecorded, so
a missing day can never move the entry/exit dates or produce a normal fill.  With no
calendar rows the evaluator falls back to benchmark dates, preserving the legacy
row-level comparison during backfill.

OHLC validity is shared with the fetch path (``qp.market.kline.bar_invalid_reason``):
rows where high < low or open/close fall outside [low, high] are rejected and the
reason is carried into the outcome status instead of being used for a return.

Rows are append-only: ``(decision_id, horizon_sessions)`` is unique and re-runs only
fill horizons that are still missing.  Multiple horizons of one decision are one trade
observed at several holding periods, never several trades.  Corrections to rows that
were already written are appended to the record chain by ``reconcile_recorded``; the
``decision_outcome`` row itself is never rewritten.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.kline import DailyBar, KlineFetcher, KlineResult, bar_invalid_reason
from qp.models.chain import ChainOutcome
from qp.models.market import TradingDay
from qp.models.signals import DecisionOutcome, TargetDecision
from qp.signals import chain
from qp.signals.eod import now_cst
from qp.signals.return_roles import role_for_decision, stamp

HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)
DEFAULT_BENCHMARK = "510300"
DEFAULT_HISTORY_DAYS = 420
WINDOW_CALENDAR = "calendar"
WINDOW_BENCHMARK = "benchmark_bars"
DETAIL_LIMIT = 100


@dataclass(frozen=True)
class OutcomeSettings:
    benchmark_code: str = DEFAULT_BENCHMARK
    notional: float = 100_000.0
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    slippage_rate: float = 0.0005
    stamp_duty_rate: float = 0.0  # ETF default: stamp duty exempt
    limit_up_unfilled_ratio: float = 0.095
    limit_down_unfilled_ratio: float = 0.095
    one_tick_range_ratio: float = 0.002


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def normalise_bars(bars: Iterable[DailyBar], *, as_of: dt.date, rejected: list[dict] | None = None) -> list[DailyBar]:
    """Keep observable, coherent bars, unique by date (last wins), ascending.

    ``rejected`` receives ``{date, reason}`` for every row dropped by the shared
    OHLC check; callers add the symbol and source when they report the gap.
    """
    seen: dict[dt.date, DailyBar] = {}
    for bar in bars:
        if bar.date > as_of:
            continue
        reason = bar_invalid_reason(bar)
        if reason is not None:
            if rejected is not None:
                rejected.append({"date": bar.date.isoformat(), "reason": reason})
            continue
        seen[bar.date] = bar
    return [seen[day] for day in sorted(seen)]


def _unfilled(
    decision: dict[str, Any],
    *,
    horizon: int,
    as_of: dt.date,
    entry_date: dt.date,
    reason: str,
    settings: OutcomeSettings,
) -> dict[str, Any]:
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


def _uncertain(
    decision: dict[str, Any],
    *,
    horizon: int,
    as_of: dt.date,
    entry_bar: DailyBar,
    exit_date: dt.date,
    prior_close: float,
    reason: str,
    settings: OutcomeSettings,
) -> dict[str, Any]:
    entry_open = float(entry_bar.open)
    return {
        "decision_id": decision["decision_id"],
        "horizon_sessions": horizon,
        "evaluation_as_of": as_of,
        "entry_date": entry_bar.date,
        "exit_date": exit_date,
        "entry_price": entry_open,
        "exit_price": None,
        "gross_return": None,
        "net_return": None,
        "benchmark_return": None,
        "excess_return": None,
        "fill_status": "uncertain",
        "fill_reason": reason,
        "details": {
            "evaluation_as_of": as_of.isoformat(),
            "benchmark_code": settings.benchmark_code,
            "entry_open": entry_open,
            "prior_close": prior_close,
            "gap_return": entry_open / prior_close - 1,
            "day_range_ratio": (float(entry_bar.high) - float(entry_bar.low)) / entry_open,
            "queue_state": "uncertain",
            "daily_bar_cannot_confirm_fill": True,
            "price_basis": "uncertain",
            "fill_reason": reason,
        },
    }


def _pending_exit(
    decision: dict[str, Any],
    *,
    horizon: int,
    as_of: dt.date,
    entry_bar: DailyBar,
    exit_bar: DailyBar,
    benchmark_entry: DailyBar | None,
    benchmark_exit: DailyBar | None,
    prior_close: float,
    settings: OutcomeSettings,
) -> dict[str, Any]:
    entry_open = float(entry_bar.open)
    exit_close = float(exit_bar.close)
    entry_fill = entry_open * (1 + settings.slippage_rate)
    benchmark_return = None
    if benchmark_entry is not None and benchmark_exit is not None:
        benchmark_return = float(benchmark_exit.close) / float(benchmark_entry.open) - 1
    return {
        "decision_id": decision["decision_id"],
        "horizon_sessions": horizon,
        "evaluation_as_of": as_of,
        "entry_date": entry_bar.date,
        "exit_date": exit_bar.date,
        "entry_price": entry_open,
        "exit_price": None,
        "gross_return": None,
        "net_return": None,
        "benchmark_return": benchmark_return,
        "excess_return": None,
        "fill_status": "unfilled",
        "fill_reason": "limit_down_no_executable_liquidity",
        "details": {
            "evaluation_as_of": as_of.isoformat(),
            "benchmark_code": settings.benchmark_code,
            "entry_open": entry_open,
            "entry_fill": entry_fill,
            "exit_close": exit_close,
            "exit_fill": None,
            "exit_state": "pending",
            "position_retained": True,
            "unrealized_return": exit_close / entry_fill - 1,
            "prior_close": prior_close,
            "price_basis": "uncertain",
            "fill_reason": "limit_down_no_executable_liquidity",
        },
    }


def _one_tick_limit_move(entry_bar: DailyBar, prior_close: float, settings: OutcomeSettings) -> bool:
    gap = float(entry_bar.open) / prior_close - 1
    day_range = (float(entry_bar.high) - float(entry_bar.low)) / float(entry_bar.open)
    return abs(gap) >= settings.limit_up_unfilled_ratio and day_range <= settings.one_tick_range_ratio


def _sealed_limit_down(exit_bar: DailyBar, previous_close: float, settings: OutcomeSettings) -> bool:
    sealed = float(exit_bar.high) <= float(exit_bar.low) * 1.000001
    return sealed and float(exit_bar.close) / previous_close - 1 <= -settings.limit_down_unfilled_ratio


def _filled(
    decision: dict[str, Any],
    *,
    code_bars: Sequence[DailyBar],
    entry_bar: DailyBar,
    exit_bar: DailyBar,
    benchmark_entry: DailyBar,
    benchmark_exit: DailyBar,
    prior_close: float,
    horizon: int,
    as_of: dt.date,
    settings: OutcomeSettings,
) -> dict[str, Any]:
    entry_open = float(entry_bar.open)
    exit_close = float(exit_bar.close)
    entry_fill = entry_open * (1 + settings.slippage_rate)
    exit_fill = exit_close * (1 - settings.slippage_rate)
    shares = math.floor(settings.notional / entry_fill / 100) * 100
    if shares <= 0:
        return _unfilled(
            decision,
            horizon=horizon,
            as_of=as_of,
            entry_date=entry_bar.date,
            reason="notional_below_one_board_lot",
            settings=settings,
        )
    buy_amount = entry_fill * shares
    sell_amount = exit_fill * shares
    buy_fee = max(buy_amount * settings.commission_rate, settings.min_commission)
    sell_fee = (
        max(sell_amount * settings.commission_rate, settings.min_commission) + sell_amount * settings.stamp_duty_rate
    )
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
            "price_basis": "raw" if decision.get("price_basis") == "raw" else "uncertain",
            "price_basis_source": decision.get("price_basis") or "not_provided",
            "fill_reason": "next_session_open",
        },
    }


def _window_dates(
    decision_date: dt.date,
    benchmark_bars: Sequence[DailyBar],
    as_of: dt.date,
    sessions: Sequence[dt.date] | None,
) -> list[dt.date]:
    source = sessions if sessions is not None else [bar.date for bar in benchmark_bars]
    return sorted({day for day in source if decision_date < day <= as_of})


def _missing_bar_reason(
    day: dt.date,
    rejections: Mapping[dt.date, str] | None,
    *,
    default: str,
    prefix: str,
) -> str:
    reason = (rejections or {}).get(day)
    return f"{prefix}:{reason}" if reason else default


def _record_benchmark_gaps(
    decision: dict[str, Any],
    *,
    horizon: int,
    entry_date: dt.date,
    exit_date: dt.date,
    benchmark_by_date: Mapping[dt.date, DailyBar],
    benchmark_rejections: Mapping[dt.date, str] | None,
    data_gaps: list[dict[str, Any]] | None,
) -> bool:
    """Record missing benchmark bars for one horizon; return True when absent."""
    missing: dict[dt.date, list[str]] = {}
    for side, day in (("entry", entry_date), ("exit", exit_date)):
        if day not in benchmark_by_date:
            missing.setdefault(day, []).append(side)
    for day, sides in sorted(missing.items()):
        invalid = (benchmark_rejections or {}).get(day)
        gap: dict[str, Any] = {
            "decision_id": decision["decision_id"],
            "horizon": horizon,
            "date": day.isoformat(),
            "sides": sides,
            "reason": "benchmark_bar_invalid" if invalid else "benchmark_bar_missing",
        }
        if invalid:
            gap["invalid_reason"] = invalid
        if data_gaps is not None:
            data_gaps.append(gap)
    return bool(missing)


def evaluate_decision(
    decision: dict[str, Any],
    *,
    code_bars: Sequence[DailyBar],
    benchmark_bars: Sequence[DailyBar],
    as_of: dt.date,
    settings: OutcomeSettings | None = None,
    sessions: Sequence[dt.date] | None = None,
    code_rejections: Mapping[dt.date, str] | None = None,
    benchmark_rejections: Mapping[dt.date, str] | None = None,
    data_gaps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the matured, not-yet-recorded horizons for one decision.

    ``sessions`` is the canonical trading-session sequence (decision date excluded,
    ``as_of`` bounded).  Without it the evaluator falls back to benchmark bar dates,
    which preserves legacy row-level parity but shifts windows when a benchmark day
    is missing.  A missing or rejected benchmark bar at a horizon's entry/exit date
    appends a structured entry to ``data_gaps`` and leaves the horizon unrecorded.
    """
    settings = settings or OutcomeSettings()
    decision_date = _as_date(decision["decision_date"])
    completed = {int(value) for value in (decision.get("completed_horizons") or set())}
    window = _window_dates(decision_date, benchmark_bars, as_of, sessions)
    basis = WINDOW_CALENDAR if sessions is not None else WINDOW_BENCHMARK
    if not window:
        return []
    code_by_date = {bar.date: bar for bar in code_bars}
    benchmark_by_date = {bar.date: bar for bar in benchmark_bars}
    entry_date = window[0]
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
    terminal_uncertain_reason = ""
    if entry_bar is None:
        terminal_unfilled_reason = _missing_bar_reason(
            entry_date,
            code_rejections,
            default="suspended_or_no_next_session_bar",
            prefix="entry_bar_invalid",
        )
    elif prior_close is None or prior_close <= 0:
        terminal_unfilled_reason = "decision_reference_close_missing"
    elif (
        float(entry_bar.open) / prior_close - 1 >= settings.limit_up_unfilled_ratio
        and float(entry_bar.low) >= float(entry_bar.open) * 0.999
    ):
        terminal_unfilled_reason = "limit_up_no_executable_liquidity"
    elif _one_tick_limit_move(entry_bar, prior_close, settings):
        terminal_uncertain_reason = "queue_uncertain"

    results: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        if horizon in completed or len(window) < horizon:
            continue
        exit_date = window[horizon - 1]
        if _record_benchmark_gaps(
            decision,
            horizon=horizon,
            entry_date=entry_date,
            exit_date=exit_date,
            benchmark_by_date=benchmark_by_date,
            benchmark_rejections=benchmark_rejections,
            data_gaps=data_gaps,
        ):
            continue
        if terminal_unfilled_reason:
            results.append(
                _unfilled(
                    decision,
                    horizon=horizon,
                    as_of=as_of,
                    entry_date=entry_bar.date if entry_bar is not None else entry_date,
                    reason=terminal_unfilled_reason,
                    settings=settings,
                )
            )
            continue
        if terminal_uncertain_reason:
            assert entry_bar is not None and prior_close is not None
            results.append(
                _uncertain(
                    decision,
                    horizon=horizon,
                    as_of=as_of,
                    entry_bar=entry_bar,
                    exit_date=exit_date,
                    prior_close=prior_close,
                    reason=terminal_uncertain_reason,
                    settings=settings,
                )
            )
            continue
        exit_bar = code_by_date.get(exit_date)
        if exit_bar is None:
            results.append(
                _unfilled(
                    decision,
                    horizon=horizon,
                    as_of=as_of,
                    entry_date=entry_date,
                    reason=_missing_bar_reason(
                        exit_date,
                        code_rejections,
                        default="exit_bar_missing_or_suspended",
                        prefix="exit_bar_invalid",
                    ),
                    settings=settings,
                )
            )
            continue
        previous_exit_close = next((float(bar.close) for bar in reversed(code_bars) if bar.date < exit_bar.date), None)
        benchmark_entry = benchmark_by_date[entry_date]
        benchmark_exit = benchmark_by_date[exit_date]
        if previous_exit_close is not None and _sealed_limit_down(exit_bar, previous_exit_close, settings):
            results.append(
                _pending_exit(
                    decision,
                    horizon=horizon,
                    as_of=as_of,
                    entry_bar=entry_bar,
                    exit_bar=exit_bar,
                    benchmark_entry=benchmark_entry,
                    benchmark_exit=benchmark_exit,
                    prior_close=prior_close,
                    settings=settings,
                )
            )
            continue
        assert prior_close is not None and entry_bar is not None
        results.append(
            _filled(
                decision,
                code_bars=code_bars,
                entry_bar=entry_bar,
                exit_bar=exit_bar,
                benchmark_entry=benchmark_entry,
                benchmark_exit=benchmark_exit,
                prior_close=prior_close,
                horizon=horizon,
                as_of=as_of,
                settings=settings,
            )
        )
    for row in results:
        row["details"]["window_basis"] = basis
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
        stmt = (
            pg_insert(DecisionOutcome)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["decision_id", "horizon_sessions"])
            .returning(DecisionOutcome.horizon_sessions)
        )
        if session.execute(stmt).first() is not None:
            inserted[int(row["horizon_sessions"])] = str(row["fill_status"])
    return inserted


def _pending_decisions(
    session: Session,
    *,
    limit: int,
    decision_date: dt.date | None = None,
    allowed_only: bool = False,
    as_of: dt.date | None = None,
) -> list[TargetDecision]:
    counts = (
        select(DecisionOutcome.decision_id.label("decision_id"), func.count().label("n"))
        .group_by(DecisionOutcome.decision_id)
        .subquery()
    )
    stmt = (
        select(TargetDecision)
        .outerjoin(counts, counts.c.decision_id == TargetDecision.id)
        .where(func.coalesce(counts.c.n, 0) < len(HORIZONS))
    )
    if decision_date is not None:
        stmt = stmt.where(TargetDecision.decision_date == decision_date)
    if allowed_only:
        # record-chain semantics: only allowed signals materialise results
        stmt = stmt.where(TargetDecision.decision_state == "allowed")
    if allowed_only and as_of is not None:
        # due-date queue: never let old incomplete decisions consume the limit
        stmt = stmt.order_by(TargetDecision.decision_date, TargetDecision.code).limit(5000)
        return _filter_due(session, list(session.execute(stmt).scalars()), as_of)[: max(1, int(limit))]
    stmt = stmt.order_by(TargetDecision.decision_date, TargetDecision.code).limit(max(1, int(limit)))
    return list(session.execute(stmt).scalars())


def _filter_due(session: Session, decisions: Sequence[TargetDecision], as_of: dt.date) -> list[TargetDecision]:
    if not decisions:
        return []
    ids = [decision.id for decision in decisions]
    completed: dict[int, set[int]] = {}
    for decision_id, horizon in session.execute(
        select(DecisionOutcome.decision_id, DecisionOutcome.horizon_sessions).where(
            DecisionOutcome.decision_id.in_(ids)
        )
    ):
        completed.setdefault(int(decision_id), set()).add(int(horizon))
    start = min(decision.decision_date for decision in decisions)
    days = list(
        session.scalars(
            select(TradingDay.date)
            .where(
                TradingDay.origin == "qp",
                TradingDay.is_open.is_(True),
                TradingDay.date > start,
                TradingDay.date <= as_of,
            )
            .order_by(TradingDay.date)
        ).all()
    )
    due: list[TargetDecision] = []
    for decision in decisions:
        sessions = sum(1 for day in days if day > decision.decision_date)
        if any(horizon <= sessions and horizon not in completed.get(decision.id, set()) for horizon in HORIZONS):
            due.append(decision)
    return due


def _completed_horizons(session: Session, decision_ids: Sequence[int]) -> dict[int, set[int]]:
    if not decision_ids:
        return {}
    result: dict[int, set[int]] = {}
    for decision_id, horizon in session.execute(
        select(DecisionOutcome.decision_id, DecisionOutcome.horizon_sessions).where(
            DecisionOutcome.decision_id.in_(decision_ids)
        )
    ):
        result.setdefault(int(decision_id), set()).add(int(horizon))
    return result


def load_trading_sessions(session: Session, *, decision_dates: Iterable[dt.date], as_of: dt.date) -> list[dt.date]:
    """Open ``qp`` sessions after the earliest decision, bounded by ``as_of``.

    Empty means there is no usable calendar; callers then fall back to benchmark
    dates so legacy backfills keep their row-level comparison.
    """
    dates = list(decision_dates)
    if not dates:
        return []
    start = min(dates)
    # A calendar that begins after the decision cannot prove the next session; using
    # it would shift the window silently, so fall back to the legacy basis instead.
    first = session.scalar(select(func.min(TradingDay.date)).where(TradingDay.origin == "qp"))
    if first is None or first > start:
        return []
    return list(
        session.scalars(
            select(TradingDay.date)
            .where(
                TradingDay.origin == "qp",
                TradingDay.is_open.is_(True),
                TradingDay.date > start,
                TradingDay.date <= as_of,
            )
            .order_by(TradingDay.date)
        ).all()
    )


def rejection_map(rejected: Iterable[dict]) -> dict[dt.date, str]:
    reasons: dict[dt.date, str] = {}
    for item in rejected:
        try:
            reasons[dt.date.fromisoformat(str(item["date"])[:10])] = str(item["reason"])
        except (KeyError, ValueError):
            continue
    return reasons


def frame_for(
    fetcher: Any,
    code: str,
    *,
    as_of: dt.date,
    invalid: list[dict[str, Any]] | None = None,
) -> tuple[list[DailyBar], dict[dt.date, str]]:
    """Fetch and normalise one symbol, reporting rejected rows with its identity."""
    result = fetcher.get(code)
    if not isinstance(result, KlineResult):
        raise TypeError("outcome fetcher must return KlineResult")
    local: list[dict] = []
    bars = normalise_bars(result.bars, as_of=as_of, rejected=local)
    source = getattr(result, "source", "") or ""
    rejected = list(getattr(result, "rejected", []) or []) + [
        {"code": code, "source": source, **item} for item in local
    ]
    if invalid is not None:
        invalid.extend(rejected)
    return bars, rejection_map(rejected)


def evaluate_pending(
    session: Session,
    *,
    as_of: dt.date | None = None,
    limit: int = 500,
    fetcher: Any | None = None,
    decision_date: dt.date | None = None,
    settings: OutcomeSettings | None = None,
) -> dict[str, Any]:
    """Collect matured outcomes for pending decisions; idempotent per horizon."""
    settings = settings or OutcomeSettings()
    as_of = as_of or now_cst().date()
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    chain_on = chain.enabled()
    if chain_on:
        chain.reconcile_expired(session, as_of=as_of)
    decisions = _pending_decisions(
        session, limit=limit, decision_date=decision_date, allowed_only=chain_on, as_of=as_of
    )
    calendar = load_trading_sessions(
        session, decision_dates=[decision.decision_date for decision in decisions], as_of=as_of
    )
    sessions_for_eval: list[dt.date] | None = calendar or None
    summary: dict[str, Any] = {
        "checked": 0,
        "inserted": 0,
        "not_mature": 0,
        "waiting_data": 0,
        "unfilled": 0,
        "failed": 0,
        "benchmark_ok": False,
        "as_of": as_of.isoformat(),
        "benchmark_code": settings.benchmark_code,
        "benchmark_source": None,
        "benchmark_attempts": [],
        "benchmark_error": None,
        "window_basis": None,
        "invalid_bars": [],
        "invalid_bar_count": 0,
        "data_gaps": [],
        "data_gap_count": 0,
    }
    if not decisions:
        summary["benchmark_ok"] = True
        return summary
    summary["window_basis"] = WINDOW_CALENDAR if sessions_for_eval else WINDOW_BENCHMARK
    invalid_bars: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    try:
        result = fetcher.get(settings.benchmark_code)
        if not isinstance(result, KlineResult):
            raise TypeError("outcome fetcher must return KlineResult")
        local: list[dict] = []
        benchmark = normalise_bars(result.bars, as_of=as_of, rejected=local)
        source = result.source or ""
        benchmark_rejected = list(getattr(result, "rejected", []) or []) + [
            {"code": settings.benchmark_code, "source": source, **item} for item in local
        ]
        invalid_bars.extend(benchmark_rejected)
        benchmark_rejections = rejection_map(benchmark_rejected)
        summary["benchmark_source"] = result.source or None
        summary["benchmark_attempts"] = result.attempts or []
    except Exception as exc:
        benchmark = []
        benchmark_rejections = {}
        summary["benchmark_error"] = type(exc).__name__
    if not benchmark:
        summary["failed"] = len(decisions)
        summary["invalid_bars"] = invalid_bars[:DETAIL_LIMIT]
        summary["invalid_bar_count"] = len(invalid_bars)
        return summary
    summary["benchmark_ok"] = True
    completed = _completed_horizons(session, [decision.id for decision in decisions])
    frames: dict[str, tuple[list[DailyBar], dict[dt.date, str]]] = {}
    for decision in decisions:
        summary["checked"] += 1
        try:
            if decision.code not in frames:
                frames[decision.code] = frame_for(fetcher, decision.code, as_of=as_of, invalid=invalid_bars)
            code_bars, code_rejections = frames[decision.code]
            payload = {
                "decision_id": decision.id,
                "decision_date": decision.decision_date,
                "reference_close": (float(decision.reference_close) if decision.reference_close is not None else None),
                "completed_horizons": completed.get(decision.id, set()),
            }
            gaps: list[dict[str, Any]] = []
            outcomes = evaluate_decision(
                payload,
                code_bars=code_bars,
                benchmark_bars=benchmark,
                as_of=as_of,
                settings=settings,
                sessions=sessions_for_eval,
                code_rejections=code_rejections,
                benchmark_rejections=benchmark_rejections,
                data_gaps=gaps,
            )
            for gap in gaps:
                gap["code"] = decision.code
            all_gaps.extend(gaps)
            if not outcomes:
                summary["not_mature"] += 1
                if gaps:
                    summary["waiting_data"] += 1
                continue
            role = role_for_decision(decision.decision_state, decision.risk_action)
            for row in outcomes:
                row["details"] = stamp(row.get("details"), role)
            inserted = _persist(session, outcomes)
            summary["inserted"] += len(inserted)
            summary["unfilled"] += sum(1 for status in inserted.values() if status == "unfilled")
            if chain_on and inserted:
                uid = chain.decision_uid_for(session, decision.decision_date, decision.code)
                if uid:
                    by_horizon = {row["horizon_sessions"]: row for row in outcomes}
                    for horizon in inserted:
                        chain.append_outcome_revision(
                            session,
                            decision_uid=uid,
                            horizon=int(horizon),
                            row=by_horizon[int(horizon)],
                            expected=chain.expected_maturity(session, uid, int(horizon)),
                            as_of=as_of,
                        )
        except Exception:
            summary["failed"] += 1
    summary["invalid_bars"] = invalid_bars[:DETAIL_LIMIT]
    summary["invalid_bar_count"] = len(invalid_bars)
    summary["data_gaps"] = all_gaps[:DETAIL_LIMIT]
    summary["data_gap_count"] = len(all_gaps)
    return summary


def _same_number(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    # Numeric(18, 8) rounds stored returns; allow half a unit in the last place.
    return abs(float(left) - float(right)) <= 1e-7


def _recorded_row_matches(stored: DecisionOutcome, row: Mapping[str, Any]) -> bool:
    return (
        str(stored.fill_status) == str(row["fill_status"])
        and stored.entry_date == row.get("entry_date")
        and stored.exit_date == row.get("exit_date")
        and _same_number(stored.net_return, row.get("net_return"))
        and _same_number(stored.benchmark_return, row.get("benchmark_return"))
        and _same_number(stored.excess_return, row.get("excess_return"))
    )


def _latest_chain_outcome(session: Session, *, decision_uid: str, horizon: int) -> ChainOutcome | None:
    return session.scalar(
        select(ChainOutcome)
        .where(ChainOutcome.decision_uid == decision_uid, ChainOutcome.horizon_sessions == horizon)
        .order_by(ChainOutcome.revision.desc())
        .limit(1)
    )


def _chain_row_matches(latest: ChainOutcome, row: Mapping[str, Any]) -> bool:
    expected_state = "filled" if row["fill_status"] == "filled" else "unfilled"
    return (
        latest.execution_state == expected_state
        and latest.entry_date == row.get("entry_date")
        and latest.exit_date == row.get("exit_date")
        and _same_number(latest.net_return, row.get("net_return"))
    )


def reconcile_recorded(
    session: Session,
    *,
    as_of: dt.date | None = None,
    limit: int = 200,
    fetcher: Any | None = None,
    decision_date: dt.date | None = None,
    settings: OutcomeSettings | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compare recorded outcomes with the calendar window and append chain revisions.

    ``decision_outcome`` is never updated.  A horizon whose stored dates or returns
    differ from the calendar-based recomputation gets an append-only
    ``chain_outcome`` revision, so ``data_gaps`` that later fill in stay traceable
    and repeated runs do not append the same correction twice.  ``dry_run`` only
    lists the affected rows (impact assessment before a release).
    """
    settings = settings or OutcomeSettings()
    as_of = as_of or now_cst().date()
    summary: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "checked": 0,
        "affected": 0,
        "revised": 0,
        "would_revise": 0,
        "unchanged": 0,
        "waiting_data": 0,
        "unavailable": 0,
        "failed": 0,
        "window_basis": WINDOW_CALENDAR,
        "affected_rows": [],
        "data_gap_count": 0,
    }
    if not chain.enabled():
        summary["skipped"] = "record_chain_disabled"
        return summary
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    stmt = (
        select(TargetDecision)
        .join(DecisionOutcome, DecisionOutcome.decision_id == TargetDecision.id)
        .where(TargetDecision.decision_state == "allowed")
        .distinct()
        .order_by(TargetDecision.decision_date, TargetDecision.code)
        .limit(max(1, int(limit)))
    )
    if decision_date is not None:
        stmt = stmt.where(TargetDecision.decision_date == decision_date)
    decisions = list(session.execute(stmt).scalars())
    if not decisions:
        summary["ok"] = True
        return summary
    sessions = load_trading_sessions(
        session, decision_dates=[decision.decision_date for decision in decisions], as_of=as_of
    )
    if not sessions:
        summary["skipped"] = "trading_calendar_missing"
        return summary
    invalid_bars: list[dict[str, Any]] = []
    try:
        result = fetcher.get(settings.benchmark_code)
        if not isinstance(result, KlineResult):
            raise TypeError("outcome fetcher must return KlineResult")
        local: list[dict] = []
        benchmark = normalise_bars(result.bars, as_of=as_of, rejected=local)
        benchmark_rejections = rejection_map(
            list(getattr(result, "rejected", []) or [])
            + [{"code": settings.benchmark_code, "source": result.source or "", **item} for item in local]
        )
    except Exception as exc:
        benchmark = []
        benchmark_rejections = {}
        summary["benchmark_error"] = type(exc).__name__
    if not benchmark:
        summary["skipped"] = "benchmark_unavailable"
        return summary
    gaps: list[dict[str, Any]] = []
    affected_rows: list[dict[str, Any]] = []
    frames: dict[str, tuple[list[DailyBar], dict[dt.date, str]]] = {}
    for decision in decisions:
        summary["checked"] += 1
        try:
            if decision.code not in frames:
                frames[decision.code] = frame_for(fetcher, decision.code, as_of=as_of, invalid=invalid_bars)
            code_bars, code_rejections = frames[decision.code]
            stored_rows = list(
                session.scalars(select(DecisionOutcome).where(DecisionOutcome.decision_id == decision.id)).all()
            )
            stored = {int(row.horizon_sessions): row for row in stored_rows}
            decision_gaps: list[dict[str, Any]] = []
            recomputed = {
                int(row["horizon_sessions"]): row
                for row in evaluate_decision(
                    {
                        "decision_id": decision.id,
                        "decision_date": decision.decision_date,
                        "reference_close": (
                            float(decision.reference_close) if decision.reference_close is not None else None
                        ),
                        "completed_horizons": set(),
                    },
                    code_bars=code_bars,
                    benchmark_bars=benchmark,
                    as_of=as_of,
                    settings=settings,
                    sessions=sessions,
                    code_rejections=code_rejections,
                    benchmark_rejections=benchmark_rejections,
                    data_gaps=decision_gaps,
                )
            }
            gaps.extend(decision_gaps)
            for horizon, row in sorted(recomputed.items()):
                stored_row = stored.get(horizon)
                if stored_row is None:
                    continue
                if _recorded_row_matches(stored_row, row):
                    summary["unchanged"] += 1
                    continue
                summary["affected"] += 1
                detail = {
                    "decision_id": decision.id,
                    "code": decision.code,
                    "horizon": horizon,
                    "stored": {
                        "entry_date": stored_row.entry_date.isoformat() if stored_row.entry_date else None,
                        "exit_date": stored_row.exit_date.isoformat() if stored_row.exit_date else None,
                        "fill_status": stored_row.fill_status,
                    },
                    "recomputed": {
                        "entry_date": row["entry_date"].isoformat() if row.get("entry_date") else None,
                        "exit_date": row["exit_date"].isoformat() if row.get("exit_date") else None,
                        "fill_status": row["fill_status"],
                    },
                }
                affected_rows.append(detail)
                uid = chain.decision_uid_for(session, decision.decision_date, decision.code)
                if not uid:
                    summary["unavailable"] += 1
                    continue
                latest = _latest_chain_outcome(session, decision_uid=uid, horizon=horizon)
                if latest is not None and _chain_row_matches(latest, row):
                    summary["unchanged"] += 1
                    continue
                if dry_run:
                    summary["would_revise"] += 1
                    continue
                if chain.append_outcome_revision(
                    session,
                    decision_uid=uid,
                    horizon=horizon,
                    row=row,
                    expected=chain.expected_maturity(session, uid, horizon),
                    as_of=as_of,
                ):
                    summary["revised"] += 1
                else:
                    summary["unavailable"] += 1
            for horizon in stored:
                if horizon not in recomputed:
                    summary["waiting_data"] += 1
        except Exception:
            summary["failed"] += 1
    summary["ok"] = summary["failed"] == 0
    summary["affected_rows"] = affected_rows[:DETAIL_LIMIT]
    summary["data_gap_count"] = len(gaps)
    summary["invalid_bar_count"] = len(invalid_bars)
    return summary
