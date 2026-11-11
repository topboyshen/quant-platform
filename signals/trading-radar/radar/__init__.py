"""
Trading Radar — A 股监控与策略库（installable package `radar`）。

包内分层（详见仓库根目录 ARCHITECTURE.md）：
  core/      监控引擎、健康检查、阵地状态、告警与指标
  data/      行情、Parquet、统一历史与缓存
  strategy/  ML / RL 策略与特征
  notify/    Telegram 等通知
  ml_serve/  进程外推理（HTTP / gRPC）
  schemas/   运行时校验辅助

仓库根目录 *.py 为进程入口；dashboard/ 为独立 Web 应用。
旧版根目录 ``radar.py`` 现在仅转发到 ``radar_monitor_v3.py``。
V1/V2 主入口已归档至 ``legacy/``。
"""

from __future__ import annotations

from .utils import setup_logging

__version__ = "2.0.0"

__all__ = [
    "setup_logging",
]
