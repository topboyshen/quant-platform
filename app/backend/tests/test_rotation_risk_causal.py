"""Causal risk-gate boundaries. These assertions describe the required contract.

The 4-day freshness window stays 4 calendar days. It is not a trading-day count
and is not extended across holidays.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.signals import RiskState, RotationShadowDay

CST = ZoneInfo("Asia/Shanghai")
_SPEC = importlib.util.spec_from_file_location(
    "contract_helpers",
    Path(__file__).with_name("test_correctness_contract_20261125.py"),
)
assert _SPEC and _SPEC.loader
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)

START = _HELPERS.ROTATION_START
BASE = _HELPERS.ROTATION_BASE
CUTOFF = dt.datetime.combine(BASE, dt.time(15, 0), CST)


def _prepare(engine, monkeypatch):
    monkeypatch.setattr(_HELPERS.rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
    with Session(engine) as session:
        _HELPERS._cleanup_rotation(session)
        _HELPERS._seed_rotation(session)
        session.commit()


def _finish(engine):
    with Session(engine) as session:
        _HELPERS._cleanup_rotation(session)
        session.commit()


def _add_risk(
    session: Session,
    *,
    level: str,
    observed_at: dt.datetime,
    created_at: dt.datetime,
) -> None:
    session.add(
        RiskState(
            observed_at=observed_at,
            created_at=created_at,
            level=level,
            values={},
            sources={},
            override=False,
            message="contract-rotation",
            raw={"level": level},
        )
    )


def _run_first_day(engine) -> dict:
    with Session(engine) as session:
        init = _HELPERS._init_rotation(session)
        assert init.get("ok") is True, init
        replay = _HELPERS.rot.replay(session, until=START, phase="forward", rule_version="qp-rotation.contract")
        session.commit()
        day = session.scalar(
            select(RotationShadowDay).where(
                RotationShadowDay.rule_version == "qp-rotation.contract",
                RotationShadowDay.trade_date == START,
            )
        )
        positions = _HELPERS._positions(session, START)
        gate = (day.detail or {}).get("risk_gate") if day is not None else None
        return {
            "replay_ok": replay.get("ok"),
            "positions": positions,
            "nav": None if day is None else float(day.nav),
            "gate": gate,
        }


def _gate_view(result: dict) -> dict:
    gate = result["gate"] or {}
    return {
        "opened_new_exposure": bool(result["positions"]),
        "valued": result["nav"] is not None,
        "reason": gate.get("reason"),
        "history_diagnostic": gate.get("history_diagnostic"),
        "level": gate.get("level"),
        "forced_liquidation": gate.get("forced_liquidation"),
        "rule_version": gate.get("rule_version"),
    }


def test_missing_observation_blocks_new_risk_without_forging_unavailable(engine, monkeypatch):
    _prepare(engine, monkeypatch)
    try:
        actual = _gate_view(_run_first_day(engine))
    finally:
        _finish(engine)
    assert actual == {
        "opened_new_exposure": False,
        "valued": True,
        "reason": "missing",
        "history_diagnostic": "absent",
        "level": None,
        "forced_liquidation": False,
        "rule_version": "qp-rotation-risk.v1",
    }


def test_stale_observation_blocks_new_risk_and_keeps_the_real_level(engine, monkeypatch):
    _prepare(engine, monkeypatch)
    stale_at = CUTOFF - dt.timedelta(days=5)
    try:
        with Session(engine) as session:
            _add_risk(session, level="ok", observed_at=stale_at, created_at=stale_at)
            session.commit()
        actual = _gate_view(_run_first_day(engine))
    finally:
        _finish(engine)
    assert actual["opened_new_exposure"] is False
    assert actual["valued"] is True
    assert actual["reason"] == "stale"
    assert actual["history_diagnostic"] == "stale"
    assert actual["level"] == "ok"
    assert actual["rule_version"] == "qp-rotation-risk.v1"


def test_fresh_ok_observation_before_cutoff_allows_new_risk(engine, monkeypatch):
    _prepare(engine, monkeypatch)
    seen = CUTOFF - dt.timedelta(hours=2)
    try:
        with Session(engine) as session:
            _add_risk(session, level="ok", observed_at=seen, created_at=seen)
            session.commit()
        actual = _gate_view(_run_first_day(engine))
    finally:
        _finish(engine)
    assert actual["opened_new_exposure"] is True
    assert actual["reason"] == "fresh"
    assert actual["level"] == "ok"
    assert actual["rule_version"] == "qp-rotation-risk.v1"


def test_later_inserted_row_is_not_visible_to_the_plan(engine, monkeypatch):
    _prepare(engine, monkeypatch)
    observed = CUTOFF - dt.timedelta(hours=1)
    created = CUTOFF + dt.timedelta(days=1)
    try:
        with Session(engine) as session:
            _add_risk(session, level="ok", observed_at=observed, created_at=created)
            session.commit()
        actual = _gate_view(_run_first_day(engine))
    finally:
        _finish(engine)
    assert actual["opened_new_exposure"] is False
    assert actual["reason"] == "missing"
    assert actual["history_diagnostic"] == "absent"
    assert actual["level"] is None


def test_observation_after_decision_cutoff_is_not_used(engine, monkeypatch):
    _prepare(engine, monkeypatch)
    after = dt.datetime.combine(START, dt.time(15, 0), CST)
    try:
        with Session(engine) as session:
            _add_risk(session, level="ok", observed_at=after, created_at=after)
            session.commit()
        actual = _gate_view(_run_first_day(engine))
    finally:
        _finish(engine)
    assert actual["opened_new_exposure"] is False
    assert actual["reason"] == "missing"
    assert actual["history_diagnostic"] == "absent"
    assert actual["level"] is None


def _init_at(session, stamp: dt.datetime):
    from unittest.mock import patch

    with patch.object(_HELPERS.rot, "now_cst", return_value=stamp):
        return _HELPERS.rot.initialize(
            session,
            start_date=START,
            phase="forward",
            rule_version="qp-rotation.contract",
            codes=_HELPERS.ROTATION_CODES,
        )


def test_same_day_critical_after_yesterdays_ok_blocks_new_risk(engine, monkeypatch):
    """EOD risk is 15:30; the plan freezes at 15:50. Yesterday's ok must not win."""
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    yesterday = dt.datetime.combine(BASE - dt.timedelta(days=1), dt.time(15, 30), CST)
    today = dt.datetime.combine(BASE, dt.time(15, 30), CST)
    try:
        with Session(engine) as session:
            _HELPERS._cleanup_rotation(session)
            _HELPERS._seed_rotation(session)
            _add_risk(session, level="ok", observed_at=yesterday, created_at=yesterday)
            critical = RiskState(
                observed_at=today,
                created_at=today,
                level="critical",
                values={},
                sources={},
                override=False,
                message="contract-rotation",
                raw={"level": "critical"},
            )
            session.add(critical)
            session.commit()
            session.refresh(critical)
            critical_id = critical.id
            init = _init_at(session, decided)
            assert init.get("ok") is True, init
            monkeypatch.setattr(_HELPERS.rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
            replay = _HELPERS.rot.replay(session, until=START, phase="forward", rule_version="qp-rotation.contract")
            assert replay.get("ok") is True, replay
            session.commit()
            positions = _HELPERS._positions(session, START)
            day = session.scalar(
                select(RotationShadowDay).where(
                    RotationShadowDay.rule_version == "qp-rotation.contract",
                    RotationShadowDay.trade_date == START,
                )
            )
        gate = (day.detail or {}).get("risk_gate") or {}
        assert positions == {}
        assert gate.get("reason") == "fresh"
        assert gate.get("level") == "critical"
        assert gate.get("observation_id") == critical_id
        assert str(gate.get("decided_at", "")).startswith(f"{BASE.isoformat()}T15:50")
    finally:
        _finish(engine)


def test_observation_arriving_after_decision_is_not_bound(engine, monkeypatch):
    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    monkeypatch.setattr(_HELPERS.rot, "now_cst", lambda: decided)
    late = decided + dt.timedelta(minutes=20)
    try:
        with Session(engine) as session:
            _HELPERS._cleanup_rotation(session)
            _HELPERS._seed_rotation(session)
            session.commit()
            init = _init_at(session, decided)
            assert init.get("ok") is True, init
            session.commit()
            _add_risk(session, level="ok", observed_at=late, created_at=late)
            session.commit()
            plan = session.get(_HELPERS.rot.RotationShadowRun, init["plan_id"])
            assert str(plan.detail.get("decided_at", "")).startswith(f"{BASE.isoformat()}T15:50")
            assert plan.detail.get("risk_observation_id") is None
            assert plan.detail["risk_binding"]["status"] == "missing"
            assert plan.detail["risk_binding"]["history_diagnostic"] == "absent"
    finally:
        _finish(engine)


def test_missing_same_day_assessment_does_not_reuse_yesterdays_ok(engine, monkeypatch):
    _prepare(engine, monkeypatch)
    yesterday = dt.datetime.combine(BASE - dt.timedelta(days=1), dt.time(15, 30), CST)
    try:
        with Session(engine) as session:
            _HELPERS._cleanup_rotation(session)
            _HELPERS._seed_rotation(session)
            _add_risk(session, level="ok", observed_at=yesterday, created_at=yesterday)
            session.commit()
        actual = _gate_view(_run_first_day(engine))
    finally:
        _finish(engine)
    assert actual["opened_new_exposure"] is False
    assert actual["reason"] == "assessment_pending"
    assert actual["level"] is None
    assert actual["level"] != "ok"


def test_four_calendar_day_boundary_is_pending_not_stale():
    from types import SimpleNamespace

    decided = dt.datetime.combine(BASE, dt.time(15, 50), CST)
    four_days = SimpleNamespace(
        id=1,
        level="ok",
        observed_at=decided - dt.timedelta(days=4),
        created_at=decided - dt.timedelta(days=4),
    )
    five_days = SimpleNamespace(
        id=2,
        level="ok",
        observed_at=decided - dt.timedelta(days=5),
        created_at=decided - dt.timedelta(days=5),
    )
    pending = _HELPERS.rot.bind_same_day_risk([four_days], BASE, decided)
    stale = _HELPERS.rot.bind_same_day_risk([five_days], BASE, decided)
    assert (pending["status"], pending["history_diagnostic"]) == ("assessment_pending", "not_qualified")
    assert (stale["status"], stale["history_diagnostic"], stale["level"]) == ("stale", "stale", "ok")
