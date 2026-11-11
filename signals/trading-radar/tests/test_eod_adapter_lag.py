"""Exercise the REAL AkShare adapter boundary, not a stub returning stale frames."""
import json
import logging
from types import SimpleNamespace

import pandas as pd
import pytest

import radar.data.market_data_v2 as market
from radar.data.kline_sources import fetch_akshare_kline
from runtime_contracts.calendar import sessions
from tests.test_eod_contracts import NOW, calendar_payload


def frame(day="2026-09-10", n=260):
    return pd.DataFrame({"date": pd.date_range(end=day, periods=n), "open": 10., "close": 10.,
                         "high": 11., "low": 9., "volume": 1000})


@pytest.fixture
def harness(monkeypatch, tmp_path):
    cal = tmp_path / "calendar.json"; cal.write_text(json.dumps(calendar_payload()))
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(cal))
    monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", "960")
    monkeypatch.setattr(market, "sessions", lambda payload, now: sessions(payload, NOW))
    for key in ("RADAR_ALL_PROXY", "RADAR_HTTP_PROXY", "RADAR_HTTPS_PROXY"):
        monkeypatch.delenv(key, raising=False)
    f = object.__new__(market.MarketDataFetcher)
    f.logger = logging.getLogger("offline-adapter-test")
    f._try_load_parquet = lambda *a: None
    f._fetch_eastmoney_kline = lambda *a: None
    f._fetch_tencent_kline = lambda *a: None
    waits, writes = [], []
    f._sleep = waits.append
    def persist(code, data, **kw):
        assert data.date.max().date().isoformat() == kw["required_date"]
        writes.append(code)
        return data
    f._normalize_and_persist = persist
    return f, waits, writes


def adapter(f, stale_day="2026-09-10", fresh_after=None, n=260):
    calls = []
    class AK:
        def fund_etf_hist_em(self, **kw):
            raise ConnectionError("offline fixture")
        def stock_zh_a_hist(self, **kw):
            raise ConnectionError("offline fixture")
        def fund_etf_hist_sina(self, **kw):
            calls.append(kw["symbol"])
            date = "2026-09-11" if fresh_after and len(calls) >= fresh_after else stale_day
            return frame(date, n)
    f._akshare_fetcher = SimpleNamespace(is_available=True, _ak=AK())
    return calls


def test_original_failure_shape_retries_and_recovers(harness, caplog):
    f, waits, writes = harness
    calls = adapter(f, fresh_after=2)
    caplog.set_level(logging.INFO, logger="offline-adapter-test")
    result = f.get_historical_data("510300", 250, required_date="2026-09-11")
    assert result.date.max().date().isoformat() == "2026-09-11"
    assert waits == [960] and calls == ["sh510300", "sh510300"] and writes == ["510300"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_expected_source_failure_is_info_when_fallback_succeeds(harness, caplog):
    f, _waits, _writes = harness
    adapter(f, fresh_after=1)
    caplog.set_level(logging.INFO, logger="offline-adapter-test")

    result = fetch_akshare_kline(
        f.logger, f._akshare_fetcher, "510300", 250,
        required_date="2026-09-11", observations=[],
    )

    assert result is not None
    assert result.date.max().date().isoformat() == "2026-09-11"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_still_stale_never_persists_and_waits_only_once_for_all_symbols(harness):
    f, waits, writes = harness; calls = adapter(f)
    for code in ("510300", "510500"):
        assert f.get_historical_data(code, 250, required_date="2026-09-11").empty
    assert waits == [960] and writes == [] and len(calls) == 3


@pytest.mark.parametrize("stale_day,n", [("2026-09-09", 260), ("2026-09-12", 260), ("2026-09-10", 29)])
def test_old_future_or_short_data_cannot_trigger_publication_retry(harness, stale_day, n):
    f, waits, writes = harness; adapter(f, stale_day=stale_day, n=n)
    assert f.get_historical_data("510300", 250, required_date="2026-09-11").empty
    assert waits == writes == []


def test_real_adapter_exposes_only_metadata_for_rejected_prices(harness):
    f, waits, writes = harness; adapter(f)
    observations = []
    result = fetch_akshare_kline(f.logger, f._akshare_fetcher, "510300", 250,
                                required_date="2026-09-11", observations=observations)
    assert result is None
    stale = next(o for o in observations if o["state"] == "stale")
    assert stale["source"] == "akshare_fund_etf_hist_sina" and stale["route"] == "direct"
    assert stale["latest_date"] == "2026-09-10" and stale["rows"] == 260
    assert "frame" not in stale and "close" not in stale


def test_outage_and_missing_calendar_do_not_wait(harness, monkeypatch, tmp_path):
    f, waits, writes = harness
    f._akshare_fetcher = SimpleNamespace(is_available=False)
    assert f.get_historical_data("510300", 250, required_date="2026-09-11").empty
    adapter(f)
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(tmp_path / "missing.json"))
    assert f.get_historical_data("510300", 250, required_date="2026-09-11").empty
    assert waits == writes == []


def test_holiday_gap_uses_previous_session_not_natural_days(harness):
    f, waits, writes = harness; adapter(f, stale_day="2026-09-30")
    assert f.get_historical_data("510300", 250, required_date="2026-10-08").empty
    assert waits == [960] and writes == []


def test_zero_disables_wait_and_bad_env_is_bounded(harness, monkeypatch):
    f, waits, writes = harness; adapter(f)
    for raw, want in (("0", 0), ("-1", 0), ("9999", 1200), ("bad", 45)):
        monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", raw)
        assert f._stale_retry_seconds() == want
    monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", "0")
    assert f.get_historical_data("510300", 250, required_date="2026-09-11").empty
    assert waits == writes == []
