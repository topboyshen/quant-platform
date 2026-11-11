"""radar.core — 监控引擎、健康检查、阵地状态、告警分发的 public API surface.

仅暴露稳定符号；mixin / 内部实现（``*_mixin``、``*_pipeline``）请直接从子模块 import。
被 8+ 个测试与 radar_monitor_v3 入口脚本依赖，向后兼容请谨慎修改本文件。
"""

from __future__ import annotations

from typing import Any

from radar.core.battlefield_state import BattlefieldStateManager
from radar.core.health_server import (
    HealthHTTPServer,
    HealthMonitor,
    HealthStatus,
    get_health_monitor,
)

__all__ = [
    "BattlefieldStateManager",
    "HealthHTTPServer",
    "HealthMonitor",
    "HealthStatus",
    "MonitorConfig",
    "MonitorEngine",
    "MonitorEngineServices",
    "MonitorEngineV3",
    "TargetStock",
    "get_health_monitor",
]


def __getattr__(name: str) -> Any:
    if name in {"MonitorConfig", "MonitorEngine", "TargetStock"}:
        from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock

        values = {
            "MonitorConfig": MonitorConfig,
            "MonitorEngine": MonitorEngine,
            "TargetStock": TargetStock,
        }
        return values[name]
    if name == "MonitorEngineV3":
        from radar.core.monitor_engine_v3 import MonitorEngineV3

        return MonitorEngineV3
    if name == "MonitorEngineServices":
        from radar.core.services import MonitorEngineServices

        return MonitorEngineServices
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
