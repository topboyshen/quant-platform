"""
PollModeMixin - 传统轮询模式管理
从 MonitorEngine 拆分出的轮询相关逻辑
"""

import logging
import time
from typing import Any


class PollModeMixin:
    """提供传统轮询模式的支持"""

    logger: logging.Logger
    config: Any
    targets: list
    running: bool
    time_checker: Any
    process_all_targets_concurrent: Any
    _cleanup_on_exit: Any

    def _run_poll_mode(self) -> None:
        """传统轮询模式（兼容旧代码）"""
        IDLE_INTERVAL = 300
        PRE_OPEN_WINDOW = 5
        HEARTBEAT_INTERVAL = 300  # 5分钟

        try:
            last_heartbeat = 0
            while self.running:
                try:
                    if not self.time_checker.is_open():
                        self._health.set_market_open(False)
                        now = self.time_checker.get_beijing_time()
                        next_open = self._calculate_next_open_time(now)
                        minutes_to_open = (next_open - now).total_seconds() / 60 if next_open else None

                        if minutes_to_open and minutes_to_open <= PRE_OPEN_WINDOW:
                            self.logger.debug(f"接近开盘({minutes_to_open:.1f}分钟)，预热模式...")
                            time.sleep(30)
                        else:
                            if minutes_to_open:
                                self.logger.debug(f"非交易时段，下次开盘还有 {minutes_to_open:.0f} 分钟，休眠{IDLE_INTERVAL}秒...")
                            else:
                                self.logger.debug(f"非交易时段（周末/节假日），休眠{IDLE_INTERVAL}秒...")
                            # scan heartbeat：每5分钟在非交易时段记录一次
                            now_ts = time.time()
                            if now_ts - last_heartbeat >= HEARTBEAT_INTERVAL:
                                self._health.record_scan('heartbeat')
                                last_heartbeat = now_ts
                            time.sleep(IDLE_INTERVAL)
                        continue

                    self._health.set_market_open(True)
                    self.logger.debug(f"扫描 {len(self.targets)} 个目标...")
                    self.process_all_targets_concurrent()
                    time.sleep(self.config.scan_interval)

                except KeyboardInterrupt:
                    self.logger.info("正在停止...")
                    self.running = False
                except Exception as e:
                    self.logger.error(f"主循环异常: {e}")
                    time.sleep(10)
        finally:
            self._cleanup_on_exit()
