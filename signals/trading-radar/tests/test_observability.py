"""Observability helpers regression tests."""

from __future__ import annotations

import json
import logging
import signal
from unittest.mock import MagicMock

import pytest

from radar.observability import build_log_formatter, configure_tracing, env_flag, install_signal_handlers, trace_span


def test_json_log_formatter_emits_structured_payload(monkeypatch):
    monkeypatch.setenv("RADAR_LOG_JSON", "1")
    formatter = build_log_formatter()
    record = logging.LogRecord(
        name="radar.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "radar.test"
    assert payload["msg"] == "hello world"
    assert payload["func"] is None
    assert "ts" in payload


def test_plain_log_formatter_remains_default(monkeypatch):
    monkeypatch.delenv("RADAR_LOG_JSON", raising=False)
    formatter = build_log_formatter()

    assert formatter.__class__ is logging.Formatter


def test_env_flag_accepts_truthy_values(monkeypatch):
    monkeypatch.setenv("RADAR_FEATURE", "yes")
    assert env_flag("RADAR_FEATURE") is True
    monkeypatch.setenv("RADAR_FEATURE", "0")
    assert env_flag("RADAR_FEATURE") is False


def test_install_signal_handlers_stops_engine_without_raising(monkeypatch):
    installed = {}

    def fake_signal(signum, handler):
        installed[signum] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)
    engine = MagicMock()
    logger = MagicMock()

    install_signal_handlers(engine, logger, raise_interrupt=False)
    installed[signal.SIGTERM](signal.SIGTERM, None)

    engine.stop.assert_called_once()
    logger.info.assert_called_once()


def test_install_signal_handlers_can_unwind_with_keyboard_interrupt(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.setdefault(signum, handler))
    engine = MagicMock()

    install_signal_handlers(engine, MagicMock(), raise_interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        installed[signal.SIGINT](signal.SIGINT, None)
    engine.stop.assert_called_once()


def test_configure_tracing_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RADAR_OTEL_ENABLED", raising=False)
    assert configure_tracing("test-service") is False


def test_trace_span_is_noop_when_otel_unavailable():
    with trace_span("unit-test", code="000001") as span:
        assert span is None or hasattr(span, "set_attribute")
