"""Transaction and schedule boundaries for rotation risk binding.

Uses the real initialize / step / replay / CLI entries on the isolated test database.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from qp.market.kline import DailyBar, KlineResult
from qp.models.signals import RiskState, RotationShadowDay, RotationShadowRun
from qp.signals.rotation_shadow import bind_same_day_risk

CST = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
_SPEC = importlib.util.spec_from_file_location(
    "contract_helpers_sessions",
    Path(__file__).with_name("test_correctness_contract_20261125.py"),
)
assert _SPEC and _SPEC.loader
H = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(H)

START = H.ROTATION_START
BASE = H.ROTATION_BASE


def _risk(session: Session, *, level: str, observed_at: dt.datetime, created_at: dt.datetime) -> RiskState:
    row = RiskState(
        observed_at=observed_at,
        created_at=created_at,
        level=level,
        values={},
        sources={},
        override=False,
        message="contract-rotation",
        raw={"level": level},
    )
    session.add(row)
    session.flush()
    return row


def _cleanup_all(session: Session) -> None:
    H._cleanup_rotation(session)
    for model in (RotationShadowRun, RotationShadowDay, H.rot.RotationShadowAccount):
        session.execute(delete(model).where(model.rule_version == H.rot.RULE))


def _init_at(session: Session, stamp: dt.datetime) -> dict:
    with patch.object(H.rot, "now_cst", return_value=stamp):
        return H.rot.initialize(
            session,
            start_date=START,
            phase="forward",
            rule_version="qp-rotation.contract",
            codes=H.ROTATION_CODES,
        )


def _fetcher_for_stored(session: Session, as_of: dt.date):
    grouped: dict[str, list[DailyBar]] = {}
    rows = session.execute(
        select(
            H.rot.RotationShadowBar.code,
            H.rot.RotationShadowBar.trade_date,
            H.rot.RotationShadowBar.open,
            H.rot.RotationShadowBar.high,
            H.rot.RotationShadowBar.low,
            H.rot.RotationShadowBar.close,
            H.rot.RotationShadowBar.volume,
        ).where(H.rot.RotationShadowBar.trade_date <= as_of)
    )
    for code, day, o, h, lo, cl, volume in rows:
        grouped.setdefault(code, []).append(DailyBar(day, float(o), float(h), float(lo), float(cl), float(volume)))

    class Fetcher:
        def get(self, code: str) -> KlineResult:
            return KlineResult(bars=list(grouped.get(code, [])), source="stored")

    return Fetcher()


def test_uncommitted_risk_is_invisible_and_later_commit_does_not_rebind(engine, monkeypatch):
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    early = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    with Session(engine) as setup:
        _cleanup_all(setup)
        H._seed_rotation(setup)
        setup.commit()
    holder = Session(engine)
    try:
        _risk(holder, level="critical", observed_at=early, created_at=early)
        with Session(engine) as planner:
            init = _init_at(planner, decided)
            assert init.get("ok") is True, init
            planner.commit()
            plan_id = init["plan_id"]
            frozen = planner.get(RotationShadowRun, plan_id)
            assert frozen.detail["risk_binding"]["status"] == "missing"
            assert frozen.detail["risk_binding"]["history_diagnostic"] == "absent"
            assert frozen.detail.get("risk_observation_id") is None
        holder.commit()
        monkeypatch.setattr(H.rot, "now_cst", lambda: dt.datetime.combine(BASE, dt.time(16, 40), CST))
        with Session(engine) as retry:
            fetcher = _fetcher_for_stored(retry, BASE)
            step = H.rot.step(retry, as_of=BASE, fetcher=fetcher, rule_version="qp-rotation.contract")
            retry.commit()
            assert step.get("error") != "calendar_day_unknown"
            plan = retry.get(RotationShadowRun, plan_id)
            assert plan.detail.get("risk_observation_id") is None
            assert plan.detail["risk_binding"]["status"] == "missing"
            assert plan.detail["risk_binding"]["history_diagnostic"] == "absent"
        monkeypatch.setattr(H.rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
        with Session(engine) as executor:
            replay = H.rot.replay(executor, until=START, phase="forward", rule_version="qp-rotation.contract")
            executor.commit()
            assert replay.get("ok") is True, replay
            assert H._positions(executor, START) == {}
            plan = executor.get(RotationShadowRun, plan_id)
            assert plan.detail.get("risk_observation_id") is None
    finally:
        holder.close()
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_committed_same_day_critical_beats_earlier_ok(engine):
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    ok_at = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    critical_at = dt.datetime.combine(BASE, dt.time(15, 40), CST)
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            _risk(session, level="ok", observed_at=ok_at, created_at=ok_at)
            critical = _risk(session, level="critical", observed_at=critical_at, created_at=critical_at)
            session.commit()
            critical_id = critical.id
            init = _init_at(session, decided)
            assert init.get("ok") is True, init
            session.commit()
            plan = session.get(RotationShadowRun, init["plan_id"])
            assert plan.detail["risk_observation_id"] == critical_id
            assert plan.detail["risk_binding"]["level"] == "critical"
            assert plan.detail["risk_binding"]["status"] == "fresh"
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_equal_timestamp_uses_later_created_at_then_id(engine):
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    same = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    later = same + dt.timedelta(seconds=1)
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            _risk(session, level="ok", observed_at=same, created_at=same)
            critical = _risk(session, level="critical", observed_at=same, created_at=later)
            session.commit()
            init = _init_at(session, decided)
            session.commit()
            plan = session.get(RotationShadowRun, init["plan_id"])
            assert plan.detail["risk_observation_id"] == critical.id
            assert plan.detail["risk_binding"]["level"] == "critical"
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_1640_retry_does_not_change_1550_binding_and_first_1640_plan_is_separate(engine, monkeypatch):
    frozen_at = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    seen = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            bound = _risk(session, level="critical", observed_at=seen, created_at=seen)
            session.commit()
            bound_id = bound.id
            init = _init_at(session, frozen_at)
            session.commit()
            plan_id = init["plan_id"]
        late = frozen_at + dt.timedelta(minutes=5)
        with Session(engine) as session:
            _risk(session, level="ok", observed_at=late, created_at=late)
            session.commit()
        monkeypatch.setattr(H.rot, "now_cst", lambda: dt.datetime.combine(BASE, dt.time(16, 40), CST))
        with Session(engine) as session:
            fetcher = _fetcher_for_stored(session, BASE)
            H.rot.step(session, as_of=BASE, fetcher=fetcher, rule_version="qp-rotation.contract")
            session.commit()
            plan = session.get(RotationShadowRun, plan_id)
            assert plan.detail["risk_observation_id"] == bound_id
            assert plan.detail["decided_at"].startswith(f"{BASE.isoformat()}T15:50")
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            only = _risk(session, level="warning", observed_at=seen, created_at=seen)
            session.commit()
            only_id = only.id
            first = _init_at(session, dt.datetime.combine(BASE, dt.time(16, 40), CST))
            session.commit()
            plan = session.get(RotationShadowRun, first["plan_id"])
            assert plan.detail["decided_at"].startswith(f"{BASE.isoformat()}T16:40")
            assert plan.detail["risk_observation_id"] == only_id
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_cst_date_boundary_does_not_treat_utc_evening_as_same_day(engine):
    signal = dt.date(2028, 3, 3)
    decided = dt.datetime(2028, 3, 4, 0, 20, tzinfo=CST)
    previous_evening_utc = dt.datetime(2028, 3, 3, 16, 30, tzinfo=UTC)  # 2028-03-04 00:30 CST
    same_day_cst = dt.datetime(2028, 3, 3, 15, 30, tzinfo=CST)
    rows = [
        SimpleNamespace(id=1, level="ok", observed_at=previous_evening_utc, created_at=previous_evening_utc),
        SimpleNamespace(id=2, level="critical", observed_at=same_day_cst, created_at=same_day_cst),
    ]
    # decided_at is the next Beijing day, so neither row is a same-day assessment for 2028-03-04.
    later_decided = decided
    bound = bind_same_day_risk(rows, dt.date(2028, 3, 4), later_decided)
    assert bound["observation_id"] is None
    # The 00:30 CST row is same-day for 2028-03-04 and before 00:40; the 15:30 CST row is not.
    morning = bind_same_day_risk(rows, dt.date(2028, 3, 4), dt.datetime(2028, 3, 4, 0, 40, tzinfo=CST))
    assert morning["observation_id"] == 1
    assert morning["level"] == "ok"
    same = bind_same_day_risk(rows, signal, dt.datetime(2028, 3, 3, 15, 50, tzinfo=CST))
    assert same["observation_id"] == 2
    assert same["level"] == "critical"
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            utc_same = dt.datetime(2028, 3, 3, 7, 30, tzinfo=UTC)  # 15:30 CST
            utc_next = dt.datetime(2028, 3, 3, 16, 30, tzinfo=UTC)  # 00:30 next CST day
            kept = _risk(session, level="critical", observed_at=utc_same, created_at=utc_same)
            _risk(session, level="ok", observed_at=utc_next, created_at=utc_next)
            session.commit()
            kept_id = kept.id
            init = _init_at(session, dt.datetime(2028, 3, 3, 15, 50, tzinfo=CST))
            session.commit()
            plan = session.get(RotationShadowRun, init["plan_id"])
            assert plan.detail["risk_observation_id"] == kept_id
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_replay_step_and_manual_cli_do_not_rebind(engine, monkeypatch):
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    seen = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            row = _risk(session, level="critical", observed_at=seen, created_at=seen)
            session.commit()
            bound_id = row.id
            init = _init_at(session, decided)
            session.commit()
            plan_id = init["plan_id"]
        monkeypatch.setattr(H.rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
        with Session(engine) as session:
            direct = H.rot.replay(session, until=START, phase="forward", rule_version="qp-rotation.contract")
            session.commit()
            assert direct.get("ok") is True, direct
            assert H._positions(session, START) == {}
            assert session.get(RotationShadowRun, plan_id).detail["risk_observation_id"] == bound_id
        from qp.cli import cmd_signals_rotation_shadow

        late = decided + dt.timedelta(hours=2)
        with Session(engine) as session:
            _risk(session, level="ok", observed_at=late, created_at=late)
            # CLI has no rule override and always replays qp-rotation.v1.
            session.execute(
                RotationShadowRun.__table__.update()
                .where(RotationShadowRun.rule_version == "qp-rotation.contract")
                .values(rule_version=H.rot.RULE)
            )
            session.execute(
                H.rot.RotationShadowDay.__table__.update()
                .where(H.rot.RotationShadowDay.rule_version == "qp-rotation.contract")
                .values(rule_version=H.rot.RULE)
            )
            session.execute(
                H.rot.RotationShadowAccount.__table__.update()
                .where(H.rot.RotationShadowAccount.rule_version == "qp-rotation.contract")
                .values(rule_version=H.rot.RULE)
            )
            session.commit()
        assert cmd_signals_rotation_shadow(
            SimpleNamespace(
                seed_bars=None,
                initialize=None,
                phase="forward",
                until=START.isoformat(),
                step=False,
                date=None,
                summary=False,
            )
        ) == 0
        with Session(engine) as session:
            plan = session.get(RotationShadowRun, plan_id)
            assert plan.detail["risk_observation_id"] == bound_id
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_historical_plan_without_decided_at_is_not_rewritten_or_treated_as_evidence(engine, monkeypatch):
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            seen = dt.datetime.combine(BASE, dt.time(14, 0), CST)
            _risk(session, level="ok", observed_at=seen, created_at=seen)
            session.commit()
            init = H._init_rotation(session)
            assert init.get("ok") is True, init
            session.commit()
            plan = session.get(RotationShadowRun, init["plan_id"])
            detail = dict(plan.detail)
            detail.pop("decided_at", None)
            detail.pop("risk_binding", None)
            detail.pop("risk_observation_id", None)
            plan.detail = detail
            baseline = session.scalar(
                select(RotationShadowDay).where(
                    RotationShadowDay.rule_version == "qp-rotation.contract",
                    RotationShadowDay.trade_date == BASE,
                )
            )
            baseline_nav = float(baseline.nav)
            baseline_detail = dict(baseline.detail)
            session.commit()
        monkeypatch.setattr(H.rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
        with Session(engine) as session:
            replay = H.rot.replay(session, until=START, phase="forward", rule_version="qp-rotation.contract")
            session.commit()
            assert replay.get("ok") is True, replay
            plan = session.get(RotationShadowRun, init["plan_id"])
            assert "decided_at" not in plan.detail
            assert plan.detail.get("risk_observation_id") is None
            baseline = session.scalar(
                select(RotationShadowDay).where(
                    RotationShadowDay.rule_version == "qp-rotation.contract",
                    RotationShadowDay.trade_date == BASE,
                )
            )
            assert float(baseline.nav) == baseline_nav
            assert baseline.detail == baseline_detail
            day = session.scalar(
                select(RotationShadowDay).where(
                    RotationShadowDay.rule_version == "qp-rotation.contract",
                    RotationShadowDay.trade_date == START,
                )
            )
            assert day.detail["risk_gate"]["reason"] == "unverified"
            assert H._positions(session, START) == {}
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_pending_risk_does_not_add_or_rotate_in_but_allows_strategy_reduction(engine, monkeypatch):
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            seen = dt.datetime.combine(BASE, dt.time(14, 0), CST)
            _risk(session, level="ok", observed_at=seen, created_at=seen)
            session.commit()
            init = H._init_rotation(session)
            session.commit()
        monkeypatch.setattr(H.rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
        with Session(engine) as session:
            H.rot.replay(session, until=START, phase="forward", rule_version="qp-rotation.contract")
            session.commit()
            before = H._positions(session, START)
            assert before
            keep = next(iter(before))
            drop = next(code for code in before if code != keep)
            new_code = next(code for code in H.ROTATION_CODES if code not in before)
            nxt = H.ROTATION_DAYS[1]
            original = session.get(RotationShadowRun, init["plan_id"])
            pending_at = dt.datetime.combine(START, dt.time(15, 50), CST)
            session.add(
                RotationShadowRun(
                    rule_version="qp-rotation.contract",
                    phase="forward",
                    signal_date=START,
                    execute_date=nxt,
                    status="pending",
                    universe=2,
                    top_k=8,
                    selected=[
                        {"code": keep, "name": keep, "score": 1, "weight": 0.7},
                        {"code": new_code, "name": new_code, "score": 1, "weight": 0.3},
                    ],
                    detail={
                        "parameters_sha256": original.detail["parameters_sha256"],
                        "ew_weights": original.detail["ew_weights"],
                        "decided_at": pending_at.isoformat(),
                        "risk_rule_version": "qp-rotation-risk.v1",
                        "risk_observation_id": None,
                        "risk_binding": {
                            "status": "assessment_pending",
                            "level": None,
                            "observation_id": None,
                            "used_previous_ok": False,
                        },
                    },
                )
            )
            session.commit()
            replay = H.rot.replay(session, until=nxt, phase="forward", rule_version="qp-rotation.contract")
            session.commit()
            assert replay.get("ok") is True, replay
            after = H._positions(session, nxt)
            assert new_code not in after
            assert after.get(keep, 0) <= before[keep]
            assert after.get(keep, 0) > 0
            assert after.get(drop, 0) < before[drop]
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_empty_risk_table_is_missing_with_absent_history(engine):
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    try:
        with Session(engine) as session:
            _cleanup_all(session)
            H._seed_rotation(session)
            session.commit()
            default_isolation = session.execute(text("SHOW transaction_isolation")).scalar_one()
            init = _init_at(session, decided)
            session.commit()
            plan = session.get(RotationShadowRun, init["plan_id"])
            binding = plan.detail["risk_binding"]
            print(f"default_transaction_isolation={default_isolation}")
            print(f"snapshot_isolation={binding.get('snapshot_isolation')}")
            assert default_isolation == "read committed"
            assert binding["status"] == "missing"
            assert binding["history_diagnostic"] == "absent"
            assert binding["snapshot_isolation"] == "repeatable read"
            assert plan.detail.get("risk_observation_id") is None
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_commit_after_snapshot_and_before_select_returns_is_not_bound(engine):
    """EOD commits while the risk read waits, with timestamps earlier than decided_at."""
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    early = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    holder = Session(engine)
    outcome: dict = {}

    def freeze() -> None:
        outcome["started"] = True
        try:
            with Session(engine) as planner:
                outcome["connected"] = True
                outcome["init"] = _init_at(planner, decided)
                planner.commit()
                outcome["committed"] = True
        except Exception as exc:
            outcome["error"] = repr(exc)

    try:
        with Session(engine) as setup:
            _cleanup_all(setup)
            H._seed_rotation(setup)
            setup.commit()
        holder.execute(text("LOCK TABLE signals.risk_state IN ACCESS EXCLUSIVE MODE"))
        late = _risk(holder, level="critical", observed_at=early, created_at=early)
        late_id = late.id
        worker = Thread(target=freeze)
        worker.start()
        deadline = time.time() + 15
        waiting = 0
        while time.time() < deadline:
            waiting = holder.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_locks l
                    JOIN pg_class c ON c.oid = l.relation
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'signals' AND c.relname = 'risk_state' AND NOT l.granted
                    """
                )
            ).scalar_one()
            if waiting:
                break
            time.sleep(0.05)
        assert waiting, outcome
        holder.commit()
        worker.join(timeout=15)
        assert not worker.is_alive()
        assert "error" not in outcome, outcome.get("error")
        with Session(engine) as session:
            plan = session.get(RotationShadowRun, outcome["init"]["plan_id"])
            assert plan.detail.get("risk_observation_id") is None
            assert plan.detail["risk_binding"]["status"] == "missing"
            assert plan.detail["risk_binding"]["history_diagnostic"] == "absent"
            assert plan.detail["risk_binding"]["snapshot_isolation"] == "repeatable read"
            assert session.get(RiskState, late_id) is not None
    finally:
        holder.close()
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()


def test_morning_initialize_binds_prior_signal_day_risk(engine):
    """The next-open plan may be frozen the following morning.

    The signal date is the prior session; the risk observation belongs to that
    signal date, not to the morning on which initialize() runs.
    """
    with Session(engine) as setup:
        _cleanup_all(setup)
        H._seed_rotation(setup)
        H._risk(setup, H.ROTATION_BASE, "ok")
        setup.commit()
    try:
        with Session(engine) as planner:
            stamp = dt.datetime.combine(H.ROTATION_START, dt.time(8), CST)
            with patch.object(H.rot, "now_cst", return_value=stamp):
                out = H.rot.initialize(
                    planner,
                    start_date=H.ROTATION_START,
                    phase="forward",
                    rule_version="qp-rotation.contract",
                    codes=H.ROTATION_CODES,
                )
            assert out.get("ok") is True, out
            planner.commit()
            plan = planner.get(RotationShadowRun, out["plan_id"])
            binding = plan.detail["risk_binding"]
            assert binding["status"] == "fresh", binding
            assert binding["level"] == "ok"
            assert binding["observation_id"] is not None
            assert plan.detail.get("decided_at", "").startswith(f"{H.ROTATION_START.isoformat()}T08:00")
        with Session(engine) as executor:
            with patch.object(
                H.rot, "now_cst", return_value=dt.datetime.combine(H.ROTATION_START, dt.time(16), CST)
            ):
                replay = H.rot.replay(
                    executor, until=H.ROTATION_START, phase="forward", rule_version="qp-rotation.contract"
                )
            assert replay.get("ok") is True, replay
            executor.commit()
            assert H._positions(executor, H.ROTATION_START)
    finally:
        with Session(engine) as cleanup:
            _cleanup_all(cleanup)
            cleanup.commit()
