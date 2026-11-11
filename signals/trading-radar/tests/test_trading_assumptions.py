from __future__ import annotations

from radar.strategy.rl_strategy_v2 import PortfolioTracker
from radar.trading_assumptions import TradingCostModel, apply_slippage


def test_cost_model_applies_buy_and_sell_fees():
    model = TradingCostModel(commission_rate=0.001, stamp_duty=0.001, transfer_fee=0.0, min_commission=0.0)

    assert model.calculate(10.0, 100, is_buy=True) == 1.0
    assert model.calculate(10.0, 100, is_buy=False) == 2.0
    assert apply_slippage(10.0, 0.01, is_buy=True) == 10.1
    assert apply_slippage(10.0, 0.01, is_buy=False) == 9.9


def test_portfolio_tracker_uses_cost_model_and_supports_reduce():
    model = TradingCostModel(commission_rate=0.0, stamp_duty=0.0, transfer_fee=0.0, min_commission=0.0)
    tracker = PortfolioTracker(initial_cash=10_000.0, cost_model=model, slippage=0.0)

    assert tracker.execute_trade("000001", "open", price=10.0, shares=100)
    assert tracker.cash == 9_000.0
    assert tracker.positions["000001"]["shares"] == 100

    assert tracker.execute_trade("000001", "add", price=20.0, shares=100)
    assert tracker.cash == 7_000.0
    assert tracker.positions["000001"]["shares"] == 200
    assert tracker.positions["000001"]["cost"] == 15.0

    assert tracker.execute_trade("000001", "reduce", price=30.0, shares=50)
    assert tracker.cash == 8_500.0
    assert tracker.positions["000001"]["shares"] == 150

    assert tracker.execute_trade("000001", "close", price=30.0)
    assert tracker.cash == 13_000.0
    assert "000001" not in tracker.positions
