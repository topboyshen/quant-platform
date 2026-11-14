"""ETF record chain writer (M10): identity, states, eligibility, outcome materialisation.

Ported from the 2026-11-10 design freeze.  Callers own the transaction; every helper is
idempotent per attempt so a retry only appends, never duplicates a decision.  The whole
module is gated by ``QP_RECORD_CHAIN`` and never changes ``target_decision`` /
``decision_outcome``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.models.chain import (ChainAttempt, ChainBatch, ChainDecision, ChainEligibility, ChainOutcome,
                             ChainPolicy, ChainSnapshot, ChainSnapshotItem, ChainState, ChainUniverse,
                             ChainUniverseMember)
from qp.models.market import TradingDay
from qp.models.signals import EodRun

CST = dt.timezone(dt.timedelta(hours=8))
PROTOCOL_VERSION = "qp.record-chain.v1"
RULE_VERSION = "qp-swing.v1"
ELIGIBILITY_POLICY = "eligibility.v1"
STATS_POLICY = "stats.snapshot.v1"
ENGINE_VERSION = "qp-chain.v1"
HORIZONS = (1, 3, 5, 10, 20)
OOS_SPLITS = frozenset({"live_oos", "walk_forward_oos"})
WINDOW_EARLIEST = dt.time(15, 25)

ELIGIBILITY_DEFINITION = {
    "policy_version": ELIGIBILITY_POLICY,
    "protocol_version": PROTOCOL_VERSION,
    "candidate": "signal=triggered",
    "primary_horizon_sessions": 20,
    "min_samples": 30,
    "oos_splits": sorted(OOS_SPLITS),
    "require_full_batch": True,
    "window_earliest": WINDOW_EARLIEST.strftime("%H:%M:%S") + "+08:00",
    "precedence": ["attribution_unknown", "invalid_record", "data_incomplete", "no_trigger",
                   "run_incomplete", "outside_decision_window", "non_oos", "risk_blocked",
                   "eligible"],
}
POLICIES: dict[str, tuple[str, dict[str, Any]]] = {
    ELIGIBILITY_POLICY: ("eligibility", ELIGIBILITY_DEFINITION),
    STATS_POLICY: ("stats", {"protocol_version": PROTOCOL_VERSION, "cutoff": "15:50",
                             "primary_horizon_sessions": 20}),
    RULE_VERSION: ("rule", {"engine": "qp-swing", "horizons": list(HORIZONS),
                            "universe": "etf40"}),
    ENGINE_VERSION: ("engine", {"name": "qp-record-chain", "version": "v1"}),
}


def enabled() -> bool:
    return bool(get_settings().record_chain)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uid(prefix: str, payload: Any) -> str:
    return prefix + sha256_hex(canonical_json(payload))[:32]


def decision_uid(trade_date: dt.date, code: str, batch_key: str, rule_version: str,
                 universe_version: str) -> str:
    return _uid("qd_", {"trade_date": trade_date.isoformat(), "code": code,
                        "batch_key": batch_key, "rule_version": rule_version,
                        "universe_version": universe_version})


def universe_version(trade_date: dt.date, codes: Iterable[str]) -> str:
    return "univ_" + sha256_hex(trade_date.isoformat() + "|" + ",".join(sorted(codes)))[:12]


def natural_batch_key(trade_date: dt.date) -> str:
    return f"eod:{trade_date.isoformat()}:15:30"


def _at(session: Session, model, *where) -> Any:
    return session.scalar(select(model).where(*where).limit(1))


def _ensure_policies(session: Session, now: dt.datetime) -> None:
    for version, (kind, definition) in POLICIES.items():
        session.execute(pg_insert(ChainPolicy).values(
            policy_version=version, kind=kind, definition=definition,
            definition_sha256=sha256_hex(canonical_json(definition)),
            effective_from=now.isoformat(), created_at=now).on_conflict_do_nothing(
                index_elements=["policy_version"]))


def _ensure_universe(session: Session, trade_date: dt.date, rows: Sequence[dict[str, Any]],
                     now: dt.datetime) -> str:
    codes = [str(row["code"]) for row in rows]
    version = universe_version(trade_date, codes)
    payload = {"trade_date": trade_date.isoformat(), "codes": sorted(codes)}
    session.execute(pg_insert(ChainUniverse).values(
        universe_version=version, trade_date=trade_date, code_count=len(codes),
        checksum=sha256_hex(canonical_json(payload)), source="qp-signals",
        created_at=now).on_conflict_do_nothing(index_elements=["universe_version"]))
    for row in rows:
        session.execute(pg_insert(ChainUniverseMember).values(
            universe_version=version, code=str(row["code"]), name=row.get("name"),
            sector=row.get("sector")).on_conflict_do_nothing(
                index_elements=["universe_version", "code"]))
    return version


def _ensure_batch(session: Session, run: EodRun, universe: str, source: str,
                  now: dt.datetime) -> str:
    started = run.started_at.astimezone(CST) if run.started_at else now.astimezone(CST)
    if source == "manual":
        key = f"manual:{run.decision_date.isoformat()}:{started.strftime('%H%M')}:{run.run_id}"
    else:
        key = natural_batch_key(run.decision_date)
    scheduled = dt.datetime.combine(run.decision_date, dt.time(15, 30), tzinfo=CST)
    session.execute(pg_insert(ChainBatch).values(
        batch_key=key, trade_date=run.decision_date, scheduled_at=scheduled,
        universe_version=universe, expected_target_count=max(1, len(
            session.scalars(select(ChainUniverseMember.code).where(
                ChainUniverseMember.universe_version == universe)).all())),
        source=source, created_at=now).on_conflict_do_nothing(index_elements=["batch_key"]))
    return key


def _append_state(session: Session, *, decision_uid: str, attempt_id: str, aspect: str, state: str,
                  observed_at: dt.datetime, reason_code: str | None = None,
                  basis: dict[str, Any] | None = None,
                  evidence: list[Any] | None = None) -> bool:
    seen = session.scalar(select(ChainState.state_id).where(
        ChainState.decision_uid == decision_uid, ChainState.aspect == aspect,
        ChainState.source_attempt_id == attempt_id).limit(1))
    if seen:
        return False
    seq = (session.scalar(select(func.max(ChainState.state_seq)).where(
        ChainState.decision_uid == decision_uid, ChainState.aspect == aspect)) or 0) + 1
    session.add(ChainState(
        state_id=_uid("st_", {"decision_uid": decision_uid, "aspect": aspect, "seq": seq}),
        decision_uid=decision_uid, source_attempt_id=attempt_id, aspect=aspect, state=state,
        reason_code=reason_code, basis=basis or {}, evidence_refs=evidence or [],
        protocol_version=PROTOCOL_VERSION, observed_at=observed_at, state_seq=seq))
    session.flush()
    return True


def _upcoming_open_days(session: Session, after: dt.date, count: int = max(HORIZONS)) -> list[dt.date]:
    return list(session.scalars(
        select(TradingDay.date).where(TradingDay.origin == "qp", TradingDay.is_open.is_(True),
                                      TradingDay.date > after)
        .order_by(TradingDay.date).limit(count)).all())


def _attempt_run_statuses(session: Session, decision_uid: str) -> list[str]:
    return list(session.scalars(
        select(EodRun.status).join(ChainAttempt, ChainAttempt.run_id == EodRun.run_id)
        .where(ChainAttempt.decision_uid == decision_uid)).all())


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


def _append_eligibility(session: Session, *, decision_uid: str, decision: dict[str, Any],
                        run: EodRun, batch_key: str, universe: str,
                        statuses: Sequence[str], now: dt.datetime) -> str:
    verdict, reason = _eligibility(decision, run=run, statuses=statuses)
    seen = session.scalar(select(ChainEligibility.eligibility_id).where(
        ChainEligibility.decision_uid == decision_uid,
        ChainEligibility.policy_version == ELIGIBILITY_POLICY,
        ChainEligibility.reason_detail["run_id"].astext == str(run.run_id),
        ChainEligibility.rule_version == RULE_VERSION,
        ChainEligibility.universe_version == universe).limit(1))
    if seen:
        return seen
    seq = (session.scalar(select(func.max(ChainEligibility.evaluation_seq)).where(
        ChainEligibility.decision_uid == decision_uid,
        ChainEligibility.policy_version == ELIGIBILITY_POLICY)) or 0) + 1
    evidence = sorted([{"kind": "decision", "id": decision_uid},
                       {"kind": "policy", "id": ELIGIBILITY_POLICY},
                       {"kind": "run", "id": str(run.run_id)},
                       {"kind": "universe", "id": universe}],
                      key=lambda item: (item["kind"], item["id"]))
    eligibility_id = _uid("el_", {"decision_uid": decision_uid,
                                  "policy_version": ELIGIBILITY_POLICY, "seq": seq})
    session.add(ChainEligibility(
        eligibility_id=eligibility_id, decision_uid=decision_uid,
        policy_version=ELIGIBILITY_POLICY, verdict=verdict, reason_code=reason,
        reason_detail={"rule_version": RULE_VERSION, "universe_version": universe,
                       "batch_key": batch_key, "run_id": run.run_id,
                       "attempt_run_ids": [run.run_id],
                       "source_validation_split": decision.get("validation_split")},
        evidence_refs=evidence, rule_version=RULE_VERSION, universe_version=universe,
        evaluated_at=now, evaluation_seq=seq))
    session.flush()
    return eligibility_id


def _materialise_outcomes(session: Session, *, decision_uid: str, attempt_id: str,
                          trade_date: dt.date, sessions: Sequence[dt.date],
                          now: dt.datetime) -> int:
    existing = set(session.scalars(select(ChainOutcome.horizon_sessions).where(
        ChainOutcome.decision_uid == decision_uid)).all())
    created = 0
    for horizon in HORIZONS:
        if horizon in existing:
            continue
        session.add(ChainOutcome(
            outcome_uid=_uid("oc_", {"decision_uid": decision_uid, "horizon": horizon,
                                     "revision": 1}),
            decision_uid=decision_uid, source_attempt_id=attempt_id,
            horizon_sessions=horizon, revision=1, execution_state="unverified",
            result_state="pending_maturity",
            expected_maturity_date=sessions[horizon - 1] if len(sessions) >= horizon else None,
            source_kind="native", revision_reason="initial"))
        created += 1
    if created:
        session.flush()
    return created


def register_run(session: Session, *, run: EodRun, decisions: Sequence[dict[str, Any]],
                 run_status: str, source: str = "timer",
                 now: dt.datetime | None = None) -> dict[str, Any]:
    """Dual-write one EOD attempt into the chain. Idempotent per ``run_id``."""
    if not enabled():
        return {"enabled": False}
    now = now or dt.datetime.now(dt.timezone.utc)
    _ensure_policies(session, now)
    universe = _ensure_universe(session, run.decision_date, decisions, now)
    batch_key = _ensure_batch(session, run, universe, source, now)
    sessions = _upcoming_open_days(session, run.decision_date)
    counts = {"decisions": 0, "attempts": 0, "states": 0, "eligible": 0, "outcomes": 0}
    for row in decisions:
        uid = decision_uid(run.decision_date, str(row["code"]), batch_key, RULE_VERSION, universe)
        session.execute(pg_insert(ChainDecision).values(
            decision_uid=uid, trade_date=run.decision_date, code=str(row["code"]),
            name=row.get("name"), batch_key=batch_key, rule_version=RULE_VERSION,
            universe_version=universe, origin="native", attribution_status="confirmed",
            input_evidence_ref=f"run:{run.run_id}", created_at=now
        ).on_conflict_do_nothing(index_elements=["decision_uid"]))
        counts["decisions"] += 1
        attempt_id = _uid("att_", {"run_id": run.run_id, "decision_uid": uid})
        inserted = session.execute(pg_insert(ChainAttempt).values(
            attempt_id=attempt_id, decision_uid=uid, run_id=run.run_id,
            started_at=run.started_at, completed_at=run.finished_at,
            engine_version=ENGINE_VERSION, created_at=now
        ).on_conflict_do_nothing(index_elements=["run_id", "decision_uid"]).returning(
            ChainAttempt.attempt_id)).first()
        if inserted:
            counts["attempts"] += 1
        data_state = "complete" if row.get("data_status") == "ok" else "collection_failed"
        signal_state = "triggered" if int(row.get("raw_trigger_count") or 0) > 0 else "not_triggered"
        blocked = row.get("risk_action") == "blocked" or row.get("decision_state") == "blocked"
        risk_state = "blocked" if blocked else "allowed"
        risk_basis = {"risk_level": row.get("risk_level"), "reason_code": row.get("reason_code"),
                      "raw_trigger_count": row.get("raw_trigger_count")}
        for aspect, state, basis in (("data", data_state, {"data_status": row.get("data_status")}),
                                     ("signal", signal_state,
                                      {"raw_trigger_count": row.get("raw_trigger_count")}),
                                     ("risk", risk_state, risk_basis)):
            if _append_state(session, decision_uid=uid, attempt_id=attempt_id, aspect=aspect,
                             state=state, observed_at=now,
                             reason_code=row.get("reason_code") if aspect == "risk" else None,
                             basis=basis):
                counts["states"] += 1
        statuses = _attempt_run_statuses(session, uid)
        eligibility_id = _append_eligibility(session, decision_uid=uid, decision=row, run=run,
                                             batch_key=batch_key, universe=universe,
                                             statuses=statuses, now=now)
        if session.scalar(select(ChainEligibility.verdict).where(
                ChainEligibility.eligibility_id == eligibility_id)) == "eligible":
            counts["eligible"] += 1
        if risk_state == "allowed" and signal_state == "triggered":
            counts["outcomes"] += _materialise_outcomes(
                session, decision_uid=uid, attempt_id=attempt_id, trade_date=run.decision_date,
                sessions=sessions, now=now)
    return {"enabled": True, "batch_key": batch_key, "universe_version": universe,
            "run_status": run_status, **counts}


def append_outcome_revision(session: Session, *, decision_uid: str, horizon: int,
                            row: dict[str, Any], expected: dt.date | None,
                            as_of: dt.date, now: dt.datetime | None = None) -> bool:
    """Append the result revision for one horizon; the pending row is never updated."""
    now = now or dt.datetime.now(dt.timezone.utc)
    latest = session.scalar(select(func.max(ChainOutcome.revision)).where(
        ChainOutcome.decision_uid == decision_uid,
        ChainOutcome.horizon_sessions == horizon)) or 0
    if latest == 0:
        return False
    filled = row.get("fill_status") == "filled"
    session.add(ChainOutcome(
        outcome_uid=_uid("oc_", {"decision_uid": decision_uid, "horizon": horizon,
                                 "revision": latest + 1}),
        decision_uid=decision_uid, source_attempt_id=None, horizon_sessions=horizon,
        revision=latest + 1,
        execution_state="filled" if filled else "unfilled",
        result_state="matured" if filled else "invalid",
        expected_maturity_date=expected, evaluation_as_of=as_of,
        entry_date=row.get("entry_date"), entry_fill=row.get("entry_price"),
        exit_date=row.get("exit_date"), exit_fill=row.get("exit_price"),
        benchmark_return=row.get("benchmark_return"), gross_return=row.get("gross_return"),
        net_return=row.get("net_return"), excess_return=row.get("excess_return"),
        fill_reason=row.get("fill_reason"),
        invalid_reason=None if filled else (row.get("fill_reason") or "unfilled"),
        revision_reason="late_result" if expected and as_of > expected else "correction",
        source_kind="native", created_at=now))
    session.flush()
    return True


def reconcile_expired(session: Session, *, as_of: dt.date,
                      now: dt.datetime | None = None) -> int:
    """Mark unverified outcomes past their maturity date as expired (append-only)."""
    if not enabled():
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = session.execute(select(ChainOutcome.decision_uid, ChainOutcome.horizon_sessions,
                                  ChainOutcome.expected_maturity_date, ChainOutcome.revision)
                           .where(ChainOutcome.expected_maturity_date < as_of,
                                  ChainOutcome.execution_state == "unverified")).all()
    latest = {(uid, horizon): revision for uid, horizon, _, revision in session.execute(
        select(ChainOutcome.decision_uid, ChainOutcome.horizon_sessions,
               ChainOutcome.expected_maturity_date, ChainOutcome.revision)).all()}
    appended = 0
    for uid, horizon, expected, revision in rows:
        if latest.get((uid, horizon)) != revision:
            continue
        session.add(ChainOutcome(
            outcome_uid=_uid("oc_", {"decision_uid": uid, "horizon": horizon,
                                     "revision": revision + 1}),
            decision_uid=uid, horizon_sessions=horizon, revision=revision + 1,
            execution_state="expired", result_state="expired_uncollected",
            expected_maturity_date=expected, evaluation_as_of=as_of,
            revision_reason="expiry_sweep", source_kind="native", created_at=now))
        appended += 1
    if appended:
        session.flush()
    return appended


def expected_maturity(session: Session, decision_uid: str, horizon: int) -> dt.date | None:
    return session.scalar(select(ChainOutcome.expected_maturity_date).where(
        ChainOutcome.decision_uid == decision_uid,
        ChainOutcome.horizon_sessions == horizon).order_by(ChainOutcome.revision.desc()).limit(1))


def decision_uid_for(session: Session, decision_date: dt.date, code: str) -> str | None:
    """Find the chain identity for a legacy decision row (natural batch first)."""
    natural = natural_batch_key(decision_date)
    uid = session.scalar(select(ChainDecision.decision_uid).where(
        ChainDecision.trade_date == decision_date, ChainDecision.code == code,
        ChainDecision.batch_key == natural).limit(1))
    if uid:
        return uid
    return session.scalar(select(ChainDecision.decision_uid).where(
        ChainDecision.trade_date == decision_date, ChainDecision.code == code).limit(1))


SAMPLE_BUCKETS = ("waiting_maturity", "qualified_matured", "historical_excluded", "other_excluded",
                  "attribution_pending", "expired_uncollected", "invalid")


def _latest(rows: Iterable[Any], key, order) -> dict[Any, Any]:
    best: dict[Any, Any] = {}
    for row in rows:
        slot = key(row)
        if slot not in best or order(row) > order(best[slot]):
            best[slot] = row
    return best


def _snapshot_body(session: Session, business_date: dt.date, batch: ChainBatch,
                   cutoff: dt.datetime) -> tuple[dict[str, Any], list[tuple[str, str, str | None, str | None]]]:
    """Assemble the deterministic snapshot body plus its expandable items."""
    decisions = list(session.scalars(select(ChainDecision).where(
        ChainDecision.trade_date == business_date,
        ChainDecision.batch_key == batch.batch_key)))
    uids = [d.decision_uid for d in decisions]
    members = list(session.scalars(select(ChainUniverseMember).where(
        ChainUniverseMember.universe_version == batch.universe_version)))
    states = list(session.scalars(select(ChainState).where(ChainState.decision_uid.in_(uids)))) if uids else []
    current_state = _latest(states, lambda r: (r.decision_uid, r.aspect), lambda r: r.state_seq)
    eligibility = list(session.scalars(select(ChainEligibility).where(
        ChainEligibility.decision_uid.in_(uids)))) if uids else []
    current_eligibility = _latest(eligibility, lambda r: r.decision_uid, lambda r: r.evaluation_seq)
    outcomes = list(session.scalars(select(ChainOutcome).where(
        ChainOutcome.decision_uid.in_(uids)))) if uids else []
    latest_outcome = _latest(outcomes, lambda r: (r.decision_uid, r.horizon_sessions),
                             lambda r: r.revision)
    runs = session.execute(select(EodRun.status, EodRun.finished_at)
                           .join(ChainAttempt, ChainAttempt.run_id == EodRun.run_id)
                           .where(ChainAttempt.decision_uid.in_(uids))).all() if uids else []
    terminal = any(finished is not None for _, finished in runs)

    funnel = {"checked": len(members), "triggered": 0, "risk_blocked": 0, "risk_allowed": 0,
              "admitted": 0}
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
            continue
        if not (triggered and allowed):
            continue
        if entry.verdict == "eligible":
            funnel["admitted"] += 1
            items.append(("funnel", "admitted", uid, decision.code))
            outcome = latest_outcome.get((uid, max(HORIZONS)))
            if outcome is None or outcome.result_state == "pending_maturity" \
                    or outcome.execution_state == "unverified":
                bucket = "waiting_maturity"
            elif outcome.result_state == "matured":
                bucket = "qualified_matured"
            elif outcome.result_state == "expired_uncollected":
                bucket = "expired_uncollected"
            else:
                bucket = "invalid"
            if outcome is not None and outcome.expected_maturity_date \
                    and outcome.expected_maturity_date < cutoff.date() \
                    and bucket == "waiting_maturity":
                matured_without_result += 1
        elif entry.verdict == "excluded":
            bucket = ("historical_excluded"
                      if decision.origin == "migrated_v1" and entry.reason_code == "run_incomplete"
                      else "other_excluded")
        else:
            bucket = "attribution_pending"
        sample[bucket] += 1
        items.append(("sample", bucket, uid, decision.code))

    loss = {"universe_members_without_decision": max(0, len(members) - len(decisions)),
            "signal_decisions_without_eligibility": without_eligibility,
            "matured_without_result": matured_without_result}
    for member in members:
        if member.code not in uid_by_code:
            items.append(("loss", "universe_members_without_decision", None, member.code))
    for decision in decisions:
        if decision.decision_uid not in current_eligibility and \
                (current_state.get((decision.decision_uid, "signal")) is not None):
            items.append(("loss", "signal_decisions_without_eligibility",
                          decision.decision_uid, decision.code))

    eligible_dates = [e.evaluated_at for e in current_eligibility.values() if e.verdict == "eligible"]
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
        "progress": {"matured": sample["qualified_matured"], "threshold": 30,
                     "primary_horizon_sessions": max(HORIZONS),
                     "last_new_eligible_date": max(eligible_dates).date().isoformat()
                     if eligible_dates else None},
        "loss": loss,
    }
    return body, items


def build_snapshot(session: Session, *, business_date: dt.date,
                   now: dt.datetime | None = None) -> dict[str, Any]:
    """Generate (or reuse) the single snapshot for a business day."""
    now = now or dt.datetime.now(dt.timezone.utc)
    batch = session.scalar(select(ChainBatch).where(
        ChainBatch.trade_date == business_date, ChainBatch.source == "timer")
        .order_by(ChainBatch.scheduled_at.desc()).limit(1))
    if batch is None:
        return {"ok": False, "reason": "no_batch"}
    cutoff = dt.datetime.combine(business_date, dt.time(15, 50), tzinfo=CST)
    body, items = _snapshot_body(session, business_date, batch, cutoff)
    digest = sha256_hex(canonical_json(body))
    snapshot_id = "snp_" + digest[:32]
    existing = session.scalar(select(ChainSnapshot.snapshot_id).where(
        ChainSnapshot.business_date == business_date,
        ChainSnapshot.protocol_version == PROTOCOL_VERSION,
        ChainSnapshot.cutoff_at == cutoff,
        ChainSnapshot.digest == digest).limit(1))
    if existing:
        return {"ok": True, "snapshot_id": existing, "digest": digest, "reused": True,
                "status": body["status"], "body": body}
    session.add(ChainSnapshot(
        snapshot_id=snapshot_id, business_date=business_date, cutoff_at=cutoff,
        protocol_version=PROTOCOL_VERSION, eligibility_policy_version=ELIGIBILITY_POLICY,
        universe_version=batch.universe_version, batch_keys=body["batch_keys"], body=body,
        digest=digest, generated_at=now, generator_version=ENGINE_VERSION))
    session.flush()
    for section, bucket, uid, code in items:
        session.execute(pg_insert(ChainSnapshotItem).values(
            snapshot_id=snapshot_id, section=section, bucket=bucket,
            item_key=uid or code or bucket, decision_uid=uid, code=code,
            batch_key=batch.batch_key, detail={}
        ).on_conflict_do_nothing(index_elements=["snapshot_id", "section", "bucket", "item_key"]))
    session.flush()
    return {"ok": True, "snapshot_id": snapshot_id, "digest": digest, "reused": False,
            "status": body["status"], "body": body}
