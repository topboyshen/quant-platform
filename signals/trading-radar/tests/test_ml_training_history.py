"""ml_training 历史 K：默认走 MarketDataFetcher（与回测同源）。"""

import pandas as pd
import pytest


@pytest.fixture
def sample_ohlcv():
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "open": [10.0, 10.1],
            "high": [10.5, 10.6],
            "low": [9.9, 10.0],
            "close": [10.2, 10.3],
            "volume": [1000, 2000],
        }
    )


def test_get_stock_history_mkt_uses_market_fetcher(monkeypatch, sample_ohlcv):
    import radar.data.market_data as mdm

    class StubFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def get_historical_data(self, code, days=60):
            assert code == "000001"
            return sample_ohlcv.copy()

    monkeypatch.setattr(mdm, "MarketDataFetcher", StubFetcher)
    from ml_training import get_stock_history_mkt

    out = get_stock_history_mkt("000001", days=5)
    assert out is not None
    assert len(out) == 2
    assert list(out["close"]) == [10.2, 10.3]
