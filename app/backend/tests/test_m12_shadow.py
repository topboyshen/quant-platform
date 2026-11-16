"""M12 shadow allowed samples: record blocked triggers, collect outcomes, API."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.market.kline import DailyBar, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import EodRun, ShadowOutcome, ShadowSignal, TargetDecision
from qp.signals import shadow

DAY = dt.date(2027, 4, 5)  # Monday, outside the fixture calendar
CODE = "513050"
BENCHMARK = "510300"


def business_days(start: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


SESSIONS = business_days(DAY + dt.timedelta(days=1), 20)


def bar(day: dt.date, *, open_: float, close: float) -> DailyBar:
    return DailyBar(day, open=open_, high=max(open_, close), low=min(open_, close),
                    close=close, volume=1000.0)


class FakeFetcher:
    def __init__(self) -> None:
        self.bars = {
            CODE: [bar(DAY, open_=9.8, close=9.8)] + [
                bar(day, open_=10.0 + i * 0.02, close=10.1 + i * 0.02)
                for i, day in enumerate(SESSIONS)],
            BENCHMARK: [bar(DAY, open_=100.0, close=100.0)] + [
                bar(day, open_=100.0, close=100.0 + i * 0.1)
                for i, day in enumerate(SESSIONS)],
        }

    def get(self, code: str) -> KlineResult:
        return KlineResult(bars=list(self.bars[code]), source="test")


class BenchmarkDownFetcher(FakeFetcher):
    def get(self, code: str) -> KlineResult:
        if code == BENCHMARK:
            return KlineResult(bars=[], source="", attempts=[
                {"source": "tencent", "rows": 0, "latest_date": None, "error": "HTTPError"},
                {"source": "sina", "rows": 0, "latest_date": None, "error": "Timeout"},
            ])
        return super().get(code)


def cleanup(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(ShadowOutcome))
        session.execute(delete(ShadowSignal))
        session.execute(delete(TargetDecision).where(TargetDecision.decision_date == DAY))
        session.execute(delete(EodRun).where(EodRun.decision_date == DAY))
        session.execute(delete(TradingDay).where(TradingDay.origin == "qp",
                                                  TradingDay.date.in_([DAY, *SESSIONS])))
        session.commit()


def seed(engine) -> int:
    with Session(engine) as session:
        for day in [DAY, *SESSIONS]:
            session.merge(TradingDay(origin="qp", date=day, is_open=True, source="test"))
        run = EodRun(decision_date=DAY, attempt_no=1, status="success",
                     decision_outcome="blocked", target_count=3, evaluated_count=3,
                     failed_count=0, signal_count=1, allowed_signal_count=0,
                     blocked_signal_count=1, reason_codes=[],
                     started_at=dt.datetime(2027, 4, 5, 15, 30),
                     finished_at=dt.datetime(2027, 4, 5, 15, 31))
        session.add(run)
        session.flush()
        rows = [
            ("blocked", CODE, 1, "macro_risk_blocked"),
            ("allowed", "159869", 1, "signal_allowed"),
            ("no_trigger", "510300", 0, "signal_not_triggered"),
            ("data_failed", "518880", 1, "trend_data_unavailable"),
        ]
        for i, (state, code, triggers, reason) in enumerate(rows):
            session.add(TargetDecision(
                run_id=run.run_id, decision_date=DAY, code=code, name=code, sector="宽基",
                data_status="failed" if state == "data_failed" else "ok",
                decision_state=state,
                risk_action="blocked" if state == "blocked" else "not_applicable",
                risk_level="critical", reason_code=reason,
                reference_close=None if state == "data_failed" else 9.8,
                raw_trigger_count=triggers if state != "data_failed" else 1,
                raw_signals=[], validation_split="live_oos"))
        session.commit()
        return run.run_id


def test_migration_tables_and_constraints(engine):
    with engine.connect() as c:
        tables = {row[0] for row in c.execute(text(
            "select table_name from information_schema.tables where table_schema='signals'"))}
    assert {"shadow_signal", "shadow_outcome"} <= tables
    with Session(engine) as session:
        session.execute(delete(ShadowOutcome))
        session.execute(delete(ShadowSignal))
        session.commit()


def test_record_only_blocked_triggered_and_idempotent(engine):
    cleanup(engine)
    try:
        seed(engine)
        with Session(engine) as session:
            first = shadow.record(session, decision_date=DAY)
            session.commit()
            second = shadow.record(session, decision_date=DAY)
            session.commit()
            rows = list(session.scalars(select(ShadowSignal)))
        assert first["inserted"] == 1 and first["total"] == 1
        assert second["inserted"] == 0 and second["total"] == 1
        assert [(r.code, r.risk_level, r.reason_code) for r in rows] == [
            (CODE, "critical", "macro_risk_blocked")]
    finally:
        cleanup(engine)


def test_collect_outcomes_and_idempotent(engine):
    cleanup(engine)
    try:
        seed(engine)
        with Session(engine) as session:
            shadow.record(session, decision_date=DAY)
            session.commit()
            first = shadow.collect(session, as_of=SESSIONS[-1], fetcher=FakeFetcher())
            session.commit()
            second = shadow.collect(session, as_of=SESSIONS[-1], fetcher=FakeFetcher())
            session.commit()
            outcomes = list(session.scalars(select(ShadowOutcome)))
        assert first["benchmark_ok"] is True
        assert first["inserted"] == 5 and first["unfilled"] == 0
        assert second["inserted"] == 0
        assert sorted(row.horizon_sessions for row in outcomes) == [1, 3, 5, 10, 20]
        assert all(row.fill_status == "filled" for row in outcomes)
        assert all(float(row.net_return) > 0 for row in outcomes)
    finally:
        cleanup(engine)


def test_collect_benchmark_failure_reports_attempts(engine):
    cleanup(engine)
    try:
        seed(engine)
        with Session(engine) as session:
            shadow.record(session, decision_date=DAY)
            session.commit()
            out = shadow.collect(session, as_of=SESSIONS[-1], fetcher=BenchmarkDownFetcher())
            session.commit()
        assert out["benchmark_ok"] is False
        assert out["failed"] == 1
        assert out["benchmark_code"] == BENCHMARK
        assert out["benchmark_error"] is None
        assert [attempt["source"] for attempt in out["benchmark_attempts"]] == ["tencent", "sina"]
    finally:
        cleanup(engine)


def test_api_returns_samples_with_outcomes(engine):
    from qp.auth.sessions import create_service_token
    from fastapi.testclient import TestClient
    from qp.api.main import app
    cleanup(engine)
    try:
        seed(engine)
        with Session(engine) as session:
            shadow.record(session, decision_date=DAY)
            shadow.collect(session, as_of=SESSIONS[-1], fetcher=FakeFetcher())
            token = create_service_token(session, f"m12-shadow-{dt.datetime.now().timestamp()}",
                                         ["signals:read"])
            session.commit()
        client = TestClient(app, base_url="https://testserver")
        response = client.get("/api/signals/shadow", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        row = body["rows"][0]
        assert row["code"] == CODE and row["risk_level"] == "critical"
        assert set(row["outcomes"]) == {"1", "3", "5", "10", "20"}
        assert row["outcomes"]["5"]["fill_status"] == "filled"
    finally:
        cleanup(engine)


def test_acceptance_strategy_reports_shadow(engine):
    from qp.signals import acceptance
    cleanup(engine)
    try:
        seed(engine)
        with Session(engine) as session:
            shadow.record(session, decision_date=DAY)
            session.commit()
            out = acceptance.evaluate(session, DAY, today=SESSIONS[-1])
        assert out["strategy"]["shadow"]["samples"] == 1
        assert out["strategy"]["shadow"]["last_sample_date"] == DAY.isoformat()
    finally:
        cleanup(engine)


def test_shadow_jobs_registered():
    assert JOBS["signals.shadow_record"].schedule_hint.startswith("Mon-Fri 15:36 CST")
    assert JOBS["signals.shadow_outcomes"].schedule_hint.startswith("Mon-Fri 16:15 CST")
