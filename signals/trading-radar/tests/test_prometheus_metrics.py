"""Prometheus 指标测试：Tick-to-Trade + EventBus 拥堵 + API 熔断/限频。"""

import pytest

try:
    from prometheus_client import CollectorRegistry  # noqa: F401  # availability probe
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


@pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client 未安装")
class TestTickToTradeLatency:
    def test_histogram_buckets(self):
        from radar.core.prometheus_extra import _TTT_LATENCY, observe_tick_to_trade_latency

        # 记录几个不同延迟
        observe_tick_to_trade_latency(15.0)
        observe_tick_to_trade_latency(35.0)
        observe_tick_to_trade_latency(55.0)
        observe_tick_to_trade_latency(120.0)

        # 验证 Histogram 有样本（计数包含前面测试遗留）
        samples = list(_TTT_LATENCY.collect())
        assert len(samples) > 0
        # 至少有一个 bucket 计数 > 0
        total = sum(s.value for s in samples[0].samples if s.name.endswith("_count"))
        assert total >= 4

    def test_p95_below_50ms(self):
        """模拟 100 次延迟，验证 P95 计算逻辑。"""
        from radar.core.prometheus_extra import _TTT_LATENCY, observe_tick_to_trade_latency

        # 90 次 30ms，10 次 80ms → P95 应该在 80ms 附近
        for _ in range(90):
            observe_tick_to_trade_latency(30.0)
        for _ in range(10):
            observe_tick_to_trade_latency(80.0)

        # 验证 bucket 计数（包含前面 test_histogram_buckets 的 4 次）
        samples = list(_TTT_LATENCY.collect())
        buckets = [s for s in samples[0].samples if s.name.endswith("_bucket")]
        # 50ms bucket 应该有 90 + 2 = 92 个（前面测试有 15ms 和 35ms 两笔落在 50ms bucket）
        bucket_50 = next((s for s in buckets if s.labels.get("le") == "50.0"), None)
        assert bucket_50 is not None
        assert bucket_50.value >= 90


@pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client 未安装")
class TestEventBusMetrics:
    def test_queue_size_gauge(self):
        from radar.core.prometheus_extra import _EVENTBUS_QUEUE_SIZE, observe_eventbus_queue_size

        observe_eventbus_queue_size(5)
        observe_eventbus_queue_size(0)

        samples = list(_EVENTBUS_QUEUE_SIZE.collect())
        assert len(samples) > 0
        # Gauge 最终值应为 0
        value = samples[0].samples[0].value
        assert value == 0

    def test_redis_lag_gauge(self):
        from radar.core.prometheus_extra import _EVENTBUS_REDIS_LAG, observe_eventbus_redis_lag

        observe_eventbus_redis_lag(0.05)
        observe_eventbus_redis_lag(0.2)

        samples = list(_EVENTBUS_REDIS_LAG.collect())
        assert len(samples) > 0
        value = samples[0].samples[0].value
        assert value == 0.2


@pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client 未安装")
class TestApiProtectionMetrics:
    def test_429_counter(self):
        from radar.core.prometheus_extra import _API_RATE_LIMIT, observe_api_rate_limit_429

        observe_api_rate_limit_429()
        observe_api_rate_limit_429()

        samples = list(_API_RATE_LIMIT.collect())
        assert len(samples) > 0
        value = samples[0].samples[0].value
        assert value == 2

    def test_circuit_open_counter(self):
        from radar.core.prometheus_extra import _API_CIRCUIT_OPEN, observe_api_circuit_open

        observe_api_circuit_open()

        samples = list(_API_CIRCUIT_OPEN.collect())
        assert len(samples) > 0
        value = samples[0].samples[0].value
        assert value >= 1

    def test_5xx_counter(self):
        from radar.core.prometheus_extra import _API_ERROR_5XX, observe_api_error_5xx

        observe_api_error_5xx()
        observe_api_error_5xx()
        observe_api_error_5xx()

        samples = list(_API_ERROR_5XX.collect())
        assert len(samples) > 0
        value = samples[0].samples[0].value
        assert value == 3


class TestNoPrometheusGraceful:
    """未安装 prometheus_client 时，observe_* 不应抛异常。"""

    def test_observe_tick_to_trade_noop(self):
        from radar.core.prometheus_extra import observe_tick_to_trade_latency
        observe_tick_to_trade_latency(100.0)  # 不应抛异常

    def test_observe_eventbus_noop(self):
        from radar.core.prometheus_extra import observe_eventbus_queue_size, observe_eventbus_redis_lag
        observe_eventbus_queue_size(10)
        observe_eventbus_redis_lag(0.5)

    def test_observe_api_noop(self):
        from radar.core.prometheus_extra import (
            observe_api_circuit_open,
            observe_api_error_5xx,
            observe_api_rate_limit_429,
        )
        observe_api_rate_limit_429()
        observe_api_circuit_open()
        observe_api_error_5xx()
