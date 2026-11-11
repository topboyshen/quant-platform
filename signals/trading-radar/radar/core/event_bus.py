"""
Redis-backed EventBus：跨进程/跨容器发布订阅。

兼容内存 EventBus 接口（register / unregister / emit / shutdown），
但底层通过 Redis Pub/Sub 广播，支持多容器/多机器共享行情事件。

环境变量：
- RADAR_REDIS_URL    默认 redis://127.0.0.1:6379/0
- RADAR_EVENT_BUS    memory（默认）| redis

用法：
    bus = create_event_bus(logger)
    bus.register("market_data", handler)
    bus.emit("market_data", event.to_dict())   # 自动序列化
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Protocol, overload


class EventBusProtocol(Protocol):
    """公共事件总线协议，覆盖内存与 Redis 后端。"""

    def register(self, event_type: str, handler: Callable[[str, Any], None], async_mode: bool = False) -> None: ...

    def unregister(self, event_type: str, handler: Callable[[str, Any], None]) -> None: ...

    def emit(self, event_type: str, data: Any) -> None: ...

    def shutdown(self) -> None: ...


class _MemoryEventBus:
    """本地内存事件总线（单进程内使用，零依赖）。"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._handlers: Dict[str, List[Callable]] = {}
        self._general_handlers: List[Callable] = []
        self._lock = threading.RLock()
        self._async_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="event_bus_"
        )
        self._closed = False

    def register(self, event_type: str, handler: Callable, async_mode: bool = False) -> None:
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append((handler, async_mode))

    def unregister(self, event_type: str, handler: Callable) -> None:
        with self._lock:
            if event_type in self._handlers:
                self._handlers[event_type] = [
                    (h, async_mode) for h, async_mode in self._handlers[event_type] if h != handler
                ]

    def emit(self, event_type: str, data: Any) -> None:
        if self._closed:
            return
        handlers = []
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))
            handlers.extend([(h, False) for h in self._general_handlers])

        # 上报队列堆积量（待提交到线程池的任务数）
        try:
            from radar.core.prometheus_extra import observe_eventbus_queue_size
            queue_size = self._async_executor._work_queue.qsize() if hasattr(self._async_executor, '_work_queue') else 0
            observe_eventbus_queue_size(queue_size)
        except (ImportError, AttributeError, RuntimeError):
            # Prometheus 指标上报失败不应阻塞事件分发  # 安全忽略
            pass

        for handler, async_mode in handlers:
            if async_mode:
                self._async_executor.submit(self._safe_call, handler, event_type, data)
            else:
                self._safe_call(handler, event_type, data)

    def _safe_call(self, handler: Callable, event_type: str, data: Any) -> None:
        try:
            handler(event_type, data)
        except Exception as e:
            self.logger.error("[EventBus] 处理器异常 %s: %s", event_type, e)

    def shutdown(self) -> None:
        self._closed = True
        self._async_executor.shutdown(wait=False)


class _RedisEventBus:
    """Redis Pub/Sub 事件总线（跨进程/跨容器）。"""

    # 频道前缀
    CHANNEL_PREFIX = "radar:event"

    def __init__(self, redis_url: str, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._redis_url = redis_url
        self._handlers: Dict[str, List[Callable]] = {}
        self._general_handlers: List[Callable] = []
        self._lock = threading.RLock()
        self._async_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="redis_event_bus_"
        )
        self._closed = False
        self._node_id = uuid.uuid4().hex[:8]

        import redis as redis_mod

        self._redis = redis_mod.Redis.from_url(redis_url, decode_responses=True)
        self._redis.ping()
        self._pubsub = self._redis.pubsub()

        # 订阅线程
        self._sub_thread: Optional[threading.Thread] = None
        self._sub_running = False

    def _channel(self, event_type: str) -> str:
        return f"{self.CHANNEL_PREFIX}:{event_type}"

    def register(self, event_type: str, handler: Callable, async_mode: bool = False) -> None:
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
                # 首次注册该事件类型时订阅 Redis 频道
                self._pubsub.subscribe(self._channel(event_type))
                self.logger.debug("[RedisEventBus] 订阅频道 %s", self._channel(event_type))
                # 确保订阅线程在运行
                self._ensure_sub_thread()
            self._handlers[event_type].append((handler, async_mode))

    def unregister(self, event_type: str, handler: Callable) -> None:
        with self._lock:
            if event_type in self._handlers:
                self._handlers[event_type] = [
                    (h, async_mode) for h, async_mode in self._handlers[event_type] if h != handler
                ]
                if not self._handlers[event_type]:
                    self._pubsub.unsubscribe(self._channel(event_type))
                    self.logger.debug("[RedisEventBus] 取消订阅频道 %s", self._channel(event_type))

    def emit(self, event_type: str, data: Any) -> None:
        if self._closed:
            return
        handlers = []
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))
            handlers.extend([(h, False) for h in self._general_handlers])

        # 上报队列堆积量（待提交到线程池的任务数）
        try:
            from radar.core.prometheus_extra import observe_eventbus_queue_size
            queue_size = self._async_executor._work_queue.qsize() if hasattr(self._async_executor, '_work_queue') else 0
            observe_eventbus_queue_size(queue_size)
        except (ImportError, AttributeError, RuntimeError):
            # Prometheus 指标上报失败不应阻塞事件分发  # 安全忽略
            pass

        for handler, async_mode in handlers:
            if async_mode:
                self._async_executor.submit(self._safe_call, handler, event_type, data)
            else:
                self._safe_call(handler, event_type, data)

        # 广播到 Redis（跨进程消费者）
        try:
            import time as _time
            payload = json.dumps(
                {"_node": self._node_id, "_ts": _time.time(), "type": event_type, "data": data},
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            self._redis.publish(self._channel(event_type), payload)
        except Exception as e:
            self.logger.debug("[RedisEventBus] publish 失败: %s", e)

    def _safe_call(self, handler: Callable, event_type: str, data: Any) -> None:
        try:
            handler(event_type, data)
        except Exception as e:
            self.logger.error("[RedisEventBus] 处理器异常 %s: %s", event_type, e)

    def _ensure_sub_thread(self) -> None:
        if self._sub_thread is not None and self._sub_thread.is_alive():
            return
        self._sub_running = True
        self._sub_thread = threading.Thread(target=self._sub_loop, name="RedisEventBusSub", daemon=True)
        self._sub_thread.start()

    def _sub_loop(self) -> None:
        """Redis 订阅线程：监听消息并分发给本地处理器。"""
        self.logger.info("[RedisEventBus] 订阅线程启动 %s", self._node_id)
        while self._sub_running and not self._closed:
            try:
                message = self._pubsub.get_message(timeout=1.0)
                if message is None or message["type"] != "message":
                    continue

                recv_time = time.time()
                payload = json.loads(message["data"])

                # 忽略自己发出的消息（避免重复处理）
                if payload.get("_node") == self._node_id:
                    continue

                event_type = payload["type"]
                data = payload["data"]

                # 计算 Redis 消费延迟
                try:
                    publish_time = payload.get("_ts", recv_time)
                    lag = recv_time - publish_time
                    from radar.core.prometheus_extra import observe_eventbus_redis_lag
                    observe_eventbus_redis_lag(lag)
                except (ImportError, AttributeError, RuntimeError):
                    # Prometheus 指标上报失败不应阻塞消息消费  # 安全忽略
                    pass

                # 异步分发给本地处理器
                handlers = []
                with self._lock:
                    handlers = list(self._handlers.get(event_type, []))
                    handlers.extend([(h, False) for h in self._general_handlers])
                for handler, async_mode in handlers:
                    if async_mode:
                        self._async_executor.submit(self._safe_call, handler, event_type, data)
                    else:
                        self._safe_call(handler, event_type, data)
            except Exception as e:
                if not self._closed:
                    self.logger.warning("[RedisEventBus] 订阅循环异常: %s", e)
                    time.sleep(1.0)
        self.logger.info("[RedisEventBus] 订阅线程退出 %s", self._node_id)

    def shutdown(self) -> None:
        self._closed = True
        self._sub_running = False
        try:
            self._pubsub.close()
        except (ImportError, AttributeError, RuntimeError):
            # 关闭阶段资源清理失败可忽略  # 安全忽略
            pass
        try:
            self._redis.close()
        except (ImportError, AttributeError, RuntimeError):
            # 关闭阶段资源清理失败可忽略  # 安全忽略
            pass
        self._async_executor.shutdown(wait=False)
        if self._sub_thread and self._sub_thread.is_alive():
            self._sub_thread.join(timeout=2.0)


@overload
def create_event_bus(logger: Optional[logging.Logger] = None) -> EventBusProtocol: ...


@overload
def create_event_bus(logger: Optional[logging.Logger], *, backend: Literal["memory", "redis", "rediss"]) -> EventBusProtocol: ...


def create_event_bus(
    logger: Optional[logging.Logger] = None,
    *,
    backend: Optional[Literal["memory", "redis", "rediss"]] = None,
) -> EventBusProtocol:
    """工厂函数：根据参数或环境变量创建内存/Redis 事件总线。"""
    backend_name = (backend or os.getenv("RADAR_EVENT_BUS") or "memory").strip().lower()
    if backend_name in ("redis", "rediss"):
        url = os.getenv("RADAR_REDIS_URL", "redis://127.0.0.1:6379/0").strip()
        try:
            bus = _RedisEventBus(url, logger=logger)
            (logger or logging.getLogger(__name__)).info("[EventBus] Redis 模式: %s", url)
            return bus
        except Exception as e:
            (logger or logging.getLogger(__name__)).warning(
                "[EventBus] Redis 初始化失败，回退到内存模式: %s", e
            )
            return _MemoryEventBus(logger=logger)
    return _MemoryEventBus(logger=logger)


@contextmanager
def event_bus_context(
    logger: Optional[logging.Logger] = None,
    *,
    backend: Optional[Literal["memory", "redis", "rediss"]] = None,
) -> Iterator[EventBusProtocol]:
    """在 with 块结束时自动关闭事件总线，避免线程池或 Redis 连接泄漏。"""
    bus = create_event_bus(logger, backend=backend)
    try:
        yield bus
    finally:
        bus.shutdown()
