"""M4 signals jobs (SHADOW). Writes only to db qp schema ``signals``; no notification and no
legacy state file is touched. Deployment of the timer stays gated on M3 PASS."""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.jobs.gates import require_job_success
from qp.jobs.registry import JobContext, job
from qp.models.market import TradingDay
from qp.signals.approved import build_previous_day_digest
from qp.signals.eod import (
    now_cst,
    previous_open_day,
    record_skipped_run,
    run_eod,
    trading_day_status,
)
from qp.signals.outcomes import evaluate_pending
from qp.signals.repair import run_repair
from qp.signals.watchdog import latest_retryable_run, run_watchdog


def _gate(
    ctx: JobContext,
    upstream: str,
    *,
    max_age_hours: float = 8.0,
    expected_date: dt.date | None = None,
    require_latest_success: bool = False,
) -> dict:
    return require_job_success(
        ctx.engine,
        upstream,
        max_age_hours=max_age_hours,
        expected_date=expected_date,
        require_latest_success=require_latest_success,
    )


def _blocked(gate: dict) -> dict | None:
    if gate.get("ok"):
        return None
    return {"ok": False, "blocked": "upstream_gate", "gate": gate}


@job(
    "signals.eod",
    "Swing EOD: 40-ETF trend scan + macro-risk gate -> signals.*",
    schedule_hint="Mon-Fri 15:30 CST (legacy 15:30; notifications live since 2026-11-13)",
)
def eod_job(ctx: JobContext) -> dict:
    today = now_cst().date()
    with Session(ctx.engine) as session:
        is_open, calendar_source = trading_day_status(session, today)
        if not is_open:
            out = record_skipped_run(session, today, source=calendar_source)
            session.commit()
            out["ok"] = True
            return out
        previous = previous_open_day(session, today)
        out = run_eod(session, decision_date=today, previous_session=previous)
        session.commit()
    out["calendar_source"] = calendar_source
    out["previous_session"] = previous.isoformat() if previous else None
    out["ok"] = out["status"] != "failed"
    return out


@job(
    "signals.eod_retry",
    "Same-day EOD retry only after data_unavailable with zero signals (legacy 15:48 gate)",
    schedule_hint="Mon-Fri 15:48 CST (ExecCondition equivalent; never starts a first run)",
)
def eod_retry_job(ctx: JobContext) -> dict:
    today = now_cst().date()
    with Session(ctx.engine) as session:
        is_open, calendar_source = trading_day_status(session, today)
        if not is_open:
            return {"ok": True, "retried": False, "reason": "non_trading_day", "calendar_source": calendar_source}
        latest, retryable = latest_retryable_run(session, today)
        if not retryable:
            return {
                "ok": True,
                "retried": False,
                "reason": "not_retryable",
                "latest_status": getattr(latest, "status", None),
                "latest_outcome": getattr(latest, "decision_outcome", None),
            }
        previous = previous_open_day(session, today)
        out = run_eod(session, decision_date=today, previous_session=previous)
        session.commit()
    out.update({"ok": out["status"] != "failed", "retried": True, "calendar_source": calendar_source})
    return out


def eod_for_date(ctx: JobContext, decision_date: dt.date) -> dict:
    """Manual/backfill helper: one explicit attempt (still shadow-only)."""
    with Session(ctx.engine) as session:
        previous = previous_open_day(session, decision_date)
        out = run_eod(session, decision_date=decision_date, previous_session=previous, batch_source="manual")
        session.commit()
    out["ok"] = out["status"] != "failed"
    return out


@job(
    "signals.watchdog",
    "Swing EOD terminal audit 15:40/15:50; shadow outbox only",
    schedule_hint="Mon-Fri 15:40,15:50 CST",
)
def watchdog_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = run_watchdog(session, source_job_run_id=ctx.run_id)
        session.commit()
    # A failed EOD is the object of this inspection, not a crash of the watchdog job.
    out["inspection_ok"] = bool(out.get("ok"))
    out["ok"] = True
    return out


@job(
    "signals.total_amount_repair",
    "Fill an empty same-day total_amount after partial/macro_data_partial (15:45-16:00)",
    schedule_hint="Mon-Fri 15:47,15:57 CST",
)
def total_amount_repair_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = run_repair(session)
        session.commit()
    out["ok"] = bool(out.get("ok"))
    return out


@job(
    "signals.chain_snapshot",
    "ETF record-chain snapshot for the business day (after EOD window)",
    schedule_hint="Mon-Fri 15:55 CST (record chain; no-op unless QP_RECORD_CHAIN=1)",
)
def chain_snapshot_job(ctx: JobContext) -> dict:
    from qp.signals import chain

    if not chain.enabled():
        return {"ok": True, "skipped": "record_chain_disabled"}
    gate = _gate(ctx, "signals.eod", expected_date=now_cst().date())
    blocked = _blocked(gate)
    if blocked:
        return blocked
    today = now_cst().date()
    with Session(ctx.engine) as session:
        out = chain.build_snapshot(session, business_date=today)
        if out.get("ok"):
            from qp.signals import chain_compare

            out["compare"] = chain_compare.compare(session, today)
        session.commit()
    out["upstream_gate"] = gate
    if out.get("reason") == "no_batch":
        return {"ok": True, "skipped": "no_batch", "business_date": today.isoformat(), "upstream_gate": gate}
    out["ok"] = bool(out.get("ok"))
    return out


@job(
    "signals.chain_window",
    "Record-chain shadow window table (first 5 native sessions) for the daily check",
    schedule_hint="Mon-Fri 16:12 CST (after snapshot 15:55 and compare 16:05)",
)
def chain_window_job(ctx: JobContext) -> dict:
    from qp.signals import chain_window

    gate = _gate(ctx, "signals.chain_snapshot", expected_date=now_cst().date())
    blocked = _blocked(gate)
    if blocked:
        return blocked
    with Session(ctx.engine) as session:
        now = now_cst()
        out = chain_window.report(session, end=now.date(), now=now)
    out["upstream_gate"] = gate
    out["ok"] = out.get("verdict") != "FAIL"
    return out


@job(
    "signals.chain_import_history",
    "One-off: import existing target_decision/decision_outcome rows as legacy.v0 chain history",
    schedule_hint="manual only (record chain history import)",
)
def chain_import_history_job(ctx: JobContext) -> dict:
    from qp.signals import chain, chain_import

    if not chain.enabled():
        return {"ok": True, "skipped": "record_chain_disabled"}
    with Session(ctx.engine) as session:
        out = chain_import.import_history(session, apply=True)
        session.commit()
    out["ok"] = True
    return out


@job(
    "signals.outcomes",
    "Collect matured 1/3/5/10/20-session outcomes for swing decisions",
    schedule_hint="Mon-Fri 16:10 CST",
)
def outcomes_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = evaluate_pending(session, as_of=now_cst().date())
        session.commit()
    out["ok"] = bool(out.get("benchmark_ok"))
    return out


@job(
    "signals.shadow_record",
    "Copy risk-blocked triggered entries into the shadow sample channel (validation only)",
    schedule_hint="Mon-Fri 15:36 CST (after EOD; no notification)",
)
def shadow_record_job(ctx: JobContext) -> dict:
    from qp.signals import shadow

    with Session(ctx.engine) as session:
        # every date: a late EOD is picked up by the next run instead of being skipped
        out = shadow.record(session)
        session.commit()
    out["ok"] = True
    return out


@job(
    "signals.rule_shadow",
    "Experimental rule qp-swing.v2 signals (2x volume breakout); shadow tables only",
    schedule_hint="Mon-Fri 15:37 CST (after EOD; never live, never notified)",
)
def rule_shadow_job(ctx: JobContext) -> dict:
    from qp.signals import rule_shadow

    today = now_cst().date()
    with Session(ctx.engine) as session:
        is_open = (
            session.scalar(
                select(func.count())
                .select_from(TradingDay)
                .where(TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date == today)
            )
            or 0
        )
        if not is_open:
            return {"ok": True, "skipped": "non_trading_day", "day": today.isoformat()}
        gate = _gate(ctx, "signals.eod", expected_date=today)
        blocked = _blocked(gate)
        if blocked:
            return blocked
        out = rule_shadow.generate(session, day=today)
        session.commit()
    out["upstream_gate"] = gate
    out["ok"] = True
    return out


@job(
    "signals.rule_shadow_outcomes",
    "Forward returns for the experimental rule signals (1/3/5/10/20 sessions)",
    schedule_hint="Mon-Fri 16:17 CST (after shadow outcomes)",
)
def rule_shadow_outcomes_job(ctx: JobContext) -> dict:
    from qp.signals import rule_shadow

    with Session(ctx.engine) as session:
        out = rule_shadow.collect(session, as_of=now_cst().date())
        session.commit()
    out["ok"] = bool(out.get("benchmark_ok"))
    return out


@job(
    "signals.rotation_shadow",
    "C13 cross-sectional ETF rotation shadow (250d Top8, 20-session rebalance); shadow tables only",
    schedule_hint="Mon-Fri 15:50, 16:40 CST (idempotent publication-lag retry; no live/notify)",
)
def rotation_shadow_job(ctx: JobContext) -> dict:
    from qp.signals import rotation_shadow

    today = now_cst().date()
    with Session(ctx.engine) as session:
        is_open = session.scalar(select(TradingDay.is_open).where(TradingDay.origin == "qp", TradingDay.date == today))
        if is_open is None:
            return {"ok": False, "error": "calendar_day_unknown", "day": today.isoformat()}
        if not is_open:
            return {"ok": True, "skipped": "non_trading_day", "day": today.isoformat()}
        out = rotation_shadow.step(session, as_of=today)
        session.commit()
    out["ok"] = bool(out.get("ok"))
    return out


@job(
    "signals.shadow_outcomes",
    "Forward returns for shadow allowed samples (1/3/5/10/20 sessions)",
    schedule_hint="Mon-Fri 16:15 CST (after outcomes 16:10)",
)
def shadow_outcomes_job(ctx: JobContext) -> dict:
    from qp.signals import shadow

    with Session(ctx.engine) as session:
        out = shadow.collect(session, as_of=now_cst().date())
        session.commit()
    out["ok"] = bool(out.get("benchmark_ok"))
    return out


@job(
    "signals.acceptance",
    "M4 live acceptance invariants for the latest decision day (replaces legacy compare)",
    schedule_hint="Mon-Fri 16:50 CST (after outcomes 16:10 and index_close 16:30)",
)
def acceptance_job(ctx: JobContext) -> dict:
    from qp.signals import acceptance

    today = now_cst().date()
    with Session(ctx.engine) as session:
        gate = _gate(ctx, "signals.outcomes", expected_date=today)
        blocked = _blocked(gate)
        if blocked:
            return blocked
        day = acceptance.latest_decision_date(session, today)
        if day is None:
            return {"ok": False, "reason": "no_runs", "upstream_gate": gate}
        out = acceptance.evaluate(session, day, today=today)
    out["upstream_gate"] = gate
    out["ok"] = out["verdict"] != "FAIL"
    return out


@job(
    "signals.execution_digest",
    "Previous-day approved-signal digest; write files + shadow row, never POST",
    schedule_hint="manual only (digest timer removed 2026-11-13)",
)
def execution_digest_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = build_previous_day_digest(session, now=now_cst())
        session.commit()
    out["ok"] = bool(out.get("ok"))
    return out


def _attribution_pct(value: object) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _attribution_pp(current: object, previous: object) -> str:
    try:
        return f"{(float(current) - float(previous)) * 100:+.2f}pp"
    except (TypeError, ValueError):
        return "—"


def _attribution_changes(data: dict, previous: dict) -> list[dict]:
    """Groups whose average excess worsened week over week (same bucket only)."""
    changes: list[dict] = []
    for dimension in data.get("group_by") or []:
        old = {group["bucket"]: group for group in (previous.get("groups") or {}).get(dimension) or []}
        for group in (data.get("groups") or {}).get(dimension) or []:
            prior = old.get(group["bucket"])
            if prior is None:
                continue
            current_excess = group.get("avg_excess_return")
            prior_excess = prior.get("avg_excess_return")
            if current_excess is None or prior_excess is None:
                continue
            delta = float(current_excess) - float(prior_excess)
            if delta >= -0.002:
                continue
            changes.append(
                {
                    "dimension": dimension,
                    "dimension_label": data["dimension_labels"][dimension],
                    "bucket": group["bucket"],
                    "n": group["n"],
                    "excess_current": current_excess,
                    "excess_previous": prior_excess,
                    "excess_delta": round(delta, 6),
                }
            )
    changes.sort(key=lambda item: item["excess_delta"])
    return changes[:3]


def _attribution_text(data: dict, previous: dict | None = None) -> str:
    pct = _attribution_pct
    sample = data["sample"]
    overall = data["overall"]
    eligible = data["eligible"]
    lines = [
        f"信号链绩效归因（{data['horizon']} 日） 截至 {data['as_of']}",
        f"样本：成熟且成交 {sample['matured_filled']} 条，合格 {sample['eligible_matured']} 条"
        f"（阈值 {data['threshold']}）",
        f"总体：n={overall['n']}，胜率 {pct(overall['win_rate'])}，平均净收益 {pct(overall['avg_net_return'])}，"
        f"平均超额 {pct(overall['avg_excess_return'])}",
        f"合格：n={eligible['n']}，胜率 {pct(eligible['win_rate'])}，平均净收益 {pct(eligible['avg_net_return'])}，"
        f"平均超额 {pct(eligible['avg_excess_return'])}",
    ]
    if previous is not None:
        old_overall = previous.get("overall") or {}
        old_eligible = previous.get("eligible") or {}
        lines.append(
            f"总体环比（截至 {previous.get('as_of')}）：胜率 "
            f"{pct(old_overall.get('win_rate'))} → {pct(overall.get('win_rate'))}"
            f"（{_attribution_pp(overall.get('win_rate'), old_overall.get('win_rate'))}）；平均超额 "
            f"{pct(old_overall.get('avg_excess_return'))} → {pct(overall.get('avg_excess_return'))}"
            f"（{_attribution_pp(overall.get('avg_excess_return'), old_overall.get('avg_excess_return'))}）"
        )
        lines.append(
            f"合格环比：胜率 {pct(old_eligible.get('win_rate'))} → {pct(eligible.get('win_rate'))}"
            f"（{_attribution_pp(eligible.get('win_rate'), old_eligible.get('win_rate'))}）；平均超额 "
            f"{pct(old_eligible.get('avg_excess_return'))} → {pct(eligible.get('avg_excess_return'))}"
            f"（{_attribution_pp(eligible.get('avg_excess_return'), old_eligible.get('avg_excess_return'))}）"
        )
        changes = _attribution_changes(data, previous)
        if changes:
            lines.append(
                "恶化维度："
                + "；".join(
                    f"{item['dimension_label']}·{item['bucket']} 超额 "
                    f"{pct(item['excess_previous'])} → {pct(item['excess_current'])}"
                    f"（{_attribution_pp(item['excess_current'], item['excess_previous'])}）"
                    for item in changes
                )
            )
        else:
            lines.append("恶化维度：无（同分组超额均未下降超过 0.2pp 或样本不可比）")
    for warning in data.get("warnings") or []:
        lines.append(f"警告：{warning.get('message') or warning.get('code')}")
    for dimension in data.get("group_by") or []:
        groups = (data.get("groups") or {}).get(dimension) or []
        if not groups:
            lines.append(f"{data['dimension_labels'][dimension]}：无样本")
            continue
        parts = [
            f"{group['bucket']} n={group['n']} 胜率{pct(group['win_rate'])} 超额{pct(group['avg_excess_return'])}"
            for group in groups[:5]
        ]
        lines.append(f"{data['dimension_labels'][dimension]}：" + "；".join(parts))
    lines.append("只读归因，不是交易建议。")
    return "\n".join(lines)


@job(
    "signals.attribution_weekly",
    "Weekly 20-session signal attribution summary (read-only; Telegram outbox)",
    schedule_hint="Mon 08:30 CST (qp-signals-attribution.timer)",
)
def attribution_weekly_job(ctx: JobContext) -> dict:
    from qp.jobs.builtin import enqueue
    from qp.signals import attribution

    today = now_cst().date()
    with Session(ctx.engine) as session:
        data = attribution.report(
            session,
            horizon=20,
            group_by=attribution.DIMENSIONS,
            end=today,
            as_of=today,
        )
        previous = attribution.report(
            session,
            horizon=20,
            group_by=attribution.DIMENSIONS,
            end=today - dt.timedelta(days=7),
            as_of=today - dt.timedelta(days=7),
        )
        changes = _attribution_changes(data, previous)
        text = _attribution_text(data, previous=previous)
        iso = today.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        payload = {
            "report_type": "attribution_weekly",
            "week": week,
            "as_of": today.isoformat(),
            "horizon": 20,
            "sample": data["sample"],
            "threshold": data["threshold"],
            "overall": data["overall"],
            "eligible": data["eligible"],
            "previous_as_of": previous["as_of"],
            "previous": {
                "overall": previous["overall"],
                "eligible": previous["eligible"],
                "sample": previous["sample"],
            },
            "changes": changes,
            "warnings": data["warnings"],
            "text": text,
        }
        mode = "live" if get_settings().signals_notify_mode.strip().lower() == "live" else "shadow"
        inserted = enqueue(
            session,
            channel="telegram",
            recipient="signals" if mode == "live" else "shadow:signals-attribution",
            dedup_key=f"attribution-weekly:{week}",
            subject=f"信号链绩效归因 {week}",
            body=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            source_job_run_id=ctx.run_id,
            mode=mode,
        )
        session.commit()
    return {
        "ok": True,
        "week": week,
        "enqueued": inserted,
        "mode": mode,
        "sample": data["sample"],
    }
