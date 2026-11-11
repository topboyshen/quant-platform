"""Dashboard helpers for macro risk-control state."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


def default_risk_state() -> Dict[str, Any]:
    """Return a safe default when live risk data is unavailable."""
    return {
        "level": "warning",
        "message": "DATA_UNAVAILABLE: 尚未收到宏观风控状态",
        "vix": None,
        "us_10y": None,
        "crude_oil": None,
        "timestamp": time.time(),
        "cached_fields": [],
        "missing_fields": ["vix", "us_10y", "crude_oil"],
    }


def load_risk_state() -> Dict[str, Any]:
    """Load macro risk state for templates without blocking dashboard startup."""
    try:
        from radar.risk.macro_risk_control import MacroRiskControl

        state = MacroRiskControl.load_state_from_file()
        if state is not None:
            state["age_seconds"] = max(0, int(time.time() - state.get("timestamp", 0)))
            state["data_complete"] = not state.get("missing_fields")
            return state

        rc = MacroRiskControl()
        return rc.check_with_fetch().to_dict()
    except Exception as e:
        logger.debug("加载风控状态失败: %s", e)
        return default_risk_state()
