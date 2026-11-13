"""M7 API: read-only research views over the research schema."""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth
from qp.models.research import (AiMessage, AiSession, AiTask, BacktestEquity, BacktestRun,
                                BacktestTrade, Branch, ProfitObservation, ProfitPick, ProfitRun,
                                ProfitRunAttempt, Project)

router = APIRouter(prefix="/api/research")


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


def _run(r: BacktestRun) -> dict[str, Any]:
    return {"run_id": r.run_id, "strategy_name": r.strategy_name, "symbol": r.symbol,
            "timeframe": r.timeframe, "status": r.status, "market": r.market,
            "start_date": r.start_date, "end_date": r.end_date,
            "initial_capital": _num(r.initial_capital), "created_at": r.created_at}


@router.get("/backtests")
def backtests(status: str | None = None, q: str | None = None,
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(BacktestRun)
    if status:
        stmt = stmt.where(BacktestRun.status == status)
    if q:
        like = f"%{q.strip()[:32]}%"
        stmt = stmt.where(or_(BacktestRun.strategy_name.ilike(like), BacktestRun.symbol.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(BacktestRun.run_id.desc()).limit(limit).offset(offset)).scalars()
    return {"total": total, "rows": [_run(r) for r in rows]}


@router.get("/backtests/{run_id}")
def backtest_detail(run_id: int, p: Principal = Depends(require_auth),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    equity = list(db.execute(select(BacktestEquity).where(BacktestEquity.run_id == run_id)
                             .order_by(BacktestEquity.point_index).limit(2000)).scalars())
    trades = list(db.execute(select(BacktestTrade).where(BacktestTrade.run_id == run_id)
                             .order_by(BacktestTrade.trade_index).limit(500)).scalars())
    equity_total = db.scalar(select(func.count()).select_from(BacktestEquity)
                             .where(BacktestEquity.run_id == run_id)) or 0
    trade_total = db.scalar(select(func.count()).select_from(BacktestTrade)
                            .where(BacktestTrade.run_id == run_id)) or 0
    return {"run": {**_run(row), "engine_version": row.engine_version,
                    "commission": _num(row.commission), "slippage": _num(row.slippage),
                    "leverage": row.leverage, "error_message": row.error_message},
            "equity": [{"index": e.point_index, "time": e.point_time,
                        "value": e.point_value} for e in equity],
            "trades": [{"index": t.trade_index, "time": t.trade_time, "side": t.side,
                        "price": t.price, "amount": t.amount, "profit": t.profit,
                        "reason": t.reason} for t in trades],
            "counts": {"equity_points": equity_total, "trades": trade_total}}


@router.get("/profit/runs")
def profit_runs(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(ProfitRun)) or 0
    rows = list(db.execute(select(ProfitRun).order_by(ProfitRun.signal_date.desc(), ProfitRun.id.desc())
                           .limit(limit).offset(offset)).scalars())
    attempts: dict[int, ProfitRunAttempt] = {}
    ids = [r.id for r in rows]
    if ids:
        latest = db.execute(select(ProfitRunAttempt).where(ProfitRunAttempt.run_id.in_(ids))
                            .order_by(ProfitRunAttempt.run_id, ProfitRunAttempt.generated_at.desc())).scalars()
        for a in latest:
            if a.run_id is not None:
                attempts.setdefault(a.run_id, a)

    def _blocked(r: ProfitRun) -> dict[str, Any]:
        attempt = attempts.get(r.id)
        doc = attempt.result_document if attempt and isinstance(attempt.result_document, dict) else {}
        diag = doc.get("diagnostics") if isinstance(doc.get("diagnostics"), dict) else {}
        suspended = diag.get("kind") == "oos_holding_interval_gap" and bool(diag.get("missing_end"))
        return {"suspended": suspended,
                "blocked_symbol": diag.get("symbol") if suspended else None,
                "blocked_interval_start": diag.get("interval_start") if suspended else None,
                "blocked_interval_end": diag.get("interval_end") if suspended else None}

    return {"total": total, "rows": [{
        "id": r.id, "signal_date": r.signal_date, "status": r.status,
        "candidate_count": r.candidate_count, "universe_count": r.universe_count,
        "data_ready": r.data_ready, "reason": r.reason, "created_at": r.created_at,
        **_blocked(r)} for r in rows]}


@router.get("/profit/runs/{run_id}/picks")
def profit_picks(run_id: int, p: Principal = Depends(require_auth),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.get(ProfitRun, run_id) is None:
        raise HTTPException(status_code=404, detail="profit run not found")
    picks = db.execute(select(ProfitPick).where(ProfitPick.run_id == run_id)
                       .order_by(ProfitPick.rank, ProfitPick.id)).scalars()
    return {"rows": [{
        "id": k.id, "symbol": k.symbol, "name": k.name, "rank": k.rank,
        "state": k.state, "signal_price": _num(k.signal_price),
        "realized_return": _num(k.realized_return),
        "latest_unrealized_return": _num(k.latest_unrealized_return),
        "excess_return": _num(k.excess_return), "entry_date": k.entry_date,
        "exit_date": k.exit_date} for k in picks]}


@router.get("/projects")
def projects(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(Project).order_by(Project.id)).scalars()
    branches = db.execute(select(Branch).order_by(Branch.id)).scalars()
    out = []
    for project in rows:
        out.append({"id": project.id, "public_id": project.public_id, "name": project.name,
                    "status": project.status, "revision": project.revision,
                    "goal": project.goal, "default_branch_id": project.default_branch_id,
                    "updated_at": project.updated_at,
                    "branches": [{"id": b.id, "public_id": b.public_id, "name": b.name,
                                  "status": b.status, "revision": b.revision}
                                 for b in branches if b.project_id == project.id]})
    return {"rows": out}


@router.get("/ai/tasks")
def ai_tasks(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
             p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(AiTask)) or 0
    rows = db.execute(select(AiTask).order_by(AiTask.id.desc()).limit(limit).offset(offset)).scalars()
    return {"total": total, "rows": [{
        "id": t.id, "market": t.market, "symbol": t.symbol, "model": t.model,
        "status": t.status, "created_at": t.created_at,
        "decision": (t.result_json or {}).get("decision"),
        "confidence": (t.result_json or {}).get("confidence")} for t in rows]}


@router.get("/ai/sessions")
def ai_sessions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(AiSession)) or 0
    rows = db.execute(select(AiSession).order_by(AiSession.updated_at.desc().nullslast())
                      .limit(limit).offset(offset)).scalars()
    return {"total": total, "rows": [{
        "id": s.id, "title": s.title, "market": s.market, "symbol": s.symbol,
        "created_at": s.created_at, "updated_at": s.updated_at} for s in rows]}


@router.get("/ai/sessions/{session_id}/messages")
def ai_messages(session_id: str, p: Principal = Depends(require_auth),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(AiMessage).where(AiMessage.session_id == session_id)
                      .order_by(AiMessage.id).limit(500)).scalars()
    return {"rows": [{"id": m.id, "role": m.role, "content": m.content,
                      "created_at": m.created_at} for m in rows]}


@router.get("/profit/observations")
def profit_observations(limit: int = Query(200, ge=1, le=500),
                        p: Principal = Depends(require_auth),
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(ProfitObservation).order_by(ProfitObservation.id.desc())
                      .limit(limit)).scalars()
    return {"rows": [{"id": o.id, "pick_id": o.pick_id, "trade_date": o.trade_date,
                      "event": o.event, "net_return": _num(o.net_return),
                      "benchmark_return": _num(o.benchmark_return),
                      "excess_return": _num(o.excess_return)} for o in rows]}
