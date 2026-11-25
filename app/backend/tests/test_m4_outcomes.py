"""M4.4 outcome collection: legacy execution model, unfilled states and idempotency."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.market.kline import DailyBar, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision
from qp.signals.outcomes import (
    OutcomeSettings,
    evaluate_decision,
    evaluate_pending,
    normalise_bars,
)

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


def bar(day: dt.date, *, open_: float, close: float, high: float | None = None, low: float | None = None) -> DailyBar:
    return DailyBar(
        day,
        open=open_,
        high=high if high is not None else max(open_, close),
        low=low if low is not None else min(open_, close),
        close=close,
        volume=1000.0,
    )


def benchmark_bars() -> list[DailyBar]:
    return [
        bar(day, open_=100.0, close=100.0 + index * 0.5, high=112.0, low=99.0)
        for index, day in enumerate(SESSIONS, start=1)
    ]


def code_bars() -> list[DailyBar]:
    bars = [bar(DAY, open_=9.8, close=9.8, high=9.9, low=9.7)]
    bars.append(bar(SESSIONS[0], open_=10.0, close=10.2, high=10.4, low=9.9))
    bars.append(bar(SESSIONS[1], open_=10.2, close=10.5, high=10.6, low=10.0))
    bars.append(bar(SESSIONS[2], open_=10.5, close=10.7, high=10.8, low=10.3))
    bars.append(bar(SESSIONS[3], open_=10.7, close=11.0, high=11.2, low=10.6))
    for index, day in enumerate(SESSIONS[4:], start=5):
        bars.append(bar(day, open_=11.0, close=11.0 + index * 0.01, high=11.3, low=10.8))
    return bars


def decision(day: dt.date = DAY, reference: float | None = 9.8, completed: set[int] | None = None) -> dict:
    return {
        "decision_id": 1,
        "decision_date": day,
        "reference_close": reference,
        "completed_horizons": completed or set(),
    }


def by_horizon(rows: list[dict]) -> dict[int, dict]:
    return {int(row["horizon_sessions"]): row for row in rows}


# --------------------------------------------------------------------------- execution model
def test_filled_horizons_use_next_session_open_and_horizon_close():
    rows = by_horizon(
        evaluate_decision(decision(), code_bars=code_bars(), benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1])
    )
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
    rows = by_horizon(
        evaluate_decision(decision(), code_bars=bars, benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1])
    )
    assert set(rows) == {1, 3, 5, 10, 20}
    assert all(row["fill_status"] == "unfilled" for row in rows.values())
    assert {row["fill_reason"] for row in rows.values()} == {"suspended_or_no_next_session_bar"}
    assert all(row["net_return"] is None and row["excess_return"] is None for row in rows.values())


def test_limit_up_gap_has_no_executable_liquidity():
    bars = code_bars()
    bars[1] = bar(SESSIONS[0], open_=10.8, close=10.8, high=10.8, low=10.8)
    rows = by_horizon(
        evaluate_decision(decision(), code_bars=bars, benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1])
    )
    assert {row["fill_reason"] for row in rows.values()} == {"limit_up_no_executable_liquidity"}


def test_missing_exit_bar_only_affects_that_horizon():
    bars = [item for item in code_bars() if item.date != SESSIONS[2]]
    rows = by_horizon(
        evaluate_decision(decision(), code_bars=bars, benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1])
    )
    assert rows[1]["fill_status"] == "filled"
    assert (rows[3]["fill_status"], rows[3]["fill_reason"]) == ("unfilled", "exit_bar_missing_or_suspended")


def test_notional_below_one_board_lot_is_unfilled():
    bars = [bar(DAY, open_=2000.0, close=2000.0)]
    bars.append(bar(SESSIONS[0], open_=2000.0, close=2000.0, high=2000.0, low=2000.0))
    bars.append(bar(SESSIONS[1], open_=2100.0, close=2100.0, high=2100.0, low=2100.0))
    rows = evaluate_decision(
        decision(reference=2000.0),
        code_bars=bars,
        benchmark_bars=benchmark_bars(),
        as_of=SESSIONS[1],
        settings=OutcomeSettings(notional=100_000),
    )
    assert rows[0]["fill_status"] == "unfilled"
    assert rows[0]["fill_reason"] == "notional_below_one_board_lot"


def test_only_matured_and_unrecorded_horizons_are_returned():
    rows = evaluate_decision(
        decision(completed={1}), code_bars=code_bars(), benchmark_bars=benchmark_bars(), as_of=SESSIONS[2]
    )
    assert sorted(int(row["horizon_sessions"]) for row in rows) == [3]
    assert evaluate_decision(decision(), code_bars=code_bars(), benchmark_bars=benchmark_bars(), as_of=DAY) == []


def test_normalise_bars_filters_as_of_and_duplicates():
    bars = code_bars()
    bars.append(bar(SESSIONS[1], open_=10.2, close=10.55, high=10.6, low=10.0))
    out = normalise_bars(bars, as_of=SESSIONS[2])
    assert out[-1].date == SESSIONS[2]
    assert [item.close for item in out if item.date == SESSIONS[1]] == [10.55]


# --------------------------------------------------------------------------- calendar window
def test_calendar_window_does_not_shift_on_a_missing_middle_benchmark_day():
    """Without a calendar a gap silently moves the 3-day exit; with one it must not."""
    benchmark = [item for item in benchmark_bars() if item.date != SESSIONS[1]]
    legacy = by_horizon(
        evaluate_decision(decision(), code_bars=code_bars(), benchmark_bars=benchmark, as_of=SESSIONS[-1])
    )
    assert legacy[3]["exit_date"] == SESSIONS[3]  # documented legacy shift

    rows = by_horizon(
        evaluate_decision(
            decision(),
            code_bars=code_bars(),
            benchmark_bars=benchmark,
            as_of=SESSIONS[-1],
            sessions=SESSIONS,
        )
    )
    assert rows[3]["exit_date"] == SESSIONS[2]
    assert rows[3]["fill_status"] == "filled"
    assert rows[3]["details"]["window_basis"] == "calendar"
    assert rows[1]["details"]["window_basis"] == "calendar"


def test_missing_benchmark_entry_day_is_a_data_gap_not_a_shifted_fill():
    benchmark = [item for item in benchmark_bars() if item.date != SESSIONS[0]]
    gaps: list[dict] = []
    rows = evaluate_decision(
        decision(),
        code_bars=code_bars(),
        benchmark_bars=benchmark,
        as_of=SESSIONS[-1],
        sessions=SESSIONS,
        data_gaps=gaps,
    )
    assert rows == []
    assert {gap["horizon"] for gap in gaps} == {1, 3, 5, 10, 20}
    assert all(gap["reason"] == "benchmark_bar_missing" for gap in gaps)
    entry_gaps = [gap for gap in gaps if gap["horizon"] == 3]
    assert entry_gaps == [
        {
            "decision_id": 1,
            "horizon": 3,
            "date": SESSIONS[0].isoformat(),
            "sides": ["entry"],
            "reason": "benchmark_bar_missing",
        }
    ]
    assert gaps[0]["sides"] == ["entry", "exit"]  # horizon 1 entry and exit are the same session


def test_missing_benchmark_exit_day_leaves_only_that_horizon_unrecorded():
    benchmark = [item for item in benchmark_bars() if item.date != SESSIONS[2]]
    gaps: list[dict] = []
    rows = by_horizon(
        evaluate_decision(
            decision(),
            code_bars=code_bars(),
            benchmark_bars=benchmark,
            as_of=SESSIONS[-1],
            sessions=SESSIONS,
            data_gaps=gaps,
        )
    )
    assert rows[1]["fill_status"] == "filled"
    assert 3 not in rows
    assert [(gap["horizon"], gap["date"], gap["sides"]) for gap in gaps] == [(3, SESSIONS[2].isoformat(), ["exit"])]


def test_flat_bar_outside_the_limit_still_fills():
    bars = code_bars()
    bars[1] = bar(SESSIONS[0], open_=10.0, close=10.0, high=10.0, low=10.0)
    rows = by_horizon(
        evaluate_decision(
            decision(), code_bars=bars, benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1], sessions=SESSIONS
        )
    )
    assert rows[1]["fill_status"] == "filled"
    assert rows[1]["gross_return"] == 0


def test_one_tick_limit_up_bar_still_reaches_the_liquidity_rule():
    bars = code_bars()
    bars[1] = bar(SESSIONS[0], open_=10.8, close=10.8, high=10.8, low=10.789)
    rows = by_horizon(
        evaluate_decision(
            decision(), code_bars=bars, benchmark_bars=benchmark_bars(), as_of=SESSIONS[-1], sessions=SESSIONS
        )
    )
    assert {row["fill_status"] for row in rows.values()} == {"uncertain"}
    assert {row["fill_reason"] for row in rows.values()} == {"queue_uncertain"}


@pytest.mark.parametrize(
    ("bad_bar", "reason"),
    [
        (DailyBar(SESSIONS[0], open=10.0, high=9.0, low=11.0, close=10.0, volume=1000.0), "high_below_low"),
        (DailyBar(SESSIONS[0], open=12.0, high=10.4, low=9.9, close=10.2, volume=1000.0), "open_out_of_range"),
        (DailyBar(SESSIONS[0], open=10.0, high=10.4, low=9.9, close=11.0, volume=1000.0), "close_out_of_range"),
    ],
)
def test_incoherent_entry_bar_cannot_fill(bad_bar, reason):
    rejected: list[dict] = []
    bars = code_bars()
    bars[1] = bad_bar
    clean = normalise_bars(bars, as_of=SESSIONS[-1], rejected=rejected)
    assert rejected == [{"date": SESSIONS[0].isoformat(), "reason": reason}]
    rows = by_horizon(
        evaluate_decision(
            decision(),
            code_bars=clean,
            benchmark_bars=benchmark_bars(),
            as_of=SESSIONS[-1],
            sessions=SESSIONS,
            code_rejections={SESSIONS[0]: reason},
        )
    )
    assert {row["fill_status"] for row in rows.values()} == {"unfilled"}
    assert {row["fill_reason"] for row in rows.values()} == {f"entry_bar_invalid:{reason}"}
    assert all(row["net_return"] is None for row in rows.values())


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


def seed_decision(
    engine,
    *,
    day: dt.date = DAY,
    code: str = CODE,
    state: str = "allowed",
    reference: float | None = 9.8,
    target_count: int = 1,
) -> int:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date == day))
        run = EodRun(
            decision_date=day,
            attempt_no=1,
            status="success",
            decision_outcome="emitted" if state == "allowed" else "no_signal",
            target_count=target_count,
            evaluated_count=target_count,
            failed_count=0,
            signal_count=1 if state == "allowed" else 0,
            allowed_signal_count=1 if state == "allowed" else 0,
            blocked_signal_count=0,
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
            data_status="ok" if state != "data_failed" else "failed",
            decision_state=state,
            risk_action="allowed" if state == "allowed" else "not_applicable",
            risk_level="ok",
            reason_code="signal_allowed" if state == "allowed" else "trend_data_unavailable",
            reference_close=reference,
            raw_trigger_count=1 if state != "no_trigger" else 0,
            raw_signals=[],
            validation_split="live_oos",
        )
        session.add(row)
        session.commit()
        return row.id


def cleanup(engine, day: dt.date = DAY) -> None:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date == day))
        session.commit()


def seed_calendar(engine, days: list[dt.date]) -> None:
    with Session(engine) as session:
        for day in days:
            session.merge(TradingDay(origin="qp", date=day, is_open=True, source="test"))
        session.commit()


def cleanup_calendar(engine, days: list[dt.date]) -> None:
    with Session(engine) as session:
        session.execute(delete(TradingDay).where(TradingDay.origin == "qp", TradingDay.date.in_(days)))
        session.commit()


def test_evaluate_pending_persists_and_is_idempotent(engine):
    decision_id = seed_decision(engine)
    fetcher = FakeFetcher({BENCHMARK: benchmark_bars(), CODE: code_bars()})
    try:
        with Session(engine) as session:
            first = evaluate_pending(session, as_of=SESSIONS[2], fetcher=fetcher, decision_date=DAY)
            session.commit()
        assert (first["checked"], first["inserted"], first["not_mature"], first["failed"]) == (1, 2, 0, 0)
        assert first["window_basis"] == "benchmark_bars"  # no calendar seeded: legacy parity path
        assert first["data_gaps"] == [] and first["data_gap_count"] == 0
        with Session(engine) as session:
            second = evaluate_pending(session, as_of=SESSIONS[2], fetcher=fetcher, decision_date=DAY)
            session.commit()
        assert (second["checked"], second["inserted"], second["not_mature"]) == (1, 0, 1)
        with Session(engine) as session:
            third = evaluate_pending(session, as_of=SESSIONS[-1], fetcher=fetcher, decision_date=DAY)
            session.commit()
        assert (third["inserted"], third["failed"]) == (3, 0)
        with Session(engine) as session:
            rows = list(
                session.execute(
                    select(DecisionOutcome)
                    .where(DecisionOutcome.decision_id == decision_id)
                    .order_by(DecisionOutcome.horizon_sessions)
                ).scalars()
            )
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
    decision_id = seed_decision(engine, code="512890", state="data_failed", reference=None)
    # Entry bar exists but neither a reference close nor a pre-decision bar does.
    fetcher = FakeFetcher({BENCHMARK: benchmark_bars(), "512890": [code_bars()[1]]})
    try:
        with Session(engine) as session:
            out = evaluate_pending(session, as_of=SESSIONS[-1], fetcher=fetcher, decision_date=DAY)
            session.commit()
        assert out["inserted"] == 5 and out["unfilled"] == 5
        with Session(engine) as session:
            rows = list(
                session.execute(
                    select(DecisionOutcome)
                    .where(DecisionOutcome.decision_id == decision_id)
                    .order_by(DecisionOutcome.horizon_sessions)
                ).scalars()
            )
            assert {row.fill_status for row in rows} == {"unfilled"}
            assert {row.fill_reason for row in rows} == {"decision_reference_close_missing"}
    finally:
        cleanup(engine)


def test_benchmark_failure_marks_every_decision_failed_without_rows(engine):
    decision_id = seed_decision(engine)
    fetcher = FakeFetcher({BENCHMARK: RuntimeError("down"), CODE: code_bars()})
    try:
        with Session(engine) as session:
            out = evaluate_pending(session, as_of=SESSIONS[-1], fetcher=fetcher, decision_date=DAY)
            session.commit()
            assert out["benchmark_ok"] is False and out["failed"] == 1
            assert out["benchmark_code"] == BENCHMARK
            assert out["benchmark_error"] == "RuntimeError"
            assert session.scalar(select(DecisionOutcome.id).where(DecisionOutcome.decision_id == decision_id)) is None
    finally:
        cleanup(engine)


def test_benchmark_empty_attempts_are_reported(engine):
    seed_decision(engine)

    class EmptyBenchmarkFetcher(FakeFetcher):
        def get(self, code: str):
            if code == BENCHMARK:
                return KlineResult(bars=[], source="", attempts=[{"source": "tencent", "error": "HTTPError"}])
            return super().get(code)

    try:
        with Session(engine) as session:
            out = evaluate_pending(
                session, as_of=SESSIONS[-1], fetcher=EmptyBenchmarkFetcher({CODE: code_bars()}), decision_date=DAY
            )
            session.commit()
        assert out["benchmark_ok"] is False
        assert out["benchmark_attempts"][0]["source"] == "tencent"
        assert out["benchmark_error"] is None
    finally:
        cleanup(engine)


def test_missing_benchmark_day_waits_then_backfills_with_the_same_window(engine):
    decision_id = seed_decision(engine)
    days = [DAY, *SESSIONS]
    seed_calendar(engine, days)
    missing = [item for item in benchmark_bars() if item.date != SESSIONS[0]]
    try:
        with Session(engine) as session:
            first = evaluate_pending(
                session,
                as_of=SESSIONS[-1],
                fetcher=FakeFetcher({BENCHMARK: missing, CODE: code_bars()}),
                decision_date=DAY,
            )
            session.commit()
            assert first["window_basis"] == "calendar"
            assert (first["inserted"], first["waiting_data"], first["data_gap_count"]) == (0, 1, 5)
            assert session.scalar(select(DecisionOutcome.id).where(DecisionOutcome.decision_id == decision_id)) is None
        with Session(engine) as session:
            second = evaluate_pending(
                session,
                as_of=SESSIONS[-1],
                fetcher=FakeFetcher({BENCHMARK: benchmark_bars(), CODE: code_bars()}),
                decision_date=DAY,
            )
            session.commit()
            rows = list(
                session.scalars(
                    select(DecisionOutcome)
                    .where(DecisionOutcome.decision_id == decision_id)
                    .order_by(DecisionOutcome.horizon_sessions)
                )
            )
        assert second["inserted"] == 5
        assert [(row.entry_date, row.exit_date) for row in rows] == [
            (SESSIONS[0], SESSIONS[0]),
            (SESSIONS[0], SESSIONS[2]),
            (SESSIONS[0], SESSIONS[4]),
            (SESSIONS[0], SESSIONS[9]),
            (SESSIONS[0], SESSIONS[19]),
        ]
        assert all(row.fill_status == "filled" for row in rows)
        assert rows[0].details["window_basis"] == "calendar"
        baseline = by_horizon(
            evaluate_decision(
                decision(),
                code_bars=code_bars(),
                benchmark_bars=benchmark_bars(),
                as_of=SESSIONS[-1],
                sessions=SESSIONS,
            )
        )
        assert [float(row.net_return) for row in rows] == pytest.approx(
            [baseline[int(row.horizon_sessions)]["net_return"] for row in rows]
        )
        with Session(engine) as session:
            third = evaluate_pending(
                session,
                as_of=SESSIONS[-1],
                fetcher=FakeFetcher({BENCHMARK: benchmark_bars(), CODE: code_bars()}),
                decision_date=DAY,
            )
            session.commit()
        assert third["inserted"] == 0
    finally:
        cleanup(engine)
        cleanup_calendar(engine, days)


def test_invalid_entry_bar_is_isolated_and_reported(engine):
    decision_id = seed_decision(engine)
    bars = code_bars()
    bars[1] = DailyBar(SESSIONS[0], open=10.0, high=9.0, low=11.0, close=10.0, volume=1000.0)
    try:
        with Session(engine) as session:
            out = evaluate_pending(
                session,
                as_of=SESSIONS[-1],
                fetcher=FakeFetcher({BENCHMARK: benchmark_bars(), CODE: bars}),
                decision_date=DAY,
            )
            session.commit()
            rows = list(session.scalars(select(DecisionOutcome).where(DecisionOutcome.decision_id == decision_id)))
        assert (out["inserted"], out["unfilled"]) == (5, 5)
        assert out["invalid_bar_count"] == 1
        assert out["invalid_bars"] == [
            {"code": CODE, "source": "test", "date": SESSIONS[0].isoformat(), "reason": "high_below_low"}
        ]
        assert {row.fill_reason for row in rows} == {"entry_bar_invalid:high_below_low"}
        assert all(row.net_return is None for row in rows)
    finally:
        cleanup(engine)


def test_outcome_job_is_registered():
    assert "signals.outcomes" in JOBS
    assert "16:10" in (JOBS["signals.outcomes"].schedule_hint or "")
