"""Contract regression for the 2026-11-25 correctness review.

These tests record the approved contract against current behavior. A failure is
the evidence; do not edit production logic in this change to make them pass.

Rule-shadow contract (module docstring and CHAIN-SHADOW-RUNBOOK §6): v2 may
record an ungated counterfactual return and must copy the live risk level so a
gated variant can be compared later. That return is research, not an executable
strategy fill, and it must stay out of target_decision, the record chain and
notifications.

Risk contract: blocking new exposure and forced liquidation are separate.
Critical risk must not open new shares. It must not, by itself, flatten an
existing book. A floating loss stays on the book.

Unsellable exit: a sealed limit-down does not realize a sale. The sample, the
position and the floating loss remain, and a later exit is still possible.

Queue: a one-tick daily range cannot prove a limit order was unfilled. Mark it
uncertain. A one-price sealed limit-up remains unfilled.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.market.kline import DailyBar, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import (
    DecisionOutcome,
    EodRun,
    RiskState,
    RotationShadowAccount,
    RotationShadowBar,
    RotationShadowDay,
    RotationShadowRun,
    RuleShadowOutcome,
    RuleShadowSignal,
    TargetDecision,
)
from qp.signals import rotation_shadow as rot
from qp.signals.outcomes import evaluate_decision, evaluate_pending
from qp.signals.risk import is_growth_risk_target
from qp.signals.universe import TARGETS

CST = ZoneInfo("Asia/Shanghai")
RULE_DAY = dt.date(2027, 11, 2)
OUTCOME_DAY = dt.date(2027, 9, 7)
ROTATION_START = dt.date(2028, 3, 6)
GROWTH_CODES = {t.code for t in TARGETS if is_growth_risk_target(t.sector, t.name)}


def _fail(case: str, expected: dict, actual: dict) -> None:
    lines = [f"{case}: contract expected != current actual"]
    keys = list(dict.fromkeys([*expected, *actual]))
    for key in keys:
        exp = expected.get(key, "<missing>")
        got = actual.get(key, "<missing>")
        mark = "OK" if exp == got else "DIFF"
        lines.append(f"  {mark} {key}: expected={exp!r} actual={got!r}")
    pytest.fail("\n".join(lines))


def _same(case: str, expected: dict, actual: dict) -> None:
    if expected != actual:
        _fail(case, expected, actual)


def _business_days(start: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def _bar(day: dt.date, open_: float, close: float, high: float | None = None, low: float | None = None) -> DailyBar:
    return DailyBar(
        day,
        open=open_,
        high=high if high is not None else max(open_, close),
        low=low if low is not None else min(open_, close),
        close=close,
        volume=1_000_000.0,
    )


def _flat(days: list[dt.date], close: float, volume: float = 1000.0) -> list[DailyBar]:
    return [DailyBar(day, open=close, high=close, low=close, close=close, volume=volume) for day in days]


class _Fetcher:
    def __init__(self, bars: dict[str, list[DailyBar]]) -> None:
        self.bars = bars

    def get(self, code: str) -> KlineResult:
        return KlineResult(bars=list(self.bars.get(code, [])), source="contract-test")


def _seed_run(session: Session, day: dt.date, code: str, state: str, risk_level: str, reference: float | None) -> int:
    run = EodRun(
        decision_date=day,
        attempt_no=1,
        status="success",
        decision_outcome="blocked" if state == "blocked" else "no_signal",
        target_count=1,
        evaluated_count=1,
        failed_count=0 if state != "data_failed" else 1,
        signal_count=0,
        allowed_signal_count=0,
        blocked_signal_count=1 if state == "blocked" else 0,
        reason_codes=[],
    )
    session.add(run)
    session.flush()
    row = TargetDecision(
        run_id=run.run_id,
        decision_date=day,
        code=code,
        name="创业板ETF",
        sector="成长",
        data_status="failed" if state == "data_failed" else "ok",
        decision_state=state,
        risk_action="blocked" if state == "blocked" else "not_applicable",
        risk_level=risk_level,
        reason_code="macro_risk_blocked" if state == "blocked" else "trend_data_unavailable",
        reference_close=reference,
        raw_trigger_count=0,
        raw_signals=[],
        validation_split="live_oos",
    )
    session.add(row)
    session.flush()
    return int(row.id)


def _cleanup_rule(session: Session) -> None:
    signal_ids = select(RuleShadowSignal.id).where(RuleShadowSignal.decision_date == RULE_DAY)
    session.execute(delete(RuleShadowOutcome).where(RuleShadowOutcome.signal_id.in_(signal_ids)))
    session.execute(delete(RuleShadowSignal).where(RuleShadowSignal.decision_date == RULE_DAY))
    session.execute(delete(EodRun).where(EodRun.decision_date == RULE_DAY))
    session.execute(delete(TradingDay).where(TradingDay.source == "contract-rule", TradingDay.date == RULE_DAY))


def _executable_view(fill_status: str | None, net_return: float | None, details: dict | None) -> dict:
    """How a consumer must read a row. An unlabeled filled return is executable."""
    details = details or {}
    role = details.get("return_role")
    marked = details.get("executable")
    research = role in {"counterfactual_ungated", "research_counterfactual"} or marked is False
    return {
        "return_role": role,
        "executable_net_return": None if research else (float(net_return) if fill_status == "filled" else None),
        "counterfactual_net_return": float(net_return) if research and net_return is not None else None,
        "sample_retained": fill_status is not None,
    }


def test_rule_shadow_keeps_ungated_research_return_out_of_live_tables(engine):
    """Design contract already met: isolation and a copied risk level."""
    from qp.signals import rule_shadow

    before = _business_days(RULE_DAY - dt.timedelta(days=130), 80)
    bars = _flat(before, 10.0) + [DailyBar(RULE_DAY, open=10.0, high=11.0, low=10.0, close=11.0, volume=3000.0)]
    with Session(engine) as session:
        _cleanup_rule(session)
        session.merge(TradingDay(origin="qp", date=RULE_DAY, is_open=True, source="contract-rule"))
        _seed_run(session, RULE_DAY, "159915", "blocked", "critical", 11.0)
        decisions_before = session.scalar(select(func.count()).select_from(TargetDecision))
        outcomes_before = session.scalar(select(func.count()).select_from(DecisionOutcome))
        rule_shadow.generate(
            session,
            day=RULE_DAY,
            fetcher=_Fetcher({"159915": bars}),
            targets=[next(t for t in TARGETS if t.code == "159915")],
        )
        session.commit()
        signal = session.scalar(select(RuleShadowSignal).where(RuleShadowSignal.decision_date == RULE_DAY))
        actual = {
            "signal_recorded": signal is not None,
            "risk_level_copied": None if signal is None else signal.risk_level,
            "live_decision_rows_added": session.scalar(select(func.count()).select_from(TargetDecision))
            - decisions_before,
            "live_outcome_rows_added": session.scalar(select(func.count()).select_from(DecisionOutcome))
            - outcomes_before,
        }
        _cleanup_rule(session)
        session.commit()
    _same(
        "rule shadow isolation",
        {
            "signal_recorded": True,
            "risk_level_copied": "critical",
            "live_decision_rows_added": 0,
            "live_outcome_rows_added": 0,
        },
        actual,
    )


def test_rule_shadow_counterfactual_is_not_presented_as_executable(engine):
    """Ungated return may exist. It must not be the executable strategy return."""
    from qp.signals import rule_shadow

    before = _business_days(RULE_DAY - dt.timedelta(days=130), 80)
    after = _business_days(RULE_DAY + dt.timedelta(days=1), 25)
    code_bars = _flat(before, 10.0) + [DailyBar(RULE_DAY, open=10.0, high=11.0, low=10.0, close=11.0, volume=3000.0)]
    code_bars += [
        DailyBar(day, open=11.05, high=11.3, low=11.0, close=11.1 + index * 0.01, volume=1000.0)
        for index, day in enumerate(after)
    ]
    bench = _flat(before, 10.0) + [DailyBar(RULE_DAY, open=10.0, high=10.2, low=10.0, close=10.1, volume=1000.0)]
    bench += _flat(after, 10.2, volume=1000.0)
    fetcher = _Fetcher({"159915": code_bars, "510300": bench})
    with Session(engine) as session:
        _cleanup_rule(session)
        for day in [RULE_DAY, *after]:
            session.merge(TradingDay(origin="qp", date=day, is_open=True, source="contract-rule"))
        _seed_run(session, RULE_DAY, "159915", "blocked", "critical", 11.0)
        rule_shadow.generate(
            session,
            day=RULE_DAY,
            fetcher=fetcher,
            targets=[next(t for t in TARGETS if t.code == "159915")],
        )
        rule_shadow.collect(session, as_of=after[-1], fetcher=fetcher)
        session.commit()
        outcome = session.scalar(
            select(RuleShadowOutcome)
            .join(RuleShadowSignal, RuleShadowSignal.id == RuleShadowOutcome.signal_id)
            .where(RuleShadowSignal.decision_date == RULE_DAY, RuleShadowOutcome.horizon_sessions == 1)
        )
        signal = session.get(RuleShadowSignal, outcome.signal_id) if outcome is not None else None
        view = _executable_view(
            None if outcome is None else outcome.fill_status,
            None if outcome is None or outcome.net_return is None else float(outcome.net_return),
            None if outcome is None else outcome.details,
        )
        actual = {
            "risk_level": None if signal is None else signal.risk_level,
            "return_role": view["return_role"],
            "executable_net_return": view["executable_net_return"],
            "counterfactual_retained": view["counterfactual_net_return"] is not None,
            "sample_retained": view["sample_retained"],
        }
        _cleanup_rule(session)
        session.commit()
    expected = {
        "risk_level": "critical",
        "return_role": "counterfactual_ungated",
        "executable_net_return": None,
        "counterfactual_retained": True,
        "sample_retained": True,
    }
    _same("rule shadow executable boundary", expected, actual)


def _weekdays(start: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


ROTATION_CODES = tuple(rot.CODES[:10])
ROTATION_HISTORY = [d for d in _weekdays(ROTATION_START - dt.timedelta(days=470), 340) if d < ROTATION_START][-330:]
ROTATION_DAYS = _weekdays(ROTATION_START, 3)
ROTATION_BASE = ROTATION_HISTORY[-1]


def _cleanup_rotation(session: Session) -> None:
    session.execute(delete(RotationShadowRun).where(RotationShadowRun.rule_version == "qp-rotation.contract"))
    session.execute(delete(RotationShadowDay).where(RotationShadowDay.rule_version == "qp-rotation.contract"))
    session.execute(delete(RotationShadowAccount).where(RotationShadowAccount.rule_version == "qp-rotation.contract"))
    session.execute(
        delete(RotationShadowBar).where(
            RotationShadowBar.code.in_(ROTATION_CODES),
            RotationShadowBar.trade_date >= ROTATION_HISTORY[0],
            RotationShadowBar.trade_date <= ROTATION_DAYS[-1],
        )
    )
    session.execute(
        delete(TradingDay).where(
            TradingDay.source == "contract-rotation",
            TradingDay.date >= ROTATION_HISTORY[0],
            TradingDay.date <= ROTATION_DAYS[-1],
        )
    )
    session.execute(delete(RiskState).where(RiskState.message == "contract-rotation"))


def _seed_rotation(session: Session) -> None:
    payload = {
        code: [
            [
                str(day),
                2 + k * 0.1 + i * (0.001 + k * 0.0002),
                2.001 + k * 0.1 + i * (0.001 + k * 0.0002),
                2.01 + k * 0.1 + i * (0.001 + k * 0.0002),
                1.99 + k * 0.1 + i * (0.001 + k * 0.0002),
                100_000.0,
            ]
            for i, day in enumerate([*ROTATION_HISTORY, *ROTATION_DAYS])
        ]
        for k, code in enumerate(ROTATION_CODES)
    }
    cursor = ROTATION_HISTORY[0]
    while cursor <= ROTATION_DAYS[-1]:
        session.merge(TradingDay(origin="qp", date=cursor, is_open=cursor.weekday() < 5, source="contract-rotation"))
        cursor += dt.timedelta(days=1)
    rot.seed_bars(session, payload)


def _init_rotation(session: Session) -> dict:
    from unittest.mock import patch

    stamp = dt.datetime.combine(ROTATION_BASE, dt.time(16), CST)
    with patch.object(rot, "now_cst", return_value=stamp):
        return rot.initialize(
            session,
            start_date=ROTATION_START,
            phase="forward",
            rule_version="qp-rotation.contract",
            codes=ROTATION_CODES,
        )


def _risk(session: Session, day: dt.date, level: str) -> None:
    session.add(
        RiskState(
            observed_at=dt.datetime.combine(day, dt.time(15, 0), CST),
            level=level,
            values={},
            sources={},
            override=False,
            message="contract-rotation",
            raw={"level": level},
        )
    )


def _positions(session: Session, day: dt.date) -> dict[str, int]:
    row = session.scalar(
        select(RotationShadowDay).where(
            RotationShadowDay.rule_version == "qp-rotation.contract",
            RotationShadowDay.trade_date == day,
        )
    )
    if row is None:
        return {}
    return {code: int(qty) for code, qty in row.positions.items()}


def test_critical_risk_does_not_open_new_exposure(engine, monkeypatch):
    monkeypatch.setattr(rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
    with Session(engine) as session:
        _cleanup_rotation(session)
        _seed_rotation(session)
        _risk(session, ROTATION_START, "critical")
        session.commit()
        init = _init_rotation(session)
        assert init.get("ok") is True, init
        replay = rot.replay(session, until=ROTATION_START, phase="forward", rule_version="qp-rotation.contract")
        session.commit()
        positions = _positions(session, ROTATION_START)
        actual = {
            "replay_ok": replay.get("ok"),
            "new_share_codes": sorted(positions),
            "opened_new_exposure": bool(positions),
        }
        _cleanup_rotation(session)
        session.commit()
    _same(
        "critical risk blocks new exposure",
        {"replay_ok": True, "new_share_codes": [], "opened_new_exposure": False},
        actual,
    )


def test_growth_block_does_not_open_growth_names(engine, monkeypatch):
    monkeypatch.setattr(rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
    with Session(engine) as session:
        _cleanup_rotation(session)
        _seed_rotation(session)
        _risk(session, ROTATION_START, "growth_blocked")
        session.commit()
        init = _init_rotation(session)
        assert init.get("ok") is True, init
        rot.replay(session, until=ROTATION_START, phase="forward", rule_version="qp-rotation.contract")
        session.commit()
        positions = _positions(session, ROTATION_START)
        opened_growth = sorted(code for code in positions if code in GROWTH_CODES)
        actual = {"opened_growth_codes": opened_growth, "non_growth_may_remain": True}
        _cleanup_rotation(session)
        session.commit()
    _same(
        "growth_blocked does not add growth exposure",
        {"opened_growth_codes": [], "non_growth_may_remain": True},
        actual,
    )


def test_critical_risk_does_not_force_liquidate_existing_book(engine, monkeypatch):
    """Separate from the new-exposure rule. Passing does not prove the gate ran."""
    monkeypatch.setattr(rot, "now_cst", lambda: dt.datetime(2028, 12, 1, 16, tzinfo=CST))
    second = ROTATION_DAYS[1]
    with Session(engine) as session:
        _cleanup_rotation(session)
        _seed_rotation(session)
        session.commit()
        init = _init_rotation(session)
        assert init.get("ok") is True, init
        first = rot.replay(session, until=ROTATION_START, phase="forward", rule_version="qp-rotation.contract")
        session.commit()
        held = _positions(session, ROTATION_START)
        assert first.get("ok") is True and held, first
        for code in held:
            bar = session.get(RotationShadowBar, (code, second))
            assert bar is not None
            bar.close = round(float(bar.open) * 0.98, 4)
        _risk(session, second, "critical")
        session.commit()
        rot.replay(session, until=second, phase="forward", rule_version="qp-rotation.contract")
        session.commit()
        after = session.scalar(
            select(RotationShadowDay).where(
                RotationShadowDay.rule_version == "qp-rotation.contract",
                RotationShadowDay.trade_date == second,
            )
        )
        before = session.scalar(
            select(RotationShadowDay).where(
                RotationShadowDay.rule_version == "qp-rotation.contract",
                RotationShadowDay.trade_date == ROTATION_START,
            )
        )
        actual = {
            "sample_retained": after is not None,
            "positions_unchanged": _positions(session, second) == held,
            "flattened": _positions(session, second) == {},
            "floating_loss_retained": after is not None and before is not None and float(after.nav) < float(before.nav),
        }
        _cleanup_rotation(session)
        session.commit()
    _same(
        "critical risk does not force liquidation",
        {
            "sample_retained": True,
            "positions_unchanged": True,
            "flattened": False,
            "floating_loss_retained": True,
        },
        actual,
    )


def _exit_view(row: dict | None) -> dict:
    if row is None:
        return {
            "sample_retained": False,
            "realized_exit": False,
            "position_retained": False,
            "floating_loss_negative": False,
            "later_exit_open": False,
        }
    details = row.get("details") or {}
    floating = details.get("unrealized_return")
    retained = details.get("position_retained") is True or details.get("exit_state") in {"open", "pending"}
    return {
        "sample_retained": True,
        "realized_exit": row.get("fill_status") == "filled" and row.get("exit_price") is not None,
        "position_retained": retained,
        "floating_loss_negative": isinstance(floating, (int, float)) and float(floating) < 0,
        "later_exit_open": details.get("exit_state") in {"open", "pending"},
    }


def test_sealed_limit_down_keeps_position_and_floating_loss():
    decision = {"decision_id": 1, "decision_date": "2026-11-02", "reference_close": 1.0, "completed_horizons": set()}
    code = [
        _bar(dt.date(2026, 11, 2), 1.0, 1.0),
        _bar(dt.date(2026, 11, 3), 1.0, 1.02),
        _bar(dt.date(2026, 11, 4), 1.01, 1.01),
        _bar(dt.date(2026, 11, 5), 0.90, 0.90, 0.90, 0.90),
        _bar(dt.date(2026, 11, 6), 0.80, 0.80),
    ]
    bench = [_bar(row.date, 1.0, 1.0) for row in code]
    rows = {
        row["horizon_sessions"]: row
        for row in evaluate_decision(decision, code_bars=code, benchmark_bars=bench, as_of=dt.date(2026, 11, 6))
    }
    actual = _exit_view(rows.get(3))
    _same(
        "sealed limit-down exit",
        {
            "sample_retained": True,
            "realized_exit": False,
            "position_retained": True,
            "floating_loss_negative": True,
            "later_exit_open": True,
        },
        actual,
    )


def _queue_view(row: dict) -> dict:
    status = row.get("fill_status")
    reason = row.get("fill_reason")
    if status == "uncertain" or reason in {"queue_uncertain", "daily_bar_cannot_confirm_fill"}:
        verdict = "uncertain"
    elif status == "unfilled":
        verdict = "rejected"
    elif status == "filled":
        verdict = "filled"
    else:
        verdict = status
    return {"queue_verdict": verdict}


def test_one_tick_limit_queue_is_uncertain_not_rejected():
    decision = {"decision_id": 1, "decision_date": "2026-11-02", "reference_close": 1.0, "completed_horizons": set()}
    code = [
        _bar(dt.date(2026, 11, 2), 1.0, 1.0),
        _bar(dt.date(2026, 11, 3), 1.10, 1.10, 1.10, 1.098),
    ]
    bench = [_bar(row.date, 1.0, 1.0) for row in code]
    rows = {
        row["horizon_sessions"]: row
        for row in evaluate_decision(decision, code_bars=code, benchmark_bars=bench, as_of=dt.date(2026, 11, 3))
    }
    actual = _queue_view(rows[1])
    assert actual["queue_verdict"] != "rejected"
    _same("one-tick limit queue", {"queue_verdict": "uncertain"}, actual)


def test_sealed_limit_up_stays_unfilled():
    decision = {"decision_id": 1, "decision_date": "2026-11-02", "reference_close": 1.0, "completed_horizons": set()}
    code = [
        _bar(dt.date(2026, 11, 2), 1.0, 1.0),
        _bar(dt.date(2026, 11, 3), 1.10, 1.10, 1.10, 1.10),
    ]
    bench = [_bar(row.date, 1.0, 1.0) for row in code]
    rows = {
        row["horizon_sessions"]: row
        for row in evaluate_decision(decision, code_bars=code, benchmark_bars=bench, as_of=dt.date(2026, 11, 3))
    }
    _same(
        "sealed limit-up control",
        {"fill_status": "unfilled", "fill_reason": "limit_up_no_executable_liquidity"},
        {"fill_status": rows[1]["fill_status"], "fill_reason": rows[1]["fill_reason"]},
    )


def test_adjusted_entry_conflict_is_not_a_confirmed_fill():
    decision = {"decision_id": 1, "decision_date": "2026-11-02", "reference_close": 1.0, "completed_horizons": set()}
    code = [
        _bar(dt.date(2026, 11, 2), 1.0, 1.0),
        _bar(dt.date(2026, 11, 3), 1.045, 1.10, 1.10, 1.045),
    ]
    bench = [_bar(row.date, 1.0, 1.0) for row in code]
    rows = {
        row["horizon_sessions"]: row
        for row in evaluate_decision(decision, code_bars=code, benchmark_bars=bench, as_of=dt.date(2026, 11, 3))
    }
    row = rows[1]
    details = row.get("details") or {}
    conflict = details.get("price_basis") in {"conflict", "uncertain"} or row.get("fill_status") == "uncertain"
    actual = {
        "confirmed_executable_fill": row.get("fill_status") == "filled" and not conflict,
        "price_basis": details.get("price_basis"),
    }
    _same(
        "adjustment basis conflict",
        {"confirmed_executable_fill": False, "price_basis": "uncertain"},
        actual,
    )


def _cleanup_outcomes(session: Session) -> None:
    session.execute(delete(EodRun).where(EodRun.decision_date == OUTCOME_DAY))


@pytest.mark.parametrize("state", ["data_failed", "no_trigger"])
def test_non_signal_prior_bar_is_not_an_executable_fill(engine, state: str):
    sessions = _business_days(OUTCOME_DAY + dt.timedelta(days=1), 4)
    code = [_bar(OUTCOME_DAY, 1.0, 1.0)]
    code.append(_bar(sessions[0], 1.0, 1.02))
    code.append(_bar(sessions[1], 1.02, 1.05))
    code.append(_bar(sessions[2], 1.05, 1.08))
    bench = [_bar(day, 1.0, 1.0) for day in [OUTCOME_DAY, *sessions]]
    fetcher = _Fetcher({"512690": code, "510300": bench})
    with Session(engine) as session:
        _cleanup_outcomes(session)
        decision_id = _seed_run(session, OUTCOME_DAY, "512690", state, "ok", None)
        session.commit()
        evaluate_pending(session, as_of=sessions[2], fetcher=fetcher, decision_date=OUTCOME_DAY)
        session.commit()
        outcome = session.scalar(
            select(DecisionOutcome).where(
                DecisionOutcome.decision_id == decision_id,
                DecisionOutcome.horizon_sessions == 1,
            )
        )
        view = _executable_view(
            None if outcome is None else outcome.fill_status,
            None if outcome is None or outcome.net_return is None else float(outcome.net_return),
            None if outcome is None else outcome.details,
        )
        actual = {
            "sample_retained": outcome is not None,
            "return_role": view["return_role"],
            "executable_net_return": view["executable_net_return"],
        }
        _cleanup_outcomes(session)
        session.commit()
    _same(
        f"{state} is not an executable fill",
        {"sample_retained": True, "return_role": "research_counterfactual", "executable_net_return": None},
        actual,
    )
