"""Append-only historical job reviews."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs.reviews import REVIEW_JOB, append_review
from qp.models.ops import JobRun


def _clean(engine):
    with Session(engine) as session:
        session.execute(delete(JobRun).where(JobRun.job_name.in_(("test.reviewed", REVIEW_JOB))))
        session.commit()


def test_review_is_appended_without_touching_original(engine):
    _clean(engine)
    try:
        with Session(engine) as session:
            original = JobRun(
                job_name="test.reviewed",
                status="succeeded",
                trigger="timer",
                started_at=dt.datetime(2026, 11, 23, 16, 50, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 11, 23, 16, 50, tzinfo=dt.UTC),
                result={"verdict": "PASS", "run_quality": "natural"},
            )
            session.add(original)
            session.flush()
            first = append_review(
                session,
                original_run_id=original.id,
                reviewer="jie",
                reason="re-check after funnel wording change",
                fields={"run_quality": "manual_recovered"},
            )
            second = append_review(
                session,
                original_run_id=original.id,
                reviewer="jie",
                reason="second independent review",
            )
            session.commit()
            original_id, first_id, second_id = original.id, first.id, second.id

        with Session(engine) as session:
            original = session.get(JobRun, original_id)
            assert original.result == {"verdict": "PASS", "run_quality": "natural"}
            reviews = list(session.scalars(select(JobRun).where(JobRun.job_name == REVIEW_JOB).order_by(JobRun.id)))
            assert [row.id for row in reviews] == [first_id, second_id]
            assert reviews[0].result["review_of_run_id"] == original_id
            assert reviews[0].result["review_of_job"] == "test.reviewed"
            assert reviews[0].result["fields"] == {"run_quality": "manual_recovered"}
            assert reviews[0].result["append_only"] is True
            assert reviews[1].result["reason"] == "second independent review"
    finally:
        _clean(engine)


def test_review_requires_existing_run_and_attribution(engine):
    _clean(engine)
    try:
        with Session(engine) as session:
            with pytest.raises(ValueError):
                append_review(session, original_run_id=999999, reviewer="jie", reason="x")
            original = JobRun(
                job_name="test.reviewed",
                status="succeeded",
                trigger="timer",
                started_at=dt.datetime(2026, 11, 23, 16, 50, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 11, 23, 16, 50, tzinfo=dt.UTC),
                result={},
            )
            session.add(original)
            session.flush()
            with pytest.raises(ValueError):
                append_review(session, original_run_id=original.id, reviewer="", reason="x")
            with pytest.raises(ValueError):
                append_review(session, original_run_id=original.id, reviewer="jie", reason="")
    finally:
        _clean(engine)
