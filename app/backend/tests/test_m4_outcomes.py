"""M4.4 outcome collection: legacy execution model, unfilled states and idempotency."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.market.kline import DailyBar, KlineResult
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision
from qp.signals.outcomes import (OutcomeSettings, evaluate_decision, evaluate_pending,
                                 normalise_bars)

DAY = dt.date(2027, 3, 1)  # Monday, outside the test calendar fixtures
CODE = "159915"
BENCHMARK = "510300"


def business_days(start: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


SESSIONS = business_days(DAY + dt.timedelta(days=1), 20)  # entry + 20 horizon sessions


def bar(day: dt.date, *, open_: float, close: float, high: float | None = None,
        low: float | None = None) -> DailyBar:
    return DailyBar(day, open=open_, high=high if high is not None else max(open_, close),
                    low=low if low is not None else min(open_, close), close=close, volume=1000.0)


def benchmark_bars() -> list[DailyBar]:
    return [bar(day, open_=100.0, close=100.0 + index * 0.5, high=102.0, low=99.0)
            for index, day in enumerate(SESSIONS, start=1)]


def code_bars() -> list[DailyBar]:
    bars = [bar(DAY, open_=9.8, close=9.8, high=9.9, low=9.7)]
    bars.append(bar(SESSIONS[0], open_=10.0, close=10.2, high=10.4, low=9.9))
    bars.append(bar(SESSIONS[1], open_=10.2, close=10.5, high=10.6, low=10.0))
    bars.append(bar(SESSIONS[2], open_=10.5, close=10.7, high=10.8, low=10.3))
    bars.append(bar(SESSIONS[3], open_=10.7, close=11.0, high=11.2, low=10.6))
    for index, day in enumerate(SESSIONS[4:], start=5):
        bars.append(bar(day, open_=11.0, close=11.0 + index * 0.01,
                        high=11.3, low=10.8))
    return bars


def decision(day: dt.date = DAY, reference: float | None = 9.8,
             completed: set[int] | None = None) -> dict:
    return {"decision_id": 1, "decision_date": day, "reference_close": reference,
            "completed_horizons": completed or set()}


def by_horizon(rows: list[dict]) -> dict[int, dict]:
    return {int(row["horizon_sessions"]): row for row in rows}


# --------------------------------------------------------------------------- execution model
def test_filled_horizons_use_next_session_open_and_horizon_close():
    rows = by_horizon(evaluate_decision(decision(), code_bars=code_bars(),
                                        benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1]))
    assert sorted(rows) == [1, 3, 5, 10, 20]
    one = rows[1]
    assert (one["fill_status"], one["fill_reason"]) == ("filled", "next_session_open")
    # Legacy horizon 1 is a same-session round trip: buy the next open, sell that close.
    assert one["entry_date"] == SESSIONS[0] and one["exit_date"] == SESSIONS[0]
    assert one["entry_price"] == 10.0 and one["exit_price"] == 10.2
    assert one["gross_return"] == pytest.approx(0.02)
    assert one["net_return"] == pytest.approx(0.01836930480076382, rel=0, abs=1e-12)
    assert one["benchmark_return"] == pytest.approx(0.005)
    assert one["excess_return"] == pytest.approx(one["net_return"] - one["benchmark_return"])
    detail = one["details"]
    assert detail["entry_fill"] == pytest.approx(10.005)
    assert detail["exit_fill"] == pytest.approx(10.1949)
    assert detail["shares"] == 9900
    assert detail["gap_return"] == pytest.approx(10.0 / 9.8 - 1)
    assert detail["fees_return"] > 0 and detail["slippage_return"] > 0
    assert detail["mfe"] == pytest.approx(10.4 / 10.005 - 1)
    assert detail["mae"] == pytest.approx(9.9 / 10.005 - 1)
    three = rows[3]
    assert three["exit_date"] == SESSIONS[2] and three["exit_price"] == 10.7
    assert three["gross_return"] == pytest.approx(0.07)
    assert rows[20]["exit_date"] == SESSIONS[19]


def test_missing_entry_bar_is_unfilled_not_zero():
    bars = [item for item in code_bars() if item.date != SESSIONS[0]]
    rows = by_horizon(evaluate_decision(decision(), code_bars=bars,
                                        benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1]))
    assert set(rows) == {1, 3, 5, 10, 20}
    assert all(row["fill_status"] == "unfilled" for row in rows.values())
    assert {row["fill_reason"] for row in rows.values()} == {"suspended_or_no_next_session_bar"}
    assert all(row["net_return"] is None and row["excess_return"] is None for row in rows.values())


def test_limit_up_gap_has_no_executable_liquidity():
    bars = code_bars()
    bars[1] = bar(SESSIONS[0], open_=10.8, close=10.8, high=10.8, low=10.8)
    rows = by_horizon(evaluate_decision(decision(), code_bars=bars,
                                        benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1]))
    assert {row["fill_reason"] for row in rows.values()} == {"limit_up_no_executable_liquidity"}


def test_missing_exit_bar_only_affects_that_horizon():
    bars = [item for item in code_bars() if item.date != SESSIONS[2]]
    rows = by_horizon(evaluate_decision(decision(), code_bars=bars,
                                        benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1]))
    assert rows[1]["fill_status"] == "filled"
    assert (rows[3]["fill_status"], rows[3]["fill_reason"]) == (
        "unfilled", "exit_bar_missing_or_suspended")


def test_notional_below_one_board_lot_is_unfilled():
    bars = [bar(DAY, open_=2000.0, close=2000.0)]
    bars.append(bar(SESSIONS[0], open_=2000.0, close=2000.0, high=2000.0, low=2000.0))
    bars.append(bar(SESSIONS[1], open_=2100.0, close=2100.0, high=2100.0, low=2100.0))
    rows = evaluate_decision(decision(reference=2000.0), code_bars=bars,
                             benchmark_bars=benchmark_bars(), as_of=SESSIONS[1],
                             settings=OutcomeSettings(notional=100_000))
    assert rows[0]["fill_status"] == "unfilled"
    assert rows[0]["fill_reason"] == "notional_below_one_board_lot"


def test_only_matured_and_unrecorded_horizons_are_returned():
    rows = evaluate_decision(decision(completed={1}), code_bars=code_bars(),
                             benchmark_bars=benchmark_bars(), as_of=SESSIONS[2])
    assert sorted(int(row["horizon_sessions"]) for row in rows) == [3]
    assert evaluate_decision(decision(), code_bars=code_bars(),
                             benchmark_bars=benchmark_bars(), as_of=DAY) == []


def test_normalise_bars_filters_as_of_and_duplicates():
    bars = code_bars()
    bars.append(bar(SESSIONS[1], open_=10.2, close=10.55, high=10.6, low=10.0))
    out = normalise_bars(bars, as_of=SESSIONS[2])
    assert out[-1].date == SESSIONS[2]
    assert [item.close for item in out if item.date == SESSIONS[1]] == [10.55]


# --------------------------------------------------------------------------- persistence
class FakeFetcher:
    def __init__(self, mapping: dict[str, object]):
        self.mapping = mapping
        self.codes: list[str] = []

    def get(self, code: str):
        self.codes.append(code)
        value = self.mapping[code]
        if isinstance(value, Exception):
            raise value
        return KlineResult(bars=value, source="test", attempts=[])


def seed_decision(engine, *, day: dt.date = DAY, code: str = CODE,
                  state: str = "allowed", reference: float | None = 9.8,
                  target_count: int = 1) -> int:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date == day))
        run = EodRun(decision_date=day, attempt_no=1, status="success",
                     decision_outcome="emitted" if state == "allowed" else "no_signal",
                     target_count=target_count, evaluated_count=target_count, failed_count=0,
                     signal_count=1 if state == "allowed" else 0,
                     allowed_signal_count=1 if state == "allowed" else 0,
                     blocked_signal_count=0, reason_codes=[])
        session.add(run)
        session.flush()
        row = TargetDecision(
            run_id=run.run_id, decision_date=day, code=code, name="创业板ETF", sector="成长",
            data_status="ok" if state != "data_failed" else "failed",
            decision_state=state,
            risk_action="allowed" if state == "allowed" else "not_applicable",
            risk_level="ok",
            reason_code="signal_allowed" if state == "allowed" else "trend_data_unavailable",
            reference_close=reference, raw_trigger_count=1 if state != "no_trigger" else 0,
            raw_signals=[], validation_split="live_oos")
        session.add(row)
        session.commit()
        return row.id


def cleanup(engine, day: dt.date = DAY) -> None:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date == day))
        session.commit()


def test_evaluate_pending_persists_and_is_idempotent(engine):
    decision_id = seed_decision(engine)
    fetcher = FakeFetcher({BENCHMARK: benchmark_bars(), CODE: code_bars()})
    try:
        with Session(engine) as session:
            first = evaluate_pending(session, as_of=SESSIONS[2], fetcher=fetcher,
                                     decision_date=DAY)
            session.commit()
        assert (first["checked"], first["inserted"], first["not_mature"], first["failed"]) == (
            1, 2, 0, 0)
        with Session(engine) as session:
            second = evaluate_pending(session, as_of=SESSIONS[2], fetcher=fetcher,
                                      decision_date=DAY)
            session.commit()
        assert (second["checked"], second["inserted"], second["not_mature"]) == (1, 0, 1)
        with Session(engine) as session:
            third = evaluate_pending(session, as_of=SESSIONS[-1], fetcher=fetcher,
                                     decision_date=DAY)
            session.commit()
        assert (third["inserted"], third["failed"]) == (3, 0)
        with Session(engine) as session:
            rows = list(session.execute(
                select(DecisionOutcome).where(DecisionOutcome.decision_id == decision_id)
                .order_by(DecisionOutcome.horizon_sessions)).scalars())
            assert [row.horizon_sessions for row in rows] == [1, 3, 5, 10, 20]
            assert all(row.fill_status == "filled" for row in rows)
            assert rows[0].entry_date == SESSIONS[0]
            assert float(rows[0].net_return) == pytest.approx(0.0183693048, abs=1e-8)
            assert rows[0].details["shares"] == 9900
            assert rows[0].fill_reason == "next_session_open"
    finally:
        cleanup(engine)


def test_data_failed_decision_still_records_unfilled_horizons(engine):
    """Legacy C6 row counts include data_failed decisions as unfilled rows."""
    decision_id = seed_decision(engine, code="512890", state="data_failed",
                                reference=None)
    # Entry bar exists but neither a reference close nor a pre-decision bar does.
    fetcher = FakeFetcher({BENCHMARK: benchmark_bars(), "512890": [code_bars()[1]]})
    try:
        with Session(engine) as session:
            out = evaluate_pending(session, as_of=SESSIONS[-1], fetcher=fetcher,
                                   decision_date=DAY)
            session.commit()
        assert out["inserted"] == 5 and out["unfilled"] == 5
        with Session(engine) as session:
            rows = list(session.execute(
                select(DecisionOutcome).where(DecisionOutcome.decision_id == decision_id)
                .order_by(DecisionOutcome.horizon_sessions)).scalars())
            assert {row.fill_status for row in rows} == {"unfilled"}
            assert {row.fill_reason for row in rows} == {"decision_reference_close_missing"}
    finally:
        cleanup(engine)


def test_benchmark_failure_marks_every_decision_failed_without_rows(engine):
    decision_id = seed_decision(engine)
    fetcher = FakeFetcher({BENCHMARK: RuntimeError("down"), CODE: code_bars()})
    try:
        with Session(engine) as session:
            out = evaluate_pending(session, as_of=SESSIONS[-1], fetcher=fetcher,
                                   decision_date=DAY)
            session.commit()
            assert out["benchmark_ok"] is False and out["failed"] == 1
            assert session.scalar(select(DecisionOutcome.id)
                                  .where(DecisionOutcome.decision_id == decision_id)) is None
    finally:
        cleanup(engine)


def test_outcome_job_is_registered():
    assert "signals.outcomes" in JOBS
    assert "16:10" in (JOBS["signals.outcomes"].schedule_hint or "")
