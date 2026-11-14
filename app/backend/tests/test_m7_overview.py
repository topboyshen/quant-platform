"""M7.1 overview API: authenticated read model for the home page."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.market import HealthResult
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import JobRun
from qp.models.reports import Report
from qp.models.signals import EodRun

DAY = dt.date(2026, 11, 12)


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (HealthResult, EodRun, Report, JobRun, DeliveryAttempt, Outbox):
            session.execute(delete(model))
        session.commit()


def seed(engine) -> None:
    now = utcnow()
    with Session(engine) as session:
        for check, status in (("db", "ok"), ("qlib", "warning")):
            session.add(HealthResult(run_at=now, check=check, status=status, summary=""))
        session.add(EodRun(run_id=1, decision_date=DAY, attempt_no=1, status="success",
                           decision_outcome="emitted", target_count=40, evaluated_count=40,
                           failed_count=0, signal_count=3, allowed_signal_count=2,
                           blocked_signal_count=1))
        session.add(Report(kind="daily_research_summary", business_date=DAY, version=1,
                           status="frozen", content_sha256="a" * 64, payload={"text": "x"}))
        session.add(JobRun(job_name="signals.eod", status="succeeded", started_at=now))
        session.add(JobRun(job_name="signals.outcomes", status="failed", started_at=now))
        session.add(Outbox(channel="telegram", recipient="ops", dedup_key="m7-1", body="x",
                           mode="shadow", status="pending"))
        session.commit()


def _token(engine) -> str:
    from qp.auth.sessions import create_service_token
    with Session(engine) as s:
        tok = create_service_token(s, "m7-probe", ["ops:read"])
        s.commit()
    return tok


def test_overview_returns_aggregates(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        r = client.get("/api/overview", headers={"authorization": f"Bearer {_token(engine)}"})
        assert r.status_code == 200
        data = r.json()
        assert data["version"]
        assert data["health"]["ok"] == 1 and data["health"]["total"] == 2
        assert data["health"]["checks"]["db"] == "ok"
        assert data["signals"]["decision_date"] == DAY.isoformat()
        assert data["signals"]["allowed"] == 2 and data["signals"]["blocked"] == 1
        assert data["reports"]["by_kind"]["daily_research_summary"] == 1
        assert data["reports"]["latest_date"] == DAY.isoformat()
        assert data["jobs"]["failed_24h"] == 1
        assert data["jobs"]["failed"][0]["job_name"] == "signals.outcomes"
        assert data["notify"]["outbox"]["shadow"] == 1
        assert data["notify"]["delivery_attempt"] == 0
        assert data["notify"]["mode"] in ("shadow", "live") and data["notify"]["signals_mode"]
    finally:
        cleanup(engine)


def test_overview_requires_auth(client):
    assert client.get("/api/overview").status_code == 401
