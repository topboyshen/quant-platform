"""价格触发策略纯逻辑。"""

import pytest

from radar.strategy.base import PriceTriggerStrategy


@pytest.mark.parametrize(
    "direction,price,trigger,triggered",
    [
        ("below", 9.0, 10.0, True),
        ("below", 10.0, 10.0, True),
        ("below", 10.01, 10.0, False),
        ("above", 11.0, 10.0, True),
        ("above", 10.0, 10.0, True),
        ("above", 9.99, 10.0, False),
    ],
)
def test_get_signal_info_trigger(direction, price, trigger, triggered):
    s = PriceTriggerStrategy(trigger_price=trigger, direction=direction)
    info = s.get_signal_info({"price": price})
    assert info["triggered"] is triggered
    assert info["trigger_price"] == trigger
    assert info["price"] == price


def test_should_exit_stop_loss():
    s = PriceTriggerStrategy(100.0, "below", stop_loss_pct=0.1, take_profit_pct=0.2)
    assert s.should_exit({"price": 89.0}, entry_price=100.0) is True


def test_should_exit_take_profit():
    s = PriceTriggerStrategy(100.0, "above", stop_loss_pct=0.1, take_profit_pct=0.2)
    assert s.should_exit({"price": 121.0}, entry_price=100.0) is True
