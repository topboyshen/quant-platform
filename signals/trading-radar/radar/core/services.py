"""Explicit service container for monitor engines.

The container centralizes construction of runtime services so entrypoints and tests can
inject fakes without patching constructors throughout the engine implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from radar.alert.accuracy_tracker import AccuracyTracker
from radar.alert.alert_manager import AlertManager
from radar.alert.cooldown_v2 import AlertCooldownManager
from radar.core.battlefield_state import BattlefieldStateManager
from radar.core.health_server import HealthHTTPServer, get_health_monitor
from radar.core.monitor_engine_v2 import MonitorConfig
from radar.core.scan_scheduler import ScanScheduler
from radar.core.strategy_engine import AsyncStrategyEngine
from radar.data.datasource_manager import DataSourceManager
from radar.notify.telegram import TelegramNotifier
from radar.risk.macro_risk_control import MacroRiskControl


@dataclass(slots=True)
class MonitorEngineServices:
    """Runtime dependencies used by ``MonitorEngineV3``."""

    datasource: Any
    strategy_engine: Any
    scheduler: Any
    notifier: Any
    alert_manager: Any
    cooldown: Any
    health: Any
    battlefield: Any
    accuracy_tracker: Any
    risk_control: Any
    health_server: Optional[Any] = None

    @classmethod
    def build(
        cls,
        config: MonitorConfig,
        logger: logging.Logger,
        *,
        start_health_server: bool = True,
    ) -> "MonitorEngineServices":
        notifier = TelegramNotifier(config.bot_token, config.chat_id, logger)
        health_server: Optional[HealthHTTPServer] = None
        if start_health_server:
            try:
                health_server = HealthHTTPServer(
                    host=config.health_host,
                    port=config.health_port,
                    logger=logger,
                )
                health_server.start_in_background()
            except Exception as e:
                logger.warning("健康检查服务启动失败（不影响主流程）: %s", e)
                health_server = None

        return cls(
            datasource=DataSourceManager(logger, max_workers=5),
            strategy_engine=AsyncStrategyEngine(config, logger, max_workers=3),
            scheduler=ScanScheduler(config.market_hours, logger),
            notifier=notifier,
            alert_manager=AlertManager(notifier, logger),
            cooldown=AlertCooldownManager(config.cache_file, logger),
            health=get_health_monitor(),
            battlefield=BattlefieldStateManager(logger=logger),
            accuracy_tracker=AccuracyTracker(logger=logger),
            risk_control=MacroRiskControl(logger_=logger),
            health_server=health_server,
        )
