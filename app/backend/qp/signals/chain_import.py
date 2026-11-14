"""Import qp's existing signals facts as record-chain history (``legacy.v0``).

One-off and idempotent: every source row gets a deterministic identity and an
``chain_import_register`` entry, so a rerun adds nothing.  Historical decisions
have no trustworthy decision time, so the ``outside_decision_window`` rule is
not applied to them; the four historical exclusions keep ``run_incomplete``
(frozen decision D8).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.models.chain import (ChainBatch, ChainDecision, ChainEligibility, ChainImportRegister,
                             ChainOutcome, ChainPolicy, ChainState, ChainUniverse,
                             ChainUniverseMember)
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision
from qp.signals import chain

LEGACY_RULE = "legacy.v0"
LEGACY_UNIVERSE = "legacy.etf40"
LEGACY_PROTOCOL = "qp.record-chain.v1"


def _legacy_policy(session: Session, now: dt.datetime) -> None:
    definition = {"engine": "legacy", "version": "v0", "protocol_version": LEGACY_PROTOCOL}
    session.execute(pg_insert(ChainPolicy).values(
        policy_version=LEGACY_RULE, kind="rule", definition=definition,
        definition_sha256=chain.sha256_hex(chain.canonical_json(definition)),
        effective_from=now).on_conflict_do_nothing(index_elements=["policy_version"]))


def _legacy_universe(session: Session, trade_date: dt.date, codes: list[str],
                     now: dt.datetime) -> None:
    payload = {"universe_version": LEGACY_UNIVERSE, "codes": sorted(set(codes))}
    session.execute(pg_insert(ChainUniverse).values(
        universe_version=LEGACY_UNIVERSE, trade_date=trade_date,
        code_count=len(set(codes)), checksum=chain.sha256_hex(chain.canonical_json(payload)),
        source="legacy-history", created_at=now).on_conflict_do_nothing(
            index_elements=["universe_version"]))
    for code in sorted(set(codes)):
        session.execute(pg_insert(ChainUniverseMember).values(
            universe_version=LEGACY_UNIVERSE, code=code).on_conflict_do_nothing(
                index_elements=["universe_version", "code"]))


def _legacy_batch(session: Session, trade_date: dt.date, codes: list[str],
                  now: dt.datetime) -> str:
    key = f"legacy:{trade_date.isoformat()}"
    scheduled = dt.datetime.combine(trade_date, dt.time(15, 30), tzinfo=chain.CST)
    session.execute(pg_insert(ChainBatch).values(
        batch_key=key, trade_date=trade_date, scheduled_at=scheduled,
        universe_version=LEGACY_UNIVERSE, expected_target_count=max(1, len(set(codes))),
        source="legacy", created_at=now).on_conflict_do_nothing(index_elements=["batch_key"]))
    return key


def _legacy_eligibility(decision: TargetDecision, run_status: str | None) -> tuple[str, str]:
    if decision.data_status != "ok":
        return "excluded", "data_incomplete"
    if int(decision.raw_trigger_count or 0) == 0:
        return "excluded", "no_trigger"
    if run_status != "success":
        return "excluded", "run_incomplete"
    if str(decision.validation_split or "") not in chain.OOS_SPLITS:
        return "excluded", "non_oos"
    if decision.risk_action == "blocked" or decision.decision_state == "blocked":
        return "excluded", "risk_blocked"
    return "eligible", "eligible"


def _outcome_row(outcome: DecisionOutcome, decision_uid: str, expected: dt.date | None,
                 now: dt.datetime) -> dict[str, Any]:
    filled = outcome.fill_status == "filled"
    return {
        "outcome_uid": chain._uid("oc_", {"decision_uid": decision_uid,
                                          "horizon": outcome.horizon_sessions, "revision": 1}),
        "decision_uid": decision_uid, "source_attempt_id": None,
        "horizon_sessions": outcome.horizon_sessions, "revision": 1,
        "execution_state": "filled" if filled else "unfilled",
        "result_state": "matured" if filled else "invalid",
        "expected_maturity_date": expected or outcome.exit_date,
        "evaluation_as_of": outcome.exit_date,
        "entry_date": outcome.entry_date, "entry_fill": outcome.entry_price,
        "exit_date": outcome.exit_date, "exit_fill": outcome.exit_price,
        "benchmark_return": outcome.benchmark_return, "gross_return": outcome.gross_return,
        "net_return": outcome.net_return, "excess_return": outcome.excess_return,
        "fill_reason": outcome.fill_reason,
        "invalid_reason": None if filled else (outcome.fill_reason or "unfilled"),
        "revision_reason": "initial", "source_kind": "migrated_v1",
        "source_ref": f"decision_outcome:{outcome.id}", "created_at": now,
    }


def import_history(session: Session, *, apply: bool, since: dt.date | None = None,
                   now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    stmt = (select(TargetDecision, EodRun.status)
            .join(EodRun, EodRun.run_id == TargetDecision.run_id)
            .order_by(TargetDecision.decision_date, TargetDecision.code))
    if since is not None:
        stmt = stmt.where(TargetDecision.decision_date >= since)
    rows = session.execute(stmt).all()
    by_date: dict[dt.date, list[TargetDecision]] = {}
    status: dict[int, str] = {}
    for decision, run_status in rows:
        by_date.setdefault(decision.decision_date, []).append(decision)
        status[decision.id] = run_status

    existing_uids = set(session.scalars(select(ChainDecision.decision_uid)).all())
    existing_outcomes = set(session.execute(
        select(ChainOutcome.decision_uid, ChainOutcome.horizon_sessions)
        .where(ChainOutcome.revision == 1)).all())
    outcomes_by_decision: dict[int, list[DecisionOutcome]] = {}
    for decision_id, outcome in session.execute(
            select(DecisionOutcome.decision_id, DecisionOutcome)
            .where(DecisionOutcome.decision_id.in_([d.id for d, _ in rows]))).all():
        outcomes_by_decision.setdefault(decision_id, []).append(outcome)

    summary: dict[str, Any] = {"apply": apply, "dates": len(by_date),
                               "source_decisions": len(rows), "source_outcomes": 0,
                               "new_decisions": 0, "new_outcomes": 0,
                               "eligibility": {}, "per_date": []}
    if apply and by_date:
        _legacy_policy(session, now)
        all_codes = [d.code for _, group in sorted(by_date.items()) for d in group]
        _legacy_universe(session, min(by_date), all_codes, now)

    for trade_date, decisions in sorted(by_date.items()):
        codes = [d.code for d in decisions]
        batch_key = f"legacy:{trade_date.isoformat()}"
        if apply:
            batch_key = _legacy_batch(session, trade_date, codes, now)
        date_counts = {"date": trade_date.isoformat(), "decisions": 0, "outcomes": 0,
                       "new_decisions": 0, "new_outcomes": 0}
        for decision in decisions:
            uid = chain.decision_uid(trade_date, decision.code, batch_key,
                                     LEGACY_RULE, LEGACY_UNIVERSE)
            if uid not in existing_uids:
                if apply:
                    session.execute(pg_insert(ChainDecision).values(
                        decision_uid=uid, trade_date=trade_date, code=decision.code,
                        name=decision.name, batch_key=batch_key, rule_version=LEGACY_RULE,
                        universe_version=LEGACY_UNIVERSE, origin="migrated_v1",
                        attribution_status="confirmed",
                        input_evidence_ref=f"target_decision:{decision.id}",
                        created_at=now, imported_at=now).on_conflict_do_nothing(
                            index_elements=["decision_uid"]))
                    existing_uids.add(uid)
                date_counts["new_decisions"] += 1
                summary["new_decisions"] += 1
            verdict, reason = _legacy_eligibility(decision, status.get(decision.id))
            summary["eligibility"][reason] = summary["eligibility"].get(reason, 0) + 1
            if apply:
                _write_states(session, decision, uid, now)
                _write_eligibility(session, decision, uid, batch_key, verdict, reason, now)
            outcomes = outcomes_by_decision.get(decision.id, [])
            date_counts["decisions"] += 1
            date_counts["outcomes"] += len(outcomes)
            summary["source_outcomes"] += len(outcomes)
            for outcome in outcomes:
                expected = outcome.exit_date
                key = (uid, outcome.horizon_sessions)
                if key not in existing_outcomes:
                    if apply:
                        session.execute(pg_insert(ChainOutcome).values(
                            **_outcome_row(outcome, uid, expected, now)).on_conflict_do_nothing(
                                index_elements=["decision_uid", "horizon_sessions", "revision"]))
                        existing_outcomes.add(key)
                    date_counts["new_outcomes"] += 1
                    summary["new_outcomes"] += 1
                if apply:
                    _register(session, "signals.decision_outcome", str(outcome.id), uid, now)
            if apply:
                _register(session, "signals.target_decision", str(decision.id), uid, now)
        summary["per_date"].append(date_counts)

    if apply:
        _register_missing(session, now)
        summary["reconcile"] = _reconcile(session, by_date)
    return summary


def _write_states(session: Session, decision: TargetDecision, uid: str,
                  now: dt.datetime) -> None:
    blocked = decision.risk_action == "blocked" or decision.decision_state == "blocked"
    pairs = (("data", "complete" if decision.data_status == "ok" else "collection_failed"),
             ("signal", "triggered" if int(decision.raw_trigger_count or 0) > 0
              else "not_triggered"),
             ("risk", "blocked" if blocked else "allowed"))
    for seq, (aspect, state) in enumerate(pairs, start=1):
        session.execute(pg_insert(ChainState).values(
            state_id=chain._uid("st_", {"decision_uid": uid, "aspect": aspect, "seq": 1}),
            decision_uid=uid, source_attempt_id=None, aspect=aspect, state=state,
            reason_code=decision.reason_code if aspect == "risk" else None,
            basis={"legacy": True, "risk_level": decision.risk_level},
            protocol_version=LEGACY_PROTOCOL, observed_at=now, state_seq=1)
            .on_conflict_do_nothing(index_elements=["decision_uid", "aspect", "state_seq"]))


def _write_eligibility(session: Session, decision: TargetDecision, uid: str, batch_key: str,
                       verdict: str, reason: str, now: dt.datetime) -> None:
    eligibility_id = chain._uid("el_", {"decision_uid": uid,
                                        "policy_version": chain.ELIGIBILITY_POLICY, "seq": 1})
    session.execute(pg_insert(ChainEligibility).values(
        eligibility_id=eligibility_id, decision_uid=uid,
        policy_version=chain.ELIGIBILITY_POLICY, verdict=verdict, reason_code=reason,
        reason_detail={"legacy_rule_version": LEGACY_RULE, "batch_key": batch_key,
                       "source_decision_id": decision.id, "migrated": True},
        evidence_refs=[{"kind": "decision", "id": uid},
                       {"kind": "policy", "id": chain.ELIGIBILITY_POLICY}],
        rule_version=LEGACY_RULE, universe_version=LEGACY_UNIVERSE,
        evaluated_at=now, evaluation_seq=1).on_conflict_do_nothing(
            index_elements=["decision_uid", "policy_version", "evaluation_seq"]))


def _register(session: Session, table: str, pk: str, uid: str, now: dt.datetime) -> None:
    session.execute(pg_insert(ChainImportRegister).values(
        import_id=chain._uid("im_", {"source_table": table, "source_pk": pk}),
        source_table=table, source_pk=pk, decision_uid=uid, mapping_status="mapped",
        imported_at=now).on_conflict_do_nothing(index_elements=["source_table", "source_pk"]))


def _register_missing(session: Session, now: dt.datetime) -> None:
    """Any chain decision imported before this run still needs a register row."""
    known = set(session.execute(select(ChainImportRegister.source_table,
                                       ChainImportRegister.source_pk)).all())
    rows = session.execute(select(ChainDecision.decision_uid, ChainDecision.input_evidence_ref)
                           .where(ChainDecision.origin == "migrated_v1")).all()
    for uid, ref in rows:
        if not ref or not ref.startswith("target_decision:"):
            continue
        pk = ref.split(":", 1)[1]
        if ("signals.target_decision", pk) not in known:
            _register(session, "signals.target_decision", pk, uid, now)


def _reconcile(session: Session, by_date: dict[dt.date, list[TargetDecision]]) -> list[dict[str, Any]]:
    out = []
    for trade_date, decisions in sorted(by_date.items()):
        imported = session.scalar(select(func.count()).select_from(ChainDecision).where(
            ChainDecision.trade_date == trade_date,
            ChainDecision.rule_version == LEGACY_RULE)) or 0
        source_outcomes = session.scalar(
            select(func.count()).select_from(DecisionOutcome)
            .join(TargetDecision, TargetDecision.id == DecisionOutcome.decision_id)
            .where(TargetDecision.decision_date == trade_date)) or 0
        imported_outcomes = session.scalar(
            select(func.count()).select_from(ChainOutcome)
            .join(ChainDecision, ChainDecision.decision_uid == ChainOutcome.decision_uid)
            .where(ChainDecision.trade_date == trade_date,
                   ChainDecision.rule_version == LEGACY_RULE)) or 0
        out.append({"date": trade_date.isoformat(), "source_decisions": len(decisions),
                    "imported_decisions": imported, "source_outcomes": source_outcomes,
                    "imported_outcomes": imported_outcomes,
                    "ok": imported >= len(decisions) and imported_outcomes >= source_outcomes})
    return out
