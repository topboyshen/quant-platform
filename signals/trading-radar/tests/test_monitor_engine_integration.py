"""MonitorEngine：mock 行情与通知，验证一轮并发扫描链路。"""

from unittest.mock import MagicMock

import pytest

from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock


@pytest.fixture
def monitor_config(tmp_path, null_logger) -> MonitorConfig:
    return MonitorConfig(
        bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
        chat_id="123",
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
        ml_threshold=0.99,
        health_port=0,
        batch_alerts=True,
    )


def test_process_all_targets_uses_fetch_multiple(
    monitor_config, null_logger, monkeypatch
):
    engine = MonitorEngine(monitor_config, null_logger)
    target = TargetStock(
        code="000001",
        name="测试",
        trigger_price=10.0,
        direction="below",
        ml_enabled=False,
        rl_enabled=False,
    )
    engine.add_target(target)

    for name in (
        "send",
        "send_price_alert",
        "send_ml_alert",
        "send_rl_alert",
        "send_system_alert",
        "send_datasource_alert",
    ):
        setattr(engine.notifier, name, MagicMock(return_value=True))

    fake = {
        "000001": {
            "code": "000001",
            "price": 9.0,
            "volume": 1000,
            "name": "测试",
            "open": 9.5,
            "high": 9.6,
            "low": 8.9,
            "prev_close": 10.0,
        }
    }
    engine.market.fetch_multiple = MagicMock(return_value=fake)

    engine.process_all_targets_concurrent()

    engine.market.fetch_multiple.assert_called_once()
    assert engine.notifier.send_price_alert.called
