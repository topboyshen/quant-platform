"""波段化模块测试：secid 解析、SwingStore、宏观采集、趋势信号、reporter 过滤。"""

import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from radar.config_schema import ConfigSchemaError, validate_swing_config_dict
from radar.data import kline_sources
from radar.data.kline_sources import fetch_akshare_kline, fetch_tencent_kline
from radar.data.market_data_v2 import MarketDataFetcher, resolve_secid
from radar.swing.macro_snapshot import MacroSnapshotCollector
from radar.swing.reporter import SwingReporter
from radar.swing.store import MacroSlice, SwingStore
from radar.swing.trend_strategy import TrendConfirmStrategy, compute_macd


# ----------------------------------------------------------------- secid 解析
@pytest.mark.parametrize(
    "code,expected",
    [
        ("510300", "1.510300"),  # 沪市 ETF（旧逻辑会错判为 0.）
        ("588000", "1.588000"),  # 科创50 ETF
        ("512480", "1.512480"),  # 半导体 ETF
        ("518880", "1.518880"),  # 黄金 ETF
        ("600519", "1.600519"),  # 沪市个股
        ("159915", "0.159915"),  # 深市 ETF
        ("000001", "0.000001"),  # 深市个股
        ("300059", "0.300059"),  # 创业板
    ],
)
def test_resolve_secid_handles_etf(code, expected):
    assert resolve_secid(code) == expected


def test_market_fetcher_falls_back_to_akshare_etf_history(monkeypatch):
    class _FakeAk:
        __version__ = "test"

        @staticmethod
        def fund_etf_hist_em(**kwargs):
            assert kwargs["symbol"] == "510300"
            dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=80, freq="D")
            return pd.DataFrame(
                {
                    "日期": dates.strftime("%Y-%m-%d"),
                    "开盘": np.full(80, 10.0),
                    "收盘": np.linspace(10.0, 11.0, 80),
                    "最高": np.linspace(10.1, 11.1, 80),
                    "最低": np.linspace(9.9, 10.9, 80),
                    "成交量": np.full(80, 1_000_000),
                }
            )

    class _FakeAkFetcher:
        is_available = True
        _ak = _FakeAk()

    fetcher = MarketDataFetcher(logging.getLogger("pytest.swing"), use_akshare=False)
    fetcher._akshare_fetcher = _FakeAkFetcher()
    monkeypatch.setattr(fetcher, "_try_load_parquet", lambda *_a, **_k: None)
    monkeypatch.setattr(fetcher, "_fetch_eastmoney_kline", lambda *_a, **_k: None)
    # 腾讯 K 线优先级高于 akshare，必须一并屏蔽，否则会真实联网拉到行情
    monkeypatch.setattr(fetcher, "_fetch_tencent_kline", lambda *_a, **_k: None)

    df = fetcher.get_historical_data("510300", days=60)

    assert len(df) >= 60
    assert {"date", "open", "close", "high", "low", "volume"}.issubset(df.columns)
    assert float(df["close"].iloc[-1]) == pytest.approx(11.0)


def test_tencent_kline_accepts_unadjusted_day_series(monkeypatch):
    rows = [
        [date.strftime("%Y-%m-%d"), "0.58", "0.59", "0.60", "0.57", "100000"]
        for date in pd.date_range(end=pd.Timestamp.now().normalize(), periods=40, freq="D")
    ]
    payload = {"code": 0, "data": {"sh513180": {"day": rows}}}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            import json

            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(kline_sources.urllib.request, "urlopen", lambda *_a, **_k: _Response())

    df = fetch_tencent_kline(logging.getLogger("pytest.swing.tencent_day"), "513180", 30)

    assert df is not None
    assert len(df) == 40
    assert df["date"].iloc[-1] == rows[-1][0]


def test_akshare_sina_etf_uses_exchange_qualified_symbol(monkeypatch):
    class _FakeAk:
        @staticmethod
        def fund_etf_hist_sina(*, symbol):
            assert symbol == "sh513180"
            dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=80, freq="D")
            return pd.DataFrame(
                {
                    "date": dates,
                    "open": 0.58,
                    "close": 0.59,
                    "high": 0.60,
                    "low": 0.57,
                    "volume": 1_000_000,
                }
            )

    class _FakeAkFetcher:
        is_available = True
        _ak = _FakeAk()

    for key in ("RADAR_ALL_PROXY", "RADAR_HTTP_PROXY", "RADAR_HTTPS_PROXY"):
        monkeypatch.delenv(key, raising=False)

    df = fetch_akshare_kline(
        logging.getLogger("pytest.swing.akshare_sina"), _FakeAkFetcher(), "513180", 60
    )

    assert df is not None
    assert len(df) == 80
    assert pd.Timestamp(df["date"].iloc[-1]).normalize() == pd.Timestamp.now().normalize()


def test_market_fetcher_rejects_stale_history(monkeypatch):
    dates = pd.date_range("2025-01-01", periods=200, freq="D")
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "close": 10.0,
            "high": 10.1,
            "low": 9.9,
            "volume": 1_000_000,
        }
    )
    fetcher = MarketDataFetcher(logging.getLogger("pytest.swing.stale"), use_akshare=False)
    monkeypatch.setattr(fetcher, "_try_load_parquet", lambda *_a, **_k: None)
    monkeypatch.setattr(fetcher, "_fetch_eastmoney_kline", lambda *_a, **_k: frame)
    monkeypatch.setattr(fetcher, "_fetch_tencent_kline", lambda *_a, **_k: None)
    monkeypatch.setattr(fetcher, "_fetch_akshare_kline", lambda *_a, **_k: None)

    assert fetcher.get_historical_data("510300", days=250).empty


def test_market_fetcher_negative_caches_batch_failure(monkeypatch):
    fetcher = MarketDataFetcher(logging.getLogger("pytest.swing.batch"), use_akshare=False)
    calls = []
    monkeypatch.setattr(fetcher, "_fetch_all_from_eastmoney", lambda: calls.append(1) or None)

    assert fetcher.fetch_all() is None
    assert fetcher.fetch_all() is None
    assert calls == [1]


def test_market_fetcher_skips_single_eastmoney_during_backoff(monkeypatch):
    fetcher = MarketDataFetcher(logging.getLogger("pytest.swing.backoff"), use_akshare=False)
    fetcher._pause_eastmoney()
    monkeypatch.setattr(
        fetcher._single_session,
        "get",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("eastmoney should be skipped")),
    )
    expected = {"code": "300059", "price": 20.0}
    monkeypatch.setattr(fetcher, "_get_backup_stock_data", lambda *_a, **_k: expected)

    assert fetcher.get_stock_data("300059", "东方财富") == expected


# ----------------------------------------------------------------- 配置校验
def _swing_cfg():
    return {
        "telegram": {"bot_token": "DISABLED", "chat_id": "DISABLED"},
        "market": {
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        "swing": {
            "decision_time": "15:30 Asia/Shanghai",
            "log_file": "radar_swing.log",
            "store_file": "data/swing_macro.db",
            "history_days": 250,
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "technical": {"weekly_macd_golden_cross": True, "volume_breakout_ma": 60},
            "macro": {"extreme_percentile_low": 5, "extreme_percentile_high": 95, "min_history_days": 20},
        },
        "targets": [
            {"code": "510300", "name": "沪深300ETF", "mode": "trend", "sector": "宽基"},
            {"code": "510500", "name": "中证500ETF", "mode": "trend", "sector": "宽基"},
            {"code": "159915", "name": "创业板ETF", "mode": "trend", "sector": "成长"},
            {"code": "588000", "name": "科创50ETF", "mode": "trend", "sector": "硬科技"},
            {"code": "512480", "name": "半导体ETF", "mode": "trend", "sector": "行业"},
            {"code": "512890", "name": "红利低波ETF", "mode": "trend", "sector": "防御"},
            {"code": "513180", "name": "恒生科技ETF", "mode": "trend", "sector": "港股科技"},
            {"code": "512000", "name": "券商ETF", "mode": "trend", "sector": "情绪"},
            {"code": "512170", "name": "医疗ETF", "mode": "trend", "sector": "行业"},
            {"code": "518880", "name": "黄金ETF", "mode": "trend", "sector": "避险"},
        ],
    }


def test_validate_swing_trend_target_needs_no_trigger_price():
    validate_swing_config_dict(_swing_cfg())  # 不抛异常即通过


def test_validate_swing_core_guardrails_require_ma60_and_extreme_percentiles():
    cfg = _swing_cfg()
    cfg["swing"]["ma_periods"] = [20, 120]
    with pytest.raises(ConfigSchemaError, match="MA60"):
        validate_swing_config_dict(cfg)

    cfg = _swing_cfg()
    cfg["swing"]["macro"]["extreme_percentile_low"] = 10
    with pytest.raises(ConfigSchemaError, match="5/95"):
        validate_swing_config_dict(cfg)


def test_validate_swing_requires_ten_core_trend_etfs():
    cfg = _swing_cfg()
    cfg["targets"] = cfg["targets"][:-1]
    with pytest.raises(ConfigSchemaError, match="10 只"):
        validate_swing_config_dict(cfg)


def test_validate_swing_price_mode_requires_trigger_price():
    cfg = _swing_cfg()
    cfg["targets"] = [{"code": "510300", "name": "x", "mode": "price"}]
    with pytest.raises(ConfigSchemaError):
        validate_swing_config_dict(cfg)


def test_validate_swing_missing_swing_block():
    cfg = _swing_cfg()
    del cfg["swing"]
    with pytest.raises(ConfigSchemaError):
        validate_swing_config_dict(cfg)


# ----------------------------------------------------------------- SwingStore
def test_swing_store_upsert_and_series(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    for i in range(5):
        store.upsert(
            MacroSlice(
                date=f"2026-05-{10 + i:02d}",
                total_amount=1000.0 + i * 100,
                margin_balance=18000.0 + i,
                margin_growth_pct=float(i),
                limit_up=50 + i,
                limit_down=10,
                limit_ratio=(50 + i) / 11,
                source="test",
            )
        )
    # upsert 幂等：同日覆盖
    store.upsert(MacroSlice(date="2026-05-10", total_amount=9999.0, source="test"))
    latest = store.get_latest()
    assert latest.date == "2026-05-14"
    series = store.get_series("total_amount", n=10)
    assert len(series) == 5
    assert series[0] == 9999.0  # 覆盖生效，升序首项


# ----------------------------------------------------------------- 宏观极值
def test_macro_detect_extremes_boiling(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    # 写入 30 天平稳的涨跌停比历史
    for i in range(30):
        store.upsert(
            MacroSlice(date=f"2026-04-{i + 1:02d}", limit_ratio=1.0, total_amount=1000.0)
        )
    collector = MacroSnapshotCollector(store, extreme_low=5, extreme_high=95, min_history_days=20)
    # 一个极高的涨跌停比 -> 沸点
    hot = MacroSlice(date="2026-05-20", limit_ratio=50.0, total_amount=1000.0)
    store.upsert(hot)
    extremes = collector.detect_extremes(hot)
    levels = {e.factor: e.level for e in extremes}
    assert levels.get("limit_ratio") == "沸点"


def test_macro_collect_falls_back_when_akshare_missing(tmp_path, monkeypatch):
    """akshare 全部失败时不抛异常，切片字段为 None，且不污染历史库。"""
    store = SwingStore(str(tmp_path / "macro.db"))
    collector = MacroSnapshotCollector(store)

    monkeypatch.setattr(collector, "_collect_total_amount", lambda s: None)
    monkeypatch.setattr(collector, "_collect_margin", lambda s: (None, None))
    monkeypatch.setattr(collector, "_collect_limit_pool", lambda s: (None, None))

    today = SwingStore.today_str()
    slice_ = collector.collect(date=today)
    assert slice_.date == today
    assert slice_.limit_ratio is None
    # 全因子缺失的切片不落库（避免污染分位样本）
    assert store.get_latest() is None


def test_macro_collect_persists_partial_slice(tmp_path, monkeypatch):
    """只要有任一因子，切片仍正常落库。"""
    store = SwingStore(str(tmp_path / "macro.db"))
    collector = MacroSnapshotCollector(store)

    monkeypatch.setattr(collector, "_collect_total_amount", lambda s: None)
    monkeypatch.setattr(collector, "_collect_margin", lambda s: (None, None))
    monkeypatch.setattr(collector, "_collect_limit_pool", lambda s: (66, 11))

    today = SwingStore.today_str()
    slice_ = collector.collect(date=today)
    assert slice_.limit_ratio == round(66 / 12, 3)
    assert store.get_latest().date == today


def test_macro_collect_partial_rerun_preserves_prior_successes(tmp_path, monkeypatch):
    """同日某源失败时不得把较早一次成功保存的因子和观测元数据清空。"""
    store = SwingStore(str(tmp_path / "macro.db"))
    collector = MacroSnapshotCollector(store)
    today = SwingStore.today_str()

    collector._set_status("total_amount", True, "first", observed_date=today)
    store.upsert(
        MacroSlice(
            date=today,
            total_amount=12345.0,
            margin_balance=19000.0,
            margin_growth_pct=0.1,
            limit_up=50,
            limit_down=10,
            limit_ratio=round(50 / 11, 3),
            source="first",
            extra={"collect_status": collector.get_collect_status()},
        )
    )

    def failed_total(_sources):
        collector._set_status("total_amount", False, "second", "upstream failed")
        return None

    monkeypatch.setattr(collector, "_collect_total_amount", failed_total)
    monkeypatch.setattr(collector, "_collect_margin", lambda _sources: (None, None))
    monkeypatch.setattr(collector, "_collect_limit_pool", lambda _sources: (66, 11))

    collector.collect(date=today)
    saved = store.get_latest()
    assert saved.total_amount == 12345.0
    assert saved.margin_balance == 19000.0
    assert saved.margin_growth_pct == 0.1
    assert saved.limit_up == 66
    assert saved.limit_down == 11
    assert saved.limit_ratio == round(66 / 12, 3)
    assert saved.extra["collect_status"]["total_amount"]["source"] == "first"
    assert saved.extra["last_collect_status"]["total_amount"]["available"] is False


def test_akshare_margin_balance_normalizes_units_and_dates(monkeypatch):
    """SSE 返回元且按日期取最新、SZSE 返回亿元，归一后相加。"""
    import sys
    from types import ModuleType

    fake_ak = ModuleType("akshare")

    def stock_margin_sse(start_date: str, end_date: str):
        # 模拟接口返回升序（旧 bug 假设 iloc[0] 是最新行）
        return pd.DataFrame(
            {
                "信用交易日期": ["20260605", "20260608", "20260609"],
                "融资融券余额": [1.30e12, 1.40e12, 1.465993828068e12],
            }
        ).sort_values("信用交易日期")

    def stock_margin_szse(date: str):
        assert len(date) == 8  # 必须显式传日期
        return pd.DataFrame({"融资融券余额": [7234.97]})

    fake_ak.stock_margin_sse = stock_margin_sse
    fake_ak.stock_margin_szse = stock_margin_szse
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    balance = collector._akshare_margin_balance()
    assert balance == round(1.465993828068e12 / 1e8 + 7234.97, 2)


def test_akshare_margin_balance_rejects_single_side(monkeypatch):
    """仅单边市场有数据时按缺失处理，避免口径漂移。"""
    import sys
    from types import ModuleType

    fake_ak = ModuleType("akshare")
    fake_ak.stock_margin_sse = lambda **kw: pd.DataFrame(
        {"信用交易日期": ["20260609"], "融资融券余额": [1.4e12]}
    )
    fake_ak.stock_margin_szse = lambda date: pd.DataFrame()
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    assert collector._akshare_margin_balance() is None


def test_akshare_margin_growth_direct_uses_recent_two_complete_days(monkeypatch):
    """库里没有前值时，直接用 AkShare 最近两期沪深合计两融余额算增速。"""
    import sys
    from types import ModuleType

    fake_ak = ModuleType("akshare")

    def stock_margin_sse(start_date: str, end_date: str):
        return pd.DataFrame(
            {
                "信用交易日期": ["20260606", "20260609"],
                "融资融券余额": [1.40e12, 1.465e12],
            }
        )

    def stock_margin_szse(date: str):
        data = {
            "20260609": 7235.0,
            "20260606": 7000.0,
        }
        value = data.get(date)
        return pd.DataFrame({"融资融券余额": [value]}) if value is not None else pd.DataFrame()

    fake_ak.stock_margin_sse = stock_margin_sse
    fake_ak.stock_margin_szse = stock_margin_szse
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    growth = collector._akshare_margin_growth_direct()
    expected = ((1.465e12 / 1e8 + 7235.0) - (1.40e12 / 1e8 + 7000.0)) / (1.40e12 / 1e8 + 7000.0) * 100
    assert growth == round(expected, 3)


def test_collect_margin_prefers_akshare_direct_growth(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    monkeypatch.setattr(
        collector,
        "_akshare_margin_daily_balances",
        lambda days=21: [("2026-07-15", 28782.21), ("2026-07-14", 28690.12)],
    )

    sources = []
    balance, growth = collector._collect_margin(sources)

    assert balance == 28782.21
    assert growth == round((28782.21 - 28690.12) / 28690.12 * 100, 3)
    assert sources == ["akshare"]


def test_collect_total_amount_uses_eastmoney_when_akshare_empty(monkeypatch):
    import sys
    from types import ModuleType

    fake_ak = ModuleType("akshare")
    fake_ak.stock_zh_a_spot_em = lambda: pd.DataFrame()
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    monkeypatch.setattr(collector, "_tencent_total_amount", lambda: None)
    monkeypatch.setattr(collector, "_sina_total_amount", lambda: None)
    monkeypatch.setattr(collector, "_eastmoney_total_amount", lambda: 12345.67)
    sources = []

    assert collector._collect_total_amount(sources) == 12345.67
    assert sources == ["eastmoney"]


def test_collect_total_amount_prefers_tencent(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    collector._collection_date = SwingStore.today_str()
    collector._total_amount_observed_date = SwingStore.today_str()
    monkeypatch.setattr(collector, "_tencent_total_amount", lambda: 28177.61)
    monkeypatch.setattr(
        collector,
        "_eastmoney_total_amount",
        lambda: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )
    sources = []

    assert collector._collect_total_amount(sources) == 28177.61
    assert sources == ["tencent"]


def test_get_series_excludes_current_date(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    for i in range(5):
        store.upsert(MacroSlice(date=f"2026-05-{10 + i:02d}", total_amount=1000.0 + i))
    full = store.get_series("total_amount", n=10)
    trimmed = store.get_series("total_amount", n=10, exclude_date="2026-05-14")
    assert len(full) == 5
    assert len(trimmed) == 4
    assert 1004.0 not in trimmed


def test_get_latest_factor_value_returns_recent_non_null(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    store.upsert(MacroSlice(date="2026-05-10", total_amount=1000.0))
    store.upsert(MacroSlice(date="2026-05-11", total_amount=None))

    assert store.get_latest_factor_value("total_amount") == ("2026-05-10", 1000.0)


def test_swing_store_preserves_extra(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    store.upsert(
        MacroSlice(
            date="2026-05-12",
            total_amount=1000.0,
            extra={"collect_status": {"total_amount": {"available": True, "source": "test"}}},
        )
    )

    latest = store.get_latest()
    assert latest.extra["collect_status"]["total_amount"]["source"] == "test"


# ----------------------------------------------------------------- 趋势策略
def test_compute_macd_shape():
    close = pd.Series(np.linspace(10, 20, 100))
    macd = compute_macd(close)
    assert set(macd.columns) == {"dif", "dea", "hist"}
    assert len(macd) == 100


class _FakeFetcher:
    def __init__(self, df):
        self._df = df

    def get_historical_data(self, code, days=60):
        return self._df


def _trending_df(n=260):
    """构造长期横盘、最后一根放量上穿 MA60 的日 K（让上穿恰好发生在末日）。"""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = np.full(n, 10.0)
    close[-1] = 12.0  # 末日跳上 MA60 上方（前一日恰在均线处）
    vol = np.full(n, 1_000_000.0)
    vol[-1] = 5_000_000.0  # 末日放量
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": vol.astype(int),
        }
    )


def test_trend_strategy_detects_volume_breakout():
    strat = TrendConfirmStrategy(_FakeFetcher(_trending_df()), ma_periods=[20, 60], volume_breakout_ratio=1.5)
    signals = strat.evaluate("510300", "沪深300ETF")
    types = {s.signal_type for s in signals}
    assert "volume_breakout_ma60" in types


def test_trend_strategy_requires_volume_confirmation_for_breakout():
    df = _trending_df().drop(columns=["volume"])
    strat = TrendConfirmStrategy(_FakeFetcher(df), ma_periods=[20, 60], volume_breakout_ratio=1.5)

    assert "volume_breakout_ma60" not in {s.signal_type for s in strat.evaluate("510300", "沪深300ETF")}


def test_trend_strategy_ignores_invalid_kline_rows():
    df = _trending_df()
    dirty = pd.concat(
        [
            df.iloc[:10],
            pd.DataFrame(
                [{"date": "not-a-date", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1_000_000}]
            ),
            df.iloc[10:],
            pd.DataFrame(
                [{"date": df["date"].iloc[-1], "open": 0, "high": 0, "low": 0, "close": 0, "volume": 9_999_999}]
            ),
        ],
        ignore_index=True,
    )
    strat = TrendConfirmStrategy(_FakeFetcher(dirty), ma_periods=[20, 60], volume_breakout_ratio=1.5)
    signals = strat.evaluate("510300", "沪深300ETF")

    assert strat.last_data_ok is True
    assert strat.last_data_rows == len(df)
    assert "volume_breakout_ma60" in {s.signal_type for s in signals}


def test_trend_strategy_weekly_resample_uses_trading_week_friday():
    dates = pd.to_datetime(["2026-06-04", "2026-06-05", "2026-06-08"])
    df = pd.DataFrame({"date": dates, "close": [10.0, 11.0, 12.0], "volume": [100, 200, 300]})

    weekly = TrendConfirmStrategy._resample_weekly(df)

    assert weekly is not None
    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2026-06-05", "2026-06-12"]
    assert weekly["close"].tolist() == [11.0, 12.0]
    assert weekly["volume"].tolist() == [300, 300]


def test_trend_strategy_excludes_incomplete_week():
    dates = pd.to_datetime(["2026-06-04", "2026-06-05", "2026-06-08"])
    df = pd.DataFrame({"date": dates, "close": [10.0, 11.0, 12.0], "volume": [100, 200, 300]})

    weekly = TrendConfirmStrategy._resample_completed_weekly(df)

    assert weekly is not None
    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2026-06-05"]
    assert weekly["close"].tolist() == [11.0]


def test_trend_strategy_keeps_completed_friday_week():
    dates = pd.to_datetime(["2026-06-05", "2026-06-08", "2026-06-12"])
    df = pd.DataFrame({"date": dates, "close": [10.0, 11.0, 12.0]})

    weekly = TrendConfirmStrategy._resample_completed_weekly(df)

    assert weekly is not None
    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2026-06-05", "2026-06-12"]


def test_trend_strategy_silent_on_flat_market():
    n = 260
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = np.full(n, 10.0)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.01,
            "low": close - 0.01,
            "close": close,
            "volume": np.full(n, 1_000_000, dtype=int),
        }
    )
    strat = TrendConfirmStrategy(_FakeFetcher(df))
    assert strat.evaluate("510300", "x") == []


# ----------------------------------------------------------------- Reporter
class _SpyNotifier:
    def __init__(self):
        self.macro_calls = []
        self.trend_calls = []

    def send_macro_alert(self, fact, level="info", detail=None):
        self.macro_calls.append((fact, level))
        return True

    def send_trend_alert(self, name, code, signal, period, detail=None):
        self.trend_calls.append((name, code, signal, period))
        return True


def test_reporter_silent_when_nothing():
    spy = _SpyNotifier()
    result = SwingReporter(spy).report([], [])
    assert result["silent"] is True
    assert result["sent"] == 0
    assert not spy.macro_calls and not spy.trend_calls


def test_reporter_sends_on_signal():
    from radar.swing.macro_snapshot import MacroExtreme
    from radar.swing.trend_strategy import TrendSignal

    spy = _SpyNotifier()
    extremes = [MacroExtreme("limit_ratio", "涨跌停家数比", 50.0, 99.0, "沸点", 30)]
    signals = [TrendSignal("510300", "沪深300ETF", "weekly_macd_golden_cross", "W", "沪深300ETF 周线 MACD 金叉")]
    result = SwingReporter(spy).report(extremes, signals)
    assert result["silent"] is False
    assert result["sent"] == 2
    assert len(spy.macro_calls) == 1 and len(spy.trend_calls) == 1


def test_reporter_returns_only_successfully_notified_signal_keys():
    from radar.swing.trend_strategy import TrendSignal

    spy = _SpyNotifier()
    signal = TrendSignal(
        "510300",
        "沪深300ETF",
        "weekly_macd_golden_cross",
        "W",
        "沪深300ETF 周线 MACD 金叉",
        detail={"data_date": "2026-07-03"},
    )

    result = SwingReporter(spy).report([], [signal])

    assert result["sent_signal_keys"] == [
        ("510300", "weekly_macd_golden_cross", "W", "2026-07-03")
    ]
    assert len(spy.trend_calls) == 1


def test_reporter_can_hold_macro_for_daily_report():
    from radar.swing.macro_snapshot import MacroExtreme
    from radar.swing.trend_strategy import TrendSignal

    spy = _SpyNotifier()
    extremes = [MacroExtreme("limit_ratio", "涨跌停家数比", 50.0, 99.0, "沸点", 30)]
    signals = [TrendSignal("510300", "沪深300ETF", "weekly_macd_golden_cross", "W", "沪深300ETF 周线 MACD 金叉")]
    result = SwingReporter(spy).report(extremes, signals, dispatch_macro=False)
    assert result["facts"] and spy.macro_calls == []
    assert len(spy.trend_calls) == 1
    assert result["sent"] == 1


def test_reporter_dry_run_does_not_send():
    from radar.swing.trend_strategy import TrendSignal

    spy = _SpyNotifier()
    signals = [TrendSignal("510300", "x", "volume_breakout_ma60", "D", "x 放量突破 MA60")]
    result = SwingReporter(spy).report([], signals, dry_run=True)
    assert result["sent"] == 0
    assert not spy.trend_calls
    assert result["facts"]  # 仍然产出 facts 供日志


def test_swing_eod_reports_datasource_failure_when_all_etfs_have_no_kline(tmp_path, monkeypatch):
    import radar_swing_eod as eod

    cfg = _swing_cfg()
    cfg["swing"]["store_file"] = str(tmp_path / "swing_macro.db")
    cfg_path = tmp_path / "config.swing.json"
    cfg_path.write_text(__import__("json").dumps(cfg), encoding="utf-8")

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        def is_trading_day(self):
            return True

    class _Collector:
        def __init__(self, *_args, **_kwargs):
            pass

        def collect(self, **_kwargs):
            return MacroSlice(date="2026-06-05", source="test")

        def detect_extremes(self, _slice):
            return []

    class _NoDataStrategy:
        last_data_ok = False

        def __init__(self, *_args, **_kwargs):
            pass

        def evaluate(self, *_args, **_kwargs):
            self.last_data_ok = False
            return []

    sent = []

    class _Notifier:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_datasource_alert(self, source_name, failure_count, status="abnormal"):
            sent.append((source_name, failure_count, status))
            return True

    monkeypatch.setattr(eod, "ScanScheduler", _Scheduler)
    monkeypatch.setattr(eod, "MacroSnapshotCollector", _Collector)
    monkeypatch.setattr(eod, "TrendConfirmStrategy", _NoDataStrategy)
    monkeypatch.setattr(eod, "MarketDataFetcher", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(eod, "TelegramNotifier", _Notifier)

    result = eod.run(str(cfg_path), dry_run=False)

    assert result["signals"] == 0
    assert result["trend_data_ok"] == 0
    assert len(result["trend_data_failed"]) == 10
    assert sent == []
    assert result.get("deferred_to_daily_report") is True
    assert "不能视为‘无推荐’" in result["facts"][0]


def test_swing_eod_data_date_dedupe_and_dry_run_do_not_consume_signal(tmp_path, monkeypatch):
    import radar_swing_eod as eod
    from radar.swing.trend_strategy import TrendSignal

    cfg = _swing_cfg()
    cfg["swing"]["store_file"] = str(tmp_path / "swing_macro.db")
    from tests.notify_v2_helpers import make_v6_store
    make_v6_store(tmp_path / "swing_macro.db")  # v6 dispatch queue (pre-v6 DBs pause sending)
    cfg_path = tmp_path / "config.swing.json"
    cfg_path.write_text(__import__("json").dumps(cfg), encoding="utf-8")

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def is_trading_day():
            return True

    decision_dates = iter(["2026-07-03", "2026-07-06", "2026-07-07"])

    class _Collector:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def collect(**_kwargs):
            return MacroSlice(date=next(decision_dates), source="test")

        @staticmethod
        def detect_extremes(_slice):
            return []

    class _Strategy:
        last_data_ok = True

        def __init__(self, *_args, **_kwargs):
            pass

        def evaluate(self, code, name):
            self.last_data_ok = True
            if code != "510300":
                return []
            return [
                TrendSignal(
                    code,
                    name,
                    "weekly_macd_golden_cross",
                    "W",
                    f"{name} 周线 MACD 金叉",
                    detail={"data_date": "2026-07-03"},
                )
            ]

    delivered = []

    class _Notifier:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def send_trend_alert(name, code, signal, period, detail=None):
            delivered.append((code, detail["data_date"]))
            return True

    monkeypatch.setattr(eod, "ScanScheduler", _Scheduler)
    monkeypatch.setattr(eod, "MacroSnapshotCollector", _Collector)
    monkeypatch.setattr(eod, "TrendConfirmStrategy", _Strategy)
    monkeypatch.setattr(eod, "MarketDataFetcher", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(eod, "TelegramNotifier", _Notifier)
    monkeypatch.setattr(
        eod,
        "_check_runtime_dependencies",
        lambda *_a, **_k: {"akshare": True, "requests": True, "store_writable": True},
    )

    dry = eod.run(str(cfg_path), dry_run=True)
    first_real = eod.run(str(cfg_path), dry_run=False)
    duplicate = eod.run(str(cfg_path), dry_run=False)

    assert dry["notification_candidates"] == 1
    assert first_real["sent"] == 1
    assert duplicate["signals"] == 0
    assert duplicate["notification_candidates"] == 0
    assert delivered == [("510300", "2026-07-03")]

    recent = SwingStore(str(tmp_path / "swing_macro.db")).get_recent_signals(10)
    assert len(recent) == 1
    assert recent[0]["date"] == "2026-07-06"
    assert recent[0]["notified"] is True
