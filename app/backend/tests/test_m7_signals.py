"""M7.3 signals read APIs and shadow compare file reader."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.signals import DecisionOutcome, EodRun, TargetDecision, TrendSignal

DAY = dt.date(2026, 11, 12)
NOW = dt.datetime(2026, 11, 12, 15, 30)


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (DecisionOutcome, TrendSignal, TargetDecision, EodRun):
            session.execute(delete(model))
        session.commit()


def seed(engine) -> None:
    with Session(engine) as session:
        run = EodRun(run_id=11, decision_date=DAY, attempt_no=1, status="success",
                     decision_outcome="emitted", target_count=40, evaluated_count=40,
                     failed_count=0, signal_count=2, allowed_signal_count=1,
                     blocked_signal_count=1, started_at=NOW)
        session.add(run)
        decision = TargetDecision(run_id=11, decision_date=DAY, code="510300", name="沪深300ETF",
                                  sector="宽基", data_status="ok", decision_state="allowed",
                                  risk_action="allowed", risk_level="growth_ok",
                                  reason_code="trigger", reference_close=4.12,
                                  raw_trigger_count=1)
        session.add(decision)
        session.flush()
        session.add(TrendSignal(decision_date=DAY, code="510300", name="沪深300ETF",
                                signal_type="volume_breakout_ma60", period="D", data_date=DAY,
                                title="放量突破", detail={"close": 4.12}, confidence=0.55))
        session.add(DecisionOutcome(decision_id=decision.id, horizon_sessions=5,
                                    entry_date=DAY, exit_date=DAY, net_return=0.01,
                                    benchmark_return=0.005, excess_return=0.005,
                                    fill_status="filled"))
        session.commit()


def _token(engine) -> str:
    import uuid
    from qp.auth.sessions import create_service_token
    with Session(engine) as s:
        tok = create_service_token(s, f"m7-signals-{uuid.uuid4().hex[:8]}", ["ops:read"])
        s.commit()
    return tok


def test_signal_endpoints(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        h = {"authorization": f"Bearer {_token(engine)}"}

        runs = client.get("/api/signals/runs", headers=h).json()
        assert runs["total"] == 1 and runs["rows"][0]["run_id"] == 11

        detail = client.get("/api/signals/runs/11", headers=h).json()
        assert detail["decisions"][0]["code"] == "510300"
        assert detail["decisions"][0]["risk_level"] == "growth_ok"
        assert client.get("/api/signals/runs/999", headers=h).status_code == 404

        trends = client.get("/api/signals/trends?date=2026-11-12", headers=h).json()
        assert trends["rows"][0]["signal_type"] == "volume_breakout_ma60"
        assert client.get("/api/signals/trends?date=bad", headers=h).status_code == 422

        outcomes = client.get("/api/signals/outcomes", headers=h).json()
        assert outcomes["rows"][0]["code"] == "510300"
        assert outcomes["rows"][0]["fill_status"] == "filled"
    finally:
        cleanup(engine)


def test_compare_reads_shadow_report(engine, client, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_SHADOW_REPORT_DIR", str(tmp_path))
        h = {"authorization": f"Bearer {_token(engine)}"}
        assert client.get("/api/signals/compare?date=2026-11-12", headers=h).status_code == 404
        report = {"date": "2026-11-12", "overall": "PASS", "checks": [{"id": "C1", "status": "PASS"}]}
        (tmp_path / "2026-11-12.compare.json").write_text(json.dumps(report), encoding="utf-8")
        r = client.get("/api/signals/compare?date=2026-11-12", headers=h)
        assert r.status_code == 200 and r.json()["overall"] == "PASS"
        assert client.get("/api/signals/compare?date=bad", headers=h).status_code == 422
    finally:
        cleanup(engine)


def test_etf_pool_returns_the_configured_universe(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        h = {"authorization": f"Bearer {_token(engine)}"}
        data = client.get("/api/signals/etf-pool?date=2026-11-12", headers=h).json()
        assert data["decision_date"] == "2026-11-12"
        assert len(data["items"]) == 40
        item = next(i for i in data["items"] if i["code"] == "510300")
        assert item["state"] == "allowed" and item["risk_action"] == "allowed"
        assert item["reference_close"] == 4.12 and len(item["signals"]) == 1
        other = next(i for i in data["items"] if i["code"] != "510300")
        assert other["state"] == "missing"
        assert data["counts"]["allowed"] == 1 and data["counts"]["missing"] == 39

        latest = client.get("/api/signals/etf-pool", headers=h).json()
        assert latest["decision_date"] == "2026-11-12"
        assert client.get("/api/signals/etf-pool?date=bad", headers=h).status_code == 422
    finally:
        cleanup(engine)


def test_signals_requires_auth(client):
    assert client.get("/api/signals/runs").status_code == 401
