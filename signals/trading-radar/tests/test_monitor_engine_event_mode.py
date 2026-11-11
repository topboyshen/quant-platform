"""MonitorEngine 事件路径：行情事件 → process_target → 阵地持久化。"""

import time
from unittest.mock import MagicMock, patch

import pytest

from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock
from radar.core.push_adapter import MarketDataEvent


@pytest.fixture
def event_mode_config(tmp_path):
    return MonitorConfig(
        bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
        chat_id="1",
        cooldown_minutes=30,
        scan_interval=60,
        cache_file=str(tmp_path / "radar_cache.json"),
        log_file=str(tmp_path / "radar.log"),
        market_hours={
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        health_port=0,
        batch_alerts=True,
    )


def test_on_market_data_calls_process_target_and_save_all(
    event_mode_config, null_logger
):
    engine = MonitorEngine(event_mode_config, null_logger)
    engine.add_target(
        TargetStock(
            code="000001",
            name="测试",
            trigger_price=25.0,
            direction="below",
            ml_enabled=False,
            rl_enabled=False,
        )
    )
    save_mock = MagicMock()
    engine._battlefield.save_all = save_mock

    event = MarketDataEvent(
        code="000001",
        name="测试",
        price=23.5,
        volume=10000,
        change_pct=-1.0,
        open=24.0,
        high=24.5,
        low=23.0,
        prev_close=24.0,
        timestamp=time.time(),
    )

    with patch.object(engine, "process_target", wraps=engine.process_target) as pt:
        engine._on_market_data("market_data", event)
        pt.assert_called_once()

    save_mock.assert_called_once_with(engine)
    assert engine._portfolio.positions["000001"]["last_price"] == 23.5


def test_event_bus_emit_invokes_engine_handler(event_mode_config, null_logger):
    """enable_event_mode 注册的处理器应能通过 EventBus.emit 触发。"""
    engine = MonitorEngine(event_mode_config, null_logger)
    engine.add_target(
        TargetStock(
            code="300059",
            name="东方财富",
            trigger_price=100.0,
            direction="below",
            ml_enabled=False,
            rl_enabled=False,
        )
    )
    for name in (
        "send",
        "send_price_alert",
        "send_ml_alert",
        "send_rl_alert",
        "send_system_alert",
        "send_datasource_alert",
    ):
        setattr(engine.notifier, name, MagicMock(return_value=True))

    engine.enable_event_mode()
    save_mock = MagicMock()
    engine._battlefield.save_all = save_mock

    event = MarketDataEvent(
        code="300059",
        name="东方财富",
        price=50.0,
        volume=1000,
        change_pct=-1.0,
        open=51.0,
        high=52.0,
        low=49.0,
        prev_close=51.0,
        timestamp=time.time(),
    )
    engine._event_bus.emit("market_data", event)
    save_mock.assert_called_once_with(engine)
