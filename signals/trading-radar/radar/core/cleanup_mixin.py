"""
CleanupMixin - 退出清理与状态持久化
从 MonitorEngine 拆分出的退出清理逻辑
"""

import logging
from typing import Any


class CleanupMixin:
    """提供优雅退出时的状态保存和资源清理"""

    logger: logging.Logger
    _battlefield: Any
    _alert_batcher: Any
    market: Any
    _health_server: Any

    def _cleanup_on_exit(self) -> None:
        """退出清理（两种模式共用）"""
        if hasattr(self, '_battlefield'):
            try:
                self._battlefield.save_all(self)
                self.logger.info("[MonitorEngine] 退出前阵地状态已保存")
            except Exception as e:
                self.logger.warning(f"[MonitorEngine] 退出保存状态失败: {e}")

        # 强制刷新冷却状态
        if hasattr(self, 'cooldown') and hasattr(self.cooldown, 'flush'):
            try:
                self.cooldown.flush()
            except Exception as e:
                self.logger.debug(f"退出前刷新冷却状态: {e}")

        try:
            self._flush_alert_batch()
        except Exception as e:
            self.logger.debug(f"退出前刷新告警批次: {e}")

        if hasattr(self.market, 'shutdown'):
            try:
                self.market.shutdown()
            except Exception as e:
                self.logger.debug(f"关闭行情线程池: {e}")

        # 输出性能统计
        if hasattr(self, '_perf_stats') and self._perf_stats.get('total_scans', 0) > 0:
            scans = self._perf_stats['total_scans']
            avg_fetch = self._perf_stats['total_fetch_time'] / scans
            avg_process = self._perf_stats['total_process_time'] / scans
            self.logger.info(
                f"[PerfStats] 扫描 {scans} 次，"
                f"avg_fetch={avg_fetch:.2f}s, avg_process={avg_process:.2f}s"
            )

        self.logger.info("监控已停止")

    def _flush_alert_batch(self) -> None:
        """刷新告警批次（由 AlertDispatchMixin 实际实现）"""
        pass  # 占位，实际由 AlertDispatchMixin 覆盖
