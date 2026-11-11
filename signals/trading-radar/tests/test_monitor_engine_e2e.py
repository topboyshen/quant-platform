"""End-to-end monitor flow tests: scan → alert → notify and perf guards."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

from radar.alert.batcher import AlertPriority, AlertType
from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _monitor_config(tmp_path, *, batch_alerts: bool = True) -> MonitorConfig:
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
        batch_alerts=batch_alerts,
    )


def _engine(tmp_path, logger: logging.Logger, *, batch_alerts: bool = True) -> MonitorEngine:
    engine = MonitorEngine(_monitor_config(tmp_path, batch_alerts=batch_alerts), logger)
    engine.market.fetch_multiple = MagicMock()
    engine._battlefield.save_all = MagicMock()
    engine._battlefield.save_alert = MagicMock()
    for name in (
        "send",
        "send_price_alert",
        "send_ml_alert",
        "send_rl_alert",
        "send_system_alert",
        "send_datasource_alert",
    ):
        setattr(engine.notifier, name, MagicMock(return_value=True))
    return engine


def test_scan_alert_notify_flow_batches_medium_priority_alert(tmp_path, null_logger):
    engine = _engine(tmp_path, null_logger, batch_alerts=True)
    engine._alert_batcher.priorities.clear()
    engine._alert_batcher.priorities.update({AlertType.PRICE: AlertPriority.MEDIUM})
    engine.add_target(
        TargetStock(code="000001", name="测试", trigger_price=10.0, direction="below", ml_enabled=False, rl_enabled=False)
    )
    engine.market.fetch_multiple.return_value = {
        "000001": {
            "code": "000001",
            "name": "测试",
            "price": 9.0,
            "volume": 1000,
            "open": 9.5,
            "high": 9.6,
            "low": 8.9,
            "prev_close": 10.0,
        }
    }

    engine.process_all_targets_concurrent()

    engine.market.fetch_multiple.assert_called_once_with([("000001", "测试")])
    engine.notifier.send.assert_called_once()
    assert engine.notifier.send_price_alert.call_count == 0
    assert engine.cooldown.is_in_cooldown("000001", "price", cooldown_minutes=30)
    assert engine.get_perf_stats()["scans"] == 1


def test_scan_alert_notify_flow_sends_high_priority_price_alert_immediately(tmp_path, null_logger):
    engine = _engine(tmp_path, null_logger, batch_alerts=True)
    engine.add_target(
        TargetStock(code="000001", name="测试", trigger_price=10.0, direction="below", ml_enabled=False, rl_enabled=False)
    )
    engine.market.fetch_multiple.return_value = {
        "000001": {
            "code": "000001",
            "name": "测试",
            "price": 9.0,
            "volume": 1000,
            "open": 9.5,
            "high": 9.6,
            "low": 8.9,
            "prev_close": 10.0,
        }
    }

    engine.process_all_targets_concurrent()

    engine.notifier.send_price_alert.assert_called_once_with(
        name="测试",
        code="000001",
        price=9.0,
        trigger=10.0,
        direction="below",
    )
    assert engine.notifier.send.call_count == 0
    assert engine.cooldown.is_in_cooldown("000001", "price", cooldown_minutes=30)


def test_slow_scan_warning_threshold_is_reported(tmp_path, monkeypatch):
    logger = logging.getLogger(f"pytest.slow_scan.{time.time_ns()}")
    logger.handlers.clear()
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    engine = _engine(tmp_path, logger, batch_alerts=False)
    engine.add_target(
        TargetStock(code="000001", name="测试", trigger_price=10.0, direction="below", ml_enabled=False, rl_enabled=False)
    )
    engine.market.fetch_multiple.return_value = {
        "000001": {
            "code": "000001",
            "name": "测试",
            "price": 11.0,
            "volume": 1000,
            "open": 10.5,
            "high": 11.1,
            "low": 10.4,
            "prev_close": 10.0,
        }
    }
    values = [100.0, 100.1, 100.2, 100.3, 100.4, 106.0, 106.1, 106.2]
    monkeypatch.setattr("radar.core.monitor_engine.time.time", lambda: values.pop(0) if values else 106.2)

    engine.process_all_targets_concurrent()

    assert any("[SlowScan]" in record.getMessage() for record in handler.records)
    stats = engine.get_perf_stats()
    assert stats["max_fetch_ms"] == 100.0
    assert stats["max_process_ms"] >= 5000.0
