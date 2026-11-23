"""M7.1 overview API: business read model and scope inheritance."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.market import HealthResult, QlibStatus, TradingDay
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import Alert, JobRun
from qp.models.reports import Report
from qp.models.research import EtfScore, EtfScoreRun
from qp.models.signals import EodRun

DAY = dt.date(2026, 11, 23)
NOW = dt.datetime(2026, 11, 23, 18, 15, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (HealthResult, QlibStatus, TradingDay, EodRun, Report, JobRun, DeliveryAttempt, Outbox, Alert):
            session.execute(delete(model))
        session.execute(delete(EtfScore))
        session.execute(delete(EtfScoreRun))
        session.commit()


def _add_job(session: Session, name: str, result: dict, *, status: str = "succeeded") -> JobRun:
    row = JobRun(
        job_name=name,
        status=status,
        trigger="test",
        started_at=NOW,
        finished_at=NOW,
        result=result,
    )
    session.add(row)
    session.flush()
    return row


def seed(engine, *, manual: bool = False) -> None:
    with Session(engine) as session:
        session.add(TradingDay(origin="qp", date=DAY, is_open=True, source="test"))
        session.add(
            HealthResult(
                run_at=NOW,
                check="db",
                status="ok",
                summary="",
            )
        )
        session.add(
            HealthResult(
                run_at=NOW,
                check="qlib",
                status="ok",
                summary="",
            )
        )
        session.add(QlibStatus(kind="catalog", at=NOW, data={"latest_eod": DAY.isoformat()}))
        session.add(
            EodRun(
                run_id=9001,
                decision_date=DAY,
                attempt_no=1,
                status="success",
                decision_outcome="blocked",
                target_count=40,
                evaluated_count=40,
                failed_count=0,
                signal_count=1,
                allowed_signal_count=0,
                blocked_signal_count=1,
                started_at=NOW,
                finished_at=NOW,
            )
        )
        _add_job(
            session,
            "market.health",
            {"checks": {"db": "ok", "qlib.current": "ok"}, "alerts": {"firing": 0}},
        )
        _add_job(
            session,
            "signals.chain_window",
            {
                "rows": [
                    {
                        "date": DAY.isoformat(),
                        "status": "PASS",
                        "run_status": "success",
                        "run_outcome": "blocked",
                    }
                ]
            },
        )
        _add_job(
            session,
            "signals.acceptance",
            {
                "date": DAY.isoformat(),
                "verdict": "PASS",
                "run_quality": "manual_recovered" if manual else "natural",
                "natural_pass": not manual,
            },
        )
        _add_job(session, "research.compare", {"overall": "PASS"})
        for horizon, version in ((20, "qp-etf-lgbm20-v1-test"), (5, "qp-etf-lgbm5-v1-test")):
            run = EtfScoreRun(
                model_version=version,
                model_sha256="a" * 64,
                data_cutoff=DAY,
                label_horizon=horizon,
                metrics={},
                notes="test",
            )
            session.add(run)
            session.flush()
            for rank, code in enumerate(("510300", "510500"), start=1):
                session.add(
                    EtfScore(
                        run_id=run.id,
                        code=code,
                        name=code,
                        score=1.0 / rank,
                        rank=rank,
                        eligible=True,
                        reason="ok",
                    )
                )
        daily = Outbox(
            channel="telegram",
            recipient="signals",
            dedup_key=f"watchdog:{DAY.isoformat()}:daily",
            subject="daily",
            body="{}",
            mode="live",
            status="sent",
            created_at=NOW,
        )
        session.add(daily)
        session.flush()
        session.add(
            DeliveryAttempt(
                outbox_id=daily.id,
                at=NOW,
                mode="live",
                ok=True,
                detail={"attempt": 1, "message_id": 5001},
            )
        )
        session.add(
            Report(
                kind="daily_research_summary",
                business_date=DAY,
                version=1,
                status="frozen",
                content_sha256="b" * 64,
                payload={},
            )
        )
        session.commit()


def _token(engine, scopes: list[str]) -> str:
    import uuid

    from qp.auth.sessions import create_service_token

    with Session(engine) as s:
        tok = create_service_token(s, f"m7-overview-{uuid.uuid4().hex[:8]}", scopes)
        s.commit()
    return tok


def _get(client, engine, scopes: list[str]) -> dict:
    response = client.get("/api/overview", headers={"authorization": f"Bearer {_token(engine, scopes)}"})
    assert response.status_code == 200, response.text
    return response.json()


def test_overview_returns_business_modules(engine, client, monkeypatch):
    monkeypatch.setattr("qp.api.overview._now", lambda: NOW)
    monkeypatch.setattr("qp.research.etf_score.expected_session", lambda _: DAY)
    cleanup(engine)
    try:
        seed(engine)
        data = _get(
            client,
            engine,
            ["ops:read", "signals:read", "market:read", "research:read", "reports:read"],
        )
        assert data["business_date"] == DAY.isoformat()
        assert data["session"] == "closed"
        assert data["version"]
        assert data["modules"]["market"]["status"] == "ok"
        assert data["modules"]["signals"]["status"] == "ok"
        assert data["modules"]["signals"]["metrics"]["acceptance_verdict"] == "PASS"
        assert data["modules"]["research"]["status"] == "ok"
        assert data["modules"]["research"]["metrics"]["feeds"]["20d"]["eligible"] == 2
        assert data["modules"]["reports"]["status"] == "ok"
        assert data["modules"]["reports"]["metrics"]["daily"]["message_id"] == 5001
        assert data["pending"] == []
        assert data["recent_quality"][-1]["run_quality"] == "natural"
    finally:
        cleanup(engine)


def test_overview_filters_domains_by_scope(engine, client, monkeypatch):
    monkeypatch.setattr("qp.api.overview._now", lambda: NOW)
    monkeypatch.setattr("qp.research.etf_score.expected_session", lambda _: DAY)
    cleanup(engine)
    try:
        seed(engine)
        data = _get(client, engine, ["signals:read"])
        assert data["modules"]["signals"]["available"] is True
        assert data["modules"]["market"]["available"] is False
        assert data["modules"]["research"]["available"] is False
        assert data["modules"]["ops"]["available"] is False
        assert data["modules"]["market"]["required_scope"] == "market:read"
        assert data["modules"]["signals"]["metrics"]["run_id"] == 9001
    finally:
        cleanup(engine)


def test_overview_marks_manual_recovery_as_pending(engine, client, monkeypatch):
    monkeypatch.setattr("qp.api.overview._now", lambda: NOW)
    monkeypatch.setattr("qp.research.etf_score.expected_session", lambda _: DAY)
    cleanup(engine)
    try:
        seed(engine, manual=True)
        data = _get(client, engine, ["signals:read"])
        assert data["modules"]["signals"]["metrics"]["run_quality"] == "manual_recovered"
        assert any(issue["id"] == "signals:manual_recovery" for issue in data["pending"])
        assert any(
            row["business_date"] == DAY.isoformat() and row["run_quality"] == "manual_recovered"
            for row in data["recent_quality"]
        )
    finally:
        cleanup(engine)


def test_overview_requires_auth(client):
    assert client.get("/api/overview").status_code == 401
