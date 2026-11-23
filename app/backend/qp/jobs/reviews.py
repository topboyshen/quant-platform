"""Append-only reviews for historical job runs.

A review never edits the original ``ops.job_run`` row.  It creates a new
``ops.job_run`` row named ``ops.job_run_review`` whose result points at the
original run, so the original payload and verdict remain immutable and every
review is independently timestamped and attributable.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from qp.models.ops import JobRun

REVIEW_JOB = "ops.job_run_review"


def append_review(
    session: Session,
    *,
    original_run_id: int,
    reviewer: str,
    reason: str,
    fields: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> JobRun:
    original = session.get(JobRun, original_run_id)
    if original is None:
        raise ValueError(f"job run not found: {original_run_id}")
    reviewer = str(reviewer or "").strip()
    reason = str(reason or "").strip()
    if not reviewer or not reason:
        raise ValueError("reviewer and reason are required")
    at = now or dt.datetime.now(dt.UTC)
    row = JobRun(
        job_name=REVIEW_JOB,
        status="succeeded",
        trigger="manual",
        started_at=at,
        finished_at=at,
        result={
            "review_of_run_id": original.id,
            "review_of_job": original.job_name,
            "original_status": original.status,
            "reviewer": reviewer,
            "reason": reason,
            "fields": dict(fields or {}),
            "append_only": True,
        },
    )
    session.add(row)
    session.flush()
    return row
