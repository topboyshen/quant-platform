"""MonitorEngineV3 strategy guardrail regressions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import radar.core.monitor_engine_v3 as monitor_engine_module
from radar.alert.alert_manager import AlertLevel
from radar.core.monitor_engine_v2 import MonitorConfig, TargetStock
from radar.core.monitor_engine_v3 import MonitorEngineV3
from radar.core.strategy_engine import StrategyResult


class _Cooldown:
    def __init__(self, in_cooldown: bool = False):
        self.in_cooldown = in_cooldown
        self.recorded: list[tuple[str, str]] = []

    def is_in_cooldown(self, code: str, alert_type: str, minutes: int) -> bool:
        return self.in_cooldown and alert_type == "price"

    def record_alert(self, code: str, alert_type: str) -> None:
        self.recorded.append((code, alert_type))


def _config(tmp_path) -> MonitorConfig:
    return MonitorConfig(
        bot_token="DISABLED",
        chat_id="DISABLED",
        cooldown_minutes=30,
        scan_interval=60,
        cache_file=str(tmp_path / "cache.json"),
        log_file=str(tmp_path / "radar.log"),
        market_hours={},
        health_host="127.0.0.1",
        health_port=0,
    )


def _engine(tmp_path, null_logger, *, in_cooldown: bool = False) -> tuple[MonitorEngineV3, MagicMock, _Cooldown]:
    strategy_engine = MagicMock()
    alert_manager = MagicMock()
    alert_manager.submit.return_value = True
    cooldown = _Cooldown(in_cooldown=in_cooldown)
    services = SimpleNamespace(
        datasource=MagicMock(),
        strategy_engine=strategy_engine,
        scheduler=MagicMock(),
        notifier=MagicMock(),
        alert_manager=alert_manager,
        cooldown=cooldown,
        health=MagicMock(),
        health_server=None,
        battlefield=MagicMock(),
        accuracy_tracker=MagicMock(),
        risk_control=MagicMock(),
    )
    services.battlefield.restore_all.return_value = None
    engine = MonitorEngineV3(_config(tmp_path), null_logger, services=services)
    return engine, strategy_engine, cooldown


def _target() -> TargetStock:
    return TargetStock("000001", "测试", 10.0, "below", ml_enabled=True, rl_enabled=True)


def test_v3_skips_strategy_evaluation_for_invalid_price(tmp_path, null_logger):
    engine, strategy_engine, _ = _engine(tmp_path, null_logger)

    alerts = engine._process_target(_target(), {"price": 0, "fresh": True})

    assert alerts == []
    strategy_engine.evaluate_all.assert_not_called()


def test_v3_skips_alerts_for_stale_cached_data(tmp_path, null_logger):
    engine, strategy_engine, _ = _engine(tmp_path, null_logger)

    alerts = engine._process_target(_target(), {"price": 9.0, "fresh": False, "stale": True})

    assert alerts == []
    strategy_engine.evaluate_all.assert_not_called()


def test_v3_price_cooldown_still_evaluates_ml_rl_but_suppresses_price(tmp_path, null_logger):
    engine, strategy_engine, cooldown = _engine(tmp_path, null_logger, in_cooldown=True)
    strategy_engine.evaluate_all.return_value = [
        StrategyResult("price", True, {"price": 9.0}, 1.0),
        StrategyResult("ml", True, {"probability": 0.9}, 1.0),
        StrategyResult("rl", True, {"action": "open"}, 1.0),
    ]

    alerts = engine._process_target(_target(), {"price": 9.0, "fresh": True})

    strategy_engine.evaluate_all.assert_called_once()
    assert [item.alert_type for item in alerts] == ["ml", "rl"]
    assert all(item.level is AlertLevel.WARNING for item in alerts)
    assert cooldown.recorded == []
    for item in alerts:
        item.on_delivery(True)
    assert cooldown.recorded == [("000001", "ml"), ("000001", "rl")]


def test_v3_failed_delivery_does_not_record_business_cooldown(tmp_path, null_logger):
    engine, strategy_engine, cooldown = _engine(tmp_path, null_logger)
    strategy_engine.evaluate_all.return_value = [
        StrategyResult("ml", True, {"probability": 0.9}, 1.0),
    ]

    alerts = engine._process_target(_target(), {"price": 9.0, "fresh": True})

    assert len(alerts) == 1
    alerts[0].on_delivery(False)
    assert cooldown.recorded == []


def test_v3_total_failure_alert_false_is_retried_after_short_backoff(
    monkeypatch,
    tmp_path,
    null_logger,
):
    now = [1_000_000.0]
    monkeypatch.setattr(monitor_engine_module.time, "time", lambda: now[0])
    engine, _, _ = _engine(tmp_path, null_logger)
    engine.targets = [_target()]
    engine.notifier.send_system_alert.return_value = False

    for _ in range(3):
        engine._handle_total_failure()
    engine._handle_total_failure()

    assert engine.notifier.send_system_alert.call_count == 1
    assert engine._failure_alert_sent is False

    now[0] += engine.ALERT_FAILURE_BACKOFF + 0.1
    engine.notifier.send_system_alert.return_value = True
    engine._handle_total_failure()

    assert engine.notifier.send_system_alert.call_count == 2
    assert engine._failure_alert_sent is True


def test_v3_macro_false_does_not_mark_sent_and_is_backed_off(
    monkeypatch,
    tmp_path,
    null_logger,
):
    now = [1_000_000.0]
    monkeypatch.setattr(monitor_engine_module.time, "time", lambda: now[0])
    engine, strategy_engine, _ = _engine(tmp_path, null_logger)
    engine.targets = [_target()]
    state = SimpleNamespace(level="critical", message="risk")
    engine._risk_control.check_with_fetch.return_value = state
    engine._risk_control.should_alert_now.return_value = True
    engine.scheduler.is_trading_day.return_value = True
    engine.scheduler.is_trading_time.return_value = True
    engine.datasource.fetch_multiple.return_value = {
        "000001": {"price": 11.0, "fresh": True},
    }
    strategy_engine.evaluate_all.return_value = []
    engine.notifier.send_system_alert.return_value = False

    engine._scan_once_impl()
    engine._scan_once_impl()

    assert engine.notifier.send_system_alert.call_count == 1
    engine._risk_control.mark_alert_sent.assert_not_called()

    now[0] += engine.ALERT_FAILURE_BACKOFF + 0.1
    engine.notifier.send_system_alert.return_value = True
    engine._scan_once_impl()

    assert engine.notifier.send_system_alert.call_count == 2
    engine._risk_control.mark_alert_sent.assert_called_once_with(state)
