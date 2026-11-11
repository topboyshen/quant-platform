"""
健康检查 HTTP 服务器
提供 /health 和 /metrics 端点供 Kubernetes 探针使用
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class HealthStatus:
    """健康状态数据"""
    status: str  # "healthy", "degraded", "unhealthy", "closed"
    last_successful_scan: Optional[float]  # 时间戳
    scan_count: int
    error_count: int
    uptime_seconds: float
    monitored_stocks: int
    market_open: bool = True
    version: str = "1.0.0"

    def to_dict(self) -> Dict:
        result = asdict(self)
        # 转换时间戳为可读格式
        if self.last_successful_scan:
            result['last_successful_scan_iso'] = datetime.fromtimestamp(
                self.last_successful_scan
            ).isoformat()
            result['seconds_since_last_scan'] = time.time() - self.last_successful_scan
        else:
            result['last_successful_scan_iso'] = None
            result['seconds_since_last_scan'] = None
        return result


class HealthMonitor:
    """
    健康状态监控器
    维护运行状态，供 HTTP 端点查询
    """

    _instance: Optional['HealthMonitor'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._start_time = time.time()
        self._last_scan_time: Optional[float] = None
        self._scan_count = 0
        self._error_count = 0
        self._monitored_stocks = 0
        self._market_open = True
        self._logger = logging.getLogger(__name__)
        self._initialized = True

    def record_scan(self, stock_count: int, success: bool = True) -> None:
        """记录一次扫描"""
        self._scan_count += 1
        if success:
            self._last_scan_time = time.time()
        else:
            self._error_count += 1
        self._monitored_stocks = stock_count

    def record_error(self) -> None:
        """记录一次错误"""
        self._error_count += 1

    def update_stock_count(self, count: int) -> None:
        """更新监控股票数"""
        self._monitored_stocks = count

    def set_market_open(self, market_open: bool) -> None:
        """设置市场开关状态"""
        self._market_open = market_open

    def get_status(self) -> HealthStatus:
        """获取当前健康状态"""
        now = time.time()
        uptime = now - self._start_time

        # 市场关闭时返回 closed 状态
        if not self._market_open:
            status = "closed"
        elif self._last_scan_time is None:
            status = "starting"  # 刚启动，还没完成第一次扫描
        elif now - self._last_scan_time > 120:  # 超过2分钟没扫描
            status = "unhealthy"
        elif now - self._last_scan_time > 60:  # 超过1分钟没扫描
            status = "degraded"
        else:
            status = "healthy"

        return HealthStatus(
            status=status,
            last_successful_scan=self._last_scan_time,
            scan_count=self._scan_count,
            error_count=self._error_count,
            uptime_seconds=uptime,
            monitored_stocks=self._monitored_stocks,
            market_open=self._market_open
        )


class HealthHTTPServer:
    """
    轻量级 HTTP 健康检查服务器
    使用 aiohttp（已包含在项目中）
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080,
                 logger: logging.Logger = None):
        self.host = host
        self.port = port
        self.logger = logger or logging.getLogger(__name__)
        self._monitor = HealthMonitor()
        self._app = None
        self._runner = None
        self._site = None
        # 绑定后实际端口（port=0 时由 OS 分配）
        self.effective_port: Optional[int] = None
        # 后台线程及其 event loop 句柄，用于跨线程优雅停止 aiohttp runner
        # （aiohttp 资源绑定到创建它的 loop，所以必须 dispatch 回那个 loop 才能 cleanup）
        self._bg_loop: Optional[asyncio.AbstractEventLoop] = None
        self._bg_thread: Optional["threading.Thread"] = None

    async def _health_handler(self, request) -> Any:
        """/health 端点 - 健康检查"""
        status = self._monitor.get_status()

        # HTTP 状态码映射
        status_code = {
            "healthy": 200,
            "starting": 200,  # readiness 会检查这个
            "degraded": 200,  # 仍然可用，但可能有问题
            "closed": 200,    # 市场关闭，服务正常
            "unhealthy": 503
        }.get(status.status, 503)

        return self._json_response(status.to_dict(), status=status_code)

    async def _ready_handler(self, request) -> Any:
        """/ready 端点 - 就绪检查（readinessProbe）"""
        status = self._monitor.get_status()

        # 就绪条件：市场开盘时需至少完成一次成功扫描；休市状态下服务可接流量
        # 用于避免休市期间启动后 readiness 长期 503，导致编排系统反复重启健康服务。
        is_ready = status.last_successful_scan is not None or status.status == "closed"

        body = {
            "ready": is_ready,
            "status": status.status,
            "monitored_stocks": status.monitored_stocks
        }

        return self._json_response(body, status=200 if is_ready else 503)

    async def _live_handler(self, request) -> Any:
        """/live 端点 - 存活检查（livenessProbe）"""
        status = self._monitor.get_status()

        # 存活条件：不是 unhealthy（卡死状态）
        is_alive = status.status != "unhealthy"

        body = {
            "alive": is_alive,
            "status": status.status,
            "uptime_seconds": status.uptime_seconds
        }

        return self._json_response(body, status=200 if is_alive else 503)

    async def _metrics_handler(self, request) -> Any:
        """/metrics 端点 - Prometheus 格式指标（手写核心 + 可选 prometheus_client 扩展）"""
        status = self._monitor.get_status()

        metrics = [
            "# HELP radar_uptime_seconds Service uptime in seconds",
            "# TYPE radar_uptime_seconds counter",
            f"radar_uptime_seconds {status.uptime_seconds}",
            "",
            "# HELP radar_scan_total Total number of scans",
            "# TYPE radar_scan_total counter",
            f"radar_scan_total {status.scan_count}",
            "",
            "# HELP radar_scan_errors_total Total number of scan errors",
            "# TYPE radar_scan_errors_total counter",
            f"radar_scan_errors_total {status.error_count}",
            "",
            "# HELP radar_monitored_stocks Number of monitored stocks",
            "# TYPE radar_monitored_stocks gauge",
            f"radar_monitored_stocks {status.monitored_stocks}",
            "",
            "# HELP radar_last_scan_timestamp Unix timestamp of last successful scan",
            "# TYPE radar_last_scan_timestamp gauge",
            f"radar_last_scan_timestamp {status.last_successful_scan or 0}",
            "",
            "# HELP radar_market_open Whether the market is open (1) or closed (0)",
            "# TYPE radar_market_open gauge",
            f"radar_market_open {1 if status.market_open else 0}",
        ]
        body = "\n".join(metrics)
        try:
            from radar.core.prometheus_extra import metrics_exposition_suffix

            extra = metrics_exposition_suffix().strip()
            if extra:
                body = f"{body}\n{extra}\n"
        except (OSError, RuntimeError):
            # Prometheus 扩展指标失败不应阻塞基础指标暴露  # 安全忽略
            pass

        return self._text_response(body)

    async def _info_handler(self, request) -> Any:
        """/info 端点 - 基本信息（含阵地状态）"""
        status = self._monitor.get_status()

        # 查询阵地状态
        battlefield_info = {}
        try:
            from radar.core.battlefield_state import BattlefieldStateManager
            bsm = BattlefieldStateManager()
            battlefield_info = bsm.get_stats()
            battlefield_info["position_snapshot"] = bsm.get_position_snapshot()
        except (OSError, RuntimeError):
            # 阵地状态查询失败不应阻塞基础信息返回  # 安全忽略
            pass

        body = {
            "service": "stock-radar-monitor",
            "version": status.version,
            "status": status.to_dict(),
            "battlefield": battlefield_info,
            "endpoints": {
                "/health": "Health status with full details",
                "/ready": "Readiness probe for Kubernetes",
                "/live": "Liveness probe for Kubernetes",
                "/metrics": "Prometheus format metrics",
                "/info": "This information + battlefield state"
            }
        }

        return self._json_response(body)

    def _json_response(self, data: Dict, status: int = 200) -> Any:
        """创建 JSON 响应"""
        from aiohttp import web
        return web.Response(
            text=json.dumps(data, indent=2, default=str),
            status=status,
            content_type="application/json"
        )

    def _text_response(self, text: str, status: int = 200) -> Any:
        """创建文本响应"""
        from aiohttp import web
        return web.Response(
            text=text,
            status=status,
            content_type="text/plain"
        )

    async def start(self) -> bool:
        """启动 HTTP 服务器"""
        try:
            from aiohttp import web
        except ImportError:
            self.logger.warning("aiohttp 未安装，健康检查服务器无法启动")
            self.logger.warning("安装命令: pip install aiohttp")
            return False

        self._app = web.Application()

        # 注册路由
        self._app.router.add_get("/health", self._health_handler)
        self._app.router.add_get("/ready", self._ready_handler)
        self._app.router.add_get("/live", self._live_handler)
        self._app.router.add_get("/metrics", self._metrics_handler)
        self._app.router.add_get("/info", self._info_handler)
        self._app.router.add_get("/", self._info_handler)  # 根路径重定向到 info

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        # 使用 SO_REUSEADDR 避免端口占用问题，支持优雅重启
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as e:
            self.logger.error(f"无法绑定端口 {self.port}: {e}。可能是上一次实例未完全释放。")
            sock.close()
            return False
        _bound_host, bound_port = sock.getsockname()
        self.effective_port = int(bound_port)
        sock.listen(128)

        self._site = web.SockSite(self._runner, sock)
        await self._site.start()

        self.logger.info(
            f"健康检查服务器已启动: http://{self.host}:{self.effective_port}"
        )
        self.logger.info("  - /health  健康状态详情")
        self.logger.info("  - /ready   就绪探针")
        self.logger.info("  - /live    存活探针")
        self.logger.info("  - /metrics Prometheus 指标")

        return True

    async def stop(self) -> None:
        """停止 HTTP 服务器"""
        if self._runner:
            await self._runner.cleanup()
            self.logger.info("健康检查服务器已停止")

    def start_in_background(self) -> threading.Thread:
        """在后台线程中启动服务器"""
        import threading

        loop_ready = threading.Event()

        def run_server() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._bg_loop = loop
            loop_ready.set()
            try:
                loop.run_until_complete(self.start())
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        self._bg_thread = thread

        # 等待 bg loop 创建完成（最长 2s，远短于以前的 sleep(0.5) 但更严格）
        loop_ready.wait(timeout=2.0)
        # 兼容：保留 0.5s 缓冲让 aiohttp 完成绑定
        time.sleep(0.5)
        return thread

    def stop_in_background(self, timeout: float = 5.0) -> bool:
        """跨线程优雅停止后台 HTTP 服务器。

        aiohttp 的 AppRunner/Site 资源绑定到创建它的 event loop，必须
        通过 ``run_coroutine_threadsafe`` 把 cleanup 协程 dispatch 回
        后台线程的 loop 才能正确释放（监听 socket、连接池等）。

        返回 True 表示干净停止，False 表示未启动 / 超时 / 异常。
        """
        loop = self._bg_loop
        if loop is None or not loop.is_running():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self.stop(), loop)
            future.result(timeout=timeout)
        except Exception as e:
            self.logger.error(f"健康检查服务器 stop 协程失败: {e}")
            return False
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if self._bg_thread and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=timeout)
        return True


# 便捷函数
def get_health_monitor() -> HealthMonitor:
    """获取全局健康监控器实例"""
    return HealthMonitor()
