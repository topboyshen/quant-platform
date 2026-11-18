"""Record-chain shadow window: a per-day table for the first N native sessions.

The window starts at the first business day with a native (``source='timer'``)
chain batch.  A day is only PASS when the EOD run reached a terminal,
successful/partial state, the snapshot is complete, the snapshot-vs-
target_decision funnel comparison has no diffs, and the snapshot reports no
engineering loss.  Before the daily window is
due, the row is ``PENDING`` (待运行) or ``RUNNING`` (进行中), never a premature
PASS/FAIL.  The five-session verdict is intentionally not final until all
planned sessions have been evaluated.

History imported as ``legacy`` never opens the window.

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
CST = dt.timezone(dt.timedelta(hours=8))
EOD_START = dt.time(15, 30)
WINDOW_DUE = dt.time(16, 12)
PASS_RUN_STATUSES = frozenset({"success", "partial"})
LOSS_KEYS = (
    "universe_members_without_decision",
    "signal_decisions_without_eligibility",
    "matured_without_result",
)

STATUS_LABELS = {
    "PASS": "通过",
    "FAIL": "未通过",
    "PENDING": "待运行",
    "RUNNING": "进行中",
    "IN_PROGRESS": "观察中",
    "NOT_STARTED": "未开始",
}

REASON_LABELS = {
    "ok": "通过",
    "not_due": "未到观察日",
    "before_eod": "待运行",
    "eod_window": "进行中",
    "in_progress": "进行中",
    "run_running": "进行中",
    "awaiting_snapshot": "进行中",
    "no_run": "缺少 EOD run",
    "run_not_terminal": "运行未结束",
    "run_failed": "EOD 失败",
    "run_skipped": "EOD 跳过",
    "no_snapshot": "缺少快照",
    "snapshot_incomplete": "快照未完整",
    "engineering_loss": "工程丢记录",
    "compare_diffs": "漏斗不一致",
}


def _normalise_now(now: dt.datetime | None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=CST)
    return now.astimezone(CST)


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


def _planned_days(session: Session, start: dt.date, sessions: int) -> list[dt.date]:
    """Return the planned open sessions, including future calendar days."""
    sessions = max(1, sessions)
    end = start + dt.timedelta(days=max(60, sessions * 3))
    days = _open_days(session, start, end)
    if len(days) >= sessions:
        return days[:sessions]
    # Calendar gaps or a calendar that does not reach the fifth session: keep the
    # window explicit with weekday fallback rather than silently shortening it.
    cursor = (days[-1] if days else start - dt.timedelta(days=1)) + dt.timedelta(days=1)
    while len(days) < sessions:
        if cursor.weekday() < 5 and cursor not in days:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return sorted(days)[:sessions]


def _set_status(row: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    row["status"] = status
    row["status_label"] = STATUS_LABELS.get(status, status)
    row["reason"] = reason
    row["reason_label"] = REASON_LABELS.get(reason, reason)
    return row


def _day_row(session: Session, day: dt.date, *, now: dt.datetime, as_of: dt.date, today: dt.date) -> dict[str, Any]:
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

    body = snapshot.body if snapshot is not None and isinstance(snapshot.body, dict) else {}
    row: dict[str, Any] = {
        "date": day.isoformat(),
        "run_status": run.status if run is not None else None,
        "run_outcome": run.decision_outcome if run is not None else None,
        "run_attempt": run.attempt_no if run is not None else None,
        "run_finished": run.finished_at.isoformat() if run is not None and run.finished_at is not None else None,
        "decisions": sum(int(n) for n in states.values()),
        "allowed": int(states.get("allowed", 0)),
        "blocked": int(states.get("blocked", 0)),
        "no_trigger": int(states.get("no_trigger", 0)),
        "data_failed": int(states.get("data_failed", 0)),
        "snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
        "snapshot_status": body.get("status"),
        "snapshot_complete": body.get("status") == "complete",
        "generated_at": snapshot.generated_at.isoformat() if snapshot is not None else None,
        "funnel": body.get("funnel"),
        "loss": body.get("loss") or {},
        "loss_total": sum(int((body.get("loss") or {}).get(key) or 0) for key in LOSS_KEYS),
        "compare_ok": None,
        "diffs": [],
        "shadow_samples": int(shadow_samples),
        "shadow_outcomes": int(shadow_outcomes),
        "status": None,
        "status_label": None,
        "reason": None,
        "reason_label": None,
    }

    # Future / pre-due states must never be reported as PASS or FAIL.
    if day > as_of:
        return _set_status(row, "PENDING", "not_due")
    # Only the actual current day has a wall-clock phase.  A historical day is
    # evaluated even when the check is run in the morning.
    if day == today and now.time() < EOD_START:
        if run is None and snapshot is None:
            return _set_status(row, "PENDING", "before_eod")
        return _set_status(row, "RUNNING", "in_progress")
    if day == today and now.time() < WINDOW_DUE:
        if run is not None and run.status == "running":
            return _set_status(row, "RUNNING", "run_running")
        return _set_status(row, "RUNNING", "eod_window" if run is None else "awaiting_snapshot")

    if run is None:
        return _set_status(row, "FAIL", "no_run")
    if run.status == "running" or run.finished_at is None:
        return _set_status(row, "RUNNING", "run_running" if run.status == "running" else "run_not_terminal")
    if run.status not in PASS_RUN_STATUSES:
        return _set_status(row, "FAIL", f"run_{run.status}")
    if snapshot is None:
        return _set_status(row, "FAIL", "no_snapshot")
    if not row["snapshot_complete"]:
        return _set_status(row, "FAIL", "snapshot_incomplete")
    if row["loss_total"]:
        return _set_status(row, "FAIL", "engineering_loss")

    comparison = chain_compare.compare(session, day)
    row["compare_ok"] = bool(comparison.get("ok"))
    row["diffs"] = comparison.get("diffs") or []
    return _set_status(row, "PASS" if row["compare_ok"] else "FAIL", "ok" if row["compare_ok"] else "compare_diffs")


def _not_started(sessions: int, now: dt.datetime, as_of: dt.date) -> dict[str, Any]:
    return {
        "status": "NOT_STARTED",
        "verdict": "NOT_STARTED",
        "verdict_label": STATUS_LABELS["NOT_STARTED"],
        "window_start": None,
        "window_end": None,
        "first_native_date": None,
        "days_total": 0,
        "days_evaluated": 0,
        "days_pass": 0,
        "days_failed": 0,
        "days_pending": 0,
        "sessions": sessions,
        "final": False,
        "at_risk": False,
        "as_of": as_of.isoformat(),
        "rows": [],
        "generated_at": now.astimezone(dt.UTC).isoformat(),
    }


def report(
    session: Session,
    *,
    end: dt.date | None = None,
    sessions: int = WINDOW_SESSIONS,
    start: dt.date | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Window table; by default the first ``sessions`` native days.

    ``end`` is an as-of cap (defaults to today).  Planned days may extend past it;
    those rows are reported as ``PENDING`` so the window always shows ``x/5``.
    """
    local_now = _normalise_now(now)
    sessions = max(1, int(sessions))
    today = local_now.date()
    as_of = min(end or today, today)

    if start is not None:
        first_native = start
    else:
        first_native = session.scalar(
            select(func.min(ChainBatch.trade_date)).where(ChainBatch.source == "timer", ChainBatch.trade_date <= as_of)
        )
    if first_native is None:
        return _not_started(sessions, local_now, as_of)

    days = _planned_days(session, first_native, sessions)
    rows = [_day_row(session, day, now=local_now, as_of=as_of, today=today) for day in days]

    passed = sum(1 for row in rows if row["status"] == "PASS")
    failed = sum(1 for row in rows if row["status"] == "FAIL")
    evaluated = passed + failed
    pending = len(rows) - evaluated
    final = len(rows) == sessions and evaluated == len(rows)
    if evaluated < len(rows):
        verdict = "IN_PROGRESS"
    elif failed:
        verdict = "FAIL"
    else:
        verdict = "PASS"

    return {
        "status": verdict,
        "verdict": verdict,
        "verdict_label": STATUS_LABELS.get(verdict, verdict),
        "window_start": rows[0]["date"] if rows else None,
        "window_end": rows[-1]["date"] if rows else None,
        "first_native_date": first_native.isoformat() if first_native else None,
        "days_total": len(rows),
        "days_evaluated": evaluated,
        "days_pass": passed,
        "days_failed": failed,
        "days_pending": pending,
        "sessions": sessions,
        "final": final,
        "at_risk": failed > 0 and not final,
        "as_of": as_of.isoformat(),
        "rows": rows,
        "generated_at": local_now.astimezone(dt.UTC).isoformat(),
    }


def markdown(report_data: dict[str, Any]) -> str:
    """Small evidence table for a runbook or a manual check."""
    verdict = report_data.get("verdict")
    lines = [
        f"# 记录链影子窗口 {report_data.get('window_start')} → {report_data.get('window_end')}",
        "",
        f"verdict: **{STATUS_LABELS.get(verdict, verdict)}** "
        f"（已通过 {report_data.get('days_pass', 0)}/{report_data.get('days_total', 0)} 个计划交易日；"
        f"已评估 {report_data.get('days_evaluated', 0)}；"
        f"待运行/进行中 {report_data.get('days_pending', 0)}）",
        "",
        "| 日期 | 结果 | EOD | 快照 | 一致 | 决策 | 放行 | 拦截 | 样本/结果 | 说明 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in report_data.get("rows") or []:
        compare = "—" if row.get("compare_ok") is None else ("是" if row.get("compare_ok") else "否")
        lines.append(
            f"| {row['date']} | {row.get('status_label') or row.get('status')} | "
            f"{row.get('run_status') or '-'} | "
            f"{row.get('snapshot_status') or '-'} | "
            f"{compare} | {row['decisions']} | "
            f"{row['allowed']} | {row['blocked']} | "
            f"{row['shadow_samples']}/{row['shadow_outcomes']} | "
            f"{row.get('reason_label') or row.get('reason')} |"
        )
    if not (report_data.get("rows") or []):
        lines.append("| - | - | - | - | - | - | - | - | - | 窗口未开始 |")
    return "\n".join(lines) + "\n"
