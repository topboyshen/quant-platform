"""M7 API: read-only signals views (EOD runs, decisions, signals, outcomes, shadow compare)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision, TrendSignal

router = APIRouter(prefix="/api/signals")
DEFAULT_SHADOW_DIR = "/var/lib/qp/shadow-reports"


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


def _run(r: EodRun) -> dict[str, Any]:
    return {"run_id": r.run_id, "decision_date": r.decision_date, "attempt_no": r.attempt_no,
            "status": r.status, "outcome": r.decision_outcome,
            "target_count": r.target_count, "evaluated_count": r.evaluated_count,
            "failed_count": r.failed_count, "signal_count": r.signal_count,
            "allowed": r.allowed_signal_count, "blocked": r.blocked_signal_count,
            "started_at": r.started_at, "finished_at": r.finished_at}


@router.get("/runs")
def runs(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
         p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(EodRun)) or 0
    rows = db.execute(select(EodRun).order_by(EodRun.decision_date.desc(),
                                              EodRun.attempt_no.desc())
                      .limit(limit).offset(offset)).scalars()
    return {"total": total, "rows": [_run(r) for r in rows]}


@router.get("/runs/{run_id}")
def run_detail(run_id: int, p: Principal = Depends(require_auth),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(EodRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    decisions = db.execute(select(TargetDecision).where(TargetDecision.run_id == run_id)
                           .order_by(TargetDecision.id).limit(500)).scalars()
    return {"run": _run(run), "decisions": [{
        "id": d.id, "code": d.code, "name": d.name, "sector": d.sector,
        "data_status": d.data_status, "decision_state": d.decision_state,
        "risk_action": d.risk_action, "risk_level": d.risk_level,
        "reason_code": d.reason_code, "reference_close": _num(d.reference_close),
        "raw_trigger_count": d.raw_trigger_count} for d in decisions]}


@router.get("/trends")
def trends(date: str, p: Principal = Depends(require_auth),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        day = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bad date") from exc
    rows = db.execute(select(TrendSignal).where(TrendSignal.decision_date == day)
                      .order_by(TrendSignal.code, TrendSignal.signal_type)).scalars()
    return {"date": day.isoformat(), "rows": [{
        "id": s.id, "code": s.code, "name": s.name, "signal_type": s.signal_type,
        "period": s.period, "data_date": s.data_date, "title": s.title,
        "detail": s.detail, "confidence": _num(s.confidence),
        "notified_at": s.notified_at} for s in rows]}


@router.get("/outcomes")
def outcomes(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
             p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(DecisionOutcome)) or 0
    rows = db.execute(
        select(DecisionOutcome, TargetDecision)
        .join(TargetDecision, DecisionOutcome.decision_id == TargetDecision.id)
        .order_by(DecisionOutcome.id.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "rows": [{
        "id": o.id, "decision_id": o.decision_id, "code": d.code, "name": d.name,
        "decision_date": d.decision_date, "horizon_sessions": o.horizon_sessions,
        "entry_date": o.entry_date, "exit_date": o.exit_date,
        "net_return": _num(o.net_return), "benchmark_return": _num(o.benchmark_return),
        "excess_return": _num(o.excess_return), "fill_status": o.fill_status}
        for o, d in rows]}


@router.get("/compare")
def compare(date: str, p: Principal = Depends(require_auth)) -> dict[str, Any]:
    try:
        day = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bad date") from exc
    root = Path(os.getenv("QP_SHADOW_REPORT_DIR", DEFAULT_SHADOW_DIR))
    path = root / f"{day.isoformat()}.compare.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="compare report not found")
    return json.loads(path.read_text(encoding="utf-8"))
