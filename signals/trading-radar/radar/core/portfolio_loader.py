"""从 portfolio.json 加载真实持仓到 PortfolioTracker（供 MonitorEngine 使用）。"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from radar.strategy.rl_strategy_v2 import PortfolioTracker


def load_portfolio_into_tracker(
    tracker: PortfolioTracker,
    portfolio_path: str,
    logger: logging.Logger,
) -> None:
    if not os.path.exists(portfolio_path):
        logger.info("未找到 portfolio.json，跳过真实持仓加载")
        return

    try:
        with open(portfolio_path, "r", encoding="utf-8") as f:
            portfolio: Dict[str, Any] = json.load(f)

        positions = portfolio.get("positions", [])
        for pos in positions:
            code = pos.get("code")
            shares = pos.get("shares", 0)
            avg_cost = pos.get("avg_cost", 0.0)
            if code and shares > 0:
                tracker.set_position(code, shares, avg_cost)
                logger.info(
                    f"[Portfolio] 加载真实持仓: {pos.get('name', code)} "
                    f"({code}) - {shares}股 @ {avg_cost:.2f}"
                )

        account = portfolio.get("account", {})
        if "initial_capital" in account:
            tracker.initial_cash = account["initial_capital"]
        if "cash_balance" in account:
            tracker.cash = account["cash_balance"]

    except Exception as e:
        logger.warning(f"加载 portfolio.json 失败: {e}")
