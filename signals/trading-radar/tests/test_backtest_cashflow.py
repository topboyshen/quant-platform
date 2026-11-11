from __future__ import annotations

import math

import pandas as pd

from radar.backtest.backtest_engine import BacktestEngine as SimpleBacktestEngine
from radar.backtest.backtest_engine import Strategy as SimpleStrategy
from radar.backtest.backtest_pro import BacktestEngine as ProBacktestEngine
from radar.backtest.backtest_pro import Strategy as ProStrategy


class EnterThenExitSimple(SimpleStrategy):
    def __init__(self):
        super().__init__("enter_then_exit")

    def should_enter(self, data: pd.Series, context: dict) -> bool:
        return context["index"] == 0

    def should_exit(self, data: pd.Series, context: dict, entry_price: float):
        return context["index"] == 2, "planned_exit"


class EnterThenExitPro(ProStrategy):
    def __init__(self):
        super().__init__("enter_then_exit")

    def should_enter(self, data: pd.Series, context: dict) -> bool:
        return context["index"] == 0

    def should_exit(self, data: pd.Series, context: dict, entry_price: float):
        return context["index"] == 2, "planned_exit"

    def calculate_position_size(self, capital: float, price: float) -> int:
        return int((capital * 0.5) / price)


def _four_bar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "price": [100.0, 100.0, 110.0, 110.0],
            "volume": [1000.0, 1000.0, 1000.0, 1000.0],
        },
        index=pd.date_range("2024-01-01", periods=4, freq="D"),
    )


def test_simple_backtest_uses_next_bar_execution_and_real_cashflow():
    engine = SimpleBacktestEngine(initial_capital=10_000.0, commission_rate=0.0, slippage=0.0)

    result = engine.run(_four_bar_frame(), EnterThenExitSimple(), position_size=0.5)

    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.entry_time == pd.Timestamp("2024-01-02")
    assert trade.exit_time == pd.Timestamp("2024-01-04")
    assert trade.shares == 50
    assert trade.pnl == 500.0
    assert trade.pnl_pct == 10.0
    assert result.equity_curve[-1] == 10_500.0
    assert result.total_return == 0.05


def test_pro_backtest_close_returns_sale_proceeds_to_cash():
    engine = ProBacktestEngine(
        initial_capital=10_000.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        transfer_fee=0.0,
        slippage=0.0,
    )

    result = engine.run(_four_bar_frame(), EnterThenExitPro())

    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.entry_time == pd.Timestamp("2024-01-02")
    assert trade.exit_time == pd.Timestamp("2024-01-04")
    assert trade.pnl == 500.0
    assert trade.pnl_pct == 10.0
    assert math.isclose(result.equity_curve["cash"].iloc[-1], 10_500.0)
    assert math.isclose(result.equity_curve["total_value"].iloc[-1], 10_500.0)
    assert result.total_return == 0.05


def test_pro_backtest_context_only_exposes_past_bars():
    seen_lengths: list[int] = []

    class ContextStrategy(ProStrategy):
        def __init__(self):
            super().__init__("context")

        def should_enter(self, data: pd.Series, context: dict) -> bool:
            seen_lengths.append(len(context["df"]))
            return False

        def should_exit(self, data: pd.Series, context: dict, entry_price: float):
            return False, ""

    engine = ProBacktestEngine(initial_capital=10_000.0, commission_rate=0.0, stamp_duty=0.0, transfer_fee=0.0, slippage=0.0)
    engine.run(_four_bar_frame(), ContextStrategy())

    assert seen_lengths == [1, 2, 3, 4]
