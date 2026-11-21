"""ETF record chain snapshot assembly and cumulative verification progress (M10).

Reads existing append-only facts and stores only the deterministic snapshot
envelope plus its expandable items.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.models.chain import (
    ChainAttempt,
    ChainBatch,
    ChainDecision,
    ChainEligibility,
    ChainOutcome,
    ChainSnapshot,
    ChainSnapshotItem,
    ChainState,
    ChainUniverseMember,
)
from qp.models.signals import EodRun
from qp.signals.chain_base import (
    CST,
    ELIGIBILITY_POLICY,
    ENGINE_VERSION,
    HORIZONS,
    PROTOCOL_VERSION,
    SAMPLE_THRESHOLD,
    canonical_json,
    sha256_hex,
)

SAMPLE_BUCKETS = (
    "waiting_maturity",
    "qualified_matured",
    "historical_excluded",
    "other_excluded",
    "attribution_pending",
    "expired_uncollected",
    "invalid",
)


def _latest(rows: Iterable[Any], key, order) -> dict[Any, Any]:
    best: dict[Any, Any] = {}
    for row in rows:
        slot = key(row)
        if slot not in best or order(row) > order(best[slot]):
            best[slot] = row
    return best


def _cumulative_progress(session: Session, cutoff: dt.datetime) -> dict[str, Any]:
    """Verification progress across all decisions under the frozen eligibility policy.

    One row per eligible decision (latest eligibility, latest primary-horizon
    outcome), independent of the snapshot's business date.  Legacy imports are
    counted but reported separately so old and new rules stay distinguishable.
    """
    rows = (
        session.execute(
            text("""
        SELECT d.decision_uid, d.trade_date, d.origin,
               o.execution_state, o.result_state, o.expected_maturity_date
        FROM signals.chain_decision d
        JOIN signals.chain_decision_current_eligibility e
          ON e.decision_uid = d.decision_uid AND e.policy_version = :policy
        LEFT JOIN LATERAL (
            SELECT execution_state, result_state, expected_maturity_date
            FROM signals.chain_outcome
            WHERE decision_uid = d.decision_uid AND horizon_sessions = :horizon
            ORDER BY revision DESC LIMIT 1
        ) o ON TRUE
        WHERE e.verdict = 'eligible'
    """),
            {"policy": ELIGIBILITY_POLICY, "horizon": max(HORIZONS)},
        )
        .mappings()
        .all()
    )
    matured = waiting = expired = invalid = legacy = 0
    next_maturity: dt.date | None = None
    last_eligible: dt.date | None = None
    for row in rows:
        if last_eligible is None or row["trade_date"] > last_eligible:
            last_eligible = row["trade_date"]
        if row["origin"] == "migrated_v1":
            legacy += 1
        result = row["result_state"]
        expected = row["expected_maturity_date"]
        if result == "matured":
            matured += 1
        elif result == "expired_uncollected" or (expected is not None and expected < cutoff.date()):
            expired += 1
        elif result in (None, "pending_maturity") or row["execution_state"] == "unverified":
            waiting += 1
            if expected is not None and (next_maturity is None or expected < next_maturity):
                next_maturity = expected
        else:
            invalid += 1
    if matured >= SAMPLE_THRESHOLD:
        stall = "threshold_met"
    elif expired:
        stall = "expired_uncollected"
    elif waiting:
        stall = "waiting_maturity"
    else:
        stall = "no_admitted"
    return {
        "scope": "cumulative",
        "matured": matured,
        "waiting": waiting,
        "expired_uncollected": expired,
        "invalid": invalid,
        "eligible_total": len(rows),
        "legacy_eligible": legacy,
        "threshold": SAMPLE_THRESHOLD,
        "primary_horizon_sessions": max(HORIZONS),
        "last_new_eligible_date": last_eligible.isoformat() if last_eligible else None,
        "next_expected_maturity": next_maturity.isoformat() if next_maturity else None,
        "stall_code": stall,
    }


def _snapshot_body(
    session: Session, business_date: dt.date, batch: ChainBatch, cutoff: dt.datetime
) -> tuple[dict[str, Any], list[tuple[str, str, str | None, str | None]]]:
    """Assemble the deterministic snapshot body plus its expandable items."""
    decisions = list(
        session.scalars(
            select(ChainDecision).where(
                ChainDecision.trade_date == business_date, ChainDecision.batch_key == batch.batch_key
            )
        )
    )
    uids = [d.decision_uid for d in decisions]
    members = list(
        session.scalars(
            select(ChainUniverseMember).where(ChainUniverseMember.universe_version == batch.universe_version)
        )
    )
    states = list(session.scalars(select(ChainState).where(ChainState.decision_uid.in_(uids)))) if uids else []
    current_state = _latest(states, lambda r: (r.decision_uid, r.aspect), lambda r: r.state_seq)
    eligibility = (
        list(
            session.scalars(
                select(ChainEligibility).where(
                    ChainEligibility.decision_uid.in_(uids), ChainEligibility.policy_version == ELIGIBILITY_POLICY
                )
            )
        )
        if uids
        else []
    )
    current_eligibility = _latest(eligibility, lambda r: r.decision_uid, lambda r: r.evaluation_seq)
    outcomes = list(session.scalars(select(ChainOutcome).where(ChainOutcome.decision_uid.in_(uids)))) if uids else []
    latest_outcome = _latest(outcomes, lambda r: (r.decision_uid, r.horizon_sessions), lambda r: r.revision)
    runs = (
        session.execute(
            select(EodRun.status, EodRun.finished_at)
            .join(ChainAttempt, ChainAttempt.run_id == EodRun.run_id)
            .where(ChainAttempt.decision_uid.in_(uids))
        ).all()
        if uids
        else []
    )
    terminal = any(finished is not None for _, finished in runs)

    funnel = {"checked": len(members), "triggered": 0, "risk_blocked": 0, "risk_allowed": 0, "admitted": 0}
    sample = {bucket: 0 for bucket in SAMPLE_BUCKETS}
    items: list[tuple[str, str, str | None, str | None]] = []
    uid_by_code = {d.code: d.decision_uid for d in decisions}
    for member in members:
        items.append(("funnel", "checked", uid_by_code.get(member.code), member.code))

    without_eligibility = 0
    matured_without_result = 0
    for decision in decisions:
        uid = decision.decision_uid
        signal = current_state.get((uid, "signal"))
        risk = current_state.get((uid, "risk"))
        triggered = bool(signal and signal.state == "triggered")
        allowed = bool(risk and risk.state == "allowed")
        entry = current_eligibility.get(uid)
        if triggered:
            funnel["triggered"] += 1
            items.append(("funnel", "triggered", uid, decision.code))
            if allowed:
                funnel["risk_allowed"] += 1
                items.append(("funnel", "risk_allowed", uid, decision.code))
            else:
                funnel["risk_blocked"] += 1
                items.append(("funnel", "risk_blocked", uid, decision.code))
        if entry is None:
            if triggered:
                without_eligibility += 1
                if allowed:
                    sample["attribution_pending"] += 1
                    items.append(("sample", "attribution_pending", uid, decision.code))
            continue
        if not (triggered and allowed):
            continue
        if entry.verdict == "eligible":
            funnel["admitted"] += 1
            items.append(("funnel", "admitted", uid, decision.code))
            outcome = latest_outcome.get((uid, max(HORIZONS)))
            if outcome is None or outcome.result_state == "pending_maturity" or outcome.execution_state == "unverified":
                bucket = "waiting_maturity"
            elif outcome.result_state == "matured":
                bucket = "qualified_matured"
            elif outcome.result_state == "expired_uncollected":
                bucket = "expired_uncollected"
            else:
                bucket = "invalid"
            if (
                outcome is not None
                and outcome.expected_maturity_date
                and outcome.expected_maturity_date < cutoff.date()
                and bucket == "waiting_maturity"
            ):
                matured_without_result += 1
        elif entry.verdict == "excluded":
            bucket = (
                "historical_excluded"
                if decision.origin == "migrated_v1" and entry.reason_code == "run_incomplete"
                else "other_excluded"
            )
        else:
            bucket = "attribution_pending"
        sample[bucket] += 1
        items.append(("sample", bucket, uid, decision.code))

    loss = {
        "universe_members_without_decision": max(0, len(members) - len(decisions)),
        "signal_decisions_without_eligibility": without_eligibility,
        "matured_without_result": matured_without_result,
    }
    for member in members:
        if member.code not in uid_by_code:
            items.append(("loss", "universe_members_without_decision", None, member.code))
    for decision in decisions:
        if decision.decision_uid not in current_eligibility and (
            current_state.get((decision.decision_uid, "signal")) is not None
        ):
            items.append(("loss", "signal_decisions_without_eligibility", decision.decision_uid, decision.code))

    body = {
        "business_date": business_date.isoformat(),
        "protocol_version": PROTOCOL_VERSION,
        "cutoff_at": cutoff.isoformat(),
        "eligibility_policy_version": ELIGIBILITY_POLICY,
        "universe_version": batch.universe_version,
        "batch_keys": [batch.batch_key],
        "status": "complete" if terminal else "incomplete",
        "funnel": funnel,
        "sample": sample,
        "progress": _cumulative_progress(session, cutoff),
        "loss": loss,
    }
    return body, items


def build_snapshot(session: Session, *, business_date: dt.date, now: dt.datetime | None = None) -> dict[str, Any]:
    """Generate (or reuse) the single snapshot for a business day."""
    now = now or dt.datetime.now(dt.UTC)
    batch = session.scalar(
        select(ChainBatch)
        .where(ChainBatch.trade_date == business_date, ChainBatch.source == "timer")
        .order_by(ChainBatch.scheduled_at.desc())
        .limit(1)
    )
    if batch is None:
        return {"ok": False, "reason": "no_batch"}
    cutoff = dt.datetime.combine(business_date, dt.time(15, 50), tzinfo=CST)
    body, items = _snapshot_body(session, business_date, batch, cutoff)
    digest = sha256_hex(canonical_json(body))
    snapshot_id = "snp_" + digest[:32]
    existing = session.scalar(
        select(ChainSnapshot.snapshot_id)
        .where(
            ChainSnapshot.business_date == business_date,
            ChainSnapshot.protocol_version == PROTOCOL_VERSION,
            ChainSnapshot.cutoff_at == cutoff,
            ChainSnapshot.digest == digest,
        )
        .limit(1)
    )
    if existing:
        return {
            "ok": True,
            "snapshot_id": existing,
            "digest": digest,
            "reused": True,
            "status": body["status"],
            "body": body,
        }
    session.add(
        ChainSnapshot(
            snapshot_id=snapshot_id,
            business_date=business_date,
            cutoff_at=cutoff,
            protocol_version=PROTOCOL_VERSION,
            eligibility_policy_version=ELIGIBILITY_POLICY,
            universe_version=batch.universe_version,
            batch_keys=body["batch_keys"],
            body=body,
            digest=digest,
            generated_at=now,
            generator_version=ENGINE_VERSION,
        )
    )
    session.flush()
    for section, bucket, uid, code in items:
        session.execute(
            pg_insert(ChainSnapshotItem)
            .values(
                snapshot_id=snapshot_id,
                section=section,
                bucket=bucket,
                item_key=uid or code or bucket,
                decision_uid=uid,
                code=code,
                batch_key=batch.batch_key,
                detail={},
            )
            .on_conflict_do_nothing(index_elements=["snapshot_id", "section", "bucket", "item_key"])
        )
    session.flush()
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "digest": digest,
        "reused": False,
        "status": body["status"],
        "body": body,
    }
