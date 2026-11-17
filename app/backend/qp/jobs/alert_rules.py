"""P1 operational alert rules.

These rules only enqueue through the existing ``notify.outbox`` path; delivery
still obeys ``QP_NOTIFY_MODE``, the kill switch and the disabled file.  They do
not write business tables, run jobs or send directly.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.ops import JobRun


def _job_runs(session: Session, job_name: str, limit: int) -> list[JobRun]:
    return list(
        session.scalars(
            select(JobRun)
            .where(JobRun.job_name == job_name, JobRun.status.not_in(("skipped_locked", "skipped_dup")))
            .order_by(JobRun.id.desc())
            .limit(limit)
        )
    )


def _enqueue(session: Session, *, key: str, subject: str, body: str, run_id: int, now: dt.datetime) -> bool:
    from qp.jobs.builtin import enqueue

    return enqueue(
        session,
        channel="telegram",
        recipient="ops",
        dedup_key=f"ops.rule:{key}:{now:%Y%m%dT%H}",
        subject=f"[warning] {subject}",
        body=body,
        source_job_run_id=run_id,
    )


def evaluate_alerts(session: Session, run_id: int, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Evaluate the P1 rules and enqueue at most one row per key/hour."""
    from qp.jobs.registry import JOBS

    current = now or utcnow()
    enqueued: list[str] = []

    for name in sorted(JOBS):
        if name in {"ops.healthcheck", "ops.backup"}:
            continue
        runs = _job_runs(session, name, 2)
        if len(runs) == 2 and all(run.status in {"failed", "blocked"} for run in runs):
            key = f"job_consecutive:{name}"
            body = (
                f"{name} failed twice consecutively: {runs[1].id}/{runs[1].status} then {runs[0].id}/{runs[0].status}"
            )
            if _enqueue(session, key=key, subject=key, body=body, run_id=run_id, now=current):
                enqueued.append(key)

    backup_runs = _job_runs(session, "ops.backup", 1)
    if backup_runs and backup_runs[0].status in {"failed", "blocked"}:
        key = "backup_failure"
        run = backup_runs[0]
        body = f"ops.backup latest run {run.id} status={run.status} finished={run.finished_at}"
        if _enqueue(session, key=key, subject=key, body=body, run_id=run_id, now=current):
            enqueued.append(key)

    source_runs = _job_runs(session, "market.sources", 1)
    if source_runs:
        result = source_runs[0].result if isinstance(source_runs[0].result, dict) else {}
        failed = [str(item) for item in (result.get("failed") or [])]
        # Eastmoney is the documented fallback (KL-001); do not page for it alone.
        bad = sorted({item for item in failed if "eastmoney" not in item.lower()})
        if bad:
            key = "market_source_failure"
            body = "market.sources failed: " + ", ".join(bad)
            if _enqueue(session, key=key, subject=key, body=body, run_id=run_id, now=current):
                enqueued.append(key)

    return {"ok": True, "enqueued": enqueued, "checked_at": current.isoformat()}
