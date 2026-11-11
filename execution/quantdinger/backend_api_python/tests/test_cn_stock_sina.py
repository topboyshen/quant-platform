import json
from urllib import request

from app.data_sources import cn_stock


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit):
        return json.dumps(self._payload).encode("utf-8")


def test_sina_daily_uses_canonical_time_contract(monkeypatch):
    payload = [
        {
            "day": "2026-08-19",
            "open": "3900.00",
            "high": "3920.00",
            "low": "3880.00",
            "close": "3905.23",
            "volume": "123456",
        }
    ]
    monkeypatch.setattr(request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    rows = cn_stock.CNStockDataSource()._fetch_sina_daily("sh000001", 5, None)

    assert len(rows) == 1
    assert rows[0]["close"] == 3905.23
    assert "time" in rows[0]
    assert "ts" not in rows[0]
    assert rows[0]["time"] < 10_000_000_000


def test_daily_fallback_returns_sina_rows_end_to_end(monkeypatch):
    sina_rows = [
        {
            "time": 1_776_585_600,
            "open": 3900.0,
            "high": 3920.0,
            "low": 3880.0,
            "close": 3905.23,
            "volume": 123456.0,
        }
    ]
    monkeypatch.setattr(cn_stock, "fetch_twelvedata_klines", lambda **_kwargs: [])
    monkeypatch.setattr(cn_stock, "fetch_kline", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        cn_stock.CNStockDataSource,
        "_fetch_sina_daily",
        lambda *_args, **_kwargs: sina_rows,
    )
    monkeypatch.setattr(cn_stock, "fetch_yfinance_klines", lambda **_kwargs: [])

    rows = cn_stock.CNStockDataSource().get_kline("sh000001", "1D", 5)

    assert rows == sina_rows


def test_sina_daily_honors_before_time(monkeypatch):
    payload = [
        {"day": "2026-08-18", "open": "1", "high": "2", "low": "1", "close": "2", "volume": "3"},
        {"day": "2026-08-19", "open": "2", "high": "3", "low": "2", "close": "3", "volume": "4"},
    ]
    monkeypatch.setattr(request, "urlopen", lambda *_args, **_kwargs: _Response(payload))
    source = cn_stock.CNStockDataSource()
    all_rows = source._fetch_sina_daily("sh000001", 5, None)

    rows = source._fetch_sina_daily("sh000001", 5, all_rows[-1]["time"])

    assert rows == all_rows[:-1]
