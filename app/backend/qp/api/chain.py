"""Read-only ETF record-chain API: snapshot envelope, decisions and one decision's full trail."""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth, require_scope
from qp.models.chain import (
    ChainAttempt,
    ChainDecision,
    ChainEligibility,
    ChainEvidenceLink,
    ChainOutcome,
    ChainSnapshot,
    ChainState,
)
from qp.models.signals import TargetDecision, TrendSignal

router = APIRouter(prefix="/api/signals",
                   dependencies=[Depends(require_scope("signals:read"))])


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else value


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


def _legacy_evidence(db: Session, decision: ChainDecision,
                     evidence: list[ChainEvidenceLink]) -> dict[str, Any]:
    """Expand a decision's db_row evidence into the legacy signal detail.

    The chain snapshot stays light: this lookup happens only on the single
    decision detail endpoint, reads ``target_decision``/``trend_signal``
    read-only, and is labelled as legacy evidence rather than chain payload.
    """
    link = next((item for item in evidence if item.sidecar_kind == "db_row"), None)
    if link is None:
        return {"available": False, "reason": "no_db_row_evidence",
                "source": "legacy_target_decision", "chain_native": False}
    detail = link.detail or {}
    run_id = detail.get("run_id")
    code = detail.get("code") or decision.code
    if run_id is None:
        return {"available": False, "reason": "evidence_missing_run_id",
                "source": "legacy_target_decision", "chain_native": False,
                "link_id": link.link_id}
    row = db.scalar(select(TargetDecision).where(
        TargetDecision.run_id == int(run_id), TargetDecision.code == code))
    if row is None:
        return {"available": False, "reason": "target_decision_missing",
                "source": "legacy_target_decision", "chain_native": False,
                "link_id": link.link_id, "run_id": int(run_id),
                "db_state": link.db_state, "sidecar_state": link.sidecar_state}
    signals = list(db.scalars(select(TrendSignal).where(
        TrendSignal.decision_date == row.decision_date,
        TrendSignal.code == code).order_by(TrendSignal.signal_type)))
    return {
        "available": True, "source": "legacy_target_decision", "chain_native": False,
        "link_id": link.link_id, "run_id": int(run_id), "code": code,
        "decision_date": row.decision_date, "reference_close": _num(row.reference_close),
        "decision_state": row.decision_state, "risk_action": row.risk_action,
        "raw_trigger_count": row.raw_trigger_count,
        "raw_signals": row.raw_signals or [],
        "db_state": link.db_state, "sidecar_state": link.sidecar_state,
        "verified": link.sidecar_state == "verified",
        "signals": [{"signal_type": s.signal_type, "period": s.period,
                     "data_date": s.data_date, "title": s.title,
                     "detail": s.detail, "confidence": _num(s.confidence)}
                    for s in signals],
    }


@router.get("/chain/snapshot")
def chain_snapshot(date: dt.date | None = None, p: Principal = Depends(require_auth),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = (select(ChainSnapshot)
            .order_by(ChainSnapshot.business_date.desc(), ChainSnapshot.generated_at.desc())
            .limit(1))
    if date is not None:
        stmt = stmt.where(ChainSnapshot.business_date == date)
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no snapshot")
    return {"snapshot_id": row.snapshot_id, "digest": row.digest,
            "business_date": row.business_date, "cutoff_at": row.cutoff_at,
            "generated_at": row.generated_at, "protocol_version": row.protocol_version,
            "generator_version": row.generator_version,
            "status": (row.body or {}).get("status"), "body": row.body}


@router.get("/chain/window")
def chain_window(end: dt.date | None = None, sessions: int = Query(5, ge=1, le=30),
                 start: dt.date | None = None, p: Principal = Depends(require_auth),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """Per-day PASS/FAIL table for the first native record-chain sessions."""
    from qp.signals import chain_window as window
    return window.report(db, end=end, sessions=sessions, start=start)


@router.get("/chain/decisions")
def chain_decisions(date: dt.date | None = None, state: str | None = None,
                    eligibility: str | None = None,
                    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                    p: Principal = Depends(require_auth),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    business_date = date
    if business_date is None:
        business_date = db.scalar(select(ChainSnapshot.business_date)
                                  .order_by(ChainSnapshot.business_date.desc()).limit(1))
    if business_date is None:
        raise HTTPException(status_code=404, detail="no snapshot")
    where = ["d.trade_date = :date"]
    params: dict[str, Any] = {"date": business_date, "limit": limit, "offset": offset}
    if eligibility:
        where.append("e.verdict = :eligibility")
        params["eligibility"] = eligibility
    if state:
        where.append(":state IN (sd.state, ss.state, sr.state)")
        params["state"] = state
    base = f"""
        SELECT d.decision_uid, d.code, d.name, d.origin,
               sd.state AS data_state, ss.state AS signal_state, sr.state AS risk_state,
               e.verdict, e.reason_code AS eligibility_reason,
               o.execution_state, o.result_state, o.expected_maturity_date
        FROM signals.chain_decision d
        LEFT JOIN LATERAL (SELECT state FROM signals.chain_state
                           WHERE decision_uid = d.decision_uid AND aspect = 'data'
                           ORDER BY state_seq DESC LIMIT 1) sd ON TRUE
        LEFT JOIN LATERAL (SELECT state FROM signals.chain_state
                           WHERE decision_uid = d.decision_uid AND aspect = 'signal'
                           ORDER BY state_seq DESC LIMIT 1) ss ON TRUE
        LEFT JOIN LATERAL (SELECT state FROM signals.chain_state
                           WHERE decision_uid = d.decision_uid AND aspect = 'risk'
                           ORDER BY state_seq DESC LIMIT 1) sr ON TRUE
        LEFT JOIN LATERAL (SELECT verdict, reason_code FROM signals.chain_eligibility
                           WHERE decision_uid = d.decision_uid
                           ORDER BY evaluation_seq DESC LIMIT 1) e ON TRUE
        LEFT JOIN LATERAL (SELECT execution_state, result_state, expected_maturity_date
                           FROM signals.chain_outcome WHERE decision_uid = d.decision_uid
                           ORDER BY horizon_sessions DESC, revision DESC LIMIT 1) o ON TRUE
        WHERE {' AND '.join(where)}
    """
    rows = [dict(row) for row in db.execute(
        text(base + " ORDER BY d.code LIMIT :limit OFFSET :offset"), params).mappings()]
    counted = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    total = db.scalar(text(f"SELECT count(*) FROM ({base}) counted"), counted)
    return {"business_date": business_date, "total": int(total or 0),
            "rows": [{k: _iso(v) for k, v in row.items()} for row in rows]}


@router.get("/chain/decision/{decision_uid}")
def chain_decision(decision_uid: str, p: Principal = Depends(require_auth),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    decision = db.get(ChainDecision, decision_uid)
    if decision is None:
        raise HTTPException(status_code=404, detail="unknown decision")
    attempts = list(db.scalars(select(ChainAttempt)
                               .where(ChainAttempt.decision_uid == decision_uid)
                               .order_by(ChainAttempt.created_at)))
    states = list(db.scalars(select(ChainState)
                             .where(ChainState.decision_uid == decision_uid)
                             .order_by(ChainState.aspect, ChainState.state_seq)))
    outcomes = list(db.scalars(select(ChainOutcome)
                               .where(ChainOutcome.decision_uid == decision_uid)
                               .order_by(ChainOutcome.horizon_sessions, ChainOutcome.revision)))
    eligibility = list(db.scalars(select(ChainEligibility)
                                  .where(ChainEligibility.decision_uid == decision_uid)
                                  .order_by(ChainEligibility.evaluation_seq)))
    evidence = list(db.scalars(select(ChainEvidenceLink)
                               .where(ChainEvidenceLink.owner_kind == "decision",
                                      ChainEvidenceLink.owner_id == decision_uid)
                               .order_by(ChainEvidenceLink.checked_at.desc())))
    legacy_evidence = _legacy_evidence(db, decision, evidence)
    return {
        "decision": {"decision_uid": decision.decision_uid, "trade_date": decision.trade_date,
                     "code": decision.code, "name": decision.name,
                     "batch_key": decision.batch_key, "rule_version": decision.rule_version,
                     "universe_version": decision.universe_version, "origin": decision.origin,
                     "created_at": decision.created_at},
        "attempts": [{"attempt_id": a.attempt_id, "run_id": a.run_id,
                      "started_at": a.started_at, "completed_at": a.completed_at,
                      "error_code": a.error_code} for a in attempts],
        "states": [{"aspect": s.aspect, "state": s.state, "reason_code": s.reason_code,
                    "state_seq": s.state_seq, "observed_at": s.observed_at,
                    "basis": s.basis} for s in states],
        "outcomes": [{"horizon_sessions": o.horizon_sessions, "revision": o.revision,
                      "execution_state": o.execution_state, "result_state": o.result_state,
                      "expected_maturity_date": o.expected_maturity_date,
                      "entry_date": o.entry_date, "exit_date": o.exit_date,
                      "net_return": float(o.net_return) if o.net_return is not None else None,
                      "revision_reason": o.revision_reason} for o in outcomes],
        "eligibility": [{"verdict": e.verdict, "reason_code": e.reason_code,
                         "policy_version": e.policy_version, "evaluation_seq": e.evaluation_seq,
                         "evaluated_at": e.evaluated_at,
                         "reason_detail": e.reason_detail} for e in eligibility],
        "evidence": [{"link_id": e.link_id, "sidecar_kind": e.sidecar_kind,
                      "sidecar_path": e.sidecar_path, "expected_sha256": e.expected_sha256,
                      "observed_sha256": e.observed_sha256, "db_state": e.db_state,
                      "sidecar_state": e.sidecar_state, "checked_at": e.checked_at,
                      "detail": e.detail} for e in evidence],
        "legacy_evidence": legacy_evidence,
    }
