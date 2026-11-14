"""M4 signals jobs (SHADOW). Writes only to db qp schema ``signals``; no notification and no
legacy state file is touched. Deployment of the timer stays gated on M3 PASS."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext, job
from qp.signals.eod import (now_cst, previous_open_day, record_skipped_run, run_eod,
                            trading_day_status)
from qp.signals.approved import build_previous_day_digest
from qp.signals.outcomes import evaluate_pending
from qp.signals.repair import run_repair
from qp.signals.watchdog import latest_retryable_run, run_watchdog


@job("signals.eod", "Swing EOD: 40-ETF trend scan + macro-risk gate -> signals.*",
     schedule_hint="Mon-Fri 15:30 CST (legacy 15:30; notifications live since 2026-11-13)")
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


@job("signals.eod_retry",
     "Same-day EOD retry only after data_unavailable with zero signals (legacy 15:48 gate)",
     schedule_hint="Mon-Fri 15:48 CST (ExecCondition equivalent; never starts a first run)")
def eod_retry_job(ctx: JobContext) -> dict:
    today = now_cst().date()
    with Session(ctx.engine) as session:
        is_open, calendar_source = trading_day_status(session, today)
        if not is_open:
            return {"ok": True, "retried": False, "reason": "non_trading_day",
                    "calendar_source": calendar_source}
        latest, retryable = latest_retryable_run(session, today)
        if not retryable:
            return {"ok": True, "retried": False, "reason": "not_retryable",
                    "latest_status": getattr(latest, "status", None),
                    "latest_outcome": getattr(latest, "decision_outcome", None)}
        previous = previous_open_day(session, today)
        out = run_eod(session, decision_date=today, previous_session=previous)
        session.commit()
    out.update({"ok": out["status"] != "failed", "retried": True,
                "calendar_source": calendar_source})
    return out


def eod_for_date(ctx: JobContext, decision_date: dt.date) -> dict:
    """Manual/backfill helper: one explicit attempt (still shadow-only)."""
    with Session(ctx.engine) as session:
        previous = previous_open_day(session, decision_date)
        out = run_eod(session, decision_date=decision_date, previous_session=previous)
        session.commit()
    out["ok"] = out["status"] != "failed"
    return out


@job("signals.watchdog", "Swing EOD terminal audit 15:40/15:50; shadow outbox only",
     schedule_hint="Mon-Fri 15:40,15:50 CST")
def watchdog_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = run_watchdog(session, source_job_run_id=ctx.run_id)
        session.commit()
    # A failed EOD is the object of this inspection, not a crash of the watchdog job.
    out["inspection_ok"] = bool(out.get("ok"))
    out["ok"] = True
    return out


@job("signals.total_amount_repair",
     "Fill an empty same-day total_amount after partial/macro_data_partial (15:45-16:00)",
     schedule_hint="Mon-Fri 15:47,15:57 CST")
def total_amount_repair_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = run_repair(session)
        session.commit()
    out["ok"] = bool(out.get("ok"))
    return out


@job("signals.outcomes", "Collect matured 1/3/5/10/20-session outcomes for swing decisions",
     schedule_hint="Mon-Fri 16:10 CST")
def outcomes_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = evaluate_pending(session, as_of=now_cst().date())
        session.commit()
    out["ok"] = bool(out.get("benchmark_ok"))
    return out


@job("signals.execution_digest",
     "Previous-day approved-signal digest; write files + shadow row, never POST",
     schedule_hint="manual only (digest timer removed 2026-11-13)")
def execution_digest_job(ctx: JobContext) -> dict:
    with Session(ctx.engine) as session:
        out = build_previous_day_digest(session, now=now_cst())
        session.commit()
    out["ok"] = bool(out.get("ok"))
    return out
