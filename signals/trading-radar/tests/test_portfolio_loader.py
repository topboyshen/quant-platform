"""portfolio_loader 单元测试（不经过 MonitorEngine）。"""

import json
import logging

from radar.core.portfolio_loader import load_portfolio_into_tracker
from radar.strategy.rl_strategy_v2 import PortfolioTracker


def test_load_portfolio_into_tracker_applies_positions(tmp_path):
    log = logging.getLogger("pl_test")
    log.setLevel(logging.INFO)
    p = tmp_path / "portfolio.json"
    p.write_text(
        json.dumps(
            {
                "positions": [
                    {"code": "000001", "name": "x", "shares": 100, "avg_cost": 10.5}
                ],
                "account": {"initial_capital": 1e6, "cash_balance": 50000},
            }
        ),
        encoding="utf-8",
    )
    tr = PortfolioTracker(initial_cash=100_000)
    load_portfolio_into_tracker(tr, str(p), log)
    assert "000001" in tr.positions
    assert tr.positions["000001"]["shares"] == 100
    assert tr.initial_cash == 1e6
    assert tr.cash == 50000


def test_load_portfolio_invalid_json_warns(tmp_path, caplog):
    log = logging.getLogger("pl_bad")
    log.setLevel(logging.WARNING)
    p = tmp_path / "portfolio.json"
    p.write_text("{ not json", encoding="utf-8")
    tr = PortfolioTracker()
    with caplog.at_level(logging.WARNING, logger=log.name):
        load_portfolio_into_tracker(tr, str(p), log)
    assert "加载 portfolio.json 失败" in caplog.text
