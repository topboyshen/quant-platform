"""P1 same-day upstream job gates."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.jobs import JOBS, run_job
from qp.jobs.gates import require_job_success
from qp.jobs.registry import JobContext
from qp.jobs.signals import chain_window_job
from qp.models.ops import JobRun


def _run(engine, *, status="succeeded", finished=None, started=None, result=None, name="test.gate.upstream"):
    at = finished or dt.datetime.now(dt.UTC)
    with Session(engine) as s:
        s.add(
            JobRun(
                job_name=name,
                status=status,
                trigger="test",
                started_at=started or at,
                finished_at=at if status == "succeeded" else None,
                result=result or {},
            )
        )
        s.commit()


def _clean(engine, name="test.gate.upstream"):
    with Session(engine) as s:
        s.execute(delete(JobRun).where(JobRun.job_name == name))
        s.commit()


def test_gate_disabled_by_default(engine):
    out = require_job_success(engine, "test.gate.upstream")
    assert out["ok"] is True and out["gate"] == "disabled"


def test_gate_blocks_missing_failed_stale_and_future(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")
    _clean(engine)
    try:
        missing = require_job_success(engine, "test.gate.upstream")
        assert missing["ok"] is False and missing["reason"] == "upstream_missing"
        _run(engine, status="failed")
        failed = require_job_success(engine, "test.gate.upstream")
        assert failed["ok"] is False and failed["reason"] == "upstream_missing"
        _clean(engine)
        reference = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)
        old = reference - dt.timedelta(hours=2)
        _run(engine, finished=old)
        stale = require_job_success(engine, "test.gate.upstream", now=reference, max_age_hours=1)
        assert stale["ok"] is False and stale["reason"] == "upstream_stale"
        _clean(engine)
        future = reference + dt.timedelta(minutes=10)
        _run(engine, finished=future)
        ahead = require_job_success(engine, "test.gate.upstream", now=reference)
        assert ahead["ok"] is False and ahead["reason"] == "upstream_in_future"
    finally:
        _clean(engine)


def test_gate_exposes_later_failure_and_can_require_latest(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")
    _clean(engine)
    try:
        reference = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)
        _run(
            engine,
            finished=reference - dt.timedelta(minutes=2),
            result={"decision_date": reference.date().isoformat()},
        )
        _run(engine, status="failed", finished=reference - dt.timedelta(minutes=1))
        visible = require_job_success(engine, "test.gate.upstream", now=reference)
        assert visible["ok"] is True
        assert visible["later_failure"]["status"] == "failed"
        strict = require_job_success(
            engine,
            "test.gate.upstream",
            now=reference,
            require_latest_success=True,
        )
        assert strict["ok"] is False and strict["reason"] == "upstream_superseded"
    finally:
        _clean(engine)


def test_gate_rejects_stale_artifact_date(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")
    _clean(engine)
    try:
        reference = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)
        _run(
            engine,
            finished=reference,
            result={"decision_date": (reference.date() - dt.timedelta(days=1)).isoformat()},
        )
        out = require_job_success(
            engine,
            "test.gate.upstream",
            now=reference,
            expected_date=reference.date(),
        )
        assert out["ok"] is False and out["reason"] == "upstream_artifact_stale"
        assert out["artifact_date"] == (reference.date() - dt.timedelta(days=1)).isoformat()
    finally:
        _clean(engine)


def test_skipped_runs_do_not_count_as_upstream_success(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")
    _clean(engine)
    try:
        reference = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)
        for status in ("skipped_dup", "skipped_locked"):
            _run(engine, status=status, finished=reference)
            out = require_job_success(engine, "test.gate.upstream", now=reference)
            assert out["ok"] is False and out["reason"] == "upstream_missing"
            _clean(engine)
    finally:
        _clean(engine)


def test_gate_accepts_today_success(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")
    _clean(engine)
    try:
        _run(engine)
        out = require_job_success(engine, "test.gate.upstream")
        assert out["ok"] is True and out["upstream_run_id"] > 0
    finally:
        _clean(engine)


def test_successful_job_records_adopted_upstream_evidence(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")
    _clean(engine, "signals.chain_snapshot")
    now = dt.datetime.now(dt.UTC).astimezone(dt.timezone(dt.timedelta(hours=8)))
    with Session(engine) as s:
        s.add(
            JobRun(
                job_name="signals.chain_snapshot",
                status="succeeded",
                trigger="test",
                started_at=now,
                finished_at=now,
                result={"body": {"business_date": now.date().isoformat()}},
            )
        )
        s.commit()
    try:
        out = chain_window_job(JobContext(engine=engine, run_id=0, name="test"))
        assert out["upstream_gate"]["ok"] is True
        assert out["upstream_gate"]["upstream_run_id"]
        assert out["upstream_gate"]["artifact_date"] == now.date().isoformat()
    finally:
        _clean(engine, "signals.chain_snapshot")


def test_blocked_result_gets_blocked_status(engine, monkeypatch):
    monkeypatch.setenv("QP_JOB_GATES", "1")

    def blocked(_ctx):
        return {"ok": False, "blocked": "upstream_gate", "gate": {"reason": "test"}}

    from qp.jobs.registry import JobSpec

    JOBS["test.gate.blocked"] = JobSpec("test.gate.blocked", blocked, "test", None, None)
    try:
        row = run_job(engine, "test.gate.blocked", trigger="test")
        assert row.status == "blocked"
        assert row.result["blocked"] == "upstream_gate"
    finally:
        JOBS.pop("test.gate.blocked", None)
        with Session(engine) as s:
            s.execute(delete(JobRun).where(JobRun.job_name == "test.gate.blocked"))
            s.commit()
