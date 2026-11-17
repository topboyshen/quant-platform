"""C13 forward paper portfolio: time/ledger/isolation contract, not profit fitting."""

from __future__ import annotations

import copy
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from qp.market.kline import DailyBar, KlineResult
from qp.models.market import TradingDay
from qp.models.signals import (
    RotationShadowAccount,
    RotationShadowBar,
    RotationShadowDay,
    RotationShadowRun,
)
from qp.signals import rotation_shadow as rot

CODES = tuple(rot.CODES[:10])
_INIT_LOCK = Lock()
START = dt.date(2027, 7, 5)


def weekdays(start: dt.date, count: int) -> list[dt.date]:
    days = []
    while len(days) < count:
        if start.weekday() < 5:
            days.append(start)
        start += dt.timedelta(days=1)
    return days


HISTORY = [d for d in weekdays(START - dt.timedelta(days=470), 340) if d < START][-330:]
DAYS = weekdays(START, 47)
BASE = HISTORY[-1]


@pytest.fixture
def book(engine, monkeypatch):
    from zoneinfo import ZoneInfo

    # Advance deliberately synthetic fixture time; production cannot backdate forward data.
    monkeypatch.setattr(rot, "now_cst", lambda: dt.datetime(2027, 12, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai")))

    def cleanup():
        with Session(engine) as s:
            s.execute(delete(RotationShadowRun))
            s.execute(delete(RotationShadowDay))
            s.execute(delete(RotationShadowAccount))
            s.execute(delete(RotationShadowBar))
            s.execute(delete(TradingDay).where(TradingDay.source == "m14-test"))
            s.commit()

    cleanup()
    payload = {
        c: [
            [
                str(day),
                2 + k * 0.1 + i * (0.001 + k * 0.0002),
                2.001 + k * 0.1 + i * (0.001 + k * 0.0002),
                2.01 + k * 0.1 + i * (0.001 + k * 0.0002),
                1.99 + k * 0.1 + i * (0.001 + k * 0.0002),
                100_000.0,
            ]
            for i, day in enumerate([*HISTORY, *DAYS])
        ]
        for k, c in enumerate(CODES)
    }
    with Session(engine) as s:
        # Earlier chain tests seed every calendar day as open. Explicitly close
        # weekends in this fixture range, so warm-up completeness is truly isolated.
        cursor = HISTORY[0]
        while cursor <= DAYS[-1]:
            s.merge(TradingDay(origin="qp", date=cursor, is_open=cursor.weekday() < 5, source="m14-test"))
            cursor += dt.timedelta(days=1)
        rot.seed_bars(s, payload)
        s.commit()
    yield engine, payload
    cleanup()


def init(s, *, phase="forward", rule=rot.RULE):
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    stamp = dt.datetime.combine(BASE, dt.time(16), ZoneInfo("Asia/Shanghai"))
    with _INIT_LOCK, patch.object(rot, "now_cst", return_value=stamp):
        return rot.initialize(s, start_date=START, phase=phase, rule_version=rule, codes=CODES)


def snapshots(s, rule=rot.RULE):
    return [
        (
            str(r.trade_date),
            r.session_index,
            float(r.nav),
            float(r.cash),
            r.positions,
            float(r.ew_nav),
            float(r.benchmark_nav),
            r.detail["state"],
        )
        for r in s.scalars(
            select(RotationShadowDay)
            .where(RotationShadowDay.rule_version == rule)
            .order_by(RotationShadowDay.trade_date)
        )
    ]


def test_tables_and_migration_match(engine):
    with engine.connect() as c:
        names = set(
            c.execute(text("select table_name from information_schema.tables where table_schema='signals'")).scalars()
        )
    assert {"rotation_shadow_account", "rotation_shadow_bar", "rotation_shadow_day", "rotation_shadow_run"} <= names


def test_seed_strict_idempotent_and_raw_only(book):
    engine, payload = book
    with Session(engine) as s:
        out = rot.seed_bars(s, payload)
        assert out["ok"] and out["bars"] == 0
        revised = copy.deepcopy(payload)
        revised[CODES[0]][-1][2] += 0.1
        with pytest.raises(rot.DataIssue, match="raw_history_revision"):
            rot.seed_bars(s, revised)
        invalid = copy.deepcopy(payload)
        invalid[CODES[0]][-1][2] = float("nan")
        with pytest.raises(rot.DataIssue, match="invalid_raw_bar"):
            rot.seed_bars(s, invalid)


def test_selection_uses_only_signal_day_and_fixed_top8(book):
    engine, _ = book
    with Session(engine) as s:
        frames = rot._load_frames(s, until=DAYS[-1], codes=CODES)
        selected, universe = rot._selection(frames, BASE)
        assert universe == len(CODES)
        assert len(selected) == 8
        assert sum(x["weight"] for x in selected) == 1.0
        original = list(selected)
        f = frames[CODES[0]]
        f.closes[-1] *= 100
        f.amount20[-1] = 1e20
        assert rot._selection(frames, BASE)[0] == original
        assert selected[0]["score"] >= selected[-1]["score"]


def test_newest_close_plan_is_persisted_and_not_executed_early(book):
    engine, _ = book
    with Session(engine) as s:
        out = init(s)
        assert out["ok"]
        s.commit()
        summary = rot.summary(s)
        assert summary["days"] == 0 and summary["nav"] == rot.INITIAL
        assert summary["positions"] == []
        assert summary["pending"]["signal_date"] == str(BASE)
        assert summary["pending"]["execute_date"] == str(START)
        assert rot.replay(s, until=BASE)["skipped"] == "up_to_date"
        assert rot.replay(s, until=DAYS[0])["runs_executed"] == 1
        s.commit()
        plan = s.scalar(select(RotationShadowRun))
        assert plan.status == "executed" and plan.execute_date == DAYS[0]
        assert plan.fills
        after = rot.summary(s)
        assert after["days"] == 1 and after["positions"]
        assert after["metrics"]["cagr"] is None
        assert after["next_rebalance_date"] == str(DAYS[19])
        assert after["next_execute_date"] == str(DAYS[20])


def test_batch_incremental_replay_are_identical_and_idempotent(book):
    engine, _ = book
    with Session(engine) as s:
        assert init(s, rule="batch")["ok"]
        assert init(s, rule="incremental")["ok"]
        s.commit()
        assert rot.replay(s, rule_version="batch", until=DAYS[-1])["ok"]
        s.commit()
        for day in DAYS:
            assert rot.replay(s, rule_version="incremental", until=day)["ok"]
            s.commit()
        assert snapshots(s, "batch") == snapshots(s, "incremental")
        runs = list(
            s.scalars(
                select(RotationShadowRun)
                .where(RotationShadowRun.rule_version == "incremental")
                .order_by(RotationShadowRun.signal_date)
            )
        )
        assert [(p.signal_date, p.execute_date) for p in runs] == [
            (BASE, DAYS[0]),
            (DAYS[19], DAYS[20]),
            (DAYS[39], DAYS[40]),
        ]
        original = snapshots(s, "batch")
        assert rot.replay(s, rule_version="batch", until=DAYS[-1])["skipped"] == "up_to_date"
        s.commit()
        assert snapshots(s, "batch") == original


def test_last_rebalance_day_does_not_require_future_market_bars(book):
    engine, _ = book
    with Session(engine) as s:
        assert init(s)["ok"]
        s.commit()
        assert rot.replay(s, until=DAYS[19])["ok"]
        s.commit()
        pending = s.scalar(select(RotationShadowRun).where(RotationShadowRun.status == "pending"))
        assert pending.signal_date == DAYS[19] and pending.execute_date == DAYS[20]
        selected = copy.deepcopy(pending.selected)
        # Next close changes ranks, but the prior close plan is immutable.
        bar = s.get(RotationShadowBar, (CODES[0], DAYS[20]))
        bar.close = float(bar.close) * 1.1
        bar.high = max(float(bar.high), float(bar.close))
        s.commit()
        assert rot.replay(s, until=DAYS[20])["ok"]
        s.commit()
        assert pending.selected == selected and pending.status == "executed"


def test_missing_bar_blocks_nav_and_recovers_without_clock_drift(book):
    engine, payload = book
    with Session(engine) as s:
        assert init(s)["ok"]
        s.commit()
        s.execute(
            delete(RotationShadowBar).where(
                RotationShadowBar.code == CODES[-1], RotationShadowBar.trade_date == DAYS[0]
            )
        )
        s.commit()
        out = rot.replay(s, until=DAYS[0])
        s.commit()
        assert out["ok"] is False and CODES[-1] in out["missing_codes"]
        assert rot.summary(s)["days"] == 0
        assert rot.summary(s)["pending"]["status"] == "pending"
        assert rot.summary(s)["quality"]["status"] == "blocked"
        row = next(r for r in payload[CODES[-1]] if r[0] == str(DAYS[0]))
        rot.seed_bars(s, {CODES[-1]: [row]})
        assert rot.replay(s, until=DAYS[0])["runs_executed"] == 1
        s.commit()
        assert rot.summary(s)["days"] == 1


def test_price_discontinuity_never_fabricates_zero_pnl(book):
    engine, _ = book
    with Session(engine) as s:
        assert init(s)["ok"]
        s.commit()
        row = s.get(RotationShadowBar, (CODES[0], DAYS[0]))
        row.open = float(row.open) / 2
        row.high = float(row.high) / 2
        row.low = float(row.low) / 2
        row.close = float(row.close) / 2
        s.commit()
        out = rot.replay(s, until=DAYS[0])
        s.commit()
        assert out["ok"] is False and "跳变" in out["error"]
        assert rot.summary(s)["days"] == 0


def test_trade_sells_reductions_first_and_uses_sell_slippage():
    bars = {c: DailyBar(START, 10, 10.1, 9.9, 10, 100_000) for c in ("a", "z")}
    shares = {"z": 100_000}
    cash, fills, traded, before = rot._trade(0, shares, {"a": 0.5, "z": 0.5}, bars)
    assert [f["side"] for f in fills] == ["sell", "buy"]
    assert fills[0]["price"] == pytest.approx(10 * (1 - rot.SLIPPAGE))
    assert fills[1]["price"] == pytest.approx(10 * (1 + rot.SLIPPAGE))
    assert shares["a"] > 49_000 and shares["z"] < 51_000
    assert cash >= 0 and before == 1_000_000
    assert rot._mark(cash, shares, bars) < before


def test_summary_marks_match_nav_and_benchmarks_are_costed(book):
    engine, _ = book
    with Session(engine) as s:
        init(s)
        rot.replay(s, until=DAYS[2])
        s.commit()
        out = rot.summary(s)
        assert out["phase"] == "forward" and out["days"] == 3
        assert out["cash"] + sum(p["value"] for p in out["positions"]) == pytest.approx(out["nav"], abs=0.001)
        assert rot.summary(s, phase="replay")["series"] == []
        run = out["runs"][0]
        assert run["detail"]["ew_fills"] and run["detail"]["benchmark_fills"]
        assert out["ew"]["nav"] is not None


def test_locked_registration_blocks_parameter_changes(book):
    engine, _ = book
    with Session(engine) as s:
        init(s)
        s.commit()
        account = s.get(RotationShadowAccount, (rot.RULE, "forward"))
        account.parameters = {**account.parameters, "top_k": 3}
        s.commit()
        out = rot.replay(s, until=DAYS[0])
        assert out["ok"] is False and out["error"] == "registered_parameters_changed"


def test_registration_and_advance_serialized_across_sessions(book):
    engine, _ = book
    with Session(engine) as s:
        assert init(s)["ok"]
        s.commit()

    def run():
        with Session(engine) as s:
            out = {"ok": True}
            result = rot.replay(s, until=DAYS[1])
            s.commit()
            return out, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: run(), range(2)))
    assert all(result[1]["ok"] for result in outputs)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(RotationShadowRun)) == 1
        assert s.scalar(select(func.count()).select_from(RotationShadowDay)) == 3


def test_api_auth_scope_phase_and_empty(book):
    from fastapi.testclient import TestClient

    from qp.api.main import app
    from qp.auth.sessions import create_service_token

    engine, _ = book
    with Session(engine) as s:
        init(s)
        token = create_service_token(s, f"m14-{dt.datetime.now().timestamp()}", ["signals:read"])
        other = create_service_token(s, f"m14-other-{dt.datetime.now().timestamp()}", ["ops:read"])
        s.commit()
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/signals/rotation-shadow").status_code == 401
    assert client.get("/api/signals/rotation-shadow", headers={"authorization": f"Bearer {other}"}).status_code == 403
    headers = {"authorization": f"Bearer {token}"}
    data = client.get("/api/signals/rotation-shadow", headers=headers).json()
    assert data["days"] == 0 and data["pending"] and data["series"]
    assert data["parameters"]["top_k"] == 8
    assert client.get("/api/signals/rotation-shadow?phase=replay", headers=headers).json()["series"] == []
    assert client.get("/api/signals/rotation-shadow?phase=no", headers=headers).status_code == 422


def test_intraday_step_refuses_before_fetch_or_nav(book, monkeypatch):
    from zoneinfo import ZoneInfo

    engine, _ = book

    class MustNotFetch:
        def get(self, code):
            raise AssertionError("intraday fetch must not occur")

    with Session(engine) as s:
        init(s)
        s.commit()
        monkeypatch.setattr(
            rot, "now_cst", lambda: dt.datetime.combine(DAYS[0], dt.time(10), ZoneInfo("Asia/Shanghai"))
        )
        result = rot.step(s, as_of=DAYS[0], fetcher=MustNotFetch())
        assert result == {"ok": False, "error": "session_not_closed"}
        assert rot.summary(s)["days"] == 0
        assert rot.replay(s, until=DAYS[0])["error"] == "session_not_closed"


def test_suspended_zero_volume_no_fabricated_fills(book):
    engine, _ = book
    with Session(engine) as s:
        init(s)
        s.commit()
        bar = s.get(RotationShadowBar, (CODES[0], DAYS[0]))
        bar.volume = 0
        s.commit()
        result = rot.replay(s, until=DAYS[0])
        assert result["ok"] is False and "零成交量" in result["error"]
        assert rot.summary(s)["pending"]["fills"] == []
        assert rot.summary(s)["days"] == 0


def test_sync_recovers_interior_publication_gap(book):
    engine, payload = book
    code = CODES[0]

    class Fetcher:
        def get(self, _):
            rows = [r for r in payload[code] if r[0] in (str(BASE), str(DAYS[0]), str(DAYS[1]))]
            return KlineResult(
                bars=[DailyBar(dt.date.fromisoformat(r[0]), r[1], r[3], r[4], r[2], r[5]) for r in rows], source="test"
            )

    with Session(engine) as s:
        init(s)
        s.commit()
        s.execute(
            delete(RotationShadowBar).where(RotationShadowBar.code == code, RotationShadowBar.trade_date == DAYS[0])
        )
        s.commit()
        assert s.get(RotationShadowBar, (code, DAYS[1]))
        result = rot.sync_bars(s, as_of=DAYS[1], codes=[code], fetcher=Fetcher())
        assert result["ok"] and result["bars"] == 1
        assert s.get(RotationShadowBar, (code, DAYS[0]))


def test_interior_calendar_hole_blocks_rebalance_clock(book):
    engine, _ = book
    with Session(engine) as s:
        init(s)
        rot.replay(s, until=DAYS[0])
        s.commit()
        s.execute(delete(TradingDay).where(TradingDay.origin == "qp", TradingDay.date == DAYS[1]))
        s.commit()
        result = rot.replay(s, until=DAYS[19])
        assert result["ok"] is False and str(DAYS[1]) in result["missing_dates"]
        assert rot.summary(s)["days"] == 1
        benchmark_bar = s.get(RotationShadowBar, (rot.BENCHMARK, DAYS[1]))
        saved = {
            rot.BENCHMARK: [
                [
                    str(DAYS[1]),
                    float(benchmark_bar.open),
                    float(benchmark_bar.close),
                    float(benchmark_bar.high),
                    float(benchmark_bar.low),
                    float(benchmark_bar.volume),
                ]
            ]
        }
        s.delete(benchmark_bar)
        s.commit()
        # Missing benchmark and calendar together cannot hide other ETF evidence.
        assert rot.replay(s, until=DAYS[19])["ok"] is False
        assert rot.summary(s)["days"] == 1
        rot.seed_bars(s, saved)
        s.add(TradingDay(origin="qp", date=DAYS[1], is_open=True, source="m14-test"))
        assert rot.replay(s, until=DAYS[19])["ok"]
        s.commit()
        assert rot.summary(s)["days"] == 20
        assert rot.summary(s)["pending"]["signal_date"] == str(DAYS[19])


def test_warmup_gap_not_stretched_to_older_momentum_anchor(book):
    engine, _ = book
    with Session(engine) as s:
        s.execute(
            delete(RotationShadowBar).where(
                RotationShadowBar.code == CODES[0], RotationShadowBar.trade_date == HISTORY[-20]
            )
        )
        result = init(s)
        assert result["ok"] is False and result["error"] == "ranking_warmup_session_gap"
        assert CODES[0] in result["missing_codes"]


def test_job_cli_and_timer_registration():
    from pathlib import Path

    from qp.cli import main
    from qp.jobs.registry import JOBS

    assert JOBS["signals.rotation_shadow"].schedule_hint.startswith("Mon-Fri 15:50, 16:40")
    timer = Path(__file__).resolve().parents[2] / "deploy/systemd/qp-signals-rotation-shadow.timer"
    assert "15:50:00 Asia/Shanghai" in timer.read_text()
    assert "16:40:00 Asia/Shanghai" in timer.read_text()
    with pytest.raises(SystemExit) as out:
        main(["signals", "rotation-shadow", "--help"])
    assert out.value.code == 0
