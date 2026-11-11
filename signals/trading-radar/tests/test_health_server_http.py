"""HealthHTTPServer：绑定端口与 /metrics Prometheus 文本。"""

import asyncio

import aiohttp
import pytest

from radar.core.health_server import HealthHTTPServer, get_health_monitor


@pytest.fixture(autouse=True)
def reset_health_monitor():
    """单例 HealthMonitor 在测试间清零。"""
    m = get_health_monitor()
    m._start_time = __import__("time").time()
    m._last_scan_time = None
    m._scan_count = 0
    m._error_count = 0
    m._monitored_stocks = 0
    yield


def test_health_server_metrics_prometheus_format(null_logger):
    async def run():
        srv = HealthHTTPServer(host="127.0.0.1", port=0, logger=null_logger)
        ok = await srv.start()
        assert ok is True
        assert srv.effective_port and srv.effective_port > 0
        get_health_monitor().record_scan(stock_count=2, success=True)
        url = f"http://127.0.0.1:{srv.effective_port}/metrics"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                assert resp.status == 200
                text = await resp.text()
        assert "radar_scan_total" in text
        assert "TYPE radar_scan_total counter" in text
        assert "TYPE radar_monitored_stocks gauge" in text
        await srv.stop()

    asyncio.run(run())
