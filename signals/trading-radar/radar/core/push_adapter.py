"""
事件驱动行情推送适配器 (Event-Driven Market Data Push Adapter)

解决核心问题：东财没有官方 WebSocket，只能基于 HTTP 轮询实现"伪推送"。

架构：
- PushAdapter: 独立线程持续 HTTP 轮询数据源，将变化的数据包装为事件推送到本地 EventBus
- EventBus: 内存中的 pub/sub，支持同步/异步处理器注册
- MonitorEngine: 注册为 EventBus 消费者，收到事件后触发策略计算（无 time.sleep 阻塞）

优势：
1. 数据源与策略解耦（同一数据可推送给多个消费者）
2. 消除 MonitorEngine 的 time.sleep 阻塞（用 threading.Event 等待）
3. 支持可变频率（交易活跃时自动缩短间隔，冷清时延长）
4. 为后续接入真实 WebSocket 数据源预留接口（调研与路线见 radar/ops/PUSH_AND_OBSERVABILITY.txt）

实现细节：
- 批量接口 fetch_all() 返回全市场 5000 只股票，我们提取监控目标子集
- 对比上一次数据，仅当价格/成交量变化时才推送事件（减少无效计算）
- 自适应频率：基于变化率动态调整轮询间隔（1-30秒）
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


@dataclass(frozen=True, slots=True)
class MarketDataEvent:
    """行情数据事件（不可变，便于哈希和比较）"""
    code: str
    name: str
    price: float
    volume: int
    change_pct: float
    open: float
    high: float
    low: float
    prev_close: float
    timestamp: float
    source: str = "eastmoney_push2"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_significant_change(self, other: "MarketDataEvent", price_threshold: float = 0.001) -> bool:
        """判断相对于另一个事件是否有显著变化"""
        if self.price == 0 or other.price == 0:
            return True  # 首次数据视为变化
        price_diff = abs(self.price - other.price) / other.price
        vol_diff = abs(self.volume - other.volume) / max(other.volume, 1)
        return price_diff > price_threshold or vol_diff > 0.05


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """告警事件"""
    alert_type: str  # price_trigger / ml_prediction / rl_signal
    code: str
    name: str
    price: float
    data: Dict[str, Any]
    timestamp: float


class EventBus:
    """
    轻量级本地事件总线（pub/sub）

    线程安全，支持：
    - 同步处理器（在同一线程立即执行）
    - 异步处理器（提交到线程池后台执行）
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._handlers: Dict[str, List[Callable]] = {}
        self._general_handlers: List[Callable] = []
        self._lock = threading.RLock()
        self._async_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="event_bus_"
        )

    def register(self, event_type: str, handler: Callable, async_mode: bool = False) -> None:
        """注册事件处理器"""
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append((handler, async_mode))

    def unregister(self, event_type: str, handler: Callable) -> None:
        """注销事件处理器"""
        with self._lock:
            if event_type in self._handlers:
                self._handlers[event_type] = [
                    (h, async_mode) for h, async_mode in self._handlers[event_type]
                    if h != handler
                ]

    def emit(self, event_type: str, data: Any) -> None:
        """发布事件（同步处理器立即执行，异步处理器提交线程池）"""
        handlers = []
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))
            handlers.extend([(h, False) for h in self._general_handlers])

        for handler, async_mode in handlers:
            if async_mode:
                self._async_executor.submit(self._safe_call, handler, event_type, data)
            else:
                self._safe_call(handler, event_type, data)

    def _safe_call(self, handler: Callable, event_type: str, data: Any) -> None:
        """安全调用处理器，捕获异常不传播"""
        try:
            handler(event_type, data)
        except Exception as e:
            self.logger.error(f"[EventBus] 处理器异常 {event_type}: {e}")

    def shutdown(self) -> None:
        """关闭事件总线"""
        self._async_executor.shutdown(wait=False)


class PushAdapter:
    """
    行情推送适配器

    独立线程持续轮询数据源，通过 EventBus 推送变化事件。
    自适应频率：基于数据变化率动态调整轮询间隔。
    """

    # 频率配置（秒）
    MIN_INTERVAL = 1.0      # 最快 1 秒（交易活跃时）
    MAX_INTERVAL = 30.0     # 最慢 30 秒（非交易时段）
    DEFAULT_INTERVAL = 5.0  # 默认 5 秒

    # 变化率阈值（用于频率调整）
    HIGH_ACTIVITY_THRESHOLD = 0.3  # 30% 股票变化视为高活跃
    LOW_ACTIVITY_THRESHOLD = 0.1   # 10% 股票变化视为低活跃

    def __init__(
        self,
        fetcher: Any,  # MarketDataFetcher or ConcurrentMarketDataFetcher
        event_bus: EventBus,
        logger: logging.Logger,
        target_codes: Optional[Set[str]] = None,
    ):
        self.fetcher = fetcher
        self.event_bus = event_bus
        self.logger = logger
        self.target_codes = target_codes or set()

        # 运行时状态
        self._last_events: Dict[str, MarketDataEvent] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._current_interval = self.DEFAULT_INTERVAL

        # 统计
        self._poll_count = 0
        self._event_count = 0
        self._skip_count = 0

    def set_targets(self, codes: List[str]) -> None:
        """设置监控目标代码"""
        self.target_codes = set(codes)
        # 清理不再监控的旧数据
        for code in list(self._last_events.keys()):
            if code not in self.target_codes:
                del self._last_events[code]

    def _fetch_and_emit(self) -> Tuple[int, int]:
        """
        获取数据并推送变化事件。
        返回: (推送事件数, 监控股票数)
        """
        try:
            # 使用批量接口获取全市场（一次请求）
            df = self.fetcher.fetch_all()
            if df is None or df.empty:
                return 0, 0

            # 提取监控目标（使用统一的列名解析器）
            from radar.data.column_resolver import StandardColumns, build_column_resolver

            resolver = build_column_resolver(df.columns)
            code_col = resolver.get(StandardColumns.CODE)
            name_col = resolver.get(StandardColumns.NAME)
            price_col = resolver.get(StandardColumns.CLOSE) or resolver.get(StandardColumns.PRICE)
            volume_col = resolver.get(StandardColumns.VOLUME)
            change_pct_col = resolver.get(StandardColumns.CHANGE_PCT)
            open_col = resolver.get(StandardColumns.OPEN)
            high_col = resolver.get(StandardColumns.HIGH)
            low_col = resolver.get(StandardColumns.LOW)
            prev_close_col = resolver.get(StandardColumns.PREV_CLOSE)

            # 如果还是找不到代码列，打印列名用于调试
            if code_col is None:
                self.logger.warning(f"[PushAdapter] 无法识别代码列，可用列: {df.columns.tolist()}")
                return 0, 0

            emitted = 0
            monitored = 0
            now = time.time()

            for _, row in df.iterrows():
                code = str(row.get(code_col, "")).strip().zfill(6)

                # 只处理监控目标
                if self.target_codes and code not in self.target_codes:
                    continue

                monitored += 1

                # 构建事件
                price = float(row.get(price_col, 0) or 0)
                if price == 0:
                    continue  # 停牌或无数据

                event = MarketDataEvent(
                    code=code,
                    name=str(row.get(name_col, code)) if name_col else code,
                    price=price,
                    volume=int(row.get(volume_col, 0) or 0) if volume_col else 0,
                    change_pct=float(row.get(change_pct_col, 0) or 0) if change_pct_col else 0,
                    open=float(row.get(open_col, 0) or 0) if open_col else 0,
                    high=float(row.get(high_col, 0) or 0) if high_col else 0,
                    low=float(row.get(low_col, 0) or 0) if low_col else 0,
                    prev_close=float(row.get(prev_close_col, 0) or 0) if prev_close_col else 0,
                    timestamp=now,
                )

                # 对比上一次，判断是否有显著变化
                last_event = self._last_events.get(code)
                if last_event and not event.is_significant_change(last_event):
                    self._skip_count += 1
                    continue  # 无显著变化，跳过

                # 推送事件
                self._last_events[code] = event
                self.event_bus.emit("market_data", event)
                emitted += 1
                self._event_count += 1

            # 防止 _last_events 无界增长：set_targets 才 prune 过时 key 不够
            # （当 target_codes 在外部静态配置时，set_targets 永远不会再被调），
            # 这里每次 fetch 后增量清理不在 target_codes 里的旧 key。
            # 对应诊断报告 5.1 修复。
            if self.target_codes:
                stale = [k for k in self._last_events if k not in self.target_codes]
                for k in stale:
                    self._last_events.pop(k, None)

            return emitted, monitored

        except Exception as e:
            self.logger.error(f"[PushAdapter] 数据获取异常: {e}")
            return 0, 0

    def _adjust_interval(self, emitted: int, monitored: int) -> None:
        """根据数据活跃度自适应调整轮询间隔"""
        if monitored == 0:
            return

        change_ratio = emitted / monitored

        if change_ratio > self.HIGH_ACTIVITY_THRESHOLD:
            # 高活跃，缩短间隔
            self._current_interval = max(self.MIN_INTERVAL, self._current_interval * 0.8)
        elif change_ratio < self.LOW_ACTIVITY_THRESHOLD:
            # 低活跃，延长间隔
            self._current_interval = min(self.MAX_INTERVAL, self._current_interval * 1.2)

        # 交易时段外强制延长
        # 注意：不直接依赖 MarketTimeChecker，由外部控制是否调用 start/stop

    def _run_loop(self) -> None:
        """推送线程主循环"""
        self.logger.info(f"[PushAdapter] 推送线程启动，初始间隔 {self._current_interval}s")

        while self._running:
            start_time = time.time()

            emitted, monitored = self._fetch_and_emit()
            self._poll_count += 1

            poll_elapsed = time.time() - start_time
            try:
                from radar.core.prometheus_extra import observe_pushadapter_poll

                observe_pushadapter_poll(poll_elapsed, emitted)
            except (RuntimeError, OSError):
                # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
                pass

            # 自适应频率
            self._adjust_interval(emitted, monitored)

            # 计算休眠时间（扣除本次轮询耗时）
            elapsed = poll_elapsed
            sleep_time = max(0.1, self._current_interval - elapsed)

            # 使用分段休眠以便快速响应停止信号
            while sleep_time > 0 and self._running:
                sleep_chunk = min(0.5, sleep_time)
                time.sleep(sleep_chunk)
                sleep_time -= sleep_chunk

        self.logger.info("[PushAdapter] 推送线程已停止")

    def start(self) -> None:
        """启动推送线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="PushAdapter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止推送线程"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "poll_count": self._poll_count,
            "event_count": self._event_count,
            "skip_count": self._skip_count,
            "current_interval": round(self._current_interval, 2),
            "target_count": len(self.target_codes),
            "last_event_count": len(self._last_events),
        }


# ───────────────────────────────────────────────
# 便捷函数
# ───────────────────────────────────────────────

def create_push_adapter(
    fetcher: Any,
    logger: logging.Logger,
    target_codes: Optional[List[str]] = None,
) -> Tuple[PushAdapter, EventBus]:
    """
    创建推送适配器 + 事件总线（便捷工厂函数）

    用法:
        adapter, bus = create_push_adapter(fetcher, logger, ["300059", "300033"])

        # MonitorEngine 注册为消费者
        bus.register("market_data", engine.on_market_data)

        # Dashboard 注册为消费者
        bus.register("market_data", dashboard.on_price_update, async_mode=True)

        adapter.start()
    """
    bus = EventBus(logger=logger)
    adapter = PushAdapter(
        fetcher=fetcher,
        event_bus=bus,
        logger=logger,
        target_codes=set(target_codes) if target_codes else None,
    )
    return adapter, bus
