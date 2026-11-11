import pandas as pd

from app.services.backtest import BacktestService


def test_buy_hold_benchmark_calculates_return_drawdown_and_annualization():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2024-01-01", "2024-06-01", "2024-12-31"]),
        "close": [100.0, 80.0, 120.0],
    })

    result = BacktestService()._buy_hold_benchmark(frame, 100_000)

    assert result["totalReturn"] == 20.0
    assert result["maxDrawdown"] == -20.0
    assert result["annualReturn"] == 20.0
    assert result["sharpeRatio"] is not None


def test_buy_hold_benchmark_aligns_valid_prices_and_timestamps():
    frame = pd.DataFrame({
        "date": ["2024-01-03", "invalid", "2024-01-01", "2024-01-02"],
        "Close": [121.0, 999.0, 100.0, 110.0],
    })

    result = BacktestService()._buy_hold_benchmark(frame, 10_000)

    assert result["totalReturn"] == 21.0
    assert result["maxDrawdown"] == 0.0


def test_buy_hold_benchmark_rejects_insufficient_or_invalid_prices():
    service = BacktestService()

    assert service._buy_hold_benchmark(pd.DataFrame({"close": [100.0]}), 10_000) is None
    assert service._buy_hold_benchmark(pd.DataFrame({"close": [0.0, -1.0]}), 10_000) is None
    assert service._buy_hold_benchmark(pd.DataFrame({"open": [1.0, 2.0]}), 10_000) is None
