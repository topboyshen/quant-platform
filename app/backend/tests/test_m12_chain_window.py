"""M12 record-chain shadow window table (first native sessions, PASS/FAIL per day)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.models.market import TradingDay
from qp.models.signals import EodRun, ShadowOutcome, ShadowSignal, TargetDecision
from qp.signals import chain, chain_window

DAY = dt.date(2027, 6, 7)  # Monday, outside the fixture calendar
CST = dt.timezone(dt.timedelta(hours=8))


@pytest.fixture()
def chain_on(monkeypatch):
    monkeypatch.setenv("QP_RECORD_CHAIN", "1")
    return True


def _wipe(engine) -> None:
    from qp.models.chain import (
        ChainAttempt,
        ChainBatch,
        ChainDecision,
        ChainEligibility,
        ChainImportRegister,
        ChainOutcome,
        ChainPolicy,
        ChainSnapshot,
        ChainSnapshotItem,
        ChainState,
        ChainUniverse,
        ChainUniverseMember,
    )
    with Session(engine) as session:
        for model in (ChainSnapshotItem, ChainSnapshot, ChainImportRegister, ChainOutcome,
                      ChainState, ChainEligibility, ChainAttempt, ChainDecision, ChainBatch,
                      ChainUniverseMember, ChainUniverse, ChainPolicy):
            session.execute(text(f"ALTER TABLE signals.{model.__tablename__} DISABLE TRIGGER USER"))
            session.execute(model.__table__.delete())
            session.execute(text(f"ALTER TABLE signals.{model.__tablename__} ENABLE TRIGGER USER"))
        session.execute(ShadowOutcome.__table__.delete())
        session.execute(ShadowSignal.__table__.delete())
        session.commit()


def _cleanup_day(engine, day: dt.date) -> None:
    with Session(engine) as session:
        session.execute(TargetDecision.__table__.delete().where(TargetDecision.decision_date == day))
        session.execute(EodRun.__table__.delete().where(EodRun.decision_date == day))
        session.execute(TradingDay.__table__.delete().where(TradingDay.origin == "qp",
                                                           TradingDay.date == day))
        session.commit()


def _calendar(engine, start: dt.date, days: int = 10) -> None:
    with Session(engine) as session:
        for i in range(days):
            session.merge(TradingDay(origin="qp", date=start + dt.timedelta(days=i),
                                     is_open=True, source="test"))
        session.commit()


def _decisions() -> list[dict]:
    return [
        {"code": "510300", "name": "沪深300ETF", "sector": "宽基", "data_status": "ok",
         "decision_state": "allowed", "risk_action": "allowed", "risk_level": "ok",
         "reason_code": "r", "reference_close": 1.0, "raw_trigger_count": 1,
         "raw_signals": [], "validation_split": "live_oos"},
        {"code": "510500", "name": "中证500ETF", "sector": "宽基", "data_status": "ok",
         "decision_state": "no_trigger", "risk_action": "not_applicable", "risk_level": "ok",
         "reason_code": "r", "reference_close": 1.0, "raw_trigger_count": 0,
         "raw_signals": [], "validation_split": "live_oos"},
        {"code": "159915", "name": "创业板ETF", "sector": "成长", "data_status": "ok",
         "decision_state": "blocked", "risk_action": "blocked", "risk_level": "critical",
         "reason_code": "r", "reference_close": 1.0, "raw_trigger_count": 1,
         "raw_signals": [], "validation_split": "live_oos"},
    ]


def _seed_day(engine, day: dt.date, *, snapshot: bool = True) -> int:
    decisions = _decisions()
    with Session(engine) as session:
        run = EodRun(decision_date=day, attempt_no=1, status="success",
                     decision_outcome="emitted", target_count=3, evaluated_count=3,
                     failed_count=0, signal_count=2, allowed_signal_count=1,
                     blocked_signal_count=1, reason_codes=[])
        run.started_at = dt.datetime.combine(day, dt.time(15, 30), tzinfo=CST)
        run.finished_at = run.started_at + dt.timedelta(minutes=3)
        session.add(run)
        session.flush()
        for row in decisions:
            session.add(TargetDecision(
                run_id=run.run_id, decision_date=day, code=row["code"], name=row["name"],
                sector=row["sector"], data_status=row["data_status"],
                decision_state=row["decision_state"], risk_action=row["risk_action"],
                risk_level=row["risk_level"], reason_code=row["reason_code"],
                reference_close=row["reference_close"],
                raw_trigger_count=row["raw_trigger_count"], raw_signals=[],
                validation_split="live_oos"))
        session.flush()
        chain.register_run(session, run=run, decisions=decisions, run_status="success")
        session.flush()
        if snapshot:
            out = chain.build_snapshot(session, business_date=day)
            assert out["ok"], out
        session.commit()
        return run.run_id


def test_window_not_started_without_native_batch(engine):
    with Session(engine) as session:
        out = chain_window.report(session, end=dt.date(2020, 1, 1))
    assert out["verdict"] == "NOT_STARTED" and out["rows"] == []


def test_window_passes_for_a_native_day(engine, chain_on):
    _wipe(engine)
    _cleanup_day(engine, DAY)
    _calendar(engine, DAY)
    try:
        _seed_day(engine, DAY)
        with Session(engine) as session:
            out = chain_window.report(session, end=DAY, sessions=1)
        assert out["verdict"] == "PASS", out
        assert out["days_pass"] == 1
        row = out["rows"][0]
        assert (row["status"], row["compare_ok"], row["reason"]) == ("PASS", True, "ok")
        assert row["decisions"] == 3 and row["allowed"] == 1 and row["blocked"] == 1
        assert row["funnel"]["checked"] == 3 and row["funnel"]["admitted"] == 1
    finally:
        _cleanup_day(engine, DAY)
        _wipe(engine)


def test_window_fails_on_compare_diff(engine, chain_on):
    _wipe(engine)
    _cleanup_day(engine, DAY)
    _calendar(engine, DAY)
    try:
        run_id = _seed_day(engine, DAY)
        with Session(engine) as session:
            session.add(TargetDecision(
                run_id=run_id, decision_date=DAY, code="518880", name="黄金ETF", sector="商品",
                data_status="ok", decision_state="no_trigger", risk_action="not_applicable",
                risk_level="ok", reason_code="r", reference_close=1.0, raw_trigger_count=0,
                raw_signals=[], validation_split="live_oos"))
            session.commit()
            out = chain_window.report(session, end=DAY, sessions=1)
        assert out["verdict"] == "FAIL"
        assert out["rows"][0]["status"] == "FAIL"
        assert "checked" in out["rows"][0]["diffs"]
    finally:
        _cleanup_day(engine, DAY)
        _wipe(engine)


def test_window_fails_without_snapshot(engine, chain_on):
    _wipe(engine)
    _cleanup_day(engine, DAY)
    _calendar(engine, DAY)
    try:
        _seed_day(engine, DAY, snapshot=False)
        with Session(engine) as session:
            out = chain_window.report(session, end=DAY, sessions=1)
        assert out["verdict"] == "FAIL"
        assert out["rows"][0]["reason"] == "no_snapshot"
    finally:
        _cleanup_day(engine, DAY)
        _wipe(engine)


def test_cli_window_markdown_and_exit_codes(engine, chain_on, capsys):
    from qp.cli import main
    _wipe(engine)
    _cleanup_day(engine, DAY)
    _calendar(engine, DAY)
    try:
        _seed_day(engine, DAY)
        code = main(["chain", "window", "--start", DAY.isoformat(), "--end", DAY.isoformat(),
                     "--sessions", "1", "--markdown"])
        out = capsys.readouterr().out
        assert code == 0
        assert "记录链影子窗口" in out and "| 日期 |" in out and "PASS" in out
    finally:
        _cleanup_day(engine, DAY)
        _wipe(engine)


def test_api_returns_window(engine, chain_on):
    from fastapi.testclient import TestClient

    from qp.api.main import app
    from qp.auth.sessions import create_service_token
    _wipe(engine)
    _cleanup_day(engine, DAY)
    _calendar(engine, DAY)
    try:
        _seed_day(engine, DAY)
        with Session(engine) as session:
            token = create_service_token(session, f"m12-window-{dt.datetime.now().timestamp()}",
                                         ["signals:read"])
            session.commit()
        client = TestClient(app, base_url="https://testserver")
        response = client.get(f"/api/signals/chain/window?start={DAY}&end={DAY}&sessions=1",
                              headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "PASS" and body["rows"][0]["date"] == DAY.isoformat()
    finally:
        _cleanup_day(engine, DAY)
        _wipe(engine)


def test_window_job_registered():
    assert JOBS["signals.chain_window"].schedule_hint.startswith("Mon-Fri 16:12 CST")
