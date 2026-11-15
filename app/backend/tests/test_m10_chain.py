"""M10 record chain: schema, identity, dual-write and the outcome queue."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from qp.models.chain import (ChainAttempt, ChainBatch, ChainDecision, ChainEligibility,
                             ChainImportRegister, ChainOutcome, ChainPolicy, ChainState)
from qp.models.market import TradingDay
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision
from qp.signals import chain

TABLES = {
    'chain_policy', 'chain_universe', 'chain_universe_member', 'chain_batch', 'chain_decision',
    'chain_attempt', 'chain_state', 'chain_outcome', 'chain_eligibility', 'chain_snapshot',
    'chain_snapshot_supersede', 'chain_snapshot_item', 'chain_evidence_link',
    'chain_import_register',
}
VIEWS = {
    'chain_decision_canonical_attempt', 'chain_decision_current_state',
    'chain_decision_current_outcome', 'chain_decision_current_eligibility',
}
DAY = dt.date(2026, 11, 10)
CST = dt.timezone(dt.timedelta(hours=8))


def _names(engine, kind: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "select table_name from information_schema.tables "
            f"where table_schema='signals' and table_type='{kind}' and table_name like 'chain_%'"))}


def test_chain_tables_and_views_exist(engine):
    assert _names(engine, 'BASE TABLE') == TABLES
    assert _names(engine, 'VIEW') == VIEWS


def test_chain_rows_are_append_only(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text(
            "insert into signals.chain_evidence_link "
            "(link_id, owner_kind, owner_id, sidecar_kind, sidecar_path, db_state, sidecar_state,"
            " checked_at, created_at) values "
            "(:id, 'decision', 'qd_probe', 'outcome_input', '/tmp/probe', 'present', 'pending',"
            " :now, :now)"), {"id": "ev_" + "0" * 32, "now": now})
        with pytest.raises(DBAPIError):
            conn.execute(text("update signals.chain_evidence_link set db_state='missing'"))
        trans.rollback()


def _calendar(engine, start: dt.date, days: int = 40) -> None:
    with Session(engine) as session:
        for i in range(days):
            day = start + dt.timedelta(days=i)
            session.merge(TradingDay(origin="qp", date=day, is_open=True, source="test"))
        session.commit()


def _run(engine, *, attempt_no: int = 1, status: str = "success",
         started: dt.datetime | None = None, clean: bool = True,
         finished: bool = True) -> int:
    with Session(engine) as session:
        if clean:
            session.execute(EodRun.__table__.delete().where(EodRun.decision_date == DAY))
        run = EodRun(decision_date=DAY, attempt_no=attempt_no, status=status,
                     decision_outcome="emitted", target_count=3, evaluated_count=3,
                     failed_count=0, signal_count=1, allowed_signal_count=1,
                     blocked_signal_count=1, reason_codes=[])
        run.started_at = started or dt.datetime.combine(DAY, dt.time(15, 30), tzinfo=CST)
        if finished:
            run.finished_at = run.started_at + dt.timedelta(minutes=3)
        session.add(run)
        session.commit()
        return run.run_id


def _row(code: str, state: str, *, data: str = "ok", trigger: int = 0,
         risk: str = "allowed", split: str = "live_oos") -> dict:
    return {"code": code, "name": code, "sector": "test", "data_status": data,
            "decision_state": state, "risk_action": risk, "risk_level": "ok",
            "reason_code": "r", "reference_close": 1.0, "raw_trigger_count": trigger,
            "raw_signals": [], "validation_split": split}


def _decisions() -> list[dict]:
    return [_row("510300", "allowed", trigger=1),
            _row("510500", "no_trigger"),
            _row("159915", "blocked", trigger=1, risk="blocked")]


def _cleanup(engine) -> None:
    with Session(engine) as session:
        from qp.models.chain import (ChainBatch, ChainPolicy, ChainSnapshot, ChainSnapshotItem,
                                     ChainUniverse, ChainUniverseMember)
        for model in (ChainSnapshotItem, ChainSnapshot, ChainImportRegister, ChainOutcome, ChainState,
                      ChainEligibility, ChainAttempt, ChainDecision, ChainBatch,
                      ChainUniverseMember, ChainUniverse, ChainPolicy):
            session.execute(text(f"ALTER TABLE signals.{model.__tablename__} DISABLE TRIGGER USER"))
            session.execute(model.__table__.delete())
            session.execute(text(f"ALTER TABLE signals.{model.__tablename__} ENABLE TRIGGER USER"))
        session.commit()


@pytest.fixture()
def chain_on(monkeypatch):
    monkeypatch.setenv("QP_RECORD_CHAIN", "1")
    return True


def test_register_run_materialises_only_allowed(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    try:
        with Session(engine) as session:
            run = session.get(EodRun, run_id)
            out = chain.register_run(session, run=run, decisions=_decisions(),
                                     run_status="success", source="timer")
            session.commit()
        assert out["enabled"] and out["decisions"] == 3 and out["attempts"] == 3
        assert out["states"] == 9 and out["eligible"] == 1 and out["outcomes"] == 5
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(ChainDecision)) == 3
            verdicts = dict(session.execute(select(ChainEligibility.decision_uid,
                                                   ChainEligibility.reason_code)).all())
            assert sorted(verdicts.values()) == ["eligible", "no_trigger", "risk_blocked"]
            allowed_uid = session.scalar(select(ChainEligibility.decision_uid).where(
                ChainEligibility.reason_code == "eligible"))
            outcomes = list(session.scalars(select(ChainOutcome).where(
                ChainOutcome.decision_uid == allowed_uid).order_by(ChainOutcome.horizon_sessions)))
            assert [o.horizon_sessions for o in outcomes] == [1, 3, 5, 10, 20]
            assert all(o.result_state == "pending_maturity" and o.execution_state == "unverified"
                       for o in outcomes)
            assert all(o.expected_maturity_date > DAY for o in outcomes)
            assert session.scalar(select(func.count()).select_from(ChainOutcome)) == 5
    finally:
        _cleanup(engine)


def test_retry_appends_attempts_not_decisions(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    first = _run(engine, attempt_no=1, status="partial")
    second = _run(engine, attempt_no=2, clean=False)
    try:
        with Session(engine) as session:
            assert chain.register_run(session, run=session.get(EodRun, first),
                                      decisions=_decisions(), run_status="partial")["attempts"] == 3
            session.commit()
        with Session(engine) as session:
            out = chain.register_run(session, run=session.get(EodRun, second),
                                     decisions=_decisions(), run_status="success")
            session.commit()
        assert out["attempts"] == 3
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(ChainDecision)) == 3
            assert session.scalar(select(func.count()).select_from(ChainAttempt)) == 6
            assert session.scalar(select(func.count()).select_from(ChainState)) == 18
            # first attempt was partial -> run_incomplete; retry success -> eligible
            uid = chain.decision_uid_for(session, DAY, "510300")
            seqs = session.execute(select(ChainEligibility.evaluation_seq,
                                          ChainEligibility.reason_code)
                                   .where(ChainEligibility.decision_uid == uid)
                                   .order_by(ChainEligibility.evaluation_seq)).all()
            assert [seq for seq, _ in seqs] == [1, 2]
            assert [reason for _, reason in seqs] == ["run_incomplete", "eligible"]
    finally:
        _cleanup(engine)


def test_expired_reconciliation_appends_revision(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    try:
        with Session(engine) as session:
            chain.register_run(session, run=session.get(EodRun, run_id),
                               decisions=_decisions(), run_status="success")
            session.commit()
        with Session(engine) as session:
            appended = chain.reconcile_expired(session, as_of=DAY + dt.timedelta(days=60))
            session.commit()
        assert appended == 5
        with Session(engine) as session:
            rows = list(session.scalars(select(ChainOutcome).order_by(
                ChainOutcome.horizon_sessions, ChainOutcome.revision)))
            latest = [r for r in rows if r.revision == 2]
            assert len(latest) == 5
            assert all(r.execution_state == "expired" and r.result_state == "expired_uncollected"
                       for r in latest)
            assert session.scalar(select(func.count()).select_from(ChainOutcome)) == 10
    finally:
        _cleanup(engine)


def test_due_queue_skips_decisions_without_due_horizons(engine):
    _cleanup(engine)
    _calendar(engine, DAY, days=10)
    with Session(engine) as session:
        session.execute(EodRun.__table__.delete().where(EodRun.decision_date == DAY))
        run = EodRun(decision_date=DAY, attempt_no=1, status="success", decision_outcome="emitted",
                     target_count=2, evaluated_count=2, failed_count=0, signal_count=2,
                     allowed_signal_count=2, blocked_signal_count=0, reason_codes=[])
        session.add(run)
        session.flush()
        old = TargetDecision(run_id=run.run_id, decision_date=DAY, code="510300",
                             data_status="ok", decision_state="allowed", risk_action="allowed",
                             reason_code="r", raw_trigger_count=1, raw_signals=[],
                             validation_split="live_oos")
        new = TargetDecision(run_id=run.run_id, decision_date=DAY + dt.timedelta(days=3),
                             code="510500", data_status="ok", decision_state="allowed",
                             risk_action="allowed", reason_code="r", raw_trigger_count=1,
                             raw_signals=[], validation_split="live_oos")
        session.add_all([old, new])
        session.flush()
        session.add_all([DecisionOutcome(decision_id=old.id, horizon_sessions=h,
                                         fill_status="filled") for h in (1, 3)])
        session.commit()
        old_id, new_id = old.id, new.id
    try:
        from qp.signals.outcomes import _filter_due
        with Session(engine) as session:
            decisions = list(session.scalars(select(TargetDecision)))
            due = _filter_due(session, decisions, DAY + dt.timedelta(days=4))
        assert [d.id for d in due] == [new_id]
        assert old_id not in {d.id for d in due}
    finally:
        with Session(engine) as session:
            session.execute(DecisionOutcome.__table__.delete())
            session.execute(TargetDecision.__table__.delete())
            session.execute(EodRun.__table__.delete())
            session.commit()

def _token(engine) -> str:
    from qp.auth.sessions import create_service_token
    with Session(engine) as session:
        token = create_service_token(session, f"m10-chain-{dt.datetime.now().timestamp()}",
                                     ["signals:read"])
        session.commit()
    return token


def test_snapshot_build_buckets_and_replay(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    try:
        with Session(engine) as session:
            chain.register_run(session, run=session.get(EodRun, run_id),
                               decisions=_decisions(), run_status="success")
            session.commit()
        with Session(engine) as session:
            first = chain.build_snapshot(session, business_date=DAY)
            session.commit()
        assert first["ok"] and first["reused"] is False and first["status"] == "complete"
        body = first["body"]
        assert body["funnel"] == {"checked": 3, "triggered": 2, "risk_blocked": 1,
                                  "risk_allowed": 1, "admitted": 1}
        # sample only covers allowed signals; blocked/no-trigger are control groups
        assert body["sample"]["waiting_maturity"] == 1
        assert sum(body["sample"].values()) == 1
        assert sum(body["loss"].values()) == 0
        with Session(engine) as session:
            items = session.execute(text(
                "select section, bucket, count(*) from signals.chain_snapshot_item "
                "where snapshot_id=:id group by section, bucket"),
                {"id": first["snapshot_id"]}).all()
            counts = {(section, bucket): n for section, bucket, n in items}
            for bucket, value in body["funnel"].items():
                assert counts.get(("funnel", bucket), 0) == value, bucket
            for bucket, value in body["sample"].items():
                assert counts.get(("sample", bucket), 0) == value, bucket
        with Session(engine) as session:
            second = chain.build_snapshot(session, business_date=DAY)
            session.commit()
            assert second["reused"] is True and second["snapshot_id"] == first["snapshot_id"]
            assert session.scalar(select(func.count()).select_from(
                __import__("qp.models.chain", fromlist=["ChainSnapshot"]).ChainSnapshot)) == 1
    finally:
        _cleanup(engine)


def test_chain_api_snapshot_decisions_detail(engine, client, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    try:
        with Session(engine) as session:
            chain.register_run(session, run=session.get(EodRun, run_id),
                               decisions=_decisions(), run_status="success")
            session.commit()
        with Session(engine) as session:
            snap = chain.build_snapshot(session, business_date=DAY)
            session.commit()
        headers = {"authorization": f"Bearer {_token(engine)}"}
        assert client.get("/api/signals/chain/snapshot").status_code == 401

        got = client.get("/api/signals/chain/snapshot", headers=headers)
        assert got.status_code == 200
        assert got.json()["snapshot_id"] == snap["snapshot_id"]
        assert got.json()["body"]["funnel"]["admitted"] == 1

        listing = client.get("/api/signals/chain/decisions", headers=headers,
                             params={"date": DAY.isoformat()}).json()
        assert listing["total"] == 3
        blocked = client.get("/api/signals/chain/decisions", headers=headers,
                             params={"state": "blocked"}).json()
        assert blocked["total"] == 1 and blocked["rows"][0]["code"] == "159915"
        eligible = client.get("/api/signals/chain/decisions", headers=headers,
                              params={"eligibility": "eligible"}).json()
        assert eligible["total"] == 1 and eligible["rows"][0]["code"] == "510300"

        uid = eligible["rows"][0]["decision_uid"]
        detail = client.get(f"/api/signals/chain/decision/{uid}", headers=headers).json()
        assert len(detail["attempts"]) == 1
        assert {s["aspect"] for s in detail["states"]} == {"data", "signal", "risk"}
        assert [o["horizon_sessions"] for o in detail["outcomes"]] == [1, 3, 5, 10, 20]
        assert detail["eligibility"][0]["verdict"] == "eligible"
        assert client.get("/api/signals/chain/decision/qd_missing", headers=headers).status_code == 404
    finally:
        _cleanup(engine)

def test_pool_page_has_record_chain_tab():
    from pathlib import Path
    views = Path(__file__).resolve().parents[2] / "frontend" / "src" / "views" / "signals"
    pool = (views / "EtfPool.vue").read_text(encoding="utf-8")
    panel = (views / "ChainPanel.vue").read_text(encoding="utf-8")
    assert 'tab="研究池"' in pool and 'tab="记录链"' in pool and "<ChainPanel" in pool
    for label in ("检查", "触发", "风控拦截", "风控放行", "进入样本"):
        assert label in panel, label
    for label in ("待成熟", "合格且成熟", "历史排除", "成员无决策", "触发缺资格"):
        assert label in panel, label
    assert "/signals/chain/snapshot" in panel and "/signals/chain/decisions" in panel
    assert "/signals/chain/decision/" in panel

def test_history_import_dry_run_then_apply_is_idempotent(engine, chain_on):
    from qp.signals import chain_import
    _cleanup(engine)
    _calendar(engine, DAY)
    _calendar(engine, DAY + dt.timedelta(days=30))
    run_id = _run(engine)
    partial_day = DAY + dt.timedelta(days=30)
    with Session(engine) as session:
        partial = EodRun(decision_date=partial_day, attempt_no=1, status="partial",
                         decision_outcome="partial", target_count=1, evaluated_count=1,
                         failed_count=0, signal_count=1, allowed_signal_count=1,
                         blocked_signal_count=0, reason_codes=[])
        partial.started_at = dt.datetime.combine(partial_day, dt.time(15, 30), tzinfo=CST)
        partial.finished_at = partial.started_at + dt.timedelta(minutes=3)
        session.add(partial)
        session.commit()
        partial_run = partial.run_id
    with Session(engine) as session:
        rows = []
        for code, state, trigger, risk in (("510300", "allowed", 1, "allowed"),
                                           ("510500", "no_trigger", 0, "allowed"),
                                           ("159915", "blocked", 1, "blocked")):
            rows.append(TargetDecision(run_id=run_id, decision_date=DAY, code=code,
                                       data_status="ok", decision_state=state,
                                       risk_action=risk, reason_code="r",
                                       raw_trigger_count=trigger, raw_signals=[],
                                       validation_split="live_oos"))
        rows.append(TargetDecision(run_id=partial_run, decision_date=partial_day, code="510300",
                                   data_status="ok", decision_state="allowed",
                                   risk_action="allowed", reason_code="r",
                                   raw_trigger_count=1, raw_signals=[],
                                   validation_split="live_oos"))
        session.add_all(rows)
        session.flush()
        allowed = rows[0]
        for horizon in (1, 3):
            session.add(DecisionOutcome(decision_id=allowed.id, horizon_sessions=horizon,
                                        entry_date=DAY + dt.timedelta(days=1),
                                        exit_date=DAY + dt.timedelta(days=horizon),
                                        entry_price=10.0, exit_price=10.2,
                                        gross_return=0.02, net_return=0.019,
                                        fill_status="filled"))
        session.commit()
    try:
        with Session(engine) as session:
            dry = chain_import.import_history(session, apply=False)
            session.rollback()
        assert dry["source_decisions"] == 4 and dry["source_outcomes"] == 2
        assert dry["new_decisions"] == 4 and dry["new_outcomes"] == 2
        assert dry["eligibility"]["eligible"] == 1
        assert dry["eligibility"]["run_incomplete"] == 1
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(ChainDecision)) == 0

        with Session(engine) as session:
            first = chain_import.import_history(session, apply=True)
            session.commit()
        assert first["new_decisions"] == 4 and first["new_outcomes"] == 2
        assert all(item["ok"] for item in first["reconcile"])
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(ChainDecision)) == 4
            assert session.scalar(select(func.count()).select_from(ChainOutcome)) == 2
            assert session.scalar(select(func.count()).select_from(ChainImportRegister)) == 6
            origins = set(session.scalars(select(ChainDecision.origin)))
            assert origins == {"migrated_v1"}
            kinds = set(session.scalars(select(ChainOutcome.source_kind)))
            assert kinds == {"migrated_v1"}
            uid = session.scalar(select(ChainDecision.decision_uid).where(
                ChainDecision.trade_date == partial_day))
            assert session.scalar(select(ChainEligibility.reason_code).where(
                ChainEligibility.decision_uid == uid)) == "run_incomplete"

        with Session(engine) as session:
            again = chain_import.import_history(session, apply=True)
            session.commit()
        assert again["new_decisions"] == 0 and again["new_outcomes"] == 0
        with Session(engine) as session:
            rows = session.execute(select(ChainImportRegister.original_business_date,
                                          ChainImportRegister.decision_uid)).all()
            assert rows and all(row[0] is not None for row in rows)
    finally:
        _cleanup(engine)
        with Session(engine) as session:
            session.execute(DecisionOutcome.__table__.delete())
            session.execute(TargetDecision.__table__.delete())
            session.execute(EodRun.__table__.delete().where(
                EodRun.decision_date.in_([DAY, partial_day])))
            session.commit()

def test_chain_compare_matches_legacy_counts(engine, chain_on):
    from qp.signals import chain_compare
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    try:
        with Session(engine) as session:
            run = session.get(EodRun, run_id)
            decisions = _decisions()
            for row in decisions:
                session.add(TargetDecision(
                    run_id=run_id, decision_date=DAY, code=row["code"], name=row["name"],
                    data_status=row["data_status"], decision_state=row["decision_state"],
                    risk_action=row["risk_action"], reason_code=row["reason_code"],
                    raw_trigger_count=row["raw_trigger_count"], raw_signals=[],
                    validation_split=row["validation_split"]))
            session.commit()
        with Session(engine) as session:
            assert chain_compare.compare(session, DAY)["reason"] == "no_snapshot"
            chain.register_run(session, run=session.get(EodRun, run_id),
                               decisions=_decisions(), run_status="success")
            chain.build_snapshot(session, business_date=DAY)
            session.commit()
        with Session(engine) as session:
            out = chain_compare.compare(session, DAY)
        assert out["ok"] and out["diffs"] == []
        assert out["checks"][0] == {"item": "checked", "chain": 3, "legacy": 3, "match": True}
        assert out["legacy"]["triggered"] == 2 and out["legacy"]["risk_blocked"] == 1
    finally:
        _cleanup(engine)
        with Session(engine) as session:
            session.execute(DecisionOutcome.__table__.delete())
            session.execute(TargetDecision.__table__.delete())
            session.execute(EodRun.__table__.delete().where(EodRun.decision_date == DAY))
            session.commit()


def test_register_run_is_idempotent_across_sessions(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    decisions = _decisions()
    for _ in range(2):
        with Session(engine) as session:
            chain.register_run(session, run=session.get(EodRun, run_id),
                               decisions=decisions, run_status="success")
            session.commit()
    with Session(engine) as session:
        counts = (session.scalar(select(func.count()).select_from(ChainDecision)),
                  session.scalar(select(func.count()).select_from(ChainAttempt)),
                  session.scalar(select(func.count()).select_from(ChainState)),
                  session.scalar(select(func.count()).select_from(ChainEligibility)),
                  session.scalar(select(func.count()).select_from(ChainBatch)))
    assert counts == (3, 3, 9, 3, 1)


def test_snapshot_flags_unknown_attribution(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    decisions = _decisions() + [_row("588000", "allowed", trigger=1)]
    with Session(engine) as session:
        chain.register_run(session, run=session.get(EodRun, run_id),
                           decisions=decisions, run_status="success")
        session.commit()
        batch = session.scalar(select(ChainBatch).where(ChainBatch.trade_date == DAY,
                                                        ChainBatch.source == "timer"))
        uid = chain.decision_uid(DAY, "588000", batch.batch_key, chain.RULE_VERSION,
                                 batch.universe_version)
        session.execute(text("ALTER TABLE signals.chain_eligibility DISABLE TRIGGER USER"))
        session.execute(ChainEligibility.__table__.delete().where(
            ChainEligibility.decision_uid == uid))
        session.execute(text("ALTER TABLE signals.chain_eligibility ENABLE TRIGGER USER"))
        session.commit()
    with Session(engine) as session:
        out = chain.build_snapshot(session, business_date=DAY)
        session.commit()
    body = out["body"]
    assert body["funnel"]["triggered"] == 3
    assert body["funnel"]["risk_allowed"] == 2 and body["funnel"]["admitted"] == 1
    assert body["sample"]["attribution_pending"] == 1
    assert body["loss"]["signal_decisions_without_eligibility"] == 1


def test_snapshot_ignores_other_policy_versions(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY)
    run_id = _run(engine)
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        chain.register_run(session, run=session.get(EodRun, run_id),
                           decisions=_decisions(), run_status="success")
        session.commit()
        batch = session.scalar(select(ChainBatch).where(ChainBatch.trade_date == DAY,
                                                        ChainBatch.source == "timer"))
        uid = chain.decision_uid(DAY, "510300", batch.batch_key, chain.RULE_VERSION,
                                 batch.universe_version)
        session.add(ChainPolicy(policy_version="eligibility.v0", kind="eligibility",
                                definition={}, definition_sha256="0" * 64,
                                effective_from=now, created_at=now))
        session.flush()
        session.add(ChainEligibility(
            eligibility_id="el_v0_probe", decision_uid=uid, policy_version="eligibility.v0",
            verdict="excluded", reason_code="probe", reason_detail={}, evidence_refs=[],
            rule_version=chain.RULE_VERSION, universe_version=batch.universe_version,
            evaluated_at=now, evaluation_seq=99))
        session.commit()
    with Session(engine) as session:
        out = chain.build_snapshot(session, business_date=DAY)
        session.commit()
    body = out["body"]
    assert body["funnel"]["admitted"] == 1          # v1 still says eligible
    assert body["sample"]["waiting_maturity"] == 1  # the eligible decision waits for maturity
    assert body["sample"]["attribution_pending"] == 0
    # the sample only covers triggered+allowed decisions; no_trigger/blocked are not bucketed
    assert body["sample"]["other_excluded"] == 0


def test_universe_version_follows_trade_date(engine, chain_on):
    _cleanup(engine)
    _calendar(engine, DAY, 5)
    run_id = _run(engine)
    next_day = DAY + dt.timedelta(days=1)
    with Session(engine) as session:
        other = EodRun(decision_date=next_day, attempt_no=1, status="success",
                       decision_outcome="emitted", target_count=3, evaluated_count=3,
                       failed_count=0, signal_count=2, allowed_signal_count=1,
                       blocked_signal_count=1, reason_codes=[])
        other.started_at = dt.datetime.combine(next_day, dt.time(15, 30), tzinfo=CST)
        other.finished_at = other.started_at + dt.timedelta(minutes=3)
        session.add(other)
        session.commit()
        other_id = other.run_id
    with Session(engine) as session:
        chain.register_run(session, run=session.get(EodRun, run_id),
                           decisions=_decisions(), run_status="success")
        chain.register_run(session, run=session.get(EodRun, other_id),
                           decisions=_decisions(), run_status="success")
        session.commit()
    with Session(engine) as session:
        versions = dict(session.execute(
            select(ChainDecision.trade_date, ChainDecision.universe_version)
            .where(ChainDecision.code == "510300")).all())
        uids = set(session.scalars(select(ChainDecision.decision_uid)
                                   .where(ChainDecision.code == "510300")))
    assert versions[DAY] != versions[next_day]
    assert len(uids) == 2
