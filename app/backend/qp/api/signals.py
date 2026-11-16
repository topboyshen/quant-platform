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
from qp.config import get_settings
from qp.models.chain import (ChainDecision, ChainEligibility, ChainSnapshot, ChainState,
                             ChainUniverseMember)
from qp.models.signals import (DecisionOutcome, EodRun, RuleShadowOutcome, RuleShadowSignal,
                               ShadowOutcome, ShadowSignal, TargetDecision, TrendSignal)
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


@router.get("/rotation-shadow")
def rotation_shadow_portfolio(phase: str = Query("forward", pattern="^(forward|replay)$"),
                              p: Principal = Depends(require_auth),
                              db: Session = Depends(get_db)) -> dict[str, Any]:
    """Read-only C13 paper portfolio, never live trading instructions."""
    from qp.signals import rotation_shadow
    return rotation_shadow.summary(db, phase=phase)


@router.get("/rule-shadow")
def rule_shadow_samples(rule_version: str = "qp-swing.v2",
                        limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                        p: Principal = Depends(require_auth),
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    """Experimental rule signals and their forward returns (shadow only)."""
    from qp.signals import rule_shadow as rule_shadow_mod
    total = db.scalar(select(func.count()).select_from(RuleShadowSignal)
                      .where(RuleShadowSignal.rule_version == rule_version)) or 0
    samples = list(db.scalars(select(RuleShadowSignal)
                              .where(RuleShadowSignal.rule_version == rule_version)
                              .order_by(RuleShadowSignal.decision_date.desc(),
                                        RuleShadowSignal.code)
                              .limit(limit).offset(offset)))
    outcomes: dict[int, dict[str, Any]] = {}
    if samples:
        for row in db.scalars(select(RuleShadowOutcome).where(
                RuleShadowOutcome.signal_id.in_([s.id for s in samples]))):
            outcomes.setdefault(row.signal_id, {})[str(row.horizon_sessions)] = {
                "net_return": _num(row.net_return),
                "excess_return": _num(row.excess_return),
                "fill_status": row.fill_status}
    return {"rule_version": rule_version, "total": total,
            "summary": rule_shadow_mod.summary(db, rule_version),
            "rows": [{
                "id": s.id, "decision_date": s.decision_date, "code": s.code, "name": s.name,
                "sector": s.sector, "rule_version": s.rule_version,
                "signal_type": s.signal_type, "reference_close": _num(s.reference_close),
                "volume_ratio": _num(s.volume_ratio), "risk_level": s.risk_level,
                "outcomes": outcomes.get(s.id, {})} for s in samples]}


@router.get("/shadow")
def shadow_samples(limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                   p: Principal = Depends(require_auth),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """Counterfactual blocked-but-triggered entries (validation only)."""
    total = db.scalar(select(func.count()).select_from(ShadowSignal)) or 0
    shadows = list(db.execute(
        select(ShadowSignal).order_by(ShadowSignal.decision_date.desc(),
                                      ShadowSignal.code).limit(limit).offset(offset)).scalars())
    outcomes: dict[int, dict[str, Any]] = {}
    if shadows:
        for row in db.execute(select(ShadowOutcome).where(
                ShadowOutcome.shadow_id.in_([s.id for s in shadows]))).scalars():
            outcomes.setdefault(row.shadow_id, {})[str(row.horizon_sessions)] = {
                "net_return": _num(row.net_return),
                "excess_return": _num(row.excess_return),
                "fill_status": row.fill_status}
    return {"total": total, "rows": [{
        "id": s.id, "decision_date": s.decision_date, "code": s.code, "name": s.name,
        "sector": s.sector, "risk_level": s.risk_level, "reason_code": s.reason_code,
        "raw_trigger_count": s.raw_trigger_count,
        "reference_close": _num(s.reference_close),
        "outcomes": outcomes.get(s.id, {})} for s in shadows]}


def _latest_chain_states(db: Session, uids: list[str]) -> dict[tuple[str, str], ChainState]:
    if not uids:
        return {}
    latest: dict[tuple[str, str], ChainState] = {}
    for row in db.scalars(select(ChainState).where(ChainState.decision_uid.in_(uids))).all():
        key = (row.decision_uid, row.aspect)
        current = latest.get(key)
        if current is None or row.state_seq > current.state_seq:
            latest[key] = row
    return latest


def _latest_chain_eligibility(db: Session, uids: list[str],
                              policy: str) -> dict[str, ChainEligibility]:
    if not uids:
        return {}
    latest: dict[str, ChainEligibility] = {}
    for row in db.scalars(select(ChainEligibility).where(
            ChainEligibility.decision_uid.in_(uids),
            ChainEligibility.policy_version == policy)).all():
        current = latest.get(row.decision_uid)
        if current is None or row.evaluation_seq > current.evaluation_seq:
            latest[row.decision_uid] = row
    return latest


def _pool_from_chain(db: Session, day: dt.date | None) -> dict[str, Any]:
    """P7 read path: build the ETF pool from the record-chain snapshot."""
    empty = {"decision_date": day.isoformat() if day else None, "run": None, "counts": {},
             "items": [], "source": "chain", "snapshot": None,
             "fallback": False, "stale_or_fallback": True,
             "chain_snapshot_available": False}
    if day is None:
        return empty
    snapshot = db.scalar(select(ChainSnapshot).where(ChainSnapshot.business_date == day)
                         .order_by(ChainSnapshot.generated_at.desc()).limit(1))
    if snapshot is None:
        return empty
    body = snapshot.body or {}
    batch_keys = body.get("batch_keys") or []
    decisions = list(db.scalars(select(ChainDecision).where(
        ChainDecision.batch_key.in_(batch_keys)))) if batch_keys else []
    by_code = {d.code: d for d in decisions}
    uids = [d.decision_uid for d in decisions]
    states = _latest_chain_states(db, uids)
    eligibility = _latest_chain_eligibility(db, uids, snapshot.eligibility_policy_version)
    members = {m.code: m for m in db.scalars(select(ChainUniverseMember).where(
        ChainUniverseMember.universe_version == snapshot.universe_version))}
    codes = list(dict.fromkeys([m.code for m in members.values()] + [d.code for d in decisions]))
    counts: dict[str, int] = {}
    items = []
    for code in codes:
        member = members.get(code)
        decision = by_code.get(code)
        risk_action = risk_level = reason = data_status = None
        trigger_count = 0
        if decision is None:
            state = "missing"
        else:
            data_state = states.get((decision.decision_uid, "data"))
            signal_state = states.get((decision.decision_uid, "signal"))
            risk_state = states.get((decision.decision_uid, "risk"))
            if data_state is not None and data_state.state != "complete":
                state = "data_failed"
            elif signal_state is None or signal_state.state != "triggered":
                state = "no_trigger"
            elif risk_state is not None and risk_state.state == "blocked":
                state = "blocked"
            else:
                state = "allowed"
            data_status = ("ok" if data_state is not None and data_state.state == "complete"
                           else ("failed" if data_state is not None else None))
            risk_action = risk_state.state if risk_state is not None else None
            risk_level = (risk_state.basis or {}).get("risk_level") if risk_state is not None else None
            entry = eligibility.get(decision.decision_uid)
            reason = (entry.reason_code if entry is not None
                      else (risk_state.reason_code if risk_state is not None else None))
            trigger_count = (int((signal_state.basis or {}).get("raw_trigger_count") or 0)
                             if signal_state is not None else 0)
        counts[state] = counts.get(state, 0) + 1
        items.append({
            "code": code,
            "name": (decision.name if decision is not None and decision.name
                     else (member.name if member is not None else code)),
            "sector": (member.sector if member is not None and member.sector else ""),
            "state": state,
            "risk_action": risk_action,
            "risk_level": risk_level,
            "data_status": data_status,
            "reason_code": reason,
            "reference_close": None,  # the chain stores the digest, not the close
            "raw_trigger_count": trigger_count,
            "signal_count": trigger_count,
            "signals": [],
        })
    items.sort(key=lambda item: item["code"])
    return {"decision_date": day.isoformat(), "run": None, "counts": counts, "items": items,
            "source": "chain",
            "fallback": False, "stale_or_fallback": False,
            "chain_snapshot_available": True,
            "snapshot": {"snapshot_id": snapshot.snapshot_id, "digest": snapshot.digest,
                         "status": body.get("status"), "cutoff_at": snapshot.cutoff_at,
                         "generated_at": snapshot.generated_at}}


def _pool_from_legacy(db: Session, day: dt.date | None) -> dict[str, Any]:
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
        code_signals = signals.get(target.code, [])
        items.append({
            "code": target.code, "name": target.name, "sector": target.sector,
            "state": state,
            "risk_action": decision.risk_action if decision is not None else None,
            "risk_level": decision.risk_level if decision is not None else None,
            "data_status": decision.data_status if decision is not None else None,
            "reason_code": decision.reason_code if decision is not None else None,
            "reference_close": _num(decision.reference_close) if decision is not None else None,
            "raw_trigger_count": decision.raw_trigger_count if decision is not None else None,
            "signal_count": len(code_signals),
            "signals": code_signals,
        })
    return {"decision_date": day.isoformat() if day else None,
            "run": None if run is None else _run(run),
            "counts": counts, "items": items, "source": "legacy", "snapshot": None,
            "fallback": False, "stale_or_fallback": run is None,
            "chain_snapshot_available": None}


@router.get("/etf-pool")
def etf_pool(date: str | None = None, p: Principal = Depends(require_auth),
             db: Session = Depends(get_db)) -> dict[str, Any]:
    """The 40-ETF pool; reads the record chain when QP_CHAIN_READS=1 (P7 switch)."""
    day: dt.date | None = None
    if date:
        try:
            day = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="bad date") from exc
    settings = get_settings()
    requested = day
    if settings.chain_reads:
        if day is None:
            day = db.scalar(select(func.max(ChainSnapshot.business_date)))
            if day is None and settings.chain_reads_fallback:
                day = db.scalar(select(func.max(EodRun.decision_date)))
        body = _pool_from_chain(db, day)
        if body["chain_snapshot_available"] or not settings.chain_reads_fallback:
            return body
        # No snapshot for the requested/latest day: serve the legacy decision
        # table, but mark the response so the page cannot mistake it for chain
        # evidence.
        legacy = _pool_from_legacy(db, day)
        legacy.update({
            "source": "legacy_fallback",
            "fallback": True,
            "stale_or_fallback": True,
            "requested_date": requested.isoformat() if requested else None,
            "chain_snapshot_available": False,
        })
        return legacy
    if day is None:
        day = db.scalar(select(func.max(EodRun.decision_date)))
    return _pool_from_legacy(db, day)


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
