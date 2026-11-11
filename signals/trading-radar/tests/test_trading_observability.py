"""战局统筹指标刷新与 PortfolioTracker.execute_trade 打点。"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")

from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock
from radar.core.trading_observability import refresh_trading_observability, reset_peak_equity_for_tests


def _minimal_engine(targets, *, cache_file: str, log_file: str):
    cfg = MonitorConfig(
        bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
        chat_id="1",
        cooldown_minutes=5,
        scan_interval=60,
        cache_file=cache_file,
        log_file=log_file,
        market_hours={
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        health_port=0,
    )
    import logging

    logger = logging.getLogger("test")
    logger.setLevel(logging.CRITICAL)
    eng = MonitorEngine(cfg, logger)
    for t in targets:
        eng.add_target(t)
    return eng


def test_refresh_trading_observability_exposes_lane_and_pnl_metrics(tmp_path):
    reset_peak_equity_for_tests(0.0)
    t = TargetStock(
        code="000001",
        name="Test",
        trigger_price=10.0,
        direction="below",
        strategy_tag="Strategy_Grid_T0",
    )
    eng = _minimal_engine(
        [t],
        cache_file=str(tmp_path / "radar_cache.json"),
        log_file=str(tmp_path / "radar.log"),
    )
    eng._portfolio.set_position("000001", shares=100, cost=10.0)
    eng._portfolio.update_price("000001", 11.0)

    refresh_trading_observability(eng)

    from radar.core import prometheus_extra as pe

    text = pe.metrics_exposition_suffix()
    assert "radar_battlefield_lane_symbols" in text
    assert "radar_portfolio_net_profit_cny" in text
    assert "Strategy_Grid_T0" in text or "strategy_tag" in text


def test_execute_trade_close_records_win_and_slippage():
    from radar.core import prometheus_extra as pe
    from radar.strategy.rl_strategy_v2 import PortfolioTracker

    pt = PortfolioTracker(initial_cash=100_000.0)
    pt.execute_trade("000001", "open", price=10.0, shares=100)
    pt.execute_trade(
        "000001",
        "close",
        price=11.0,
        expected_price=10.5,
        strategy_tag="Strategy_MACD_Breakout",
    )

    text = pe.metrics_exposition_suffix()
    assert "radar_strategy_tag_trade_wins_total" in text
    assert "radar_order_slippage_bps" in text
