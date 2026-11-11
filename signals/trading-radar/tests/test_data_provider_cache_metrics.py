"""DataCache hit/miss 指标 + EastMoneyProvider 缓存命中决策测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

pytest.importorskip("cachetools")

from dashboard.data_provider import (
    DataCache,
    EastMoneyProvider,
    TickData,
    get_dynamic_ttl,
    get_secid,
)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("510300", "1.510300"),
        ("513180", "1.513180"),
        ("159915", "0.159915"),
        ("600000", "1.600000"),
    ],
)
def test_dashboard_secid_handles_etf_markets(code, expected):
    assert get_secid(code) == expected


def test_data_cache_metrics_starts_at_zero():
    cache = DataCache()
    m = cache.get_metrics()
    assert m == {
        "tick_hits": 0,
        "tick_misses": 0,
        "tick_hit_ratio_pct": 0.0,
        "tick_cache_size": 0,
    }


def test_data_cache_record_hit_and_miss_updates_ratio():
    cache = DataCache()
    cache.record_tick_hit()
    cache.record_tick_hit()
    cache.record_tick_hit()
    cache.record_tick_miss()
    m = cache.get_metrics()
    assert m["tick_hits"] == 3
    assert m["tick_misses"] == 1
    assert m["tick_hit_ratio_pct"] == 75.0


def test_data_cache_metrics_reflect_tick_count():
    cache = DataCache()
    cache.update_tick(TickData(symbol="000001"))
    cache.update_tick(TickData(symbol="600000"))
    assert cache.get_metrics()["tick_cache_size"] == 2


def test_provider_get_realtime_records_hit_when_cache_fresh():
    """缓存在 TTL 内时 get_realtime 不应触网，且每个 code 计入 hit。"""
    cache = DataCache()
    provider = EastMoneyProvider(cache)
    fresh_ts = datetime.now().isoformat()
    cache.update_tick(TickData(symbol="000001", price=10.0, timestamp=fresh_ts))
    cache.update_tick(TickData(symbol="600000", price=20.0, timestamp=fresh_ts))

    provider._request_with_retry = MagicMock(side_effect=AssertionError("upstream should not be called"))

    result, _ = provider.get_realtime(["000001", "600000"])
    assert set(result.keys()) == {"000001", "600000"}

    metrics = cache.get_metrics()
    assert metrics["tick_hits"] == 2
    assert metrics["tick_misses"] == 0


def test_provider_cache_hit_ratio_regression_threshold():
    cache = DataCache()
    for idx in range(8):
        cache.record_tick_hit()
    for idx in range(2):
        cache.record_tick_miss()

    assert cache.get_metrics()["tick_hit_ratio_pct"] >= 80.0


def test_provider_get_realtime_records_miss_when_cache_stale():
    """缓存过期时应记 miss；upstream 调用失败也不影响计数。"""
    cache = DataCache()
    provider = EastMoneyProvider(cache)
    stale_ts = (datetime.now() - timedelta(seconds=get_dynamic_ttl(8) + 60)).isoformat()
    cache.update_tick(TickData(symbol="000001", price=10.0, timestamp=stale_ts))

    # mock 上游：返回空，避免真正 HTTP；不影响 miss 计数
    provider._request_with_retry = MagicMock(return_value={"data": None})
    provider._fetch_tencent = MagicMock(return_value={})

    provider.get_realtime(["000001"])
    metrics = cache.get_metrics()
    assert metrics["tick_misses"] == 1
    assert metrics["tick_hits"] == 0


def test_provider_backoff_skips_repeated_eastmoney_5xx():
    cache = DataCache()
    provider = EastMoneyProvider(cache)
    failed = MagicMock(status_code=502)
    provider.session.get = MagicMock(return_value=failed)

    assert provider._request_with_retry("http://example.test", {}) is None
    assert provider._request_with_retry("http://example.test", {}) is None

    assert provider.session.get.call_count == 1


def test_provider_default_eastmoney_backoff_is_five_minutes(monkeypatch):
    monkeypatch.delenv("RADAR_DASHBOARD_EASTMONEY_BACKOFF_SEC", raising=False)
    provider = EastMoneyProvider(DataCache())
    assert provider._eastmoney_backoff_sec == 300.0


def test_tencent_fallback_emits_delta_instead_of_double_updating_cache():
    parts = [""] * 38
    parts[1] = "东方财富"
    parts[2] = "300059"
    parts[3] = "19.63"
    parts[4] = "19.20"
    parts[5] = "19.30"
    parts[6] = "1000"
    parts[32] = "2.24"
    parts[33] = "19.80"
    parts[34] = "19.10"
    parts[37] = "100000"

    response = MagicMock()
    response.text = f'v_sz300059="{"~".join(parts)}";'
    cache = DataCache()
    provider = EastMoneyProvider(cache)
    provider._request_with_retry = MagicMock(return_value=None)
    provider._tencent_session.get = MagicMock(return_value=response)

    result, deltas = provider.get_realtime(["300059"])

    assert result["300059"].source == "tencent"
    assert deltas["300059"]["price"] == pytest.approx(19.63)


def test_tencent_fallback_rejects_http_error():
    response = MagicMock()
    response.raise_for_status.side_effect = requests.HTTPError("502")
    provider = EastMoneyProvider(DataCache())
    provider._tencent_session.get = MagicMock(return_value=response)

    assert provider._fetch_tencent(["300059"]) == {}


@pytest.mark.parametrize("price", ["0", "nan", "-1"])
def test_tencent_fallback_rejects_invalid_price(price):
    parts = [""] * 38
    parts[1] = "东方财富"
    parts[2] = "300059"
    parts[3] = price
    response = MagicMock()
    response.text = f'v_sz300059="{"~".join(parts)}";'
    provider = EastMoneyProvider(DataCache())
    provider._tencent_session.get = MagicMock(return_value=response)

    assert provider._fetch_tencent(["300059"]) == {}


def test_single_eastmoney_success_does_not_get_overwritten_by_tencent():
    cache = DataCache()
    provider = EastMoneyProvider(cache)
    provider._request_with_retry = MagicMock(
        return_value={"data": {"f58": "平安银行", "f43": 10.25, "f60": 10.0}}
    )
    provider._fetch_tencent = MagicMock(return_value={})

    result, _ = provider.get_realtime(["000001"])

    assert result["000001"].source == "eastmoney"
    assert result["000001"].price == pytest.approx(10.25)
    provider._fetch_tencent.assert_not_called()


def test_all_sources_failed_preserves_stale_real_tick():
    cache = DataCache()
    old_timestamp = (datetime.now() - timedelta(minutes=10)).isoformat()
    cache.update_tick(TickData(symbol="000001", name="平安银行", price=10.0, timestamp=old_timestamp))
    provider = EastMoneyProvider(cache)
    provider._request_with_retry = MagicMock(return_value=None)
    provider._fetch_tencent = MagicMock(return_value={})

    result, _ = provider.get_realtime(["000001"])

    tick = result["000001"]
    assert tick.price == pytest.approx(10.0)
    assert tick.source == "eastmoney"
    assert tick.is_stale is True
    assert tick.data_age_seconds >= 600
