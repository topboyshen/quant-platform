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

from qp.api.main import Principal, get_db, require_auth, require_scope
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision, TrendSignal
from qp.signals.universe import TARGETS

router = APIRouter(prefix="/api/signals", dependencies=[Depends(require_scope("signals:read"))])
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


@router.get("/etf-pool")
def etf_pool(date: str | None = None, p: Principal = Depends(require_auth),
             db: Session = Depends(get_db)) -> dict[str, Any]:
    """The configured 40-ETF research pool joined to one EOD attempt."""
    if date:
        try:
            day = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="bad date") from exc
    else:
        day = db.scalar(select(func.max(EodRun.decision_date)))
    run = None
    if day is not None:
        run = db.scalar(select(EodRun).where(EodRun.decision_date == day)
                        .order_by(EodRun.attempt_no.desc()).limit(1))
    decisions: dict[str, TargetDecision] = {}
    signals: dict[str, list[dict[str, Any]]] = {}
    if run is not None:
        for row in db.execute(select(TargetDecision)
                              .where(TargetDecision.run_id == run.run_id)).scalars():
            decisions[row.code] = row
        for sig in db.execute(select(TrendSignal)
                              .where(TrendSignal.decision_date == day)
                              .order_by(TrendSignal.code, TrendSignal.signal_type)).scalars():
            signals.setdefault(sig.code, []).append({
                "signal_type": sig.signal_type, "period": sig.period,
                "data_date": sig.data_date, "title": sig.title,
                "detail": sig.detail, "confidence": _num(sig.confidence)})
    items = []
    counts: dict[str, int] = {}
    for target in TARGETS:
        decision = decisions.get(target.code)
        state = decision.decision_state if decision is not None else "missing"
        counts[state] = counts.get(state, 0) + 1
        items.append({
            "code": target.code, "name": target.name, "sector": target.sector,
            "state": state,
            "risk_action": decision.risk_action if decision is not None else None,
            "risk_level": decision.risk_level if decision is not None else None,
            "data_status": decision.data_status if decision is not None else None,
            "reason_code": decision.reason_code if decision is not None else None,
            "reference_close": _num(decision.reference_close) if decision is not None else None,
            "raw_trigger_count": decision.raw_trigger_count if decision is not None else None,
            "signals": signals.get(target.code, []),
        })
    return {"decision_date": day.isoformat() if day else None,
            "run": None if run is None else _run(run),
            "counts": counts, "items": items}


@router.get("/compare")
def compare(date: str | None = None, p: Principal = Depends(require_auth)) -> dict[str, Any]:
    root = Path(os.getenv("QP_SHADOW_REPORT_DIR", DEFAULT_SHADOW_DIR))
    if date is None:
        # default to the newest report; the shadow window ended with the legacy chain
        files = sorted(root.glob("*.compare.json"))
        if not files:
            raise HTTPException(status_code=404, detail="no compare reports")
        return json.loads(files[-1].read_text(encoding="utf-8"))
    try:
        day = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bad date") from exc
    path = root / f"{day.isoformat()}.compare.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="compare report not found")
    return json.loads(path.read_text(encoding="utf-8"))
