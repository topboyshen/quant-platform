from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from dashboard.alert_system import (
    AlertChannel,
    AlertEvent,
    AlertManager,
    TelegramAlertChannel,
)


class _ToggleChannel(AlertChannel):
    def __init__(self, outcomes):
        super().__init__("test")
        self.outcomes = iter(outcomes)

    def _do_send(self, event):
        return next(self.outcomes)


@pytest.mark.parametrize(
    "tick",
    [
        SimpleNamespace(price=0, source="error", is_stale=True),
        SimpleNamespace(price=9.0, source="eastmoney", is_stale=True),
        SimpleNamespace(price=float("nan"), source="eastmoney", is_stale=False),
        SimpleNamespace(price=-1, source="tencent", is_stale=False),
    ],
)
def test_price_alert_rejects_untrusted_ticks(tick):
    manager = AlertManager()
    target = {
        "code": "000001",
        "name": "平安银行",
        "trigger_price": 10,
        "direction": "below",
    }

    assert manager.check_price_alert(target, tick) is None


def test_price_alert_accepts_fresh_positive_tick():
    manager = AlertManager()
    target = {
        "code": "000001",
        "name": "平安银行",
        "trigger_price": "10",
        "direction": "below",
    }
    tick = SimpleNamespace(price=9.5, source="tencent", is_stale=False)

    event = manager.check_price_alert(target, tick)

    assert event is not None
    assert event.data["current_price"] == pytest.approx(9.5)


def test_failed_delivery_uses_short_retry_without_consuming_success_cooldown():
    manager = AlertManager()
    manager._failure_retry_seconds = 0
    manager.add_channel(_ToggleChannel([False, True]))
    event = AlertEvent(type="price_trigger", symbol="000001", message="test")

    assert manager.send(event) == {"test": False}
    assert "000001:price_trigger" not in manager._cooldown
    assert manager.send(event) == {"test": True}
    assert "000001:price_trigger" in manager._cooldown
    assert manager.send(event) is None


def test_telegram_retries_timeout_then_succeeds(monkeypatch):
    channel = TelegramAlertChannel("token", "chat")
    ok = MagicMock(status_code=200)
    channel._session.post = MagicMock(
        side_effect=[requests.Timeout("timeout"), ok]
    )
    monkeypatch.setattr("dashboard.alert_system.time.sleep", lambda _seconds: None)

    assert channel._do_send(
        AlertEvent(type="system_alert", symbol="SYSTEM", message="test")
    ) is True
    assert channel._session.post.call_count == 2


def test_telegram_does_not_retry_client_error(monkeypatch):
    channel = TelegramAlertChannel("token", "chat")
    bad = MagicMock(status_code=400, text="bad request")
    channel._session.post = MagicMock(return_value=bad)
    sleep = MagicMock()
    monkeypatch.setattr("dashboard.alert_system.time.sleep", sleep)

    assert channel._do_send(
        AlertEvent(type="system_alert", symbol="SYSTEM", message="test")
    ) is False
    assert channel._session.post.call_count == 1
    sleep.assert_not_called()
