"""
EventModeMixin - 事件驱动模式管理
从 MonitorEngine 拆分出的事件驱动相关逻辑
"""

import logging
import threading
import time
from typing import Any, Optional

from radar.core.event_bus import create_event_bus
from radar.core.push_adapter import MarketDataEvent, PushAdapter


class EventModeMixin:
    """提供事件驱动模式的支持（替代轮询）"""

    # 事件驱动模式下 battlefield 持久化最小间隔（秒）。
    # 推送速率可达每秒多次 tick，逐 tick 全量 save_all 会触发 SQLite 写风暴；
    # poll 模式以 BATTLEFIELD_WRITE_INTERVAL=5 次扫描节流，event 模式按时间节流更直观。
    BATTLEFIELD_EVENT_SAVE_MIN_INTERVAL_SEC = 2.0

    logger: logging.Logger
    config: Any
    targets: list
    market: Any
    running: bool
    _portfolio: Any
    _alert_batcher: Any
    _battlefield: Any
    time_checker: Any
    process_target: Any
    _flush_alert_batch: Any

    def enable_event_mode(self) -> None:
        """
        启用事件驱动模式（替代 time.sleep 轮询）。
        调用时机：所有 target 添加完成后调用。
        """
        if not self.targets:
            self.logger.warning("[MonitorEngine] 无监控目标，无法启用事件模式")
            return

        codes = [t.code for t in self.targets]
        self._push_adapter = PushAdapter(
            fetcher=self.market,
            event_bus=create_event_bus(logger=self.logger),
            logger=self.logger,
            target_codes=set(codes),
        )
        self._event_bus = self._push_adapter.event_bus
        self._event_bus.register("market_data", self._on_market_data)

        self._event_mode = True
        self.logger.info(
            f"[MonitorEngine] 事件驱动模式已启用，监控 {len(codes)} 只股票"
        )

    def _on_market_data(self, event_type: str, event: MarketDataEvent) -> None:
        """行情数据事件处理器（由 EventBus 调用）"""
        ttt_start = time.time()

        target = None
        for t in self.targets:
            if t.code == event.code:
                target = t
                break

        if not target:
            return

        data = {
            "code": event.code,
            "name": event.name,
            "price": event.price,
            "volume": event.volume,
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "prev_close": event.prev_close,
            "change_pct": event.change_pct,
        }

        self._portfolio.update_price(event.code, event.price)

        try:
            alerts = self.process_target(target, data)
            for alert in alerts:
                self._alert_batcher.add(alert)
            self._flush_alert_batch()

            # battlefield 持久化按时间节流，避免高频推送下 SQLite 写风暴
            if hasattr(self, "_battlefield"):
                last_save = getattr(self, "_battlefield_event_last_save_ts", 0.0)
                if time.time() - last_save >= self.BATTLEFIELD_EVENT_SAVE_MIN_INTERVAL_SEC:
                    self._battlefield.save_all(self)
                    self._battlefield_event_last_save_ts = time.time()

            try:
                from radar.core.trading_observability import refresh_trading_observability
                refresh_trading_observability(self)
            except (ImportError, AttributeError):
                # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
                pass
        except Exception as e:
            self.logger.error(f"[MonitorEngine] 处理 {event.code} 事件异常: {e}")
        finally:
            ttt_ms = (time.time() - ttt_start) * 1000
            try:
                from radar.core.prometheus_extra import observe_tick_to_trade_latency
                observe_tick_to_trade_latency(ttt_ms)
            except (ImportError, AttributeError):
                # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
                pass

    def _run_event_mode(self) -> None:
        """事件驱动模式主循环"""
        self.logger.info("[MonitorEngine] 事件驱动模式运行中...")
        self._push_adapter.start()
        stop_event = threading.Event()

        try:
            while self.running:
                if not self.time_checker.is_open():
                    self._health.set_market_open(False)
                    now = self.time_checker.get_beijing_time()
                    next_open = self._calculate_next_open_time(now)
                    minutes_to_open = (next_open - now).total_seconds() / 60 if next_open else None

                    if minutes_to_open and minutes_to_open <= 5:
                        self.logger.debug(f"接近开盘({minutes_to_open:.1f}分钟)，保持推送...")
                    else:
                        self.logger.debug("非交易时段，暂停推送适配器...")
                        self._push_adapter.stop()
                        while self.running:
                            now = self.time_checker.get_beijing_time()
                            next_open = self._calculate_next_open_time(now)
                            if next_open:
                                wait_seconds = (next_open - now).total_seconds() - 300
                                if wait_seconds <= 0:
                                    break
                                stop_event.wait(timeout=min(60, wait_seconds))
                            else:
                                stop_event.wait(timeout=60)

                            if not self.time_checker.is_open():
                                continue
                            break

                        if not self.running:
                            break

                        self.logger.info("交易时段开始，恢复推送适配器")
                        self._health.set_market_open(True)
                        self._push_adapter.start()
                else:
                    self._health.set_market_open(True)

                stop_event.wait(timeout=1.0)

        except KeyboardInterrupt:
            self.logger.info("正在停止...")
            self.running = False
        except Exception as e:
            self.logger.error(f"事件模式主循环异常: {e}")
        finally:
            self._push_adapter.stop()
            try:
                self._event_bus.shutdown()
            except (ImportError, AttributeError):
                # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
                pass
            self._cleanup_on_exit()

    def _calculate_next_open_time(self, now) -> Optional[Any]:
        """计算下一次开盘时间（A 股工作日 09:30 / 13:00 北京时间）。

        修正点：旧实现在「工作日 >= 午盘开始」时仍 ``return morning_start``
        且 ``days_ahead == 0``，把"今天 09:30"作为 next_open 返回。
        调用方 ``_run_event_mode`` 用 ``(next_open - now).total_seconds() - 300``
        当作等待秒数，负值会立即 break 跳出休市等待，引发"周五收盘后立刻
        进入 tick loop"的活锁。这里把分支重写为显式 4 种情形。
        """
        try:
            import datetime
            beijing_tz = self.time_checker.beijing_tz
            weekday = now.weekday()  # 0=Mon ... 6=Sun
            current_time = now.time()

            if weekday < 5 and current_time < self.time_checker.morning_start:
                # 工作日开盘前 -> 今天 09:30
                target_date = now.date()
                target_t = self.time_checker.morning_start
            elif weekday < 5 and current_time < self.time_checker.afternoon_start:
                # 工作日午盘前（含午休）-> 今天 13:00
                target_date = now.date()
                target_t = self.time_checker.afternoon_start
            else:
                # 工作日收盘后 / 周末 -> 跳到下一交易日 09:30
                if weekday == 4:        # 周五（午后或收盘后）
                    days_ahead = 3
                elif weekday == 5:      # 周六
                    days_ahead = 2
                elif weekday == 6:      # 周日
                    days_ahead = 1
                else:                   # 周一~周四收盘后
                    days_ahead = 1
                target_date = now.date() + datetime.timedelta(days=days_ahead)
                target_t = self.time_checker.morning_start

            naive = datetime.datetime.combine(target_date, target_t)
            return beijing_tz.localize(naive)
        except (ImportError, AttributeError):
            # 交易时间计算失败，安全返回None  # 安全忽略
            return None
