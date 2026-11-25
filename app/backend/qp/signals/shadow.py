"""Counterfactual "shadow allowed" samples.

When the macro gate blocks a triggered entry there is no forward return to judge
the trend rule.  This module copies those blocked-but-triggered decisions into
``signals.shadow_signal`` and records their 1/3/5/10/20-session outcomes with the
same execution model as ``decision_outcome``.

Validation only: nothing here is notified, counted in production statistics or
fed to the record chain.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.kline import KlineFetcher
from qp.models.market import TradingDay
from qp.models.signals import EodRun, ShadowOutcome, ShadowSignal, TargetDecision
from qp.signals.eod import now_cst
from qp.signals.outcomes import (
    DEFAULT_HISTORY_DAYS,
    DETAIL_LIMIT,
    HORIZONS,
    WINDOW_BENCHMARK,
    WINDOW_CALENDAR,
    OutcomeSettings,
    evaluate_decision,
    frame_for,
    load_trading_sessions,
    normalise_bars,
    rejection_map,
)
from qp.signals.return_roles import COUNTERFACTUAL_UNGATED, stamp

_COLUMNS = [
    "decision_date",
    "code",
    "name",
    "sector",
    "source_decision_id",
    "source_run_id",
    "risk_level",
    "reason_code",
    "raw_trigger_count",
    "raw_signals",
    "reference_close",
]


def record(session: Session, *, since: dt.date | None = None, decision_date: dt.date | None = None) -> dict[str, Any]:
    """Copy blocked-but-triggered decisions of the latest terminal run per date.

    Idempotent: ``(decision_date, code)`` is unique and conflicts are skipped.
    """
    latest = (
        select(TargetDecision.decision_date, func.max(TargetDecision.run_id).label("run_id"))
        .join(EodRun, EodRun.run_id == TargetDecision.run_id)
        .where(EodRun.status.in_(("success", "partial")))
        .group_by(TargetDecision.decision_date)
        .subquery()
    )
    selectable = (
        select(
            TargetDecision.decision_date,
            TargetDecision.code,
            TargetDecision.name,
            TargetDecision.sector,
            TargetDecision.id,
            TargetDecision.run_id,
            TargetDecision.risk_level,
            TargetDecision.reason_code,
            TargetDecision.raw_trigger_count,
            TargetDecision.raw_signals,
            TargetDecision.reference_close,
        )
        .join(
            latest,
            and_(latest.c.decision_date == TargetDecision.decision_date, latest.c.run_id == TargetDecision.run_id),
        )
        .where(
            TargetDecision.decision_state == "blocked",
            TargetDecision.raw_trigger_count > 0,
            TargetDecision.data_status == "ok",
        )
    )
    if since is not None:
        selectable = selectable.where(TargetDecision.decision_date >= since)
    if decision_date is not None:
        selectable = selectable.where(TargetDecision.decision_date == decision_date)
    before = int(session.scalar(select(func.count()).select_from(ShadowSignal)) or 0)
    stmt = (
        pg_insert(ShadowSignal)
        .from_select(_COLUMNS, selectable)
        .on_conflict_do_nothing(index_elements=["decision_date", "code"])
    )
    session.execute(stmt)
    total = int(session.scalar(select(func.count()).select_from(ShadowSignal)) or 0)
    # INSERT ... SELECT reports rowcount -1 with psycopg; count before/after instead.
    return {"ok": True, "inserted": total - before, "total": total}


def _pending(session: Session, *, as_of: dt.date, limit: int) -> list[ShadowSignal]:
    counts = (
        select(ShadowOutcome.shadow_id.label("shadow_id"), func.count().label("n"))
        .group_by(ShadowOutcome.shadow_id)
        .subquery()
    )
    rows = list(
        session.execute(
            select(ShadowSignal)
            .outerjoin(counts, counts.c.shadow_id == ShadowSignal.id)
            .where(func.coalesce(counts.c.n, 0) < len(HORIZONS))
            .order_by(ShadowSignal.decision_date, ShadowSignal.code)
            .limit(5000)
        ).scalars()
    )
    if not rows:
        return []
    completed: dict[int, set[int]] = {}
    for shadow_id, horizon in session.execute(
        select(ShadowOutcome.shadow_id, ShadowOutcome.horizon_sessions).where(
            ShadowOutcome.shadow_id.in_([row.id for row in rows])
        )
    ):
        completed.setdefault(int(shadow_id), set()).add(int(horizon))
    start = min(row.decision_date for row in rows)
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
    due: list[ShadowSignal] = []
    for row in rows:
        sessions = sum(1 for day in days if day > row.decision_date)
        if any(horizon <= sessions and horizon not in completed.get(row.id, set()) for horizon in HORIZONS):
            due.append(row)
    return due[: max(1, int(limit))]


def _persist(session: Session, rows: list[dict[str, Any]]) -> dict[int, str]:
    inserted: dict[int, str] = {}
    for row in rows:
        payload = {
            "shadow_id": row["decision_id"],
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
            pg_insert(ShadowOutcome)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["shadow_id", "horizon_sessions"])
            .returning(ShadowOutcome.horizon_sessions)
        )
        if session.execute(stmt).first() is not None:
            inserted[int(row["horizon_sessions"])] = str(row["fill_status"])
    return inserted


def collect(
    session: Session,
    *,
    as_of: dt.date | None = None,
    limit: int = 500,
    fetcher: Any | None = None,
    settings: OutcomeSettings | None = None,
) -> dict[str, Any]:
    """Record matured forward returns for shadow samples; idempotent per horizon."""
    settings = settings or OutcomeSettings()
    as_of = as_of or now_cst().date()
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    pending = _pending(session, as_of=as_of, limit=limit)
    sessions = load_trading_sessions(session, decision_dates=[row.decision_date for row in pending], as_of=as_of)
    sessions_for_eval = sessions or None
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
    if not pending:
        summary["benchmark_ok"] = True
        return summary
    summary["window_basis"] = WINDOW_CALENDAR if sessions_for_eval else WINDOW_BENCHMARK
    invalid_bars: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    try:
        result = fetcher.get(settings.benchmark_code)
        local: list[dict] = []
        benchmark = normalise_bars(result.bars, as_of=as_of, rejected=local)
        source = getattr(result, "source", "") or ""
        benchmark_rejected = list(getattr(result, "rejected", []) or []) + [
            {"code": settings.benchmark_code, "source": source, **item} for item in local
        ]
        invalid_bars.extend(benchmark_rejected)
        benchmark_rejections = rejection_map(benchmark_rejected)
        summary["benchmark_source"] = getattr(result, "source", "") or None
        summary["benchmark_attempts"] = getattr(result, "attempts", []) or []
    except Exception as exc:
        benchmark = []
        benchmark_rejections = {}
        summary["benchmark_error"] = type(exc).__name__
    if not benchmark:
        summary["failed"] = len(pending)
        return summary
    summary["benchmark_ok"] = True
    frames: dict[str, tuple[list[Any], dict[dt.date, str]]] = {}
    completed = {}
    for shadow_id, horizon in session.execute(
        select(ShadowOutcome.shadow_id, ShadowOutcome.horizon_sessions).where(
            ShadowOutcome.shadow_id.in_([row.id for row in pending])
        )
    ):
        completed.setdefault(int(shadow_id), set()).add(int(horizon))
    for row in pending:
        summary["checked"] += 1
        try:
            if row.code not in frames:
                frames[row.code] = frame_for(fetcher, row.code, as_of=as_of, invalid=invalid_bars)
            code_bars, code_rejections = frames[row.code]
            payload = {
                "decision_id": row.id,
                "decision_date": row.decision_date,
                "reference_close": float(row.reference_close) if row.reference_close is not None else None,
                "completed_horizons": completed.get(row.id, set()),
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
                gap["code"] = row.code
            all_gaps.extend(gaps)
            if not outcomes:
                summary["not_mature"] += 1
                if gaps:
                    summary["waiting_data"] += 1
                continue
            for item in outcomes:
                item["details"] = stamp(item.get("details"), COUNTERFACTUAL_UNGATED)
            inserted = _persist(session, outcomes)
            summary["inserted"] += len(inserted)
            summary["unfilled"] += sum(1 for status in inserted.values() if status == "unfilled")
        except Exception:
            summary["failed"] += 1
    summary["invalid_bars"] = invalid_bars[:DETAIL_LIMIT]
    summary["invalid_bar_count"] = len(invalid_bars)
    summary["data_gaps"] = all_gaps[:DETAIL_LIMIT]
    summary["data_gap_count"] = len(all_gaps)
    return summary


def summary(session: Session) -> dict[str, Any]:
    samples = session.scalar(select(func.count()).select_from(ShadowSignal)) or 0
    outcomes = session.scalar(select(func.count()).select_from(ShadowOutcome)) or 0
    last = session.scalar(select(func.max(ShadowSignal.decision_date)))
    last_outcome = (
        session.scalar(
            select(func.max(ShadowSignal.decision_date)).join(ShadowOutcome, ShadowOutcome.shadow_id == ShadowSignal.id)
        )
        if outcomes
        else None
    )
    return {
        "samples": int(samples),
        "outcomes": int(outcomes),
        "last_sample_date": last.isoformat() if last else None,
        "last_outcome_date": last_outcome.isoformat() if last_outcome else None,
        "return_role": "counterfactual_ungated",
        "executable": False,
    }
