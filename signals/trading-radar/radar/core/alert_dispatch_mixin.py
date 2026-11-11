"""
AlertDispatchMixin - 告警分发与批处理管理
从 MonitorEngine 拆分出的告警相关逻辑
"""

import logging
from typing import Any, Callable, Dict, Optional

from radar.alert.batcher import AlertBatcher, AlertItem, AlertPriority, AlertType, build_merged_message


class AlertDispatchMixin:
    """提供告警分发、批处理和冷却管理"""

    logger: logging.Logger
    config: Any
    cooldown: Any
    notifier: Any
    _alert_batcher: AlertBatcher
    _battlefield: Any

    def _init_alert_batcher(self) -> AlertBatcher:
        """初始化告警批处理器"""
        priorities = self.config.alert_priorities or {
            'price': AlertPriority.HIGH,
            'ml': AlertPriority.MEDIUM,
            'rl': AlertPriority.MEDIUM,
        }
        priority_map = {}
        for k, v in priorities.items():
            if isinstance(v, str):
                priority_map[AlertType(k)] = AlertPriority[v.upper()]
            else:
                priority_map[AlertType(k)] = v

        return AlertBatcher(
            priorities=priority_map,
            batch_enabled=getattr(self.config, 'batch_alerts', True),
            logger=self.logger
        )

    def _dispatch_alert(
        self,
        target: Any,
        alert_type: AlertType,
        cooldown_key: str,
        alert_data: Optional[Dict[str, Any]],
        send_immediate: Callable[[], None],
        log_msg: Optional[str] = None,
        batch_data: Optional[Dict[str, Any]] = None,
        current_price: float = 0,
    ) -> Optional[AlertItem]:
        """通用告警分发：处理 cooldown、优先级判断、立即发送/日志记录/批次合并"""
        if not alert_data:
            return None

        if self.cooldown.is_in_cooldown(target.code, cooldown_key, self.config.cooldown_minutes):
            return None

        # 记录预警到准确率追踪系统
        if hasattr(self, '_accuracy_tracker') and alert_type in (AlertType.ML, AlertType.RL):
            try:
                predicted_direction = "down" if target.direction == "below" else "up"
                self._accuracy_tracker.record_alert(
                    alert_type=alert_type.value,
                    code=target.code,
                    name=target.name,
                    trigger_price=target.trigger_price,
                    direction=target.direction,
                    alert_price=current_price or alert_data.get('price', 0),
                    predicted_direction=predicted_direction,
                    confidence=alert_data.get('probability', 0.0) if alert_type == AlertType.ML else 0.0,
                )
            except Exception as e:
                self.logger.debug(f"记录预警准确率失败: {e}")

        if self._alert_batcher.should_log_only(alert_type):
            if log_msg:
                self.logger.info(log_msg)
            self.cooldown.record_alert(target.code, cooldown_key)
            return None
        elif self._alert_batcher.should_send_immediately(alert_type):
            send_immediate()
            self.cooldown.record_alert(target.code, cooldown_key)
            return None
        else:
            item = AlertItem(
                alert_type=alert_type,
                code=target.code,
                name=target.name,
                price=current_price or alert_data.get('price', 0),
                data=batch_data or alert_data
            )
            if hasattr(self, '_battlefield'):
                try:
                    self._battlefield.save_alert(
                        alert_type=alert_type.value,
                        code=target.code,
                        name=target.name,
                        price=current_price or alert_data.get('price', 0),
                        data=batch_data or alert_data,
                    )
                except (RuntimeError, OSError):
                    # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
                    pass
            return item

    def _flush_alert_batch(self) -> None:
        """刷新并发送合并后的批次告警"""
        grouped = self._alert_batcher.flush()

        for code, batch in grouped.items():
            alerts = batch['alerts']
            if not alerts:
                continue

            msg = build_merged_message(
                code=batch['code'],
                name=batch['name'],
                price=batch['price'],
                alerts=alerts,
                suppressed_count=batch.get('suppressed_count', 0),
            )

            if msg:
                title, content = msg
                self.notifier.send(title, content)

                for alert in alerts:
                    alert_type = alert['type']
                    self.cooldown.record_alert(code, alert_type)
