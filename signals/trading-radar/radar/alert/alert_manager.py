"""
告警管理器 V3 - 智能告警系统
职责：
1. 告警去重（内容hash）
2. 限流控制（每分钟最多N条）
3. 分级路由（Critical/Warning/Info）
4. 批量合并（5秒窗口）
5. 反压处理（Telegram限流自动降级）
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class AlertLevel(Enum):
    """告警级别"""
    CRITICAL = "critical"    # 立即发送
    WARNING = "warning"    # 合并发送
    INFO = "info"          # 每小时汇总


@dataclass(slots=True)
class AlertItem:
    """告警条目"""
    level: AlertLevel
    title: str
    content: str
    stock_code: str = ""
    stock_name: str = ""
    alert_type: str = ""  # price, ml, rl, system
    # 原始信号（_send_single 渲染 price/ml/rl 专用消息时使用）
    signal_info: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    _hash: str = ""
    # Delivery is asynchronous relative to submit().  Callers that persist a
    # business cooldown must do so only after this callback receives True.
    on_delivery: Optional[Callable[[bool], None]] = field(
        default=None,
        repr=False,
        compare=False,
        kw_only=True,
    )

    def __post_init__(self):
        if not self._hash:
            # 去重 key 不包含 content：content 中常含变动价格/时间戳等抖动字段，
            # 否则同类告警的 hash 永远不同，去重失效。
            # 用 (level, alert_type, stock_code, title) 唯一标识一条逻辑告警。
            key = (
                f"{self.level.value}:{self.alert_type}:"
                f"{self.stock_code}:{self.title}"
            )
            self._hash = hashlib.md5(key.encode()).hexdigest()[:12]


class RateLimiter:
    """令牌桶限流器"""

    def __init__(self, rate: float = 10.0, burst: int = 20):
        """
        rate: 每秒允许的请求数
        burst: 最大突发容量
        """
        self.rate = rate
        self.burst = burst
        self._tokens = burst
        self._last_update = time.time()
        self._lock = False  # 简化：单线程

    def acquire(self) -> bool:
        """尝试获取一个令牌"""
        now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_update = now

        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def get_wait_time(self) -> float:
        """获取需要等待的时间"""
        if self._tokens >= 1:
            return 0
        return (1 - self._tokens) / self.rate


class AlertManager:
    """
    告警管理器 V3

    处理流程：
    1. 接收原始告警
    2. 去重（最近5分钟相同内容）
    3. 分级（Critical/Warning/Info）
    4. 限流（令牌桶）
    5. 合并（5秒窗口）
    6. 发送（Telegram/日志）
    """

    # 去重窗口必须 >= 最大扫描间隔（ScanScheduler.INTERVAL_HOLIDAY=600s），
    # 否则非交易日 / 节假日扫描每次都会重新触发同一类告警造成风暴。
    DEDUP_WINDOW = 1800.0  # 30 分钟去重窗口
    BATCH_WINDOW = 5.0     # 5 秒合并窗口
    MAX_BATCH_SIZE = 10    # 每批最多 10 条
    FAILURE_BACKOFF = 30.0  # 发送失败后短退避，避免每个扫描周期重复轰炸

    def __init__(self, notifier: Any, logger: logging.Logger):
        self.notifier = notifier
        self.logger = logger

        # 去重历史
        self._dedup_history: Dict[str, float] = {}
        self._failure_retry_after: Dict[str, float] = {}
        self._state_lock = threading.RLock()
        # State mutations are protected by ``_state_lock``.  Transport calls
        # use a separate lock so a slow notifier never blocks submit(), while
        # requests.Session-backed notifiers are still invoked serially.
        # RLock also makes future delivery callbacks safe if they re-enter
        # flush()/submit() on the same thread.
        self._delivery_lock = threading.RLock()

        # 限流器
        self._limiter = RateLimiter(rate=0.5, burst=5)  # 每分钟30条，突发5条

        # 批处理
        self._batch_buffer: List[AlertItem] = []
        self._batch_start_time: float = 0
        self._batch_timer: Optional[threading.Timer] = None

        # 统计
        self._stats = {
            'total_received': 0,
            'deduplicated': 0,
            'throttled': 0,
            'sent': 0,
            'failed': 0,
            'retry_deferred': 0,
            'batched': 0,
        }

        # 静默时段
        self._silent_hours = (0, 9)  # 0:00-9:00 静默（只发Critical）

    def _is_silent_time(self) -> bool:
        """检查是否在静默时段"""
        hour = time.localtime().tm_hour
        return self._silent_hours[0] <= hour < self._silent_hours[1]

    def _should_send(self, item: AlertItem) -> bool:
        """判断是否应该发送"""
        # Critical级别始终发送
        if item.level == AlertLevel.CRITICAL:
            return True

        # 静默时段只发Critical
        if self._is_silent_time():
            self.logger.debug(f"静默时段，跳过非Critical告警: {item.title}")
            return False

        return True

    def _is_duplicate(self, item: AlertItem) -> bool:
        """检查是否重复"""
        now = time.time()
        with self._state_lock:
            self._cleanup_state_locked(now)

            # 检查重复
            if item._hash in self._dedup_history:
                self._stats['deduplicated'] += 1
                return True
            return False

    def _cleanup_state_locked(self, now: float) -> None:
        """Drop expired success and failure state while holding _state_lock."""
        expired_dedup = [
            key
            for key, timestamp in self._dedup_history.items()
            if now - timestamp > self.DEDUP_WINDOW
        ]
        for key in expired_dedup:
            self._dedup_history.pop(key, None)

        expired_retry = [
            key
            for key, retry_after in self._failure_retry_after.items()
            if retry_after <= now
        ]
        for key in expired_retry:
            self._failure_retry_after.pop(key, None)

    def _is_in_failure_backoff(self, item: AlertItem) -> bool:
        """发送失败后的短退避不等同于成功去重。"""
        now = time.time()
        with self._state_lock:
            self._cleanup_state_locked(now)
            retry_after = self._failure_retry_after.get(item._hash, 0.0)
            if retry_after <= now:
                return False
            self._stats['retry_deferred'] += 1
        self.logger.debug(
            "告警发送失败退避中，%.1fs 后可重试: %s",
            retry_after - now,
            item.title,
        )
        return True

    def _reserve_if_sendable(self, item: AlertItem) -> str:
        """Atomically check backoff/dedup/rate-limit and reserve this item."""
        now = time.time()
        with self._state_lock:
            self._cleanup_state_locked(now)

            retry_after = self._failure_retry_after.get(item._hash, 0.0)
            if retry_after > now:
                self._stats['retry_deferred'] += 1
                outcome = 'backoff'
                detail = retry_after - now
            elif item._hash in self._dedup_history:
                self._stats['deduplicated'] += 1
                outcome = 'duplicate'
                detail = 0.0
            elif not self._limiter.acquire():
                self._stats['throttled'] += 1
                outcome = 'throttled'
                detail = self._limiter.get_wait_time()
            else:
                # Reserve before releasing the lock.  Timer delivery commits
                # this timestamp on success and removes it on failure.
                self._dedup_history[item._hash] = now
                return 'reserved'

        if outcome == 'backoff':
            self.logger.debug(
                "告警发送失败退避中，%.1fs 后可重试: %s",
                detail,
                item.title,
            )
        elif outcome == 'duplicate':
            self.logger.debug("告警去重: %s", item.title)
        else:
            self.logger.warning(
                "告警限流，等待 %.1fs: %s",
                detail,
                item.title,
            )
            self.logger.info("[限流告警] %s: %s", item.title, item.content)
        return outcome

    def _record_delivery(self, item: AlertItem, delivered: bool) -> None:
        """Commit success state, or release dedup state after a failure."""
        with self._state_lock:
            if delivered:
                self._failure_retry_after.pop(item._hash, None)
            else:
                # submit() reserves the key while an item is queued.  A failed
                # transport must release it, otherwise a transient outage silently
                # suppresses the alert for the full 30-minute dedup window.
                self._dedup_history.pop(item._hash, None)
                self._failure_retry_after[item._hash] = (
                    time.time() + self.FAILURE_BACKOFF
                )
                self._stats['failed'] += 1

        callback = getattr(item, 'on_delivery', None)
        if callback is not None:
            try:
                callback(delivered)
            except Exception as exc:
                self.logger.error("告警发送结果回调失败: %s", exc)

    def submit(self, item: AlertItem) -> bool:
        """
        提交告警
        返回是否被接受（非重复且非限流）
        """
        with self._state_lock:
            self._stats['total_received'] += 1

        # 1. 检查是否应该发送
        if not self._should_send(item):
            return False

        # 2. 原子检查失败退避、去重和限流，并为入队项预留去重键。
        if self._reserve_if_sendable(item) != 'reserved':
            return False

        # 3. 入队；发送失败时 _record_delivery() 会释放预留键。
        self._add_to_batch(item)
        return True

    def _add_to_batch(self, item: AlertItem):
        """加入批处理缓冲区"""
        detached: List[AlertItem] = []
        with self._state_lock:
            # 启动新批次。Timer 可以立刻调度，但会等待同一把锁，
            # 因此不可能在 append 前摘走空批次。
            if not self._batch_buffer:
                self._batch_start_time = time.time()
                self._start_batch_timer_locked()

            self._batch_buffer.append(item)

            if len(self._batch_buffer) >= self.MAX_BATCH_SIZE:
                detached = self._detach_batch_locked()

        if detached:
            self._deliver_batch(detached)

    def _start_batch_timer_locked(self) -> None:
        """Start a batch timer while holding _state_lock."""
        if self._batch_timer:
            self._batch_timer.cancel()

        self._batch_timer = threading.Timer(self.BATCH_WINDOW, self._flush_batch)
        self._batch_timer.daemon = True
        self._batch_timer.start()

    def _detach_batch_locked(self) -> List[AlertItem]:
        """Atomically detach the current batch while holding _state_lock."""
        if not self._batch_buffer:
            return []

        batch = self._batch_buffer
        self._batch_buffer = []
        if self._batch_timer:
            self._batch_timer.cancel()
            self._batch_timer = None
        return batch

    def _flush_batch(self):
        """刷新批次"""
        with self._state_lock:
            batch = self._detach_batch_locked()
        if batch:
            self._deliver_batch(batch)

    def _deliver_batch(self, batch: List[AlertItem]) -> None:
        """Deliver a detached batch without holding the state lock."""
        if not batch:
            return

        with self._delivery_lock:
            self._deliver_batch_locked(batch)

    def _deliver_batch_locked(self, batch: List[AlertItem]) -> None:
        """Serialize notifier access; caller must hold _delivery_lock."""

        # 按级别分组
        critical = [i for i in batch if i.level == AlertLevel.CRITICAL]
        warning = [i for i in batch if i.level == AlertLevel.WARNING]
        info = [i for i in batch if i.level == AlertLevel.INFO]

        # 发送Critical（单独发送）
        for item in critical:
            self._send_single(item)

        # 合并Warning
        if warning:
            self._send_batch(warning, "⚠️ 预警汇总")

        # 合并Info（每小时汇总，这里简化）
        if info:
            self._send_batch(info, "ℹ️ 信息汇总")

        with self._state_lock:
            self._stats['batched'] += len(batch)

    def _send_single(self, item: AlertItem) -> bool:
        """发送单条告警"""
        # 兼容旧调用方未填 signal_info 的情况，避免 AttributeError
        info = getattr(item, 'signal_info', None) or {}
        delivered = False
        try:
            if item.alert_type == 'price':
                delivered = bool(self.notifier.send_price_alert(
                    item.stock_name, item.stock_code,
                    info.get('price', 0),
                    info.get('trigger_price', 0),
                    info.get('direction', 'below'),
                    original_trigger=info.get('original_trigger', info.get('trigger_price', 0)),
                    is_adaptive=info.get('is_adaptive', False),
                ))
            elif item.alert_type == 'ml':
                delivered = bool(self.notifier.send_ml_alert(
                    item.stock_name, item.stock_code,
                    info.get('prediction', 'unknown'),
                    info.get('probability', 0),
                    info.get('target_price', 0)
                ))
            elif item.alert_type == 'rl':
                delivered = bool(self.notifier.send_rl_alert(
                    item.stock_name, item.stock_code,
                    info.get('action', 'hold'),
                    info.get('confidence')
                ))
            else:
                delivered = bool(self.notifier.send(item.title, item.content))

            if delivered:
                with self._state_lock:
                    self._stats['sent'] += 1
            else:
                self.logger.warning("通知器未确认发送成功: %s", item.title)
        except Exception as exc:
            self.logger.error(f"发送告警失败: {exc}")
        finally:
            self._record_delivery(item, delivered)
        return delivered

    def _send_batch(self, items: List[AlertItem], title_prefix: str) -> bool:
        """发送批量告警"""
        if not items:
            return False

        # 构建汇总消息
        lines = []
        for item in items:
            # 提取方向语义
            direction_hint = ""
            info = getattr(item, 'signal_info', None) or {}
            if item.alert_type == 'price':
                direction = info.get('direction', '')
                if direction == 'below':
                    direction_hint = " [📉卖]"
                elif direction == 'above':
                    direction_hint = " [📈买]"
            elif item.alert_type == 'ml':
                prediction = info.get('prediction', '')
                if '下破' in prediction or 'below' in prediction.lower():
                    direction_hint = " [📉卖]"
                elif '上破' in prediction or 'above' in prediction.lower():
                    direction_hint = " [📈买]"
            elif item.alert_type == 'rl':
                action = info.get('action', '')
                if action in ('open', 'add'):
                    direction_hint = " [📈买]"
                elif action in ('reduce', 'close'):
                    direction_hint = " [📉卖]"

            lines.append(f"• {item.stock_name}({item.stock_code}): {item.title}{direction_hint}")

        content = "\n".join(lines)

        delivered = False
        try:
            delivered = bool(
                self.notifier.send(f"{title_prefix} ({len(items)}条)", content)
            )
            if delivered:
                with self._state_lock:
                    self._stats['sent'] += len(items)
            else:
                self.logger.warning("通知器未确认批量发送成功: %s", title_prefix)
        except Exception as exc:
            self.logger.error(f"发送批量告警失败: {exc}")
        finally:
            for item in items:
                self._record_delivery(item, delivered)
        return delivered

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._state_lock:
            return {
                **self._stats,
                'buffer_size': len(self._batch_buffer),
                'dedup_cache_size': len(self._dedup_history),
                'failure_backoff_size': len(self._failure_retry_after),
                'tokens_available': self._limiter._tokens,
            }

    def flush(self):
        """强制刷新所有待发送告警"""
        self._flush_batch()
        self.logger.info("告警管理器已刷新")

    def shutdown(self):
        """关闭"""
        self.flush()
        with self._state_lock:
            if self._batch_timer:
                self._batch_timer.cancel()
                self._batch_timer = None
        self.logger.info("告警管理器已关闭")
