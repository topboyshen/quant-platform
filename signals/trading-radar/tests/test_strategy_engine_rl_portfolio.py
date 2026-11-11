from __future__ import annotations

import logging
from types import SimpleNamespace

from radar.core.strategy_engine import AsyncStrategyEngine
from radar.strategy.rl_strategy_v2 import PortfolioTracker


class FakeRLStrategy:
    def __init__(self):
        self.account = None

    def set_real_account(self, account):
        self.account = account

    def check_signal_confirm(self, price, trigger_price, direction, volume=0):
        return {
            "using_real_account": self.account is not None,
            "account_position": self.account.position_shares if self.account else 0,
        }

    def check_signal_independent(self, price, volume=0):
        return self.check_signal_confirm(price, 0, "below", volume)


def _target():
    return SimpleNamespace(
        code="000001",
        name="测试",
        direction="below",
        trigger_price=10.0,
        ml_enabled=False,
        rl_enabled=True,
    )


def test_strategy_engine_injects_real_portfolio_into_rl():
    cfg = SimpleNamespace(rl_mode="confirm", rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    fake = FakeRLStrategy()
    engine._rl_strategies["000001"] = fake
    portfolio = PortfolioTracker(initial_cash=100_000.0)
    portfolio.set_position("000001", shares=300, cost=9.5)

    result = engine._check_rl_sync(_target(), {"price": 10.0, "volume": 1000.0}, portfolio)

    assert result.triggered is True
    assert result.signal_info["using_real_account"] is True
    assert result.signal_info["account_position"] == 300
    assert fake.account.position_shares == 300
    engine.shutdown()


def test_strategy_engine_blocks_independent_rl_without_real_account():
    cfg = SimpleNamespace(rl_mode="independent", rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    engine._rl_strategies["000001"] = FakeRLStrategy()

    result = engine._check_rl_sync(_target(), {"price": 10.0}, None)

    assert result.triggered is False
    assert result.signal_info["real_account_required"] is True
    engine.shutdown()
