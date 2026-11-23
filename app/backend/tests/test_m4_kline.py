"""K-line source adapters: parsing, normalisation, fallback and publication lag."""

import datetime as dt

import pytest

from qp.market.http import FetchError
from qp.market.kline import (
    DEFAULT_SOURCES,
    DailyBar,
    KlineFetcher,
    exchange_prefixed_code,
    fetch_akshare_bars,
    fetch_sina_bars,
    normalize_bars,
    parse_eastmoney_klines,
    parse_sina_klines,
    parse_tencent_klines,
    resolve_secid,
)

END = dt.date(2026, 11, 12)


def business_days(count: int, end: dt.date = END) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= dt.timedelta(days=1)
    return sorted(days)


def eastmoney_rows(days: list[dt.date]) -> list[str]:
    return [f"{day.isoformat()},10.0,11.0,12.0,9.0,1000" for day in days]


def tencent_rows(days: list[dt.date]) -> list[list]:
    return [[day.isoformat(), "10.0", "11.0", "12.0", "9.0", "1000"] for day in days]


def sina_rows(days: list[dt.date]) -> list[dict]:
    return [
        {"day": day.isoformat(), "open": "10.0", "high": "12.0", "low": "9.0", "close": "11.0", "volume": "1000"}
        for day in days
    ]


def test_resolve_secid_and_prefix():
    assert resolve_secid("600000") == "1.600000"
    assert resolve_secid("510300") == "1.510300"
    assert resolve_secid("110059") == "1.110059"
    assert resolve_secid("000001") == "0.000001"
    assert resolve_secid("159915") == "0.159915"
    assert exchange_prefixed_code("600000") == "sh600000"
    assert exchange_prefixed_code("159915") == "sz159915"


def test_parse_eastmoney_orders_ohlcv():
    bars = parse_eastmoney_klines(eastmoney_rows(business_days(30)))
    assert len(bars) == 30
    assert bars[0].open == 10.0 and bars[0].close == 11.0
    assert bars[0].high == 12.0 and bars[0].low == 9.0
    assert bars[0].volume == 1000.0
    assert bars[-1].date == END


def test_parse_tencent_orders_ohlcv():
    bars = parse_tencent_klines(tencent_rows(business_days(30)))
    assert bars[-1].close == 11.0 and bars[-1].high == 12.0
    assert bars[-1].date == END


def test_parse_sina_orders_ohlcv_and_is_a_default_fallback():
    bars = parse_sina_klines(sina_rows(business_days(30)))
    assert bars[-1].close == 11.0 and bars[-1].high == 12.0
    assert bars[-1].date == END
    assert "sina_kline" in [name for name, _ in DEFAULT_SOURCES]


def test_sina_adapter_uses_proxy_false_and_parses(monkeypatch):
    class Response:
        def json(self):
            return sina_rows(business_days(30))

    calls = []

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("qp.market.kline.http.fetch", fake_fetch)
    bars = fetch_sina_bars("510300", 50)
    assert bars[-1].date == END
    assert calls and calls[0][1]["proxy"] is False


def test_parsers_reject_short_or_malformed_rows():
    with pytest.raises(ValueError):
        parse_eastmoney_klines(eastmoney_rows(business_days(29)))
    with pytest.raises(ValueError):
        parse_tencent_klines(tencent_rows(business_days(29)))
    with pytest.raises(ValueError):
        parse_eastmoney_klines(["2026-11-12,1,2"])
    with pytest.raises(ValueError):
        parse_tencent_klines([["2026-11-12", "1", "0", "3", "4", "5"]] * 30)  # zero close


def test_normalize_dedupes_last_and_sorts():
    days = business_days(30)
    bars = parse_tencent_klines(tencent_rows(days))
    duplicate = DailyBar(days[-1], 1, 1, 1, 99.0, 1)
    got = normalize_bars([bars[-1], bars[0], *bars[1:-1], duplicate])
    assert [b.date for b in got] == sorted({b.date for b in bars})
    assert got[-1].close == 99.0


def test_fetcher_falls_back_to_second_source():
    def broken(code, days):
        raise FetchError("eastmoney_kline:http_502")

    good = lambda code, days: parse_tencent_klines(tencent_rows(business_days(30)))  # noqa: E731
    fetcher = KlineFetcher(wait_seconds=0, sources=[("eastmoney", broken), ("tencent", good)])
    result = fetcher.get("510300", required_date=END, previous_session=business_days(2)[0])
    assert result.source == "tencent" and result.bars[-1].date == END
    assert [a["source"] for a in result.attempts] == ["eastmoney", "tencent"]
    assert result.publication_lag is False


def test_fetcher_treats_previous_session_as_publication_lag_and_waits_once():
    previous = business_days(2)[0]
    stale = lambda code, days: parse_tencent_klines(  # noqa: E731
        tencent_rows(business_days(30, previous))
    )
    sleeps: list[float] = []
    fetcher = KlineFetcher(wait_seconds=7, sleep=sleeps.append, sources=[("tencent", stale)])
    result = fetcher.get("510300", required_date=END, previous_session=previous)
    assert result.bars == [] and result.publication_lag is True
    assert sleeps == [7]
    # One bounded wait per decision date, even for a second symbol.
    fetcher.get("159915", required_date=END, previous_session=previous)
    assert sleeps == [7]


def test_fetcher_rejects_wrong_date_without_previous_session():
    stale = lambda code, days: parse_tencent_klines(  # noqa: E731
        tencent_rows(business_days(30, business_days(2)[0]))
    )
    fetcher = KlineFetcher(wait_seconds=0, sources=[("tencent", stale)])
    result = fetcher.get("510300", required_date=END)
    assert result.bars == [] and result.publication_lag is False
    assert result.attempts[-1]["error"] == "date_mismatch"


def test_akshare_source_is_unavailable():
    fetcher = KlineFetcher(wait_seconds=0, sources=[("akshare", fetch_akshare_bars)])
    result = fetcher.get("510300", required_date=END)
    assert result.bars == []
    assert result.attempts[-1]["error"] == "FetchError"
