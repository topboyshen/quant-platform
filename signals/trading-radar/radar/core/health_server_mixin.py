"""
HealthServerMixin - 健康检查服务器管理
从 MonitorEngine 拆分出的健康检查相关逻辑
"""

import logging
from typing import Any, Optional

from radar.core.health_server import HealthHTTPServer


class HealthServerMixin:
    """提供健康检查服务器初始化和管理"""

    logger: logging.Logger
    config: Any  # 由主类提供

    def _init_health_server(self) -> Optional[HealthHTTPServer]:
        """初始化健康检查 HTTP 服务器"""
        if hasattr(self.config, 'health_port') and self.config.health_port:
            return HealthHTTPServer(
                host=getattr(self.config, 'health_host', '0.0.0.0'),
                port=self.config.health_port,
                logger=self.logger
            )
        return None

    def _start_health_server(self) -> None:
        """启动健康检查服务器"""
        if hasattr(self, '_health_server') and self._health_server:
            try:
                self._health_server.start_in_background()
            except Exception as e:
                self.logger.error(f"启动健康检查服务器失败: {e}")

    def _stop_health_server(self) -> None:
        """停止健康检查服务器（跨线程优雅停止后台 aiohttp runner）"""
        if hasattr(self, '_health_server') and self._health_server:
            try:
                self._health_server.stop_in_background()
            except Exception as e:
                self.logger.error(f"关闭健康检查服务器失败: {e}")
