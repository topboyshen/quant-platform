"""波段化 Dashboard 集成测试：信号持久化 + /swing 路由 + 概览加载。"""

from __future__ import annotations

from radar.swing.store import SwingStore


def test_dashboard_background_refresh_interval_tracks_market_state(monkeypatch):
    from datetime import datetime as real_datetime

    import dashboard.app as dapp

    monkeypatch.setenv("RADAR_DASHBOARD_REFRESH_TRADING_SEC", "4")
    monkeypatch.setenv("RADAR_DASHBOARD_REFRESH_CLOSED_SEC", "90")
    monkeypatch.setenv("RADAR_DASHBOARD_REFRESH_WEEKEND_SEC", "240")

    class WeekdayDateTime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 7, 15, 10, 0)

    monkeypatch.setattr(dapp, "datetime", WeekdayDateTime)
    monkeypatch.setattr(dapp.HolidayCalendar, "is_holiday", lambda _: False)
    monkeypatch.setattr(dapp, "is_trading_time", lambda: True)
    assert dapp._background_refresh_interval() == 4

    monkeypatch.setattr(dapp, "is_trading_time", lambda: False)
    assert dapp._background_refresh_interval() == 90

    class WeekendDateTime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 7, 18, 10, 0)

    monkeypatch.setattr(dapp, "datetime", WeekendDateTime)
    assert dapp._background_refresh_interval() == 240

    monkeypatch.setattr(dapp, "datetime", WeekdayDateTime)
    monkeypatch.setattr(dapp.HolidayCalendar, "is_holiday", lambda _: True)
    monkeypatch.setattr(dapp, "is_trading_time", lambda: True)
    assert dapp._background_refresh_interval() == 240


def test_dashboard_background_refresh_interval_rejects_unsafe_values(monkeypatch):
    from datetime import datetime as real_datetime

    import dashboard.app as dapp

    class WeekdayDateTime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 7, 15, 10, 0)

    monkeypatch.setattr(dapp, "datetime", WeekdayDateTime)
    monkeypatch.setattr(dapp.HolidayCalendar, "is_holiday", lambda _: False)
    monkeypatch.setenv("RADAR_DASHBOARD_REFRESH_TRADING_SEC", "not-a-number")
    monkeypatch.setattr(dapp, "is_trading_time", lambda: True)
    assert dapp._background_refresh_interval() == 5

    monkeypatch.setenv("RADAR_DASHBOARD_REFRESH_CLOSED_SEC", "1")
    monkeypatch.setattr(dapp, "is_trading_time", lambda: False)
    assert dapp._background_refresh_interval() == 60


def test_store_signal_persistence_idempotent(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    sigs = [
        {
            "code": "510300",
            "name": "沪深300ETF",
            "signal_type": "weekly_macd_golden_cross",
            "period": "W",
            "title": "沪深300ETF 周线 MACD 金叉",
            "detail": {"dif": 0.1, "dea": 0.05},
            "confidence": 0.6,
        }
    ]
    store.replace_signals_for_date("2026-05-29", sigs)
    store.replace_signals_for_date("2026-05-29", sigs)  # 重跑覆盖，不应翻倍

    recent = store.get_recent_signals(10)
    assert len(recent) == 1
    assert recent[0]["code"] == "510300"
    assert recent[0]["detail"]["dif"] == 0.1  # JSON 往返
    assert recent[0]["period"] == "W"


def test_store_signal_multi_date_ordering(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    store.replace_signals_for_date("2026-05-28", [{"code": "159915", "name": "创业板ETF"}])
    store.replace_signals_for_date("2026-05-29", [{"code": "510300", "name": "沪深300ETF"}])
    recent = store.get_recent_signals(10)
    assert [r["date"] for r in recent] == ["2026-05-29", "2026-05-28"]  # 倒序


def test_store_deduplicates_same_market_data_bar_across_eod_dates(tmp_path):
    store = SwingStore(str(tmp_path / "macro.db"))
    signal = {
        "code": "510500",
        "name": "中证500ETF",
        "signal_type": "weekly_macd_golden_cross",
        "period": "W",
        "detail": {"data_date": "2026-07-03", "dif": 0.3, "dea": 0.29},
    }

    store.replace_signals_for_date("2026-07-03", [signal])
    store.replace_signals_for_date("2026-07-06", [signal])

    recent = store.get_recent_signals(10)
    assert len(recent) == 1
    assert recent[0]["date"] == "2026-07-03"
    key = ("510500", "weekly_macd_golden_cross", "W", "2026-07-03")
    assert store.get_signal_keys() == {key}
    assert store.get_signal_keys(notified_only=True) == set()

    store.mark_signals_notified([key])
    assert store.get_signal_keys(notified_only=True) == {key}


def test_empty_same_day_signal_rerun_preserves_notified_identity(tmp_path):
    """数据失败产生空结果时，不能抹掉已发送身份并为后续重复推送开门。"""
    store = SwingStore(str(tmp_path / "macro.db"))
    signal = {
        "code": "588000",
        "name": "科创50ETF",
        "signal_type": "weekly_macd_golden_cross",
        "period": "W",
        "title": "周线 MACD 金叉",
        "detail": {"data_date": "2026-07-24"},
        "confidence": 0.9,
    }
    key = store.signal_key(signal)

    store.replace_signals_for_date("2026-07-24", [signal])
    store.mark_signals_notified([key])
    store.replace_signals_for_date("2026-07-24", [])

    assert store.get_signal_keys(notified_only=True) == {key}
    assert len(store.get_recent_signals(10)) == 1

    store.replace_signals_for_date("2026-07-24", [signal])
    rows = store.get_recent_signals(10)
    assert len(rows) == 1
    assert rows[0]["notified"] is True



def test_load_swing_overview_filters_legacy_partial_week_signals(tmp_path):
    import json

    from dashboard.swing_view import load_swing_overview
    from radar.swing.store import MacroSlice

    (tmp_path / "data").mkdir()
    (tmp_path / "config.swing.json").write_text(
        json.dumps({"swing": {"store_file": "data/swing_macro.db"}, "targets": []}),
        encoding="utf-8",
    )
    store = SwingStore(str(tmp_path / "data" / "swing_macro.db"))
    store.upsert(MacroSlice(date="2026-07-06", total_amount=1000.0))
    legacy = {
        "code": "510500",
        "signal_type": "weekly_macd_golden_cross",
        "period": "W",
        "detail": {"dif": 0.3},
    }
    store.replace_signals_for_date("2026-07-03", [legacy])  # Friday: keep
    store.replace_signals_for_date("2026-07-06", [legacy])  # Monday partial: hide

    overview = load_swing_overview(tmp_path)

    assert [s["date"] for s in overview["recent_signals"]] == ["2026-07-03"]
    assert overview["signal_history_filtered"] == 1


def test_load_swing_overview_missing_db(tmp_path, monkeypatch):
    import dashboard.app as dapp

    monkeypatch.setattr(dapp, "BASE_DIR", tmp_path)
    overview = dapp.load_swing_overview()
    assert overview["available"] is False
    assert overview["error"]  # 给出原因


def test_load_swing_overview_with_data(tmp_path, monkeypatch):
    import json

    import dashboard.app as dapp
    from radar.swing.store import MacroSlice

    # 构造独立 BASE_DIR + config.swing.json + db
    (tmp_path / "data").mkdir()
    cfg = {
        "swing": {
            "store_file": "data/swing_macro.db",
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "macro": {"min_history_days": 5, "extreme_percentile_low": 5, "extreme_percentile_high": 95},
        },
        "targets": [{"code": "510300", "name": "沪深300ETF", "mode": "trend"}],
    }
    (tmp_path / "config.swing.json").write_text(json.dumps(cfg), encoding="utf-8")

    store = SwingStore(str(tmp_path / "data" / "swing_macro.db"))
    for i in range(10):
        store.upsert(MacroSlice(date=f"2026-05-{10 + i:02d}", total_amount=1000.0 + i, limit_ratio=1.0))
    store.replace_signals_for_date(
        "2026-05-19",
        [{"code": "510300", "name": "沪深300ETF", "signal_type": "volume_breakout_ma60",
          "period": "D", "title": "沪深300ETF 放量突破 MA60", "detail": {"close": 4.1}, "confidence": 0.55}],
    )

    monkeypatch.setattr(dapp, "BASE_DIR", tmp_path)
    # _load_json_cached 有缓存，按路径区分，这里路径唯一，安全
    overview = dapp.load_swing_overview()
    assert overview["available"] is True
    assert overview["updated"] == "2026-05-19"
    assert len(overview["factors"]) == 3
    assert len(overview["signals"]) == 1
    assert overview["signals"][0]["title"].endswith("放量突破 MA60")
    assert overview["strategy"]["target_count"] == 1
    assert overview["strategy"]["extreme_low"] == 5
    assert all("sample_size" in f for f in overview["factors"])
    assert all("history_ready" in f for f in overview["factors"])


def test_load_swing_overview_does_not_present_history_as_today(tmp_path):
    import json

    from dashboard.swing_view import load_swing_overview
    from radar.swing.store import MacroSlice

    (tmp_path / "data").mkdir()
    (tmp_path / "config.swing.json").write_text(
        json.dumps({"swing": {"store_file": "data/swing_macro.db"}, "targets": []}),
        encoding="utf-8",
    )
    store = SwingStore(str(tmp_path / "data" / "swing_macro.db"))
    store.upsert(MacroSlice(date="2026-07-13", total_amount=1000.0))
    store.replace_signals_for_date(
        "2026-07-10",
        [{"code": "510300", "name": "沪深300ETF", "signal_type": "volume_breakout_ma60"}],
    )

    overview = load_swing_overview(tmp_path)

    assert overview["signals"] == []
    assert len(overview["recent_signals"]) == 1
    assert overview["latest_signal_date"] == "2026-07-10"


def test_load_swing_overview_waits_for_min_history_before_percentile(tmp_path):
    import json

    from dashboard.swing_view import load_swing_overview
    from radar.swing.store import MacroSlice

    (tmp_path / "data").mkdir()
    cfg = {
        "swing": {
            "store_file": "data/swing_macro.db",
            "macro": {"min_history_days": 5, "extreme_percentile_low": 5, "extreme_percentile_high": 95},
        },
        "targets": [],
    }
    (tmp_path / "config.swing.json").write_text(json.dumps(cfg), encoding="utf-8")

    store = SwingStore(str(tmp_path / "data" / "swing_macro.db"))
    for i in range(3):
        store.upsert(MacroSlice(date=f"2026-05-{10 + i:02d}", total_amount=1000.0 + i))

    overview = load_swing_overview(tmp_path)
    total_amount = next(f for f in overview["factors"] if f["factor"] == "total_amount")

    assert total_amount["percentile"] is None
    assert total_amount["level"] == "样本不足"
    assert total_amount["sample_size"] == 2
    assert total_amount["history_ready"] is False


def test_load_swing_overview_uses_recent_non_null_factor_value(tmp_path):
    import json

    from dashboard.swing_view import load_swing_overview
    from radar.swing.store import MacroSlice

    (tmp_path / "data").mkdir()
    cfg = {
        "swing": {
            "store_file": "data/swing_macro.db",
            "macro": {"min_history_days": 5, "extreme_percentile_low": 5, "extreme_percentile_high": 95},
        },
        "targets": [],
    }
    (tmp_path / "config.swing.json").write_text(json.dumps(cfg), encoding="utf-8")

    store = SwingStore(str(tmp_path / "data" / "swing_macro.db"))
    store.upsert(MacroSlice(date="2026-05-10", total_amount=9999.0, margin_growth_pct=0.12))
    store.upsert(MacroSlice(date="2026-05-11", limit_ratio=1.0))

    overview = load_swing_overview(tmp_path)
    by_factor = {f["factor"]: f for f in overview["factors"]}

    assert by_factor["total_amount"]["value"] == 9999.0
    assert by_factor["total_amount"]["value_date"] == "2026-05-10"
    assert by_factor["total_amount"]["is_stale"] is True
    assert by_factor["margin_growth_pct"]["value"] == 0.12


def test_load_swing_overview_exposes_collect_status(tmp_path):
    import json

    from dashboard.swing_view import load_swing_overview
    from radar.swing.store import MacroSlice

    (tmp_path / "data").mkdir()
    cfg = {"swing": {"store_file": "data/swing_macro.db", "macro": {"min_history_days": 5}}, "targets": []}
    (tmp_path / "config.swing.json").write_text(json.dumps(cfg), encoding="utf-8")

    store = SwingStore(str(tmp_path / "data" / "swing_macro.db"))
    store.upsert(
        MacroSlice(
            date="2026-05-11",
            limit_ratio=1.0,
            extra={
                "collect_status": {
                    "total_amount": {"available": False, "source": "eastmoney", "message": "断连"}
                }
            },
        )
    )

    overview = load_swing_overview(tmp_path)
    total_amount = next(f for f in overview["factors"] if f["factor"] == "total_amount")
    assert total_amount["collect_available"] is False
    assert total_amount["collect_source"] == "eastmoney"
    assert total_amount["collect_message"] == "断连"


def test_swing_route_smoke(tmp_path, monkeypatch):
    from dashboard.app import app

    token = "s" * 32
    token_file = tmp_path / "dashboard_api_token"
    token_file.write_text(token, encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("DASHBOARD_API_TOKEN_FILE", str(token_file))
    client = app.test_client()
    resp = client.get("/swing", headers={"X-Dashboard-Token": token})
    assert resp.status_code == 200
    assert "大盘环境".encode() in resp.data or "波段".encode() in resp.data


def test_api_swing_latest_smoke(tmp_path, monkeypatch):
    from dashboard.app import app

    token = "s" * 32
    token_file = tmp_path / "dashboard_api_token"
    token_file.write_text(token, encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("DASHBOARD_API_TOKEN_FILE", str(token_file))
    client = app.test_client()
    resp = client.get(
        "/api/swing/latest", headers={"X-Dashboard-Token": token}
    )
    assert resp.status_code == 200
    assert resp.is_json
    assert "available" in resp.get_json()
