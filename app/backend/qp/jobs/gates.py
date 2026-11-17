"""Same-day upstream job gates for the signal pipeline (P1).

Timers still trigger jobs, but a downstream job verifies that its upstream
``ops.job_run`` succeeded on the same Asia/Shanghai business day.  A missing,
failed, stale, or future run is returned as a structured block; the job runner
records the run with status ``blocked`` instead of silently doing work.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.models.ops import JobRun

CST = ZoneInfo("Asia/Shanghai")
MAX_FUTURE_SKEW = dt.timedelta(minutes=5)


def require_job_success(
    engine: Any,
    upstream: str,
    *,
    now: dt.datetime | None = None,
    max_age_hours: float = 8.0,
    same_day: bool = True,
) -> dict[str, Any]:
    """Return ``{"ok": True, ...}`` only when the upstream succeeded today."""
    if not get_settings().job_gates:
        return {"ok": True, "gate": "disabled", "upstream": upstream}
    current = now or dt.datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)
    with Session(engine) as session:
        run = session.scalar(
            select(JobRun)
            .where(JobRun.job_name == upstream, JobRun.status == "succeeded")
            .order_by(JobRun.finished_at.desc(), JobRun.id.desc())
            .limit(1)
        )
    base = {"upstream": upstream, "checked_at": current.isoformat()}
    if run is None or run.finished_at is None:
        return {**base, "ok": False, "reason": "upstream_missing"}
    finished = run.finished_at
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=CST)
    finished = finished.astimezone(CST)
    if same_day and finished.date() != current.date():
        return {
            **base,
            "ok": False,
            "reason": "upstream_not_same_day",
            "upstream_run_id": run.id,
            "upstream_finished_at": finished.isoformat(),
        }
    age = current - finished
    if age < -MAX_FUTURE_SKEW:
        return {
            **base,
            "ok": False,
            "reason": "upstream_in_future",
            "upstream_run_id": run.id,
            "upstream_finished_at": finished.isoformat(),
        }
    if age > dt.timedelta(hours=max_age_hours):
        return {
            **base,
            "ok": False,
            "reason": "upstream_stale",
            "upstream_run_id": run.id,
            "upstream_finished_at": finished.isoformat(),
            "age_hours": round(age.total_seconds() / 3600, 2),
        }
    return {
        **base,
        "ok": True,
        "upstream_run_id": run.id,
        "upstream_finished_at": finished.isoformat(),
        "age_hours": round(age.total_seconds() / 3600, 2),
    }
