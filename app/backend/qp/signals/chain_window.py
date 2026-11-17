"""Record-chain shadow window: a per-day table for the first N native sessions.

The window starts at the first business day with a native (``source='timer'``)
chain batch.  Each row is PASS only when that day has a terminal EOD run, a
snapshot and a snapshot-vs-target_decision comparison without diffs.  History
imported as ``legacy`` never opens the window.

Read-only: computed from existing tables, nothing is stored.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.models.chain import ChainBatch, ChainSnapshot
from qp.models.market import TradingDay
from qp.models.signals import EodRun, ShadowOutcome, ShadowSignal, TargetDecision
from qp.signals import chain_compare

WINDOW_SESSIONS = 5


def _open_days(session: Session, start: dt.date, end: dt.date) -> list[dt.date]:
    return list(
        session.scalars(
            select(TradingDay.date)
            .where(
                TradingDay.origin == "qp",
                TradingDay.is_open.is_(True),
                TradingDay.date >= start,
                TradingDay.date <= end,
            )
            .order_by(TradingDay.date)
        )
    )


def _day_row(session: Session, day: dt.date) -> dict[str, Any]:
    run = session.scalar(
        select(EodRun)
        .where(EodRun.decision_date == day)
        .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())
        .limit(1)
    )
    states = dict(
        session.execute(
            select(TargetDecision.decision_state, func.count())
            .where(TargetDecision.decision_date == day)
            .group_by(TargetDecision.decision_state)
        ).all()
    )
    snapshot = session.scalar(
        select(ChainSnapshot)
        .where(ChainSnapshot.business_date == day)
        .order_by(ChainSnapshot.generated_at.desc())
        .limit(1)
    )
    shadow_samples = (
        session.scalar(select(func.count()).select_from(ShadowSignal).where(ShadowSignal.decision_date == day)) or 0
    )
    shadow_outcomes = (
        session.scalar(
            select(func.count())
            .select_from(ShadowOutcome)
            .join(ShadowSignal, ShadowSignal.id == ShadowOutcome.shadow_id)
            .where(ShadowSignal.decision_date == day)
        )
        or 0
    )

    funnel = (snapshot.body or {}).get("funnel") if snapshot is not None else None
    row: dict[str, Any] = {
        "date": day.isoformat(),
        "run_status": run.status if run is not None else None,
        "run_outcome": run.decision_outcome if run is not None else None,
        "run_attempt": run.attempt_no if run is not None else None,
        "decisions": sum(int(n) for n in states.values()),
        "allowed": int(states.get("allowed", 0)),
        "blocked": int(states.get("blocked", 0)),
        "no_trigger": int(states.get("no_trigger", 0)),
        "data_failed": int(states.get("data_failed", 0)),
        "snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
        "snapshot_status": (snapshot.body or {}).get("status") if snapshot is not None else None,
        "generated_at": snapshot.generated_at.isoformat() if snapshot is not None else None,
        "funnel": funnel,
        "compare_ok": None,
        "diffs": [],
        "shadow_samples": int(shadow_samples),
        "shadow_outcomes": int(shadow_outcomes),
    }
    if run is None:
        row.update(status="FAIL", reason="no_run")
        return row
    if snapshot is None:
        row.update(status="FAIL", reason="no_snapshot")
        return row
    comparison = chain_compare.compare(session, day)
    row["compare_ok"] = bool(comparison.get("ok"))
    row["diffs"] = comparison.get("diffs") or []
    row["status"] = "PASS" if row["compare_ok"] else "FAIL"
    row["reason"] = "ok" if row["compare_ok"] else "compare_diffs"
    return row


def report(
    session: Session, *, end: dt.date | None = None, sessions: int = WINDOW_SESSIONS, start: dt.date | None = None
) -> dict[str, Any]:
    """Window table; by default the first ``sessions`` native days up to ``end``."""
    end = end or dt.date.today()
    days: list[dt.date]
    if start is not None:
        days = _open_days(session, start, end)[: max(1, sessions)]
        first_native = start
    else:
        first_native = session.scalar(
            select(func.min(ChainBatch.trade_date)).where(ChainBatch.source == "timer", ChainBatch.trade_date <= end)
        )
        if first_native is None:
            return {
                "status": "NOT_STARTED",
                "verdict": "NOT_STARTED",
                "window_start": None,
                "window_end": None,
                "first_native_date": None,
                "days_total": 0,
                "days_pass": 0,
                "rows": [],
                "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        days = _open_days(session, first_native, end)[: max(1, sessions)]
        if not days:  # calendar gaps: fall back to the native batch dates
            days = list(
                session.scalars(
                    select(ChainBatch.trade_date)
                    .where(
                        ChainBatch.source == "timer",
                        ChainBatch.trade_date >= first_native,
                        ChainBatch.trade_date <= end,
                    )
                    .distinct()
                    .order_by(ChainBatch.trade_date)
                )
            )[: max(1, sessions)]

    rows = [_day_row(session, day) for day in days]
    passed = sum(1 for row in rows if row["status"] == "PASS")
    verdict = "PASS" if rows and passed == len(rows) else ("FAIL" if rows else "NOT_STARTED")
    return {
        "status": verdict,
        "verdict": verdict,
        "window_start": rows[0]["date"] if rows else None,
        "window_end": rows[-1]["date"] if rows else None,
        "first_native_date": first_native.isoformat() if first_native else None,
        "days_total": len(rows),
        "days_pass": passed,
        "sessions": sessions,
        "rows": rows,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }


def markdown(report_data: dict[str, Any]) -> str:
    """Small evidence table for a runbook or a manual check."""
    lines = [
        f"# 记录链影子窗口 {report_data.get('window_start')} → {report_data.get('window_end')}",
        "",
        f"verdict: **{report_data.get('verdict')}** ({report_data.get('days_pass')}/{report_data.get('days_total')})",
        "",
        "| 日期 | EOD | 快照 | 一致 | 决策 | 放行 | 拦截 | 样本/结果 | 说明 |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in report_data.get("rows") or []:
        lines.append(
            f"| {row['date']} | {row.get('run_status') or '-'} | "
            f"{row.get('snapshot_status') or '-'} | "
            f"{'是' if row.get('compare_ok') else '否'} | {row['decisions']} | "
            f"{row['allowed']} | {row['blocked']} | "
            f"{row['shadow_samples']}/{row['shadow_outcomes']} | {row.get('reason')} |"
        )
    if not (report_data.get("rows") or []):
        lines.append("| - | - | - | - | - | - | - | - | 窗口未开始 |")
    return "\n".join(lines) + "\n"
