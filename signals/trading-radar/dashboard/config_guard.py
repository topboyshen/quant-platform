"""Rate-limited monitor config validation for Dashboard callers."""

from __future__ import annotations

import os
import time
from typing import Callable


def maybe_warn_monitor_config_schema(
    merged: dict,
    *,
    logger,
    last_warn_monotonic: float,
    interval_sec: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    """Validate shared monitor config and return the updated warning clock."""
    if not merged or os.getenv("DASHBOARD_SKIP_CONFIG_SCHEMA"):
        return last_warn_monotonic

    from radar.config_schema import ConfigSchemaError, validate_monitor_config_dict

    try:
        validate_monitor_config_dict(merged)
    except ConfigSchemaError as exc:
        now = monotonic()
        if last_warn_monotonic > 0.0 and now - last_warn_monotonic < interval_sec:
            return last_warn_monotonic
        logger.warning(
            "config.json 未通过监控进程配置校验（stock-radar 可能无法启动）: %s",
            exc,
        )
        return now
    return last_warn_monotonic
