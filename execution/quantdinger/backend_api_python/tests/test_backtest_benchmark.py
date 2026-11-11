import copy
import json

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


def test_buy_hold_benchmark_uses_datetime_index_for_annualized_metrics():
    frame = pd.DataFrame(
        {"close": [120.0, 999.0, 100.0, 80.0]},
        index=pd.to_datetime(
            ["2024-12-31", "invalid", "2024-01-01", "2024-06-01"],
            errors="coerce",
        ),
    )

    result = BacktestService()._buy_hold_benchmark(frame, 100_000)

    assert result["totalReturn"] == 20.0
    assert result["maxDrawdown"] == -20.0
    assert result["annualReturn"] == 20.0
    assert result["sharpeRatio"] is not None


def test_buy_hold_benchmark_handles_datetime_index_named_time():
    frame = pd.DataFrame(
        {"close": [120.0, 100.0, 80.0]},
        index=pd.to_datetime(["2024-12-31", "2024-01-01", "2024-06-01"]).rename("time"),
    )

    result = BacktestService()._buy_hold_benchmark(frame, 100_000)

    assert result["totalReturn"] == 20.0
    assert result["maxDrawdown"] == -20.0
    assert result["annualReturn"] == 20.0
    assert result["sharpeRatio"] is not None


def test_buy_hold_benchmark_rejects_insufficient_or_invalid_prices():
    service = BacktestService()

    assert service._buy_hold_benchmark(pd.DataFrame({"close": [100.0]}), 10_000) is None
    assert service._buy_hold_benchmark(pd.DataFrame({"close": [0.0, -1.0]}), 10_000) is None
    assert service._buy_hold_benchmark(pd.DataFrame({"open": [1.0, 2.0]}), 10_000) is None


def test_benchmark_compat_derives_flat_fields_without_mutating_input():
    original = {
        "totalReturn": 12.5,
        "benchmark": {
            "totalReturn": 0,
            "maxDrawdown": -20.0,
            "annualReturn": 4.2,
            "sharpeRatio": 0.33,
        },
    }
    before = copy.deepcopy(original)

    result = BacktestService._with_benchmark_compat(original, symbol="600875")

    assert result["benchmarkStatus"] == "available"
    assert result["benchmarkTotalReturn"] == 0.0
    assert result["excessReturn"] == 12.5
    assert result["benchmark"]["symbol"] == "600875"
    assert result["benchmark"]["maxDrawdown"] == -20.0
    assert original == before


def test_benchmark_compat_preserves_explicit_aliases_and_symbol():
    result = BacktestService._with_benchmark_compat({
        "totalReturn": 30,
        "benchmarkStatus": "custom",
        "benchmarkTotalReturn": 8,
        "excessReturn": 17,
        "benchmark": {"totalReturn": 10, "symbol": "000300"},
    }, symbol="600875")

    assert result["benchmarkStatus"] == "custom"
    assert result["benchmarkTotalReturn"] == 8
    assert result["excessReturn"] == 17
    assert result["benchmark"]["symbol"] == "000300"


def test_benchmark_compat_rejects_non_finite_and_boolean_values():
    for value in (None, True, float("nan"), float("inf"), "not-a-number"):
        result = BacktestService._with_benchmark_compat({
            "totalReturn": 5,
            "benchmark": {"totalReturn": value},
        })
        assert result["benchmarkStatus"] == "unavailable"
        assert result["benchmarkTotalReturn"] is None
        assert result["excessReturn"] is None


def test_hydrate_old_run_adds_benchmark_compat_without_full_result_in_list():
    row = {
        "symbol": "603099",
        "timeframe": "1D",
        "strategy_config": "{}",
        "config_snapshot": "{}",
        "result_json": json.dumps({
            "totalReturn": 25.0,
            "totalTrades": 1,
            "trades": [
                {"time": "2024-01-02", "type": "open_long", "price": 10},
                {"time": "2024-02-02", "type": "close_long", "price": 12},
            ],
            "benchmark": {
                "totalReturn": -5.0,
                "maxDrawdown": -30.0,
                "annualReturn": -2.0,
                "sharpeRatio": 0.1,
            },
        }),
    }

    detail = BacktestService()._hydrate_run_row(row, include_result=True)
    summary = BacktestService()._hydrate_run_row(row, include_result=False)

    assert detail["result"]["benchmarkStatus"] == "available"
    assert detail["result"]["benchmark"]["symbol"] == "603099"
    assert detail["result"]["excessReturn"] == 30.0
    assert detail["result"]["totalExecutions"] == 2
    assert detail["result"]["resultStatus"] == "completed_trades"
    assert summary["benchmark_status"] == "available"
    assert summary["benchmark_total_return"] == -5.0
    assert summary["excess_return"] == 30.0
    assert summary["total_executions"] == 2
    assert summary["result_status"] == "completed_trades"
    assert "result" not in summary


def test_strategy_research_compat_does_not_relabel_closed_trade_rows():
    result = BacktestService._with_strategy_research_compat({
        "totalTrades": 1,
        "trades": [{
            "entry_time": "2024-01-01",
            "exit_time": "2024-02-01",
            "profit": 10,
        }],
    })

    assert "totalExecutions" not in result
    assert result["resultStatus"] == "completed_trades"
