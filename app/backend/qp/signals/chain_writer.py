"""ETF record chain writer: eligibility, decisions, attempts and outcomes (M10).

Callers own the transaction; every helper is idempotent per attempt so a retry
only appends, never duplicates a decision.  The module is gated by
``QP_RECORD_CHAIN`` and never changes ``target_decision`` / ``decision_outcome``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.models.chain import (
    ChainAttempt,
    ChainDecision,
    ChainEligibility,
    ChainEvidenceLink,
    ChainOutcome,
)
from qp.models.signals import EodRun, TargetDecision
from qp.signals.chain_base import (
    CST,
    ELIGIBILITY_POLICY,
    ENGINE_VERSION,
    HORIZONS,
    OOS_SPLITS,
    RULE_VERSION,
    WINDOW_EARLIEST,
    _append_state,
    _attempt_run_statuses,
    _ensure_batch,
    _ensure_policies,
    _ensure_universe,
    _uid,
    _upcoming_open_days,
    decision_uid,
    enabled,
    input_evidence_digest,
    natural_batch_key,
)


def _eligibility(decision: dict[str, Any], *, run: EodRun, statuses: Sequence[str]) -> tuple[str, str]:
    """Return ``(verdict, reason_code)`` in the frozen precedence order."""
    if decision.get("data_status") != "ok":
        return "excluded", "data_incomplete"
    if int(decision.get("raw_trigger_count") or 0) == 0:
        return "excluded", "no_trigger"
    if "success" not in statuses:
        return "excluded", "run_incomplete"
    started = run.started_at.astimezone(CST) if run.started_at else None
    if started is None or started.date() != run.decision_date or started.time() < WINDOW_EARLIEST:
        return "excluded", "outside_decision_window"
    if str(decision.get("validation_split") or "") not in OOS_SPLITS:
        return "excluded", "non_oos"
    if decision.get("risk_action") == "blocked" or decision.get("decision_state") == "blocked":
        return "excluded", "risk_blocked"
    return "eligible", "eligible"


def _append_eligibility(
    session: Session,
    *,
    decision_uid: str,
    decision: dict[str, Any],
    run: EodRun,
    batch_key: str,
    universe: str,
    statuses: Sequence[str],
    now: dt.datetime,
) -> str:
    verdict, reason = _eligibility(decision, run=run, statuses=statuses)
    seen = session.scalar(
        select(ChainEligibility.eligibility_id)
        .where(
            ChainEligibility.decision_uid == decision_uid,
            ChainEligibility.policy_version == ELIGIBILITY_POLICY,
            ChainEligibility.reason_detail["run_id"].astext == str(run.run_id),
            ChainEligibility.rule_version == RULE_VERSION,
            ChainEligibility.universe_version == universe,
        )
        .limit(1)
    )
    if seen:
        return seen
    seq = (
        session.scalar(
            select(func.max(ChainEligibility.evaluation_seq)).where(
                ChainEligibility.decision_uid == decision_uid, ChainEligibility.policy_version == ELIGIBILITY_POLICY
            )
        )
        or 0
    ) + 1
    evidence = sorted(
        [
            {"kind": "decision", "id": decision_uid},
            {"kind": "policy", "id": ELIGIBILITY_POLICY},
            {"kind": "run", "id": str(run.run_id)},
            {"kind": "universe", "id": universe},
        ],
        key=lambda item: (item["kind"], item["id"]),
    )
    eligibility_id = _uid("el_", {"decision_uid": decision_uid, "policy_version": ELIGIBILITY_POLICY, "seq": seq})
    session.add(
        ChainEligibility(
            eligibility_id=eligibility_id,
            decision_uid=decision_uid,
            policy_version=ELIGIBILITY_POLICY,
            verdict=verdict,
            reason_code=reason,
            reason_detail={
                "rule_version": RULE_VERSION,
                "universe_version": universe,
                "batch_key": batch_key,
                "run_id": run.run_id,
                "attempt_run_ids": [run.run_id],
                "source_validation_split": decision.get("validation_split"),
            },
            evidence_refs=evidence,
            rule_version=RULE_VERSION,
            universe_version=universe,
            evaluated_at=now,
            evaluation_seq=seq,
        )
    )
    session.flush()
    return eligibility_id


def _materialise_outcomes(
    session: Session,
    *,
    decision_uid: str,
    attempt_id: str,
    trade_date: dt.date,
    sessions: Sequence[dt.date],
    now: dt.datetime,
) -> int:
    existing = set(
        session.scalars(select(ChainOutcome.horizon_sessions).where(ChainOutcome.decision_uid == decision_uid)).all()
    )
    created = 0
    for horizon in HORIZONS:
        if horizon in existing:
            continue
        session.add(
            ChainOutcome(
                outcome_uid=_uid("oc_", {"decision_uid": decision_uid, "horizon": horizon, "revision": 1}),
                decision_uid=decision_uid,
                source_attempt_id=attempt_id,
                horizon_sessions=horizon,
                revision=1,
                execution_state="unverified",
                result_state="pending_maturity",
                expected_maturity_date=sessions[horizon - 1] if len(sessions) >= horizon else None,
                source_kind="native",
                revision_reason="initial",
            )
        )
        created += 1
    if created:
        session.flush()
    return created


def _record_decision_evidence(
    session: Session, *, run: EodRun, code: str, decision_uid: str, expected_sha256: str, now: dt.datetime
) -> bool:
    """Append one DB-row evidence link and verify it against the source decision.

    The current evidence is a database row (``target_decision``), not an external
    sidecar; ``sidecar_kind=db_row`` makes that explicit.  The digest is
    recomputed from the stored row, so a missing row or a changed field is
    visible as ``missing/pending`` or ``failed`` instead of a silent claim.
    """
    source = session.scalar(
        select(TargetDecision).where(TargetDecision.run_id == run.run_id, TargetDecision.code == code)
    )
    if source is None:
        observed, db_state, sidecar_state = None, "missing", "pending"
    else:
        observed = input_evidence_digest(
            run_id=run.run_id,
            decision_date=run.decision_date,
            code=code,
            data_status=str(source.data_status or ""),
            raw_trigger_count=int(source.raw_trigger_count or 0),
            raw_signal_count=len(source.raw_signals or []),
            reference_close=source.reference_close,
        )
        db_state = "present"
        sidecar_state = "verified" if observed == expected_sha256 else "failed"
    sidecar_path = f"signals.target_decision:run={run.run_id}:code={code}"
    link_id = _uid(
        "lnk_",
        {"owner_kind": "decision", "owner_id": decision_uid, "sidecar_kind": "db_row", "sidecar_path": sidecar_path},
    )
    inserted = session.execute(
        pg_insert(ChainEvidenceLink)
        .values(
            link_id=link_id,
            owner_kind="decision",
            owner_id=decision_uid,
            sidecar_kind="db_row",
            sidecar_path=sidecar_path,
            expected_sha256=expected_sha256,
            observed_sha256=observed,
            db_state=db_state,
            sidecar_state=sidecar_state,
            detail={"decision_uid": decision_uid, "run_id": run.run_id, "code": code},
            checked_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["link_id"])
        .returning(ChainEvidenceLink.link_id)
    ).first()
    return bool(inserted)


def register_run(
    session: Session,
    *,
    run: EodRun,
    decisions: Sequence[dict[str, Any]],
    run_status: str,
    source: str = "timer",
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Dual-write one EOD attempt into the chain. Idempotent per ``run_id``."""
    if not enabled():
        return {"enabled": False}
    now = now or dt.datetime.now(dt.UTC)
    # Serialise concurrent writers for the same EOD run.  The chain helper is
    # called directly by tests/jobs as well as the timer, so the job-level lock
    # alone is not enough to keep decisions and evidence links unique.
    session.execute(text("SELECT pg_advisory_xact_lock(20817, :key)"), {"key": int(run.run_id)})
    _ensure_policies(session, now)
    universe = _ensure_universe(session, run.decision_date, decisions, now)
    batch_key = _ensure_batch(session, run, universe, source, now)
    sessions = _upcoming_open_days(session, run.decision_date)
    counts = {"decisions": 0, "attempts": 0, "states": 0, "eligible": 0, "outcomes": 0, "evidence_links": 0}
    for row in decisions:
        code = str(row["code"])
        uid = decision_uid(run.decision_date, code, batch_key, RULE_VERSION, universe)
        expected = input_evidence_digest(
            run_id=run.run_id,
            decision_date=run.decision_date,
            code=code,
            data_status=str(row.get("data_status") or ""),
            raw_trigger_count=int(row.get("raw_trigger_count") or 0),
            raw_signal_count=len(row.get("raw_signals") or []),
            reference_close=row.get("reference_close"),
        )
        session.execute(
            pg_insert(ChainDecision)
            .values(
                decision_uid=uid,
                trade_date=run.decision_date,
                code=code,
                name=row.get("name"),
                batch_key=batch_key,
                rule_version=RULE_VERSION,
                universe_version=universe,
                origin="native",
                attribution_status="confirmed",
                input_evidence_ref=f"run:{run.run_id}",
                input_evidence_sha256=expected,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["decision_uid"])
        )
        counts["decisions"] += 1
        if _record_decision_evidence(session, run=run, code=code, decision_uid=uid, expected_sha256=expected, now=now):
            counts["evidence_links"] += 1
        attempt_id = _uid("att_", {"run_id": run.run_id, "decision_uid": uid})
        inserted = session.execute(
            pg_insert(ChainAttempt)
            .values(
                attempt_id=attempt_id,
                decision_uid=uid,
                run_id=run.run_id,
                started_at=run.started_at,
                completed_at=run.finished_at,
                engine_version=ENGINE_VERSION,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["run_id", "decision_uid"])
            .returning(ChainAttempt.attempt_id)
        ).first()
        if inserted:
            counts["attempts"] += 1
        data_state = "complete" if row.get("data_status") == "ok" else "collection_failed"
        signal_state = "triggered" if int(row.get("raw_trigger_count") or 0) > 0 else "not_triggered"
        blocked = row.get("risk_action") == "blocked" or row.get("decision_state") == "blocked"
        risk_state = "blocked" if blocked else "allowed"
        risk_basis = {
            "risk_level": row.get("risk_level"),
            "reason_code": row.get("reason_code"),
            "raw_trigger_count": row.get("raw_trigger_count"),
        }
        for aspect, state, basis in (
            ("data", data_state, {"data_status": row.get("data_status")}),
            ("signal", signal_state, {"raw_trigger_count": row.get("raw_trigger_count")}),
            ("risk", risk_state, risk_basis),
        ):
            if _append_state(
                session,
                decision_uid=uid,
                attempt_id=attempt_id,
                aspect=aspect,
                state=state,
                observed_at=now,
                reason_code=row.get("reason_code") if aspect == "risk" else None,
                basis=basis,
            ):
                counts["states"] += 1
        statuses = _attempt_run_statuses(session, uid)
        eligibility_id = _append_eligibility(
            session,
            decision_uid=uid,
            decision=row,
            run=run,
            batch_key=batch_key,
            universe=universe,
            statuses=statuses,
            now=now,
        )
        if (
            session.scalar(select(ChainEligibility.verdict).where(ChainEligibility.eligibility_id == eligibility_id))
            == "eligible"
        ):
            counts["eligible"] += 1
        if risk_state == "allowed" and signal_state == "triggered":
            counts["outcomes"] += _materialise_outcomes(
                session,
                decision_uid=uid,
                attempt_id=attempt_id,
                trade_date=run.decision_date,
                sessions=sessions,
                now=now,
            )
    return {"enabled": True, "batch_key": batch_key, "universe_version": universe, "run_status": run_status, **counts}


def append_outcome_revision(
    session: Session,
    *,
    decision_uid: str,
    horizon: int,
    row: dict[str, Any],
    expected: dt.date | None,
    as_of: dt.date,
    now: dt.datetime | None = None,
) -> bool:
    """Append the result revision for one horizon; the pending row is never updated."""
    now = now or dt.datetime.now(dt.UTC)
    latest = (
        session.scalar(
            select(func.max(ChainOutcome.revision)).where(
                ChainOutcome.decision_uid == decision_uid, ChainOutcome.horizon_sessions == horizon
            )
        )
        or 0
    )
    if latest == 0:
        return False
    filled = row.get("fill_status") == "filled"
    session.add(
        ChainOutcome(
            outcome_uid=_uid("oc_", {"decision_uid": decision_uid, "horizon": horizon, "revision": latest + 1}),
            decision_uid=decision_uid,
            source_attempt_id=None,
            horizon_sessions=horizon,
            revision=latest + 1,
            execution_state="filled" if filled else "unfilled",
            result_state="matured" if filled else "invalid",
            expected_maturity_date=expected,
            evaluation_as_of=as_of,
            entry_date=row.get("entry_date"),
            entry_fill=row.get("entry_price"),
            exit_date=row.get("exit_date"),
            exit_fill=row.get("exit_price"),
            benchmark_return=row.get("benchmark_return"),
            gross_return=row.get("gross_return"),
            net_return=row.get("net_return"),
            excess_return=row.get("excess_return"),
            fill_reason=row.get("fill_reason"),
            invalid_reason=None if filled else (row.get("fill_reason") or "unfilled"),
            revision_reason="late_result" if expected and as_of > expected else "correction",
            source_kind="native",
            created_at=now,
        )
    )
    session.flush()
    return True


def reconcile_expired(session: Session, *, as_of: dt.date, now: dt.datetime | None = None) -> int:
    """Mark unverified outcomes past their maturity date as expired (append-only)."""
    if not enabled():
        return 0
    now = now or dt.datetime.now(dt.UTC)
    rows = session.execute(
        select(
            ChainOutcome.decision_uid,
            ChainOutcome.horizon_sessions,
            ChainOutcome.expected_maturity_date,
            ChainOutcome.revision,
        ).where(ChainOutcome.expected_maturity_date < as_of, ChainOutcome.execution_state == "unverified")
    ).all()
    latest = {
        (uid, horizon): revision
        for uid, horizon, _, revision in session.execute(
            select(
                ChainOutcome.decision_uid,
                ChainOutcome.horizon_sessions,
                ChainOutcome.expected_maturity_date,
                ChainOutcome.revision,
            )
        ).all()
    }
    appended = 0
    for uid, horizon, expected, revision in rows:
        if latest.get((uid, horizon)) != revision:
            continue
        session.add(
            ChainOutcome(
                outcome_uid=_uid("oc_", {"decision_uid": uid, "horizon": horizon, "revision": revision + 1}),
                decision_uid=uid,
                horizon_sessions=horizon,
                revision=revision + 1,
                execution_state="expired",
                result_state="expired_uncollected",
                expected_maturity_date=expected,
                evaluation_as_of=as_of,
                revision_reason="expiry_sweep",
                source_kind="native",
                created_at=now,
            )
        )
        appended += 1
    if appended:
        session.flush()
    return appended


def expected_maturity(session: Session, decision_uid: str, horizon: int) -> dt.date | None:
    return session.scalar(
        select(ChainOutcome.expected_maturity_date)
        .where(ChainOutcome.decision_uid == decision_uid, ChainOutcome.horizon_sessions == horizon)
        .order_by(ChainOutcome.revision.desc())
        .limit(1)
    )


def decision_uid_for(session: Session, decision_date: dt.date, code: str) -> str | None:
    """Find the chain identity for a legacy decision row (natural batch first)."""
    natural = natural_batch_key(decision_date)
    uid = session.scalar(
        select(ChainDecision.decision_uid)
        .where(
            ChainDecision.trade_date == decision_date, ChainDecision.code == code, ChainDecision.batch_key == natural
        )
        .limit(1)
    )
    if uid:
        return uid
    return session.scalar(
        select(ChainDecision.decision_uid)
        .where(ChainDecision.trade_date == decision_date, ChainDecision.code == code)
        .limit(1)
    )
