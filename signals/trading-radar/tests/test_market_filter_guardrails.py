"""大盘环境过滤器的数据时点、完整性与状态语义回归测试。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import radar.swing.macro_snapshot as macro_module
from dashboard.swing_view import load_swing_overview
from radar.swing.macro_snapshot import (
    MacroCollectStatus,
    MacroSnapshotCollector,
)
from radar.swing.store import NUMERIC_FACTORS, MacroSlice, SwingStore

_BEIJING = ZoneInfo("Asia/Shanghai")


def test_eod_dry_run_does_not_allow_macro_persistence(tmp_path, monkeypatch):
    """EOD dry-run 必须把 persist=False 明确传给宏观采集器。"""
    import radar_swing_eod as eod

    db_path = tmp_path / "swing_macro.db"
    config = {
        "swing": {
            "store_file": str(db_path),
            "macro": {},
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "history_days": 250,
        },
        "market": {},
        "telegram": {"bot_token": "test", "chat_id": "1"},
        "targets": [],
    }
    persist_flags = []

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def is_trading_day():
            return True

    class _Collector:
        def __init__(self, *_args, **_kwargs):
            pass

        def collect(self, *, persist=True):
            persist_flags.append(persist)
            return MacroSlice(date=SwingStore.today_str(), source="test")

        @staticmethod
        def detect_extremes(_slice):
            return []

        @staticmethod
        def get_collect_status():
            return {}

    class _Strategy:
        last_data_ok = True

        def __init__(self, *_args, **_kwargs):
            pass

    class _Notifier:
        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(eod, "_read_config", lambda _path: config)
    monkeypatch.setattr(eod, "validate_swing_config_dict", lambda _data: None)
    monkeypatch.setattr(eod, "_setup_logging", lambda _path: logging.getLogger(__name__))
    monkeypatch.setattr(eod, "_check_runtime_dependencies", lambda *_args: {})
    monkeypatch.setattr(eod, "ScanScheduler", _Scheduler)
    monkeypatch.setattr(eod, "MacroSnapshotCollector", _Collector)
    monkeypatch.setattr(eod, "MarketDataFetcher", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(eod, "TrendConfirmStrategy", _Strategy)
    monkeypatch.setattr(eod, "TelegramNotifier", _Notifier)

    result = eod.run("unused.json", dry_run=True)

    assert result["skipped"] is False
    assert persist_flags == [False]
    assert SwingStore(str(db_path)).get_latest() is None


def test_collector_rejects_historical_date_before_current_only_sources(tmp_path, monkeypatch):
    """实时快照采集器不得把今天的数据贴到任意历史日期。"""
    now = dt.datetime(2026, 7, 16, 16, 0, tzinfo=_BEIJING)
    monkeypatch.setattr(macro_module, "_beijing_now", lambda: now)
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))
    total = MagicMock(return_value=25000.0)
    margin = MagicMock(return_value=(29000.0, 0.1))
    limit_pool = MagicMock(return_value=(80, 20))
    monkeypatch.setattr(collector, "_collect_total_amount", total)
    monkeypatch.setattr(collector, "_collect_margin", margin)
    monkeypatch.setattr(collector, "_collect_limit_pool", limit_pool)

    with pytest.raises(ValueError):
        collector.collect(date="2026-07-01", persist=False)

    total.assert_not_called()
    margin.assert_not_called()
    limit_pool.assert_not_called()


@pytest.mark.parametrize("factor", NUMERIC_FACTORS)
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_factor_never_enters_extremes(tmp_path, factor, bad_value):
    """非有限行情不能被误判为 0/100 分位的冰点或沸点。"""
    store = SwingStore(str(tmp_path / "macro.db"))
    for index in range(20):
        values = {name: 1.0 + index for name in NUMERIC_FACTORS}
        store.upsert(MacroSlice(date=f"2026-06-{index + 1:02d}", **values))
    collector = MacroSnapshotCollector(store, min_history_days=20)

    current = MacroSlice(date="2026-07-16", **{factor: bad_value})

    assert collector.detect_extremes(current) == []


def test_limit_pool_requires_same_day_pair_and_backtracks(tmp_path, monkeypatch):
    """当日只有单侧股池时，应回溯到最近一个双侧均完整的交易日。"""
    now = dt.datetime(2026, 7, 15, 16, 0, tzinfo=_BEIJING)
    monkeypatch.setattr(macro_module, "_beijing_now", lambda: now)
    calls = []
    fake_akshare = ModuleType("akshare")

    def limit_up(date):
        calls.append(("up", date))
        size = 5 if date == "20260715" else 7
        return pd.DataFrame({"code": [f"U{i}" for i in range(size)]})

    def limit_down(date):
        calls.append(("down", date))
        if date == "20260715":
            raise RuntimeError("当日跌停池尚未就绪")
        return pd.DataFrame({"code": ["D1", "D2"]})

    fake_akshare.stock_zt_pool_em = limit_up
    fake_akshare.stock_zt_pool_dtgc_em = limit_down
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))
    sources = []

    up, down = collector._collect_limit_pool(sources)

    assert (up, down) == (7, 2)
    assert ("up", "20260715") in calls
    assert ("down", "20260715") in calls
    assert ("up", "20260714") in calls
    assert ("down", "20260714") in calls
    assert sources == ["akshare"]


def test_margin_growth_same_day_rerun_excludes_current_slice(tmp_path, monkeypatch):
    """同日重跑必须相对前一日计算，不能拿当天旧记录当作前值。"""
    today = dt.date.fromisoformat(SwingStore.today_str())
    previous = today - dt.timedelta(days=1)
    fixed_now = dt.datetime.combine(today, dt.time(16, 0), tzinfo=_BEIJING)
    monkeypatch.setattr(macro_module, "_beijing_now", lambda: fixed_now)
    store = SwingStore(str(tmp_path / "macro.db"))
    store.upsert(MacroSlice(date=previous.isoformat(), margin_balance=100.0))
    store.upsert(MacroSlice(date=today.isoformat(), margin_balance=110.0))
    collector = MacroSnapshotCollector(store)

    growth = collector._margin_growth_from_store(111.0)

    assert growth == pytest.approx(11.0)


def test_collect_status_is_reset_for_every_collection(tmp_path, monkeypatch):
    """复用 Collector 时，上一轮成功状态不得泄漏到下一轮。"""
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))

    def first_total(_sources):
        collector._set_status("total_amount", True, "first")
        return 25000.0

    def first_margin(_sources):
        collector._set_status("margin_balance", True, "first")
        collector._set_status("margin_growth_pct", True, "first")
        return 29000.0, 0.1

    monkeypatch.setattr(collector, "_collect_total_amount", first_total)
    monkeypatch.setattr(collector, "_collect_margin", first_margin)
    monkeypatch.setattr(collector, "_collect_limit_pool", lambda _sources: (80, 20))
    collector.collect(date=SwingStore.today_str(), persist=False)
    collector.collect_status["previous_run_only"] = MacroCollectStatus(
        "previous_run_only", True, "first"
    )

    def second_total(_sources):
        collector._set_status("total_amount", False, "second", "failed")
        return None

    def second_margin(_sources):
        collector._set_status("margin_growth_pct", False, "second", "failed")
        return None, None

    monkeypatch.setattr(collector, "_collect_total_amount", second_total)
    monkeypatch.setattr(collector, "_collect_margin", second_margin)
    monkeypatch.setattr(collector, "_collect_limit_pool", lambda _sources: (None, None))

    second = collector.collect(date=SwingStore.today_str(), persist=False)
    status = second.extra["collect_status"]

    assert "previous_run_only" not in status
    assert not status.get("margin_balance", {}).get("available", False)
    assert status["total_amount"]["available"] is False
    assert status["margin_growth_pct"]["available"] is False
    assert status["limit_ratio"]["available"] is False


def test_dashboard_missing_and_unready_factors_are_not_normal(tmp_path):
    """API 层必须区分缺失、样本不足与真正的正常环境。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "swing": {
            "store_file": "data/swing_macro.db",
            "macro": {
                "min_history_days": 20,
                "extreme_percentile_low": 5,
                "extreme_percentile_high": 95,
            },
        },
        "targets": [],
    }
    (tmp_path / "config.swing.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    store = SwingStore(str(data_dir / "swing_macro.db"))
    store.upsert(MacroSlice(date="2026-07-13", total_amount=10000.0))
    store.upsert(MacroSlice(date="2026-07-14", total_amount=10100.0))
    store.upsert(
        MacroSlice(date="2026-07-15", total_amount=10200.0, limit_ratio=1.0)
    )

    overview = load_swing_overview(tmp_path)
    factors = {item["factor"]: item for item in overview["factors"]}

    assert factors["total_amount"]["history_ready"] is False
    assert factors["total_amount"]["level"] == "样本不足"
    assert factors["margin_growth_pct"]["value"] is None
    assert factors["margin_growth_pct"]["level"] == "缺失"


def test_eastmoney_total_amount_deduplicates_overlapping_pages(tmp_path):
    """东财分页重叠时，同一证券代码的成交额只能累计一次。"""
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))
    collector.MIN_TOTAL_AMOUNT_SECURITIES = 3
    yi = 1e8
    responses = [
        {"data": {"total": 6000}},
        {
            "data": {
                "diff": [
                    {"f12": "000001", "f6": 1000 * yi},
                    {"f12": "000002", "f6": 2000 * yi},
                ]
            }
        },
        {
            "data": {
                "diff": [
                    {"f12": "000002", "f6": 2000 * yi},
                    {"f12": "600000", "f6": 3000 * yi},
                ]
            }
        },
    ]
    collector._get_json_with_retry = MagicMock(side_effect=responses)

    total = collector._eastmoney_total_amount()

    assert collector.is_valid_factor_value("total_amount", 6000.0) is True
    assert collector._get_json_with_retry.call_count == 3
    assert total == pytest.approx(6000.0)
    page_params = [
        call.args[1] for call in collector._get_json_with_retry.call_args_list[1:]
    ]
    assert [params["pn"] for params in page_params] == [1, 2]
    assert all("f12" in params["fields"] for params in page_params)


def test_eastmoney_total_amount_rejects_implausible_total(tmp_path):
    """明显越过宽松 A 股量级上限的东财总额不得进入历史分位。"""
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))
    collector._get_json_with_retry = MagicMock(
        side_effect=[
            {"data": {"total": 1}},
            {"data": {"diff": [{"f12": "000001", "f6": 90000 * 1e8}]}},
        ]
    )

    assert collector._eastmoney_total_amount() is None


def test_tencent_total_without_observed_bar_date_is_not_marked_fresh(tmp_path, monkeypatch):
    fake_akshare = ModuleType("akshare")
    fake_akshare.stock_zh_a_spot_em = lambda: pd.DataFrame()
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))
    collector._collection_date = SwingStore.today_str()
    monkeypatch.setattr(collector, "_tencent_total_amount", lambda: 25000.0)
    monkeypatch.setattr(collector, "_sina_total_amount", lambda: None)
    monkeypatch.setattr(collector, "_eastmoney_total_amount", lambda: None)

    assert collector._collect_total_amount([]) is None
    assert collector.get_collect_status()["total_amount"]["available"] is False
    assert "真实交易日" in collector.get_collect_status()["total_amount"]["message"]


def test_mx_limit_pair_without_observed_date_is_rejected(tmp_path, monkeypatch):
    fake_akshare = ModuleType("akshare")
    fake_akshare.stock_zt_pool_em = lambda **_kwargs: None
    fake_akshare.stock_zt_pool_dtgc_em = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))
    collector._collection_date = SwingStore.today_str()
    monkeypatch.setattr(
        collector,
        "_mx_sentiment",
        lambda: {"limit_up_count": 80, "limit_down_count": 20},
    )
    sources = []

    assert collector._collect_limit_pool(sources) == (None, None)
    assert sources == []


def test_akshare_margin_dates_are_normalized_to_iso(tmp_path, monkeypatch):
    """AkShare 的 Timestamp、紧凑字符串和整数日期统一返回 YYYY-MM-DD。"""
    fake_akshare = ModuleType("akshare")
    requested_szse_dates = []

    def margin_sse(**_kwargs):
        return pd.DataFrame(
            {
                "信用交易日期": [
                    pd.Timestamp("2026-07-15"),
                    "20260714",
                    20260713,
                ],
                "融资融券余额": [1.0e12, 1.1e12, 1.2e12],
            }
        )

    def margin_szse(date):
        requested_szse_dates.append(date)
        return pd.DataFrame({"融资融券余额": [7000.0]})

    fake_akshare.stock_margin_sse = margin_sse
    fake_akshare.stock_margin_szse = margin_szse
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    collector = MacroSnapshotCollector(SwingStore(str(tmp_path / "macro.db")))

    daily = collector._akshare_margin_daily_balances(days=21)

    assert [date for date, _balance in daily] == [
        "2026-07-15",
        "2026-07-14",
        "2026-07-13",
    ]
    assert requested_szse_dates == ["20260715", "20260714", "20260713"]


def test_backfill_skips_historical_slice_without_collecting(tmp_path, monkeypatch):
    """历史切片只能计入 skipped，禁止调用实时 Collector 或改写数据库。"""
    import scripts.backfill_swing_macro as backfill

    db_path = tmp_path / "macro.db"
    today = dt.date.fromisoformat(SwingStore.today_str())
    historical_date = (today - dt.timedelta(days=1)).isoformat()
    store = SwingStore(str(db_path))
    store.upsert(MacroSlice(date=historical_date, source="existing"))
    collector = MagicMock()
    collector.collect.side_effect = AssertionError("历史切片不得访问实时采集器")
    monkeypatch.setattr(
        backfill,
        "_read_config",
        lambda _path: {"swing": {"store_file": str(db_path), "macro": {}}},
    )
    monkeypatch.setattr(backfill, "MacroSnapshotCollector", lambda *_args, **_kwargs: collector)

    summary = backfill.run("unused.json", days=30, dry_run=False)

    collector.collect.assert_not_called()
    assert summary == {
        "checked": 1,
        "updated": 0,
        "historical_skipped": 1,
        "unusable_skipped": 0,
        "fields": {},
        "dry_run": False,
    }
    unchanged = SwingStore(str(db_path)).get_latest()
    assert unchanged is not None
    assert unchanged.date == historical_date
    assert unchanged.total_amount is None
    assert unchanged.source == "existing"


def test_backfill_never_splices_partial_coupled_factor_groups():
    from scripts.backfill_swing_macro import _merge_slice

    existing = MacroSlice(
        date=SwingStore.today_str(),
        margin_balance=29000.0,
        limit_up=72,
        source="existing",
    )
    collected = MacroSlice(
        date=existing.date,
        margin_balance=29100.0,
        margin_growth_pct=0.3,
        limit_up=80,
        limit_down=20,
        limit_ratio=round(80 / 21, 3),
        source="fresh",
        extra={
            "collect_status": {
                "margin_balance": {"available": True, "stale": False},
                "margin_growth_pct": {"available": True, "stale": False},
                "limit_ratio": {"available": True, "stale": False},
            }
        },
    )

    merged = _merge_slice(existing, collected)

    assert merged.margin_balance == 29000.0
    assert merged.margin_growth_pct is None
    assert merged.limit_up == 72
    assert merged.limit_down is None
    assert merged.limit_ratio is None
    assert merged.source == "existing"


def test_replayed_margin_observation_does_not_emit_duplicate_extreme(tmp_path):
    store = _write_market_filter_config(tmp_path)
    for index in range(20):
        store.upsert(
            MacroSlice(
                date=f"2026-06-{index + 1:02d}",
                margin_growth_pct=0.1 + index / 1000,
            )
        )
    replay_status = {
        "collect_status": {
            "margin_growth_pct": {
                "available": True,
                "observed_date": "2026-07-17",
                "stale": False,
            }
        }
    }
    store.upsert(
        MacroSlice(
            date="2026-07-20",
            margin_growth_pct=5.0,
            extra=replay_status,
        )
    )
    current = MacroSlice(
        date="2026-07-21",
        margin_growth_pct=5.0,
        extra=replay_status,
    )

    assert MacroSnapshotCollector(store, min_history_days=20).detect_extremes(current) == []

    classified = MacroSnapshotCollector(store, min_history_days=20).detect_extremes(
        current,
        suppress_replayed=False,
    )
    assert {(item.factor, item.level) for item in classified} == {
        ("margin_growth_pct", "沸点")
    }

    # 通知层抑制同一 observed_date 重放，不代表页面应隐藏当前仍成立的极值。
    store.upsert(current)
    overview = load_swing_overview(tmp_path)
    margin = next(
        item for item in overview["factors"] if item["factor"] == "margin_growth_pct"
    )
    assert margin["state"] == "extreme"
    assert margin["level"] == "沸点"


def _write_market_filter_config(base_dir, *, min_history_days=20):
    data_dir = base_dir / "data"
    data_dir.mkdir(exist_ok=True)
    config = {
        "swing": {
            "store_file": "data/swing_macro.db",
            "macro": {
                "min_history_days": min_history_days,
                "extreme_percentile_low": 5,
                "extreme_percentile_high": 95,
            },
        },
        "targets": [],
    }
    (base_dir / "config.swing.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return SwingStore(str(data_dir / "swing_macro.db"))


def _seed_ready_market_history(store):
    for index in range(20):
        store.upsert(
            MacroSlice(
                date=f"2026-06-{index + 1:02d}",
                total_amount=20000.0 + index,
                margin_growth_pct=0.1 + index / 1000,
                limit_ratio=2.0 + index / 100,
            )
        )


def test_dashboard_stale_factor_degrades_overall_environment(tmp_path):
    store = _write_market_filter_config(tmp_path)
    _seed_ready_market_history(store)
    today = dt.date.fromisoformat(SwingStore.today_str())
    observed = (today - dt.timedelta(days=1)).isoformat()
    store.upsert(
        MacroSlice(
            date=today.isoformat(),
            total_amount=25000.0,
            margin_growth_pct=0.2,
            limit_ratio=3.0,
            extra={
                "collect_status": {
                    "total_amount": {
                        "available": True,
                        "source": "tencent",
                        "observed_date": observed,
                        "stale": True,
                    },
                    "margin_growth_pct": {"available": True, "source": "akshare"},
                    "limit_ratio": {"available": True, "source": "akshare"},
                }
            },
        )
    )

    overview = load_swing_overview(tmp_path)
    factors = {item["factor"]: item for item in overview["factors"]}

    assert factors["total_amount"]["state"] == "stale"
    assert overview["environment_state"] == "degraded"
    assert overview["environment_ready"] is False
    assert overview["environment_stale"] is True


def test_dashboard_anomalous_factor_degrades_overall_environment(tmp_path):
    store = _write_market_filter_config(tmp_path)
    _seed_ready_market_history(store)
    today = SwingStore.today_str()
    store.upsert(
        MacroSlice(
            date=today,
            total_amount=90000.0,
            margin_growth_pct=0.2,
            limit_ratio=3.0,
            extra={
                "collect_status": {
                    "total_amount": {
                        "available": False,
                        "source": "eastmoney",
                        "observed_date": today,
                        "anomalous": True,
                        "message": "成交额越界",
                    },
                    "margin_growth_pct": {"available": True, "source": "akshare"},
                    "limit_ratio": {"available": True, "source": "akshare"},
                }
            },
        )
    )

    overview = load_swing_overview(tmp_path)
    factors = {item["factor"]: item for item in overview["factors"]}

    assert factors["total_amount"]["state"] == "anomalous"
    assert factors["total_amount"]["level"] == "异常"
    assert overview["environment_state"] == "degraded"
    assert overview["environment_ready"] is False
    assert overview["environment_stale"] is False


def test_dashboard_marks_missing_completed_eod_slice_as_stale(tmp_path, monkeypatch):
    import dashboard.swing_view as swing_view

    store = _write_market_filter_config(tmp_path)
    _seed_ready_market_history(store)
    store.upsert(
        MacroSlice(
            date="2026-07-14",
            total_amount=25000.0,
            margin_growth_pct=0.2,
            limit_ratio=3.0,
        )
    )
    monkeypatch.setattr(
        swing_view,
        "_beijing_now",
        lambda: dt.datetime(2026, 7, 16, 16, 0, tzinfo=_BEIJING),
    )

    overview = load_swing_overview(tmp_path)

    assert overview["environment_expected_date"] == "2026-07-16"
    assert overview["environment_slice_stale"] is True
    assert overview["environment_stale"] is True
    assert overview["environment_state"] == "degraded"
    assert overview["environment_ready"] is False


def test_margin_publication_lag_is_fresh_but_same_day_factors_are_not():
    """周一允许上周五两融；成交额和涨跌停仍必须是周一同日数据。"""
    monday = "2026-07-20"
    friday = "2026-07-17"

    assert MacroSnapshotCollector._observation_is_stale(
        "margin_balance", friday, monday
    ) is False
    assert MacroSnapshotCollector._observation_is_stale(
        "margin_growth_pct", friday, monday
    ) is False
    assert MacroSnapshotCollector._observation_is_stale(
        "total_amount", friday, monday
    ) is True
    assert MacroSnapshotCollector._observation_is_stale(
        "limit_ratio", friday, monday
    ) is True
    assert MacroSnapshotCollector._observation_is_stale(
        "margin_balance", "2026-07-15", monday
    ) is True


def test_dashboard_accepts_expected_weekend_margin_lag(tmp_path, monkeypatch):
    """周一页面不能把上周五最新两融数据永久判为环境降级。"""
    import dashboard.swing_view as swing_view_module

    store = _write_market_filter_config(tmp_path)
    _seed_ready_market_history(store)
    monday = "2026-07-20"
    friday = "2026-07-17"
    monkeypatch.setattr(
        swing_view_module, "_latest_completed_trading_date", lambda _base: monday
    )
    store.upsert(
        MacroSlice(
            date=monday,
            total_amount=20010.0,
            margin_growth_pct=0.11,
            limit_ratio=2.10,
            extra={
                "collect_status": {
                    "total_amount": {
                        "available": True,
                        "observed_date": monday,
                        "stale": False,
                    },
                    "margin_growth_pct": {
                        "available": True,
                        "observed_date": friday,
                        "stale": False,
                    },
                    "limit_ratio": {
                        "available": True,
                        "observed_date": monday,
                        "stale": False,
                    },
                }
            },
        )
    )

    overview = load_swing_overview(tmp_path)
    margin = next(
        item for item in overview["factors"] if item["factor"] == "margin_growth_pct"
    )

    assert margin["state"] == "normal"
    assert overview["environment_state"] == "normal"
    assert overview["environment_ready"] is True
    assert overview["environment_stale"] is False


def test_dashboard_counts_each_margin_observation_date_once(tmp_path):
    """同一两融观测日被多次采集时，只能贡献一个历史样本。"""
    store = _write_market_filter_config(tmp_path, min_history_days=2)

    def margin_slice(slice_date, observed_date, value):
        return MacroSlice(
            date=slice_date,
            margin_growth_pct=value,
            extra={
                "collect_status": {
                    "margin_growth_pct": {
                        "available": True,
                        "source": "akshare",
                        "observed_date": observed_date,
                        "stale": False,
                    }
                }
            },
        )

    store.upsert(margin_slice("2026-07-20", "2026-07-17", 0.10))
    store.upsert(margin_slice("2026-07-21", "2026-07-17", 0.11))
    store.upsert(margin_slice("2026-07-22", "2026-07-22", 0.20))

    overview = load_swing_overview(tmp_path)
    margin = next(
        item for item in overview["factors"] if item["factor"] == "margin_growth_pct"
    )

    assert margin["sample_size"] == 1
    assert margin["history_ready"] is False
    assert margin["level"] == "样本不足"


def _risk_gate_fixture():
    signals = [
        SimpleNamespace(code="588000", name="科创50ETF"),
        SimpleNamespace(code="512480", name="半导体ETF"),
        SimpleNamespace(code="510300", name="沪深300ETF"),
        SimpleNamespace(code="512890", name="红利低波ETF"),
    ]
    targets = [
        {"code": "588000", "name": "科创50ETF", "sector": "硬科技"},
        {"code": "512480", "name": "半导体ETF", "sector": "成长"},
        {"code": "510300", "name": "沪深300ETF", "sector": "宽基"},
        {"code": "512890", "name": "红利低波ETF", "sector": "防御"},
    ]
    return signals, targets


def test_eod_risk_gate_critical_blocks_every_entry():
    import radar_swing_eod as eod

    signals, targets = _risk_gate_fixture()
    allowed, blocked = eod._apply_eod_risk_gate(
        signals,
        targets,
        {"level": "critical", "message": "crisis"},
        logging.getLogger(__name__),
    )

    assert allowed == []
    assert {signal.code for signal in blocked} == {signal.code for signal in signals}


def test_eod_risk_gate_growth_blocks_only_growth_and_technology():
    import radar_swing_eod as eod

    signals, targets = _risk_gate_fixture()
    allowed, blocked = eod._apply_eod_risk_gate(
        signals,
        targets,
        {"level": "growth_blocked", "message": "growth ceiling"},
        logging.getLogger(__name__),
    )

    assert {signal.code for signal in blocked} == {"588000", "512480"}
    assert {signal.code for signal in allowed} == {"510300", "512890"}


def test_eod_risk_gate_missing_state_fails_closed():
    import radar_swing_eod as eod

    signals, targets = _risk_gate_fixture()
    allowed, blocked = eod._apply_eod_risk_gate(
        signals,
        targets,
        None,
        logging.getLogger(__name__),
    )

    assert allowed == []
    assert {signal.code for signal in blocked} == {signal.code for signal in signals}


def test_deployed_swing_config_and_schema_agree_on_decision_time():
    """部署配置的 15:30 决策时刻必须能通过同版本配置校验。"""
    import radar_swing_eod as eod
    from radar.config_schema import validate_swing_config_dict

    project_root = Path(eod.__file__).resolve().parent
    config = json.loads((project_root / "config.swing.json").read_text(encoding="utf-8"))

    assert config["swing"]["decision_time"] == "15:30 Asia/Shanghai"
    validate_swing_config_dict(config)


def test_eod_dry_run_does_not_save_refreshed_risk_state(tmp_path, monkeypatch):
    """dry-run 可现场读取/计算风险，但不得改写共享风险状态文件。"""
    import radar.risk.macro_risk_control as risk_module
    import radar_swing_eod as eod

    db_path = tmp_path / "swing_macro.db"
    config = {
        "swing": {
            "store_file": str(db_path),
            "macro": {},
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "history_days": 250,
            "risk_gate": {
                "enabled": True,
                "state_file": str(tmp_path / "risk_state.json"),
                "override_file": str(tmp_path / "risk_override.json"),
            },
        },
        "market": {},
        "telegram": {"bot_token": "test", "chat_id": "1"},
        "targets": [],
    }
    saved = []

    class _RiskState:
        @staticmethod
        def to_dict():
            return {"level": "ok", "message": "fresh"}

    class _RiskControl:
        _STATE_FILE = None
        _OVERRIDE_FILE = None

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def load_state_from_file():
            return None

        @staticmethod
        def check_with_fetch():
            return _RiskState()

        @staticmethod
        def save_state(state):
            saved.append(state)

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def is_trading_day():
            return True

    class _Collector:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def collect(*, persist=True):
            assert persist is False
            return MacroSlice(date=SwingStore.today_str(), source="test")

        @staticmethod
        def detect_extremes(_slice):
            return []

        @staticmethod
        def get_collect_status():
            return {}

    monkeypatch.setattr(risk_module, "MacroRiskControl", _RiskControl)
    monkeypatch.setattr(eod, "_read_config", lambda _path: config)
    monkeypatch.setattr(eod, "validate_swing_config_dict", lambda _data: None)
    monkeypatch.setattr(eod, "_setup_logging", lambda _path: logging.getLogger(__name__))
    monkeypatch.setattr(eod, "_check_runtime_dependencies", lambda *_args: {})
    monkeypatch.setattr(eod, "ScanScheduler", _Scheduler)
    monkeypatch.setattr(eod, "MacroSnapshotCollector", _Collector)
    monkeypatch.setattr(eod, "MarketDataFetcher", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        eod,
        "TrendConfirmStrategy",
        lambda *_args, **_kwargs: SimpleNamespace(last_data_ok=True),
    )
    monkeypatch.setattr(eod, "TelegramNotifier", lambda *_args, **_kwargs: SimpleNamespace())

    eod.run("unused.json", dry_run=True)

    assert saved == []


def test_risk_gate_transition_preserves_notified_signal_identity(tmp_path, monkeypatch):
    """ok→warning→ok 重跑不得删除已通知身份并造成恢复后重复推送。"""
    import radar_swing_eod as eod
    from radar.swing.trend_strategy import TrendSignal

    db_path = tmp_path / "swing_macro.db"
    # notifications use the v6 dispatch queue; a pre-v6 DB pauses sending by design
    from tests.notify_v2_helpers import make_v6_store
    make_v6_store(db_path)
    config = {
        "swing": {
            "store_file": str(db_path),
            "macro": {},
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "history_days": 250,
        },
        "market": {},
        "telegram": {"bot_token": "test", "chat_id": "1"},
        "targets": [
            {"code": "588000", "name": "科创50ETF", "mode": "trend", "sector": "硬科技"}
        ],
    }
    risk_states = iter(
        [
            {"level": "ok", "message": "normal"},
            {"level": "warning", "message": "missing"},
            {"level": "ok", "message": "recovered"},
        ]
    )
    delivered = []

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def is_trading_day():
            return True

    class _Collector:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def collect(*, persist=True):
            return MacroSlice(date=SwingStore.today_str(), source="test")

        @staticmethod
        def detect_extremes(_slice):
            return []

        @staticmethod
        def get_collect_status():
            return {}

    class _Strategy:
        last_data_ok = True

        def __init__(self, *_args, **_kwargs):
            pass

        def evaluate(self, code, name):
            self.last_data_ok = True
            return [
                TrendSignal(
                    code,
                    name,
                    "weekly_macd_golden_cross",
                    "W",
                    "周线 MACD 金叉",
                    detail={"data_date": "2026-07-17"},
                )
            ]

    class _Notifier:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def send_trend_alert(name, code, signal, period, detail=None):
            delivered.append((code, detail["data_date"]))
            return True

    monkeypatch.setattr(eod, "_read_config", lambda _path: config)
    monkeypatch.setattr(eod, "validate_swing_config_dict", lambda _data: None)
    monkeypatch.setattr(eod, "_setup_logging", lambda _path: logging.getLogger(__name__))
    monkeypatch.setattr(eod, "_check_runtime_dependencies", lambda *_args: {})
    monkeypatch.setattr(
        eod,
        "_load_eod_risk_state",
        lambda *_args, **_kwargs: next(risk_states),
    )
    monkeypatch.setattr(eod, "ScanScheduler", _Scheduler)
    monkeypatch.setattr(eod, "MacroSnapshotCollector", _Collector)
    monkeypatch.setattr(eod, "MarketDataFetcher", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(eod, "TrendConfirmStrategy", _Strategy)
    monkeypatch.setattr(eod, "TelegramNotifier", _Notifier)

    first = eod.run("unused.json", dry_run=False)
    blocked = eod.run("unused.json", dry_run=False)
    recovered = eod.run("unused.json", dry_run=False)

    assert first["sent"] == 1
    assert blocked["risk_blocked_signals"] == 1
    assert recovered["sent"] == 0
    assert delivered == [("588000", "2026-07-17")]
    rows = SwingStore(str(db_path)).get_recent_signals(10)
    assert len(rows) == 1
    assert rows[0]["notified"] is True


def test_risk_gate_cross_day_recovery_migrates_unnotified_signal(tmp_path, monkeypatch):
    """前日被拦截的同一行情 bar，次日恢复后须迁移、放行并记录已通知。"""
    import radar_swing_eod as eod
    from radar.swing.trend_strategy import TrendSignal

    db_path = tmp_path / "swing_macro.db"
    # notifications use the v6 dispatch queue; a pre-v6 DB pauses sending by design
    from tests.notify_v2_helpers import make_v6_store
    make_v6_store(db_path)
    config = {
        "swing": {
            "store_file": str(db_path),
            "macro": {},
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "history_days": 250,
        },
        "market": {},
        "telegram": {"bot_token": "test", "chat_id": "1"},
        "targets": [
            {"code": "588000", "name": "科创50ETF", "mode": "trend", "sector": "硬科技"}
        ],
    }
    run_dates = iter(["2026-07-15", "2026-07-16"])
    risk_states = iter(
        [
            {"level": "warning", "message": "blocked on day one"},
            {"level": "ok", "message": "recovered on day two"},
        ]
    )
    delivered = []

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def is_trading_day():
            return True

    class _Collector:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def collect(*, persist=True):
            return MacroSlice(date=next(run_dates), source="test")

        @staticmethod
        def detect_extremes(_slice):
            return []

        @staticmethod
        def get_collect_status():
            return {}

    class _Strategy:
        last_data_ok = True

        def __init__(self, *_args, **_kwargs):
            pass

        def evaluate(self, code, name):
            self.last_data_ok = True
            return [
                TrendSignal(
                    code,
                    name,
                    "weekly_macd_golden_cross",
                    "W",
                    "周线 MACD 金叉",
                    detail={"data_date": "2026-07-10"},
                )
            ]

    class _Notifier:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def send_trend_alert(name, code, signal, period, detail=None):
            delivered.append((code, detail["data_date"]))
            return True

    monkeypatch.setattr(eod, "_read_config", lambda _path: config)
    monkeypatch.setattr(eod, "validate_swing_config_dict", lambda _data: None)
    monkeypatch.setattr(eod, "_setup_logging", lambda _path: logging.getLogger(__name__))
    monkeypatch.setattr(eod, "_check_runtime_dependencies", lambda *_args: {})
    monkeypatch.setattr(
        eod,
        "_load_eod_risk_state",
        lambda *_args, **_kwargs: next(risk_states),
    )
    monkeypatch.setattr(eod, "ScanScheduler", _Scheduler)
    monkeypatch.setattr(eod, "MacroSnapshotCollector", _Collector)
    monkeypatch.setattr(eod, "MarketDataFetcher", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(eod, "TrendConfirmStrategy", _Strategy)
    monkeypatch.setattr(eod, "TelegramNotifier", _Notifier)

    day_one = eod.run("unused.json", dry_run=False)
    day_two = eod.run("unused.json", dry_run=False)

    assert day_one["risk_blocked_signals"] == 1
    assert day_one["sent"] == 0
    assert day_two["risk_blocked_signals"] == 0
    assert day_two["sent"] == 1
    assert delivered == [("588000", "2026-07-10")]

    rows = SwingStore(str(db_path)).get_recent_signals(10)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-16"
    assert rows[0]["detail"]["risk_gate"]["blocked"] is False
    assert rows[0]["notified"] is True
