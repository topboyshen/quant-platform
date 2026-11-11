"""EventBus：内存模式 + Redis 模式（若 redis 可用）测试。"""

import os
import threading
import time

import pytest

from radar.core.event_bus import _MemoryEventBus, create_event_bus


class TestMemoryEventBus:
    def test_register_emit(self):
        bus = _MemoryEventBus()
        received = []

        def handler(event_type, data):
            received.append((event_type, data))

        bus.register("test", handler)
        bus.emit("test", {"msg": "hello"})
        time.sleep(0.1)
        assert len(received) == 1
        assert received[0] == ("test", {"msg": "hello"})
        bus.shutdown()

    def test_async_handler(self):
        bus = _MemoryEventBus()
        received = []

        def handler(event_type, data):
            received.append(data)

        bus.register("test", handler, async_mode=True)
        bus.emit("test", 42)
        time.sleep(0.2)
        assert received == [42]
        bus.shutdown()

    def test_unregister(self):
        bus = _MemoryEventBus()
        received = []

        def handler(event_type, data):
            received.append(data)

        bus.register("test", handler)
        bus.unregister("test", handler)
        bus.emit("test", 1)
        time.sleep(0.1)
        assert received == []
        bus.shutdown()

    def test_concurrent_emit(self):
        bus = _MemoryEventBus()
        counter = {"n": 0}
        lock = threading.Lock()

        def handler(event_type, data):
            with lock:
                counter["n"] += 1

        bus.register("test", handler, async_mode=True)
        threads = [threading.Thread(target=lambda: bus.emit("test", None)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.3)
        assert counter["n"] == 20
        bus.shutdown()


class TestRedisEventBus:
    """需要本地 redis-server；跳过若不可用。"""

    @pytest.fixture(scope="class")
    def redis_available(self):
        try:
            import redis as redis_mod

            r = redis_mod.Redis.from_url("redis://127.0.0.1:6379/0", socket_connect_timeout=1)
            r.ping()
            return True
        except (ConnectionError, TimeoutError, ImportError):
            # Redis未运行，安全返回False  # 安全忽略
            return False

    def test_redis_cross_process(self, redis_available):
        if not redis_available:
            pytest.skip("redis 不可用")

        from radar.core.event_bus import _RedisEventBus

        bus_a = _RedisEventBus("redis://127.0.0.1:6379/0")
        bus_b = _RedisEventBus("redis://127.0.0.1:6379/0")

        received_b = []

        def handler(event_type, data):
            received_b.append(data)

        bus_b.register("market_data", handler)
        time.sleep(0.2)  # 订阅建立

        bus_a.emit("market_data", {"code": "000001", "price": 10.5})
        time.sleep(0.3)

        assert len(received_b) == 1
        assert received_b[0]["code"] == "000001"

        bus_a.shutdown()
        bus_b.shutdown()

    def test_redis_no_self_echo(self, redis_available):
        """自己发出的消息不应被自己的订阅线程重复处理。"""
        if not redis_available:
            pytest.skip("redis 不可用")

        from radar.core.event_bus import _RedisEventBus

        bus = _RedisEventBus("redis://127.0.0.1:6379/0")
        received = []

        def handler(event_type, data):
            received.append(data)

        bus.register("test", handler)
        time.sleep(0.2)

        bus.emit("test", {"x": 1})
        time.sleep(0.3)

        # 只应收到 1 次（本地 emit 直接调用），不应收到 Redis 回环
        assert len(received) == 1
        bus.shutdown()


class TestFactory:
    def test_default_memory(self):
        os.environ.pop("RADAR_EVENT_BUS", None)
        bus = create_event_bus()
        assert isinstance(bus, _MemoryEventBus)
        bus.shutdown()

    def test_explicit_memory(self):
        os.environ["RADAR_EVENT_BUS"] = "memory"
        bus = create_event_bus()
        assert isinstance(bus, _MemoryEventBus)
        bus.shutdown()
