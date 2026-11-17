"""M7.2 research read APIs: backtests, profit research, AI records, projects."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.research import (
    AiMessage,
    AiSession,
    AiTask,
    BacktestEquity,
    BacktestRun,
    BacktestTrade,
    Branch,
    ProfitPick,
    ProfitRun,
    ProfitRunAttempt,
    Project,
)

NOW = dt.datetime(2026, 11, 12, 15, 30)


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (BacktestEquity, BacktestTrade, BacktestRun, ProfitPick, ProfitRun,
                      ProfitRunAttempt, AiMessage, AiSession, AiTask, Branch, Project):
            session.execute(delete(model))
        session.commit()


def seed(engine) -> None:
    with Session(engine) as session:
        session.add(BacktestRun(run_id=7, strategy_name="etf-trend", symbol="510300",
                                timeframe="1d", status="success", market="cn",
                                start_date="2025-01-01", end_date="2026-08-01",
                                initial_capital=100000, created_at=NOW))
        session.add(BacktestEquity(run_id=7, point_index=0, point_time="2026-08-01",
                                   point_value=101000.0))
        session.add(BacktestTrade(run_id=7, trade_index=0, trade_time="2026-08-01",
                                  side="buy", price=4.1, amount=1000, profit=12.5))
        session.add(ProfitRun(id=47, signal_date=dt.date(2026, 11, 2), status="PAPER_CANDIDATES",
                              candidate_count=3, universe_count=5668, data_ready=True))
        session.add(ProfitPick(id=108, run_id=47, symbol="sh600000", name="浦发银行", rank=1,
                               state="OPEN", signal_price=10.5, realized_return=0.01,
                               latest_unrealized_return=0.02, excess_return=0.005))
        session.add(ProfitRun(id=48, signal_date=dt.date(2026, 11, 10), status="DATA_BLOCKED",
                              candidate_count=0, data_ready=False,
                              reason="OOS持有区间数据缺失：sh600825 2026-09-30→2026-10-14 缺终点"))
        session.add(ProfitRunAttempt(
            execution_id="00000000-0000-0000-0000-000000000048", run_id=48,
            signal_date=dt.date(2026, 11, 10), status="DATA_BLOCKED", generated_at=NOW,
            result_document={"diagnostics": {
                "kind": "oos_holding_interval_gap", "symbol": "sh600825",
                "missing_end": True, "interval_start": "2026-09-30",
                "interval_end": "2026-10-14"}}))
        session.add(Project(id=1, public_id="proj-1", name="etf-trend", status="active",
                            revision=3, goal="improve"))
        session.add(Branch(id=1, public_id="branch-1", project_id=1, name="main",
                           status="active", revision=2))
        session.add(AiTask(id=1, market="cn", symbol="510300", model="kimi-k3",
                           status="completed", result_json={"decision": "buy", "confidence": 72},
                           created_at=NOW))
        session.add(AiSession(id="sess-1", title="chat", market="cn", symbol="510300",
                              created_at=NOW, updated_at=NOW))
        session.add(AiMessage(id=1, session_id="sess-1", role="user", content="hi",
                              created_at=NOW))
        session.commit()


def _token(engine) -> str:
    from qp.auth.sessions import create_service_token
    with Session(engine) as s:
        tok = create_service_token(s, "m7-research", ["research:read"])
        s.commit()
    return tok


def test_research_endpoints(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        h = {"authorization": f"Bearer {_token(engine)}"}

        backtests = client.get("/api/research/backtests", headers=h).json()
        assert backtests["total"] == 1
        assert backtests["rows"][0]["run_id"] == 7

        detail = client.get("/api/research/backtests/7", headers=h).json()
        assert detail["counts"] == {"equity_points": 1, "trades": 1}
        assert detail["trades"][0]["profit"] == 12.5
        assert client.get("/api/research/backtests/999", headers=h).status_code == 404

        runs = client.get("/api/research/profit/runs", headers=h).json()
        assert runs["total"] == 2
        by_id = {r["id"]: r for r in runs["rows"]}
        assert by_id[47]["candidate_count"] == 3 and by_id[47]["suspended"] is False
        assert by_id[48]["suspended"] is True
        assert by_id[48]["blocked_symbol"] == "sh600825"
        assert by_id[48]["blocked_interval_end"] == "2026-10-14"
        picks = client.get("/api/research/profit/runs/47/picks", headers=h).json()
        assert picks["rows"][0]["symbol"] == "sh600000"
        assert client.get("/api/research/profit/runs/999/picks", headers=h).status_code == 404

        projects = client.get("/api/research/projects", headers=h).json()
        assert projects["rows"][0]["branches"][0]["name"] == "main"

        tasks = client.get("/api/research/ai/tasks", headers=h).json()
        assert tasks["rows"][0]["decision"] == "buy"
        sessions = client.get("/api/research/ai/sessions", headers=h).json()
        assert sessions["rows"][0]["id"] == "sess-1"
        messages = client.get("/api/research/ai/sessions/sess-1/messages", headers=h).json()
        assert messages["rows"][0]["content"] == "hi"
    finally:
        cleanup(engine)


def test_research_requires_auth(client):
    assert client.get("/api/research/backtests").status_code == 401
