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
ARTIFACT_DATE_KEYS = ("decision_date", "as_of", "business_date", "data_cutoff", "trade_date", "latest_eod", "date")


def _artifact_date(result: Any) -> dt.date | None:
    """Best-effort artifact cutoff from a job result; None means unknown."""
    if not isinstance(result, dict):
        return None
    candidates = [result]
    if isinstance(result.get("body"), dict):
        candidates.append(result["body"])
    for item in candidates:
        for key in ARTIFACT_DATE_KEYS:
            raw = item.get(key)
            if raw is None:
                continue
            try:
                return dt.date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
    return None


def require_job_success(
    engine: Any,
    upstream: str,
    *,
    now: dt.datetime | None = None,
    max_age_hours: float = 8.0,
    same_day: bool = True,
    expected_date: dt.date | None = None,
    require_latest_success: bool = False,
) -> dict[str, Any]:
    """Return ``{"ok": True, ...}`` only when the upstream succeeded today.

    When ``expected_date`` is supplied, the upstream's artifact date is part of
    the contract: missing, future, and stale dates all block with distinct
    reasons instead of only rejecting the stale case.
    """
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
        latest = session.scalar(
            select(JobRun)
            .where(JobRun.job_name == upstream)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(1)
        )
    base: dict[str, Any] = {
        "upstream": upstream,
        "checked_at": current.isoformat(),
        "latest_run_id": latest.id if latest is not None else None,
        "latest_status": latest.status if latest is not None else None,
        "latest_started_at": latest.started_at.isoformat() if latest is not None and latest.started_at else None,
    }
    later_failure = None
    if run is not None and latest is not None and latest.id != run.id and latest.status in ("failed", "blocked"):
        later_failure = {
            "run_id": latest.id,
            "status": latest.status,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "error": (latest.error or "")[:200] or None,
        }
        base["later_failure"] = later_failure
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
    if require_latest_success and later_failure is not None:
        return {
            **base,
            "ok": False,
            "reason": "upstream_superseded",
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
    artifact_date = _artifact_date(run.result)
    if expected_date is not None:
        if artifact_date is None:
            return {
                **base,
                "ok": False,
                "reason": "upstream_artifact_unknown",
                "upstream_run_id": run.id,
                "upstream_finished_at": finished.isoformat(),
                "artifact_date": None,
                "expected_date": expected_date.isoformat(),
            }
        if artifact_date > expected_date:
            return {
                **base,
                "ok": False,
                "reason": "upstream_artifact_future",
                "upstream_run_id": run.id,
                "upstream_finished_at": finished.isoformat(),
                "artifact_date": artifact_date.isoformat(),
                "expected_date": expected_date.isoformat(),
            }
        if artifact_date < expected_date:
            return {
                **base,
                "ok": False,
                "reason": "upstream_artifact_stale",
                "upstream_run_id": run.id,
                "upstream_finished_at": finished.isoformat(),
                "artifact_date": artifact_date.isoformat(),
                "expected_date": expected_date.isoformat(),
            }
    return {
        **base,
        "ok": True,
        "upstream_run_id": run.id,
        "upstream_finished_at": finished.isoformat(),
        "age_hours": round(age.total_seconds() / 3600, 2),
        "artifact_date": artifact_date.isoformat() if artifact_date is not None else None,
    }
