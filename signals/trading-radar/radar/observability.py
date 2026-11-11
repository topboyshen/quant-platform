"""Production observability helpers: structured logs, signals, and tracing.

All integrations are optional by default. OpenTelemetry is enabled only when
``RADAR_OTEL_ENABLED`` is truthy and the optional packages are installed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from types import FrameType
from typing import Any, Iterator, Optional

_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY


class JsonLogFormatter(logging.Formatter):
    """Single-line JSON formatter for Loki / ELK style collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        trace_ids = _current_trace_ids()
        if trace_ids:
            payload.update(trace_ids)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_log_formatter(*, json_enabled: Optional[bool] = None) -> logging.Formatter:
    if json_enabled is None:
        json_enabled = env_flag("RADAR_LOG_JSON")
    if json_enabled:
        return JsonLogFormatter()
    return logging.Formatter(
        "%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def install_signal_handlers(engine: Any, logger: logging.Logger, *, raise_interrupt: bool = True) -> None:
    """Install SIGTERM/SIGINT handlers that request engine shutdown.

    ``raise_interrupt`` keeps existing ``try/finally`` cleanup paths working:
    the handler calls ``engine.stop()`` and then raises ``KeyboardInterrupt`` so
    the engine loop unwinds through its normal cleanup block.
    """

    def _handle(signum: int, _frame: Optional[FrameType]) -> None:
        signame = signal.Signals(signum).name
        logger.info("收到 %s，开始优雅退出", signame)
        stop = getattr(engine, "stop", None)
        if callable(stop):
            stop()
        if raise_interrupt:
            raise KeyboardInterrupt(signame)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def configure_tracing(service_name: str, logger: Optional[logging.Logger] = None) -> bool:
    """Configure OpenTelemetry tracing if explicitly enabled.

    Jaeger deployments commonly accept OTLP; set ``RADAR_OTEL_EXPORTER_OTLP_ENDPOINT``
    (for example ``http://jaeger:4318/v1/traces``) to export spans. Returns
    ``True`` when tracing was configured, ``False`` when disabled/unavailable.
    """
    logger = logger or logging.getLogger(__name__)
    if not env_flag("RADAR_OTEL_ENABLED"):
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning("OpenTelemetry 未安装，跳过 tracing 初始化: %s", exc)
        return False

    endpoint = os.getenv("RADAR_OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter_kwargs = {"endpoint": endpoint} if endpoint else {}
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**exporter_kwargs)))
    trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry tracing 已启用 service=%s endpoint=%s", service_name, endpoint or "default")
    return True


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start an OpenTelemetry span when tracing is available, otherwise no-op."""
    try:
        from opentelemetry import trace
    except ImportError:
        with nullcontext() as span:
            yield span
        return

    tracer = trace.get_tracer("trading_radar")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


def _current_trace_ids() -> dict[str, str]:
    try:
        from opentelemetry import trace
    except ImportError:
        return {}

    span = trace.get_current_span()
    context = span.get_span_context()
    if not getattr(context, "is_valid", False):
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }
