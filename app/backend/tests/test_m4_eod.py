"""M4.3 tests: target evaluation, risk gating, storage planning and EOD persistence."""
from __future__ import annotations

import datetime as dt
import json
import time

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.kline import DailyBar, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import (
    EodRun,
    MacroSlice,
    RiskSnapshot,
    RiskState,
    TargetDecision,
    TrendSignal,
)
from qp.signals.eod import (
    Signal,
    TargetEvaluation,
    apply_risk_gate,
    evaluate_target,
    load_risk_override,
    load_risk_state,
    plan_storage,
    record_skipped_run,
    run_eod,
    target_decisions,
    trading_day_status,
)
from qp.signals.universe import TARGETS, Target

DAY = dt.date(2026, 9, 11)  # Friday
MACRO_KEYS = ("total_amount", "margin_balance", "margin_growth_pct", "limit_ratio")


# --------------------------------------------------------------------------- fixtures
def business_days(end: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= dt.timedelta(days=1)
    return list(reversed(days))


def flat_bars(end: dt.date = DAY, count: int = 65, close: float = 10.0, volume: float = 100.0) -> list[DailyBar]:
    return [DailyBar(day, open=close, high=close, low=close, close=close, volume=volume)
            for day in business_days(end, count)]


def breakout_bars(end: dt.date = DAY, last_volume: float = 200.0, last_close: float = 11.0) -> list[DailyBar]:
    bars = flat_bars(end, 65)
    last = bars[-1]
    bars[-1] = DailyBar(last.date, open=10.0, high=last_close, low=10.0, close=last_close, volume=last_volume)
    return bars


def weekly_cross_bars(weeks: int = 36, offset_days: int = 0) -> list[DailyBar]:
    """35 flat weeks then a rising final week -> weekly MACD golden cross."""
    start = dt.date(2026, 1, 5) + dt.timedelta(days=offset_days)  # Monday
    bars: list[DailyBar] = []
    for week in range(weeks):
        monday = start + dt.timedelta(days=7 * week)
        for offset in range(5):
            day = monday + dt.timedelta(days=offset)
            close = 10.0 if week < weeks - 1 else 10.0 + 0.5 * (offset + 1)
            bars.append(DailyBar(day, open=close, high=close, low=close, close=close, volume=100.0))
    return bars


def result(bars: list[DailyBar], source: str = "test", attempts: list[dict] | None = None,
           lag: bool = False) -> KlineResult:
    return KlineResult(bars=bars, source=source, attempts=attempts or [{"source": "test", "rows": len(bars)}],
                       publication_lag=lag)


class FakeFetcher:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[tuple[str, dt.date | None]] = []

    def get(self, code: str, *, required_date: dt.date | None = None, previous_session: dt.date | None = None):
        self.calls.append((code, required_date))
        value = self.mapping[code]
        if isinstance(value, Exception):
            raise value
        return value


def macro_ok(day: dt.date = DAY, **overrides) -> dict:
    status = {name: {"available": True, "source": "test", "message": "", "observed_date": day.isoformat(),
                     "stale": False, "anomalous": False} for name in MACRO_KEYS}
    payload = {"decision_date": day.isoformat(), "total_amount": 12000.0, "margin_balance": 22800.0,
               "margin_growth_pct": 0.5, "limit_up": 60, "limit_down": 2, "limit_ratio": 20.0,
               "source": "test", "collect_status": status}
    payload.update(overrides)
    return payload


def risk(level: str = "ok", message: str = "SYSTEM_OK") -> dict:
    return {"level": level, "message": message, "vix": 15.0, "us_10y": 4.0, "crude_oil": 70.0,
            "sources": {"vix": "cboe"}, "observed_at": {}, "source_timestamps": {},
            "available_at": {}, "cached_fields": [], "missing_fields": []}


# --------------------------------------------------------------------------- evaluation
def test_weekly_cross_signal_details_match_legacy():
    bars = weekly_cross_bars()
    evaluation = evaluate_target(Target("510300", "沪深300ETF", "宽基"), FakeFetcher({"510300": result(bars)}),
                                 decision_date=DAY)
    assert evaluation.data_ok is True
    assert evaluation.data_date == DAY and evaluation.reference_close == 12.5
    assert len(evaluation.signals) == 1
    signal = evaluation.signals[0]
    assert (signal.signal_type, signal.period) == ("weekly_macd_golden_cross", "W")
    assert signal.title == "沪深300ETF 周线 MACD 金叉"
    assert signal.confidence == 0.6
    assert signal.detail == {"data_date": DAY.isoformat(), "dif": round(signal.detail["dif"], 4),
                             "dea": round(signal.detail["dea"], 4), "weekly_close": 12.5}
    assert signal.detail["dif"] > signal.detail["dea"]


def test_flat_bars_do_not_trigger():
    evaluation = evaluate_target(Target("510300", "沪深300ETF", "宽基"),
                                 FakeFetcher({"510300": result(flat_bars())}), decision_date=DAY)
    assert evaluation.data_ok is True and evaluation.signals == []


def test_daily_breakout_disabled_by_default_and_enabled_explicitly():
    target = Target("510300", "沪深300ETF", "宽基")
    bars = breakout_bars()
    default = evaluate_target(target, FakeFetcher({"510300": result(bars)}), decision_date=DAY)
    assert default.signals == []
    enabled = evaluate_target(target, FakeFetcher({"510300": result(bars)}), decision_date=DAY,
                              daily_breakout_enabled=True)
    assert len(enabled.signals) == 1
    signal = enabled.signals[0]
    assert (signal.signal_type, signal.period, signal.confidence) == ("volume_breakout_ma60", "D", 0.55)
    assert signal.title == "沪深300ETF 放量突破 MA60"
    assert signal.detail["data_date"] == DAY.isoformat()
    assert signal.detail["close"] == 11.0
    assert signal.detail["volume_ratio"] == 2.0
    assert signal.detail["volume_threshold"] == 1.5


def test_volume_ratio_rounding_boundary_matches_legacy():
    target = Target("510300", "沪深300ETF", "宽基")
    below = evaluate_target(target, FakeFetcher({"510300": result(breakout_bars(last_volume=149.4))}),
                            decision_date=DAY, daily_breakout_enabled=True)
    assert below.signals == []  # 1.494 -> 1.49
    at = evaluate_target(target, FakeFetcher({"510300": result(breakout_bars(last_volume=149.6))}),
                         decision_date=DAY, daily_breakout_enabled=True)
    assert len(at.signals) == 1  # 1.496 -> 1.50, legacy compares rounded >= 1.5


def test_evaluation_failures_are_classified():
    target = Target("510300", "沪深300ETF", "宽基")
    cases = {
        "fetch_failed:RuntimeError": FakeFetcher({"510300": RuntimeError("boom")}),
        "trend_data_unavailable": FakeFetcher({"510300": result([])}),
        "data_date_mismatch": FakeFetcher({"510300": result(flat_bars(DAY - dt.timedelta(days=1)))}),
        "insufficient_daily_rows": FakeFetcher({"510300": result(flat_bars(count=64))}),
    }
    for expected, fetcher in cases.items():
        evaluation = evaluate_target(target, fetcher, decision_date=DAY)
        assert evaluation.data_ok is False
        assert evaluation.reason == expected, expected
        assert evaluation.signals == []
    lag = evaluate_target(target, FakeFetcher({"510300": result([], lag=True)}), decision_date=DAY)
    assert lag.reason == "data_date_mismatch"


# --------------------------------------------------------------------------- decisions + storage
def evaluation(target: Target, signals: list[Signal] | None = None, ok: bool = True) -> TargetEvaluation:
    return TargetEvaluation(target, ok, DAY if ok else None, 10.0 if ok else None,
                            "test", "" if ok else "trend_data_unavailable", signals or [])


def signal(code="510300", name="沪深300ETF", kind="weekly_macd_golden_cross", period="W",
           data_date=DAY.isoformat()) -> Signal:
    return Signal(code, name, kind, period, f"{name} signal", {"data_date": data_date}, 0.6)


def test_target_decision_states():
    growth = Target("159915", "创业板ETF", "成长")
    broad = Target("510300", "沪深300ETF", "宽基")
    evaluations = [
        evaluation(growth, [signal("159915", "创业板ETF")]),
        evaluation(broad, [signal()]),
        evaluation(Target("512890", "红利低波ETF", "防御")),
        evaluation(Target("518880", "黄金ETF", "避险"), ok=False),
    ]
    blocked = apply_risk_gate(evaluations, "growth_blocked")
    rows = target_decisions(evaluations, blocked, "growth_blocked")
    by_code = {row["code"]: row for row in rows}
    assert (by_code["159915"]["decision_state"], by_code["159915"]["risk_action"]) == ("blocked", "blocked")
    assert by_code["159915"]["reason_code"] == "macro_risk_blocked"
    assert (by_code["510300"]["decision_state"], by_code["510300"]["risk_action"]) == ("allowed", "allowed")
    assert by_code["510300"]["reason_code"] == "signal_allowed"
    assert (by_code["512890"]["decision_state"], by_code["512890"]["reason_code"]) == ("no_trigger", "signal_not_triggered")
    assert (by_code["518880"]["decision_state"], by_code["518880"]["risk_action"]) == ("data_failed", "not_applicable")
    assert by_code["518880"]["reason_code"] == "trend_data_unavailable"
    assert all(row["risk_level"] == "growth_blocked" for row in rows)
    assert all(row["validation_split"] == "live_oos" for row in rows)
    assert by_code["159915"]["raw_trigger_count"] == 1
    assert by_code["159915"]["raw_signals"][0]["signal_type"] == "weekly_macd_golden_cross"


def test_all_unknown_risk_levels_block_everything():
    evaluations = [evaluation(TARGETS[0], [signal()])]
    for level in ("critical", "warning", "unavailable", "unknown", None):
        blocked = apply_risk_gate(evaluations, level)
        rows = target_decisions(evaluations, blocked, str(level))
        assert rows[0]["decision_state"] == "blocked", level


def test_plan_storage_dedup_and_notified_other_date():
    target = TARGETS[0]
    evaluations = [evaluation(target, [signal()])]
    key = ("510300", "weekly_macd_golden_cross", "W", DAY.isoformat())
    plan = plan_storage(evaluations, set(), risk_level="ok", risk_message="ok",
                        existing_other_dates=set(), notified_keys=set())
    assert len(plan.records) == 1 and plan.blocked_count == 0
    assert plan.records[0]["detail"]["risk_gate"] == {"blocked": False, "level": "ok", "message": "ok"}
    assert len(plan.notification_candidates) == 1

    notified = plan_storage(evaluations, set(), risk_level="ok", risk_message="ok",
                            existing_other_dates={key}, notified_keys={key})
    assert notified.records == [] and notified.notification_candidates == []

    # Unnotified identity on another date migrates to today instead of being dropped.
    migrated = plan_storage(evaluations, set(), risk_level="ok", risk_message="ok",
                            existing_other_dates={key}, notified_keys=set())
    assert len(migrated.records) == 1

    blocked_ids = {id(evaluations[0].signals[0])}
    blocked = plan_storage(evaluations, blocked_ids, risk_level="warning", risk_message="DATA_PARTIAL",
                           existing_other_dates=set(), notified_keys=set())
    assert blocked.blocked_count == 1 and blocked.unique_allowed == []
    assert blocked.records[0]["detail"]["risk_gate"]["blocked"] is True


# --------------------------------------------------------------------------- DB-backed EOD
def test_run_eod_persists_ok_path(engine):
    day = DAY
    targets = [Target("510300", "沪深300ETF", "宽基"), Target("512890", "红利低波ETF", "防御")]
    fetcher = FakeFetcher({"510300": result(weekly_cross_bars()), "512890": result(flat_bars())})
    with Session(engine) as session:
        out = run_eod(session, decision_date=day, fetcher=fetcher, macro_slice=macro_ok(day),
                      risk_state=risk(), targets=targets, now=time.time())
        session.commit()

    assert out["status"] == "success" and out["outcome"] == "emitted"
    assert (out["target_count"], out["evaluated_count"], out["failed_count"]) == (2, 2, 0)
    assert out["signal_count"] == 1 and out["allowed_signal_count"] == 1
    assert out["blocked_signal_count"] == 0

    with Session(engine) as session:
        run = session.get(EodRun, out["run_id"])
        assert run.attempt_no == 1 and run.status == "success" and run.decision_outcome == "emitted"
        assert run.signal_count == 1 and run.allowed_signal_count == 1 and run.blocked_signal_count == 0
        assert run.details["macro"]["source"] == "test"
        assert run.details["risk_state"]["level"] == "ok"

        decisions = session.execute(select(TargetDecision).where(TargetDecision.run_id == run.run_id)
                                    .order_by(TargetDecision.code)).scalars().all()
        assert len(decisions) == 2
        by_code = {row.code: row for row in decisions}
        assert by_code["510300"].decision_state == "allowed"
        assert by_code["510300"].risk_level == "ok"
        assert float(by_code["510300"].reference_close) == 12.5
        assert by_code["512890"].decision_state == "no_trigger"

        signals = session.execute(select(TrendSignal).where(TrendSignal.decision_date == day)).scalars().all()
        assert len(signals) == 1
        assert signals[0].code == "510300"
        assert signals[0].data_date == day
        assert signals[0].notified_at is None
        assert signals[0].detail["risk_gate"]["blocked"] is False

        snapshot = session.execute(select(RiskSnapshot).where(RiskSnapshot.run_id == run.run_id)).scalar_one()
        assert snapshot.level == "ok" and float(snapshot.values["vix"]) == 15.0

        macro = session.get(MacroSlice, day)
        assert macro is not None and float(macro.total_amount) == 12000.0
        assert macro.source == "test" and macro.limit_up == 60


def test_run_eod_blocked_records_signals_but_blocks_notification(engine):
    bars = weekly_cross_bars(offset_days=-28)  # distinct decision date per test database
    day = bars[-1].date
    targets = [Target("510300", "沪深300ETF", "宽基")]
    fetcher = FakeFetcher({"510300": result(bars)})
    with Session(engine) as session:
        out = run_eod(session, decision_date=day, fetcher=fetcher, macro_slice=macro_ok(day),
                      risk_state=risk("warning", "DATA_PARTIAL: crude_oil"), targets=targets)
        session.commit()
    assert (out["status"], out["outcome"]) == ("success", "blocked")
    assert out["signal_count"] == 1 and out["blocked_signal_count"] == 1
    with Session(engine) as session:
        stored = session.execute(select(TrendSignal).where(TrendSignal.decision_date == day)).scalar_one()
        assert stored.detail["risk_gate"]["blocked"] is True
        decision = session.execute(select(TargetDecision).where(TargetDecision.run_id == out["run_id"])).scalar_one()
        assert (decision.decision_state, decision.risk_action) == ("blocked", "blocked")
        assert decision.reason_code == "macro_risk_blocked"


def test_run_eod_failed_targets(engine):
    day = DAY - dt.timedelta(days=56)
    targets = [Target("510300", "沪深300ETF", "宽基"), Target("512890", "红利低波ETF", "防御")]
    fetcher = FakeFetcher({"510300": result([]), "512890": result([])})
    with Session(engine) as session:
        out = run_eod(session, decision_date=day, fetcher=fetcher, macro_slice=macro_ok(day),
                      risk_state=risk(), targets=targets)
        session.commit()
    assert (out["status"], out["outcome"]) == ("failed", "data_unavailable")
    assert out["failed_count"] == 2 and out["signal_count"] == 0
    with Session(engine) as session:
        decisions = session.execute(select(TargetDecision).where(TargetDecision.run_id == out["run_id"])).scalars().all()
        assert {row.reason_code for row in decisions} == {"trend_data_unavailable"}
        run = session.get(EodRun, out["run_id"])
        assert run.reason_codes == ["trend_data_unavailable"]


def test_run_eod_rerun_is_idempotent(engine):
    bars = weekly_cross_bars(offset_days=-84)
    day = bars[-1].date
    targets = [Target("510300", "沪深300ETF", "宽基")]
    fetcher = FakeFetcher({"510300": result(bars)})
    with Session(engine) as session:
        first = run_eod(session, decision_date=day, fetcher=fetcher, macro_slice=macro_ok(day),
                        risk_state=risk(), targets=targets)
        session.commit()
    with Session(engine) as session:
        second = run_eod(session, decision_date=day, fetcher=fetcher, macro_slice=macro_ok(day),
                         risk_state=risk(), targets=targets)
        session.commit()
    assert (first["run_id"] != second["run_id"])
    with Session(engine) as session:
        attempts = list(session.execute(select(EodRun.attempt_no).where(EodRun.decision_date == day)
                                        .order_by(EodRun.attempt_no)).scalars())
        assert attempts == [1, 2]
        assert session.scalar(select(func.count()).select_from(TrendSignal)
                              .where(TrendSignal.decision_date == day)) == 1
        assert session.scalar(select(func.count()).select_from(TargetDecision)
                              .where(TargetDecision.decision_date == day)) == 2


def test_notified_other_date_signal_is_not_recounted(engine):
    bars = weekly_cross_bars(offset_days=700)  # unique data_date so later tests are unaffected
    day = bars[-1].date
    targets = [Target("510300", "沪深300ETF", "宽基")]
    key_date = day.isoformat()
    with Session(engine) as session:
        session.add(TrendSignal(decision_date=day - dt.timedelta(days=7), code="510300",
                                name="沪深300ETF", signal_type="weekly_macd_golden_cross", period="W",
                                data_date=day, title="old", detail={"data_date": key_date},
                                confidence=0.6, notified_at=dt.datetime.now(dt.UTC)))
        session.commit()
    with Session(engine) as session:
        out = run_eod(session, decision_date=day, fetcher=FakeFetcher({"510300": result(bars)}),
                      macro_slice=macro_ok(day), risk_state=risk(), targets=targets)
        session.commit()
    assert out["signal_count"] == 0 and out["notification_candidates"] == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TrendSignal)
                              .where(TrendSignal.decision_date == day)) == 0


# --------------------------------------------------------------------------- calendar
def test_trading_day_status_prefers_covering_calendar(engine):
    closed_day = dt.date(2098, 6, 3)  # Wednesday, inside a calendar that ends later
    with Session(engine) as session:
        session.execute(pg_insert(TradingDay).values(origin="qp", date=dt.date(2099, 12, 31),
                                                     is_open=False, source="test")
                        .on_conflict_do_nothing(index_elements=["origin", "date"]))
        session.commit()
    with Session(engine) as session:
        assert trading_day_status(session, closed_day) == (False, "qp_calendar_no_row")
        assert trading_day_status(session, dt.date(2099, 12, 31)) == (False, "qp_calendar")
        # Beyond the calendar's last stored day the status falls back to weekdays.
        weekend = dt.date(2100, 1, 2)
        assert weekend.weekday() == 5
        assert trading_day_status(session, weekend) == (False, "weekday_fallback")
        assert trading_day_status(session, dt.date(2100, 1, 4)) == (True, "weekday_fallback")
    with Session(engine) as session:
        session.execute(delete(TradingDay).where(TradingDay.source == "test",
                                                 TradingDay.date == dt.date(2099, 12, 31)))
        session.commit()


def test_record_skipped_run(engine):
    day = dt.date(2097, 5, 1)  # May Day; never used by other tests
    with Session(engine) as session:
        out = record_skipped_run(session, day, source="qp_calendar")
        session.commit()
    assert (out["status"], out["outcome"]) == ("skipped", "skipped")
    assert out["reason_codes"] == ["non_trading_day"]
    with Session(engine) as session:
        run = session.get(EodRun, out["run_id"])
        assert run.status == "skipped" and run.decision_outcome == "skipped"
        assert run.details == {"calendar_source": "qp_calendar"}
        assert run.finished_at is not None


# --------------------------------------------------------------------------- risk state loading
def test_risk_override_file_priority_and_expiry(tmp_path):
    now = time.time()
    path = tmp_path / "override.json"
    path.write_text(json.dumps({"level": "critical", "message": "override", "expires_at": now + 60}))
    state = load_risk_override(path, now=now)
    assert state["level"] == "critical" and state["override"] is True
    path.write_text(json.dumps({"level": "critical", "expires_at": now - 1}))
    assert load_risk_override(path, now=now) is None
    path.write_text("not json")
    assert load_risk_override(path, now=now) is None


def test_load_risk_state_override_wins_without_fetch(engine, tmp_path):
    path = tmp_path / "override.json"
    path.write_text(json.dumps({"level": "growth_blocked", "message": "US10Y", "expires_at": time.time() + 60}))

    def boom(*a, **k):
        raise AssertionError("fetch must not run when an override is active")

    with Session(engine) as session:
        state = load_risk_state(session, fetch_fn=boom, override_path=path)
    assert state["level"] == "growth_blocked" and state["override"] is True


def test_load_risk_state_reuses_fresh_row(engine):
    now = time.time()
    with Session(engine) as session:
        session.add(RiskState(observed_at=dt.datetime.fromtimestamp(now - 30, tz=dt.UTC),
                              level="ok", values={"vix": 15.0, "us_10y": 4.0, "crude_oil": 70.0},
                              sources={"vix": "cboe"}, override=False, message="SYSTEM_OK",
                              raw={"level": "ok", "message": "SYSTEM_OK", "vix": 15.0, "us_10y": 4.0,
                                   "crude_oil": 70.0, "sources": {"vix": "cboe"}, "observed_at": {},
                                   "source_timestamps": {}, "available_at": {}, "cached_fields": [],
                                   "missing_fields": []}))
        session.commit()

    def boom(*a, **k):
        raise AssertionError("fetch must not run for a fresh published state")

    with Session(engine) as session:
        state = load_risk_state(session, now=now, fetch_fn=boom, override_path="/nonexistent/override.json")
    assert state["level"] == "ok" and state["override"] is False


def test_load_risk_state_refreshes_when_stale_and_fails_closed(engine, monkeypatch):
    now = time.time() + 10_000  # outlive the fresh row inserted by the previous test
    with Session(engine) as session:
        session.add(RiskState(observed_at=dt.datetime.fromtimestamp(now - 600, tz=dt.UTC),
                              level="ok", values={}, sources={}, override=False, message="old", raw={}))
        session.commit()

    fetched = {"values": {"vix": 18.0, "us_10y": 4.6, "crude_oil": 70.0},
               "sources": {"vix": "cboe", "us_10y": "fred_dgs10_daily", "crude_oil": "yahoo"},
               "source_timestamps": {"vix": now - 3600, "us_10y": now - 86400, "crude_oil": now - 600},
               "observed_at": {"vix": now, "us_10y": now, "crude_oil": now},
               "available_at": {"vix": now, "us_10y": now, "crude_oil": now},
               "missing_fields": [], "fetched_at": now}
    from qp.signals import eod as eod_module
    monkeypatch.setattr(eod_module, "fetch_risk_values", lambda fetch_fn, now=None: fetched)
    with Session(engine) as session:
        state = load_risk_state(session, now=now, fetch_fn=lambda *a, **k: fetched,
                                override_path="/nonexistent/override.json")
        assert state["level"] == "critical" and state["vix"] == 18.0
        session.commit()
        rows = session.scalar(select(func.count()).select_from(RiskState).where(RiskState.observed_at
                                                                                 >= dt.datetime.fromtimestamp(now - 1, tz=dt.UTC)))
    assert rows == 1

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(eod_module, "fetch_risk_values", boom)
    with Session(engine) as session:
        state = load_risk_state(session, now=now + 10_000, fetch_fn=boom,
                                override_path="/nonexistent/override.json")
    assert state["level"] == "unavailable"
    assert state["message"].startswith("RISK_REFRESH_FAILED")


def test_run_eod_with_critical_risk_blocks_everything(engine):
    bars = weekly_cross_bars(offset_days=-140)
    day = bars[-1].date
    targets = [Target("510300", "沪深300ETF", "宽基")]
    with Session(engine) as session:
        out = run_eod(session, decision_date=day, fetcher=FakeFetcher({"510300": result(bars)}),
                      macro_slice=macro_ok(day), risk_state=risk("critical", "CRITICAL_WARNING"),
                      targets=targets)
        session.commit()
    assert out["risk_level"] == "critical"
    assert (out["status"], out["outcome"]) == ("success", "blocked")
    assert out["blocked_signal_count"] == 1


def test_run_eod_dry_run_writes_nothing(engine):
    bars = weekly_cross_bars(offset_days=-168)
    day = bars[-1].date
    targets = [Target("510300", "沪深300ETF", "宽基")]
    before = None
    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(EodRun))
        out = run_eod(session, decision_date=day, fetcher=FakeFetcher({"510300": result(bars)}),
                      macro_slice=macro_ok(day), risk_state=risk(), targets=targets, dry_run=True)
        assert out["run_id"] is None
        session.rollback()
        after = session.scalar(select(func.count()).select_from(EodRun))
    assert before == after
    assert out["signal_count"] == 1
