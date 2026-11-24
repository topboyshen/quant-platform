"""M13 rule shadow: v2 signal generation, cooldown, outcomes and API."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.market.kline import DailyBar, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import RuleShadowOutcome, RuleShadowSignal, TargetDecision
from qp.signals import rule_shadow
from qp.signals.universe import Target

DAY = dt.date(2027, 7, 5)  # Monday, outside the fixture calendar
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


BEFORE = [d for d in business_days(DAY - dt.timedelta(days=130), 80) if d < DAY]
AFTER = business_days(DAY + dt.timedelta(days=1), 25)


def flat_bars(days: list[dt.date], close: float = 10.0, volume: float = 1000.0) -> list[DailyBar]:
    return [DailyBar(d, open=close, high=close, low=close, close=close, volume=volume) for d in days]


def cross_bars(volume: float = 3000.0) -> list[DailyBar]:
    bars = flat_bars(BEFORE)
    bars.append(DailyBar(DAY, open=10.0, high=11.0, low=10.0, close=11.0, volume=volume))
    return bars


class FakeFetcher:
    def __init__(self, bars: dict[str, list[DailyBar]]) -> None:
        self.bars = bars

    def get(self, code: str) -> KlineResult:
        return KlineResult(bars=list(self.bars.get(code, [])), source="test")


def cleanup(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(RuleShadowOutcome))
        session.execute(delete(RuleShadowSignal))
        session.execute(delete(TargetDecision).where(TargetDecision.decision_date == DAY))
        session.execute(
            delete(TradingDay).where(TradingDay.origin == "qp", TradingDay.date.in_([DAY, *BEFORE, *AFTER]))
        )
        session.commit()


def calendar(engine, days: list[dt.date]) -> None:
    with Session(engine) as session:
        for day in days:
            session.merge(TradingDay(origin="qp", date=day, is_open=True, source="test"))
        session.commit()


def target() -> Target:
    return Target(CODE, "沪深300ETF", "宽基")


def test_tables_exist(engine):
    with engine.connect() as c:
        names = {
            row[0]
            for row in c.execute(text("select table_name from information_schema.tables where table_schema='signals'"))
        }
    assert {"rule_shadow_signal", "rule_shadow_outcome"} <= names


def test_generate_records_only_two_x_volume(engine):
    cleanup(engine)
    try:
        calendar(engine, [DAY])
        with Session(engine) as session:
            quiet = rule_shadow.generate(
                session, day=DAY, fetcher=FakeFetcher({CODE: cross_bars(1600.0)}), targets=[target()]
            )
            session.commit()
        assert quiet["inserted"] == 0
        with Session(engine) as session:
            out = rule_shadow.generate(
                session, day=DAY, fetcher=FakeFetcher({CODE: cross_bars(3000.0)}), targets=[target()]
            )
            session.commit()
            rows = list(session.scalars(select(RuleShadowSignal)))
        assert out["inserted"] == 1
        assert len(rows) == 1
        row = rows[0]
        assert (row.code, row.rule_version, row.signal_type) == (CODE, rule_shadow.V2_RULE, rule_shadow.V2_SIGNAL_TYPE)
        assert float(row.reference_close) == 11.0
        assert float(row.volume_ratio) > 2.0
    finally:
        cleanup(engine)


def test_generate_is_idempotent(engine):
    cleanup(engine)
    try:
        calendar(engine, [DAY])
        fetcher = FakeFetcher({CODE: cross_bars(3000.0)})
        with Session(engine) as session:
            first = rule_shadow.generate(session, day=DAY, fetcher=fetcher, targets=[target()])
            session.commit()
        with Session(engine) as session:
            second = rule_shadow.generate(session, day=DAY, fetcher=fetcher, targets=[target()])
            session.commit()
        assert (first["inserted"], second["inserted"]) == (1, 0)
    finally:
        cleanup(engine)


def test_backfill_replays_open_days(engine):
    cleanup(engine)
    try:
        calendar(engine, [DAY, AFTER[0]])
        with Session(engine) as session:
            out = rule_shadow.backfill(
                session,
                since=DAY,
                until=AFTER[0],
                fetcher=FakeFetcher({CODE: cross_bars(3000.0)}),
                targets=[target()],
            )
            session.commit()
        assert out["days"] == 2
        assert out["inserted"] == 1
        assert out["no_bar"] == 1
        assert out["details"] == [{"day": DAY.isoformat(), "inserted": 1}]
    finally:
        cleanup(engine)


def test_cooldown_counts_trading_sessions(engine):
    cleanup(engine)
    try:
        later = business_days(DAY + dt.timedelta(days=1), 6)
        calendar(engine, [DAY, *later])
        with Session(engine) as session:
            session.add(
                RuleShadowSignal(
                    rule_version=rule_shadow.V2_RULE,
                    signal_type=rule_shadow.V2_SIGNAL_TYPE,
                    decision_date=DAY,
                    code=CODE,
                    name="x",
                    sector="宽基",
                )
            )
            session.commit()
            assert rule_shadow._cooldown_ok(session, rule_version=rule_shadow.V2_RULE, code=CODE, day=later[0]) is False
            assert rule_shadow._cooldown_ok(session, rule_version=rule_shadow.V2_RULE, code=CODE, day=later[4]) is True
    finally:
        cleanup(engine)


def test_collect_outcomes_and_idempotent(engine):
    cleanup(engine)
    try:
        calendar(engine, [DAY, *AFTER])
        code_bars = cross_bars(3000.0) + flat_bars(AFTER, close=11.0, volume=1000.0)
        bench_bars = (
            flat_bars(BEFORE)
            + [DailyBar(DAY, open=10.0, high=10.2, low=10.0, close=10.1, volume=1000.0)]
            + flat_bars(AFTER, close=10.2, volume=1000.0)
        )
        fetcher = FakeFetcher({CODE: code_bars, BENCHMARK: bench_bars})
        with Session(engine) as session:
            rule_shadow.generate(session, day=DAY, fetcher=fetcher, targets=[target()])
            session.commit()
            first = rule_shadow.collect(session, as_of=AFTER[-1], fetcher=fetcher)
            session.commit()
            second = rule_shadow.collect(session, as_of=AFTER[-1], fetcher=fetcher)
            session.commit()
            rows = list(session.scalars(select(RuleShadowOutcome)))
        assert first["inserted"] == 5 and second["inserted"] == 0
        assert sorted(row.horizon_sessions for row in rows) == [1, 3, 5, 10, 20]
    finally:
        cleanup(engine)


def test_api_returns_rule_samples(engine):
    from fastapi.testclient import TestClient

    from qp.api.main import app
    from qp.auth.sessions import create_service_token

    cleanup(engine)
    try:
        calendar(engine, [DAY, *AFTER])
        code_bars = cross_bars(3000.0) + flat_bars(AFTER, close=11.0, volume=1000.0)
        bench_bars = (
            flat_bars(BEFORE)
            + [DailyBar(DAY, open=10.0, high=10.2, low=10.0, close=10.1, volume=1000.0)]
            + flat_bars(AFTER, close=10.2, volume=1000.0)
        )
        fetcher = FakeFetcher({CODE: code_bars, BENCHMARK: bench_bars})
        with Session(engine) as session:
            rule_shadow.generate(session, day=DAY, fetcher=fetcher, targets=[target()])
            rule_shadow.collect(session, as_of=AFTER[-1], fetcher=fetcher)
            token = create_service_token(session, f"m13-rule-{dt.datetime.now().timestamp()}", ["signals:read"])
            session.commit()
        client = TestClient(app, base_url="https://testserver")
        response = client.get("/api/signals/rule-shadow", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1 and body["summary"]["outcomes"] == 5
        row = body["rows"][0]
        assert row["code"] == CODE and row["rule_version"] == rule_shadow.V2_RULE
        assert set(row["outcomes"]) == {"1", "3", "5", "10", "20"}
    finally:
        cleanup(engine)


def test_jobs_registered():
    assert JOBS["signals.rule_shadow"].schedule_hint.startswith("Mon-Fri 15:37 CST")
    assert JOBS["signals.rule_shadow_outcomes"].schedule_hint.startswith("Mon-Fri 16:17 CST")
