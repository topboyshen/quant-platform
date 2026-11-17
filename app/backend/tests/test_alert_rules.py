"""P1 operational alert rules."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs import JOBS
from qp.jobs.alert_rules import evaluate_alerts
from qp.jobs.registry import JobSpec
from qp.models.notify import Outbox
from qp.models.ops import JobRun

NOW = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)


def _run(engine, name: str, status: str, *, result=None):
    with Session(engine) as s:
        run = JobRun(job_name=name, status=status, trigger="test", started_at=NOW, finished_at=NOW, result=result or {})
        s.add(run)
        s.commit()
        return run.id


def _clean(engine):
    with Session(engine) as s:
        s.execute(delete(Outbox).where(Outbox.dedup_key.like("ops.rule:%")))
        s.execute(delete(JobRun).where(JobRun.job_name.in_(("test.alert.job", "ops.backup", "market.sources"))))
        s.commit()


def test_consecutive_failures_enqueue_once(engine):
    _clean(engine)
    JOBS["test.alert.job"] = JobSpec("test.alert.job", lambda _ctx: {}, "test", None, None)
    try:
        _run(engine, "test.alert.job", "failed")
        _run(engine, "test.alert.job", "failed")
        with Session(engine) as s:
            out = evaluate_alerts(s, 0, now=NOW)
            s.commit()
            rows = list(s.scalars(select(Outbox).where(Outbox.dedup_key.like("ops.rule:%"))))
        assert "job_consecutive:test.alert.job" in out["enqueued"]
        assert len(rows) == 1
        assert rows[0].recipient == "ops" and rows[0].mode == "shadow"
    finally:
        JOBS.pop("test.alert.job", None)
        _clean(engine)


def test_backup_and_source_failures(engine):
    _clean(engine)
    try:
        _run(engine, "ops.backup", "failed")
        _run(engine, "market.sources", "succeeded", result={"failed": ["tencent_names"]})
        with Session(engine) as s:
            out = evaluate_alerts(s, 0, now=NOW)
            s.commit()
            keys = {row.dedup_key for row in s.scalars(select(Outbox))}
        assert "backup_failure" in out["enqueued"]
        assert "market_source_failure" in out["enqueued"]
        assert any("ops.rule:backup_failure:" in key for key in keys)
        assert any("ops.rule:market_source_failure:" in key for key in keys)
    finally:
        _clean(engine)


def test_eastmoney_only_source_failure_is_ignored(engine):
    _clean(engine)
    try:
        _run(engine, "market.sources", "succeeded", result={"failed": ["eastmoney_names"]})
        with Session(engine) as s:
            out = evaluate_alerts(s, 0, now=NOW)
            s.commit()
        assert "market_source_failure" not in out["enqueued"]
    finally:
        _clean(engine)
