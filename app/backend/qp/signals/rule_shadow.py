"""Rule shadow (M13): forward-test an experimental rule beside the live one.

``qp-swing.v2`` is the daily volume breakout above MA60 with 2x volume (R4b in
the offline backtest). Its signals and outcomes live in dedicated tables —
never in ``target_decision``, the record chain or notifications. The live risk
level is copied into ``detail``/``risk_level`` so gated and ungated variants can
be compared later.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.kline import KlineFetcher
from qp.models.market import TradingDay
from qp.models.signals import RuleShadowOutcome, RuleShadowSignal, TargetDecision
from qp.signals.eod import now_cst
from qp.signals.indicators import Bar, volume_breakout
from qp.signals.outcomes import (
    DEFAULT_HISTORY_DAYS,
    HORIZONS,
    OutcomeSettings,
    evaluate_decision,
    normalise_bars,
)
from qp.signals.return_roles import COUNTERFACTUAL_UNGATED, stamp
from qp.signals.universe import TARGETS, Target

V2_RULE = "qp-swing.v2"
V2_SIGNAL_TYPE = "volume_breakout_ma60_2x"
V2_VOLUME_RATIO = 2.0
COOLDOWN_SESSIONS = 5


def _bars_for(fetcher: Any, code: str, as_of: dt.date) -> list[Any]:
    return normalise_bars(fetcher.get(code).bars, as_of=as_of)


def _to_bars(rows: Sequence[Any]) -> list[Bar]:
    return [
        Bar(
            row.date,
            close=float(row.close),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            volume=float(row.volume),
        )
        for row in rows
    ]


def _cooldown_ok(session: Session, *, rule_version: str, code: str, day: dt.date) -> bool:
    last = session.scalar(
        select(func.max(RuleShadowSignal.decision_date)).where(
            RuleShadowSignal.rule_version == rule_version,
            RuleShadowSignal.code == code,
            RuleShadowSignal.decision_date < day,
        )
    )
    if last is None:
        return True
    sessions = (
        session.scalar(
            select(func.count())
            .select_from(TradingDay)
            .where(
                TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date > last, TradingDay.date <= day
            )
        )
        or 0
    )
    return int(sessions) >= COOLDOWN_SESSIONS


def generate(
    session: Session,
    *,
    day: dt.date,
    fetcher: Any | None = None,
    frames: dict[str, list[Any]] | None = None,
    targets: Sequence[Target] = TARGETS,
    rule_version: str = V2_RULE,
) -> dict[str, Any]:
    """Insert one day's v2 signals (idempotent, 5-session cooldown per code)."""
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    risk = dict(
        session.execute(
            select(TargetDecision.code, TargetDecision.risk_level).where(TargetDecision.decision_date == day)
        ).all()
    )
    summary = {"day": day.isoformat(), "checked": 0, "inserted": 0, "skipped_cooldown": 0, "no_bar": 0, "failed": 0}
    for target in targets:
        try:
            bars = frames.get(target.code) if frames is not None else None
            bars = [b for b in bars if b.date <= day] if bars is not None else _bars_for(fetcher, target.code, day)
        except Exception:
            summary["failed"] += 1
            continue
        if not bars or bars[-1].date != day:
            summary["no_bar"] += 1
            continue
        summary["checked"] += 1
        triggered, diag = volume_breakout(_to_bars(bars), ratio=V2_VOLUME_RATIO)
        if not triggered:
            continue
        if not _cooldown_ok(session, rule_version=rule_version, code=target.code, day=day):
            summary["skipped_cooldown"] += 1
            continue
        detail = dict(diag)
        detail.update({"rule": rule_version, "volume_threshold": V2_VOLUME_RATIO, "risk_level": risk.get(target.code)})
        stmt = (
            pg_insert(RuleShadowSignal)
            .values(
                rule_version=rule_version,
                signal_type=V2_SIGNAL_TYPE,
                decision_date=day,
                code=target.code,
                name=target.name,
                sector=target.sector,
                reference_close=diag.get("close"),
                volume_ratio=diag.get("volume_ratio"),
                risk_level=risk.get(target.code),
                detail=detail,
            )
            .on_conflict_do_nothing(index_elements=["rule_version", "decision_date", "code", "signal_type"])
            .returning(RuleShadowSignal.id)
        )
        if session.execute(stmt).first() is not None:
            summary["inserted"] += 1
    return summary


def backfill(
    session: Session,
    *,
    since: dt.date,
    until: dt.date | None = None,
    fetcher: Any | None = None,
    targets: Sequence[Target] = TARGETS,
    rule_version: str = V2_RULE,
) -> dict[str, Any]:
    """Fetch bars once and replay every open day in the range."""
    until = until or now_cst().date()
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    frames: dict[str, list[Any]] = {}
    for target in targets:
        try:
            frames[target.code] = normalise_bars(fetcher.get(target.code).bars, as_of=until)
        except Exception:
            continue
    days = list(
        session.scalars(
            select(TradingDay.date)
            .where(
                TradingDay.origin == "qp",
                TradingDay.is_open.is_(True),
                TradingDay.date >= since,
                TradingDay.date <= until,
            )
            .order_by(TradingDay.date)
        )
    )
    totals = {"days": len(days), "inserted": 0, "skipped_cooldown": 0, "no_bar": 0, "failed": 0, "details": []}
    for day in days:
        out = generate(session, day=day, frames=frames, targets=targets, rule_version=rule_version)
        totals["inserted"] += out["inserted"]
        totals["skipped_cooldown"] += out["skipped_cooldown"]
        totals["no_bar"] += out["no_bar"]
        totals["failed"] += out["failed"]
        if out["inserted"]:
            totals["details"].append({"day": out["day"], "inserted": out["inserted"]})
    return totals


def _pending(session: Session, *, as_of: dt.date, limit: int, rule_version: str) -> list[RuleShadowSignal]:
    counts = (
        select(RuleShadowOutcome.signal_id.label("signal_id"), func.count().label("n"))
        .group_by(RuleShadowOutcome.signal_id)
        .subquery()
    )
    rows = list(
        session.execute(
            select(RuleShadowSignal)
            .outerjoin(counts, counts.c.signal_id == RuleShadowSignal.id)
            .where(func.coalesce(counts.c.n, 0) < len(HORIZONS), RuleShadowSignal.rule_version == rule_version)
            .order_by(RuleShadowSignal.decision_date, RuleShadowSignal.code)
            .limit(5000)
        ).scalars()
    )
    if not rows:
        return []
    completed: dict[int, set[int]] = {}
    for signal_id, horizon in session.execute(
        select(RuleShadowOutcome.signal_id, RuleShadowOutcome.horizon_sessions).where(
            RuleShadowOutcome.signal_id.in_([row.id for row in rows])
        )
    ):
        completed.setdefault(int(signal_id), set()).add(int(horizon))
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
        )
    )
    due: list[RuleShadowSignal] = []
    for row in rows:
        sessions = sum(1 for day in days if day > row.decision_date)
        if any(horizon <= sessions and horizon not in completed.get(row.id, set()) for horizon in HORIZONS):
            due.append(row)
    return due[: max(1, int(limit))]


def _persist(session: Session, rows: list[dict[str, Any]]) -> dict[int, str]:
    inserted: dict[int, str] = {}
    for row in rows:
        payload = {
            "signal_id": row["decision_id"],
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
            pg_insert(RuleShadowOutcome)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["signal_id", "horizon_sessions"])
            .returning(RuleShadowOutcome.horizon_sessions)
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
    rule_version: str = V2_RULE,
) -> dict[str, Any]:
    """Record matured forward returns for rule-shadow signals; idempotent."""
    settings = settings or OutcomeSettings()
    as_of = as_of or now_cst().date()
    fetcher = fetcher or KlineFetcher(days=DEFAULT_HISTORY_DAYS)
    pending = _pending(session, as_of=as_of, limit=limit, rule_version=rule_version)
    summary: dict[str, Any] = {
        "checked": 0,
        "inserted": 0,
        "not_mature": 0,
        "unfilled": 0,
        "failed": 0,
        "benchmark_ok": False,
        "as_of": as_of.isoformat(),
    }
    if not pending:
        summary["benchmark_ok"] = True
        return summary
    try:
        benchmark = normalise_bars(fetcher.get(settings.benchmark_code).bars, as_of=as_of)
    except Exception:
        benchmark = []
    if not benchmark:
        summary["failed"] = len(pending)
        return summary
    summary["benchmark_ok"] = True
    frames: dict[str, list[Any]] = {}
    completed: dict[int, set[int]] = {}
    for signal_id, horizon in session.execute(
        select(RuleShadowOutcome.signal_id, RuleShadowOutcome.horizon_sessions).where(
            RuleShadowOutcome.signal_id.in_([row.id for row in pending])
        )
    ):
        completed.setdefault(int(signal_id), set()).add(int(horizon))
    for signal in pending:
        summary["checked"] += 1
        try:
            if signal.code not in frames:
                frames[signal.code] = normalise_bars(fetcher.get(signal.code).bars, as_of=as_of)
            payload = {
                "decision_id": signal.id,
                "decision_date": signal.decision_date,
                "reference_close": (float(signal.reference_close) if signal.reference_close is not None else None),
                "completed_horizons": completed.get(signal.id, set()),
            }
            outcomes = evaluate_decision(
                payload, code_bars=frames[signal.code], benchmark_bars=benchmark, as_of=as_of, settings=settings
            )
            if not outcomes:
                summary["not_mature"] += 1
                continue
            for row in outcomes:
                row["details"] = stamp(row.get("details"), COUNTERFACTUAL_UNGATED)
            inserted = _persist(session, outcomes)
            summary["inserted"] += len(inserted)
            summary["unfilled"] += sum(1 for status in inserted.values() if status == "unfilled")
        except Exception:
            summary["failed"] += 1
    return summary


def summary(session: Session, rule_version: str = V2_RULE) -> dict[str, Any]:
    samples = (
        session.scalar(
            select(func.count()).select_from(RuleShadowSignal).where(RuleShadowSignal.rule_version == rule_version)
        )
        or 0
    )
    outcomes = (
        session.scalar(
            select(func.count())
            .select_from(RuleShadowOutcome)
            .join(RuleShadowSignal, RuleShadowSignal.id == RuleShadowOutcome.signal_id)
            .where(RuleShadowSignal.rule_version == rule_version)
        )
        or 0
    )
    last = session.scalar(
        select(func.max(RuleShadowSignal.decision_date)).where(RuleShadowSignal.rule_version == rule_version)
    )
    return {
        "rule_version": rule_version,
        "samples": int(samples),
        "outcomes": int(outcomes),
        "last_sample_date": last.isoformat() if last else None,
        "return_role": "counterfactual_ungated",
        "executable": False,
    }
