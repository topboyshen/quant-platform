"""Shared trading assumption helpers for backtests and portfolio tracking."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradingCostModel:
    """A-share style transaction cost model."""

    commission_rate: float = 0.0003
    stamp_duty: float = 0.001
    transfer_fee: float = 0.00002
    min_commission: float = 5.0

    def calculate(self, price: float, shares: int, is_buy: bool) -> float:
        amount = float(price) * int(shares)
        if amount <= 0:
            return 0.0
        commission = max(amount * self.commission_rate, self.min_commission) if self.commission_rate > 0 else 0.0
        transfer = amount * self.transfer_fee
        stamp = 0.0 if is_buy else amount * self.stamp_duty
        return commission + transfer + stamp


def apply_slippage(price: float, slippage: float, is_buy: bool) -> float:
    """Return the assumed fill price after slippage."""

    return float(price) * (1 + slippage if is_buy else 1 - slippage)
