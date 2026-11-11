"""Alpha 曲线（策略 vs 沪深300）与仓位占比 Gauge。"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")

from radar.core.alpha_benchmark import (
    refresh_alpha_and_allocation_metrics,
    reset_alpha_benchmark_state_for_tests,
)
from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock
from radar.core.trading_observability import reset_daily_pnl_state_for_tests


def _engine(tmp_path):
    cfg = MonitorConfig(
        bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
        chat_id="1",
        cooldown_minutes=5,
        scan_interval=60,
        cache_file=str(tmp_path / "x.json"),
        log_file=str(tmp_path / "log.txt"),
        market_hours={
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        health_port=0,
    )
    import logging

    lg = logging.getLogger("t_alpha")
    lg.setLevel(logging.CRITICAL)
    eng = MonitorEngine(cfg, lg)
    eng.add_target(
        TargetStock(
            code="000001",
            name="Ping",
            trigger_price=10.0,
            direction="below",
            strategy_tag="Strategy_Grid_T0",
        )
    )
    eng._portfolio.initial_cash = 100000.0
    eng._portfolio.cash = 90000.0
    eng._portfolio.set_position("000001", shares=1000, cost=10.0)
    eng._portfolio.update_price("000001", 11.0)
    return eng


def test_refresh_alpha_and_allocation(monkeypatch, tmp_path):
    reset_alpha_benchmark_state_for_tests()
    reset_daily_pnl_state_for_tests()

    monkeypatch.setattr(
        "radar.core.alpha_benchmark._fetch_hs300_price_best_effort",
        lambda: 3850.0,
    )
    monkeypatch.setattr(
        "radar.core.alpha_benchmark._FETCH_INTERVAL_SEC",
        0.0,
    )

    eng = _engine(tmp_path)
    refresh_alpha_and_allocation_metrics(eng)

    from radar.core import prometheus_extra as pe

    body = pe.metrics_exposition_suffix()
    assert "radar_alpha_curve_index" in body
    assert 'series="strategy"' in body
    assert 'series="hs300"' in body
    assert "radar_strategy_tag_capital_allocation_ratio" in body
    assert "Strategy_Grid_T0" in body
