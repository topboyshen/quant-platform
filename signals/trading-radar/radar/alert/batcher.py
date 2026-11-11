"""
告警批处理与合并模块
解决同一股票同一周期内多条消息轰炸问题
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

MAX_ALERTS_PER_STOCK_BATCH = 3


class AlertType(Enum):
    PRICE = "price"
    ML = "ml"
    RL = "rl"


class AlertPriority(Enum):
    HIGH = 1    # 立即发送，不合并
    MEDIUM = 2  # 可合并到汇总消息
    LOW = 3     # 只记录日志，不发 Telegram


@dataclass(slots=True)
class AlertItem:
    """单个告警项"""
    alert_type: AlertType
    code: str
    name: str
    price: float
    data: Dict = field(default_factory=dict)


class AlertBatcher:
    """
    告警批处理器

    一个扫描周期内：
    1. 收集所有告警
    2. 按股票合并
    3. 根据优先级决定发送策略
    """

    # 默认优先级配置
    DEFAULT_PRIORITIES = {
        AlertType.PRICE: AlertPriority.HIGH,
        AlertType.ML: AlertPriority.MEDIUM,
        AlertType.RL: AlertPriority.MEDIUM,
    }

    def __init__(self, priorities: Dict[AlertType, AlertPriority] = None,
                 batch_enabled: bool = True, logger: logging.Logger = None):
        self.priorities = priorities or self.DEFAULT_PRIORITIES.copy()
        self.batch_enabled = batch_enabled
        self.logger = logger or logging.getLogger(__name__)

        # 当前批次的告警
        self._alerts: List[AlertItem] = []

    def add(self, alert: AlertItem) -> None:
        """添加告警到当前批次"""
        # 如果批处理关闭，或高优先级告警，不等待立即处理
        if not self.batch_enabled:
            return [alert]

        priority = self.priorities.get(alert.alert_type, AlertPriority.MEDIUM)
        if priority == AlertPriority.HIGH:
            # 高优先级立即返回，不放入批次等待
            return [alert]

        # 防止重复告警累积：同一 code + type 已存在则替换，避免重复
        existing = None
        for idx, a in enumerate(self._alerts):
            if a.code == alert.code and a.alert_type == alert.alert_type:
                existing = idx
                break
        if existing is not None:
            self._alerts[existing] = alert
        else:
            self._alerts.append(alert)
        return None  # 已加入批次，等待 flush

    def should_send_immediately(self, alert_type: AlertType) -> bool:
        """判断某类告警是否应该立即发送"""
        if not self.batch_enabled:
            return True
        priority = self.priorities.get(alert_type, AlertPriority.MEDIUM)
        return priority == AlertPriority.HIGH

    def should_log_only(self, alert_type: AlertType) -> bool:
        """判断某类告警是否只记录日志"""
        priority = self.priorities.get(alert_type, AlertPriority.MEDIUM)
        return priority == AlertPriority.LOW

    def flush(self) -> Dict[str, Dict]:
        """
        刷新批次，返回按股票合并后的告警
        :return: {code: {name, price, alerts: [{type, data}], priority}, ...}
        """
        if not self._alerts:
            return {}

        # 按股票分组
        grouped: Dict[str, Dict] = {}
        try:
            for alert in self._alerts:
                if alert.code not in grouped:
                    grouped[alert.code] = {
                        'name': alert.name,
                        'code': alert.code,
                        'price': alert.price,
                        'alerts': [],
                        'max_priority': AlertPriority.LOW,
                    }

                priority = self.priorities.get(alert.alert_type, AlertPriority.MEDIUM)
                alerts = grouped[alert.code]['alerts']
                existing_idx = next(
                    (idx for idx, item in enumerate(alerts) if item['type'] == alert.alert_type.value),
                    None,
                )
                item = {
                    'type': alert.alert_type.value,
                    'data': alert.data,
                    'priority': priority.value,
                }
                if existing_idx is None:
                    alerts.append(item)
                else:
                    alerts[existing_idx] = item

                if priority.value < grouped[alert.code]['max_priority'].value:
                    grouped[alert.code]['max_priority'] = priority
        finally:
            # 无论处理是否异常，都清空批次，防止重复发送或内存泄漏
            self._alerts = []

        return {
            code: {
                **batch,
                'alerts': sorted(batch['alerts'], key=lambda item: item['priority'])[:MAX_ALERTS_PER_STOCK_BATCH],
                'suppressed_count': max(0, len(batch['alerts']) - MAX_ALERTS_PER_STOCK_BATCH),
            }
            for code, batch in grouped.items()
        }

    def clear(self) -> None:
        """清空当前批次"""
        self._alerts = []


def build_merged_message(code: str, name: str, price: float,
                         alerts: List[Dict], notifier=None,
                         suppressed_count: int = 0) -> Optional[tuple]:
    """
    构建合并后的消息
    :return: (title, content) 或 None
    """
    if not alerts:
        return None

    has_price = any(a['type'] == AlertType.PRICE.value for a in alerts)
    has_ml = any(a['type'] == AlertType.ML.value for a in alerts)
    # RL 信号目前与 ML 共用标题"ML/RL 信号"，无需单独统计

    # 确定标题
    if has_price:
        title = f"📊 综合告警 - {name}"
    elif has_ml:
        title = f"🤖 ML/RL 信号 - {name}"
    else:
        title = f"🧠 策略信号 - {name}"

    # 构建内容
    lines = [
        f"📌 股票: {name} ({code})",
        f"💰 当前价格: ¥{price:.2f}",
        ""
    ]

    # 价格触发信息放最前面
    for alert in alerts:
        if alert['type'] == AlertType.PRICE.value:
            data = alert['data']
            direction_text = "跌破" if data.get('direction') == 'below' else "突破"
            lines.append(f"🚨 价格触发: {direction_text} ¥{data.get('trigger', 0):.2f}")

    # ML 预测
    for alert in alerts:
        if alert['type'] == AlertType.ML.value:
            data = alert['data']
            triggered = data.get('triggered', False)
            trigger_price = data.get('trigger_price', 0)
            current = data.get('current_price', price)
            direction = data.get('direction', 'below')

            if triggered:
                status_emoji = "✅"
                status_text = "已触发"
            else:
                status_emoji = "🔮"
                status_text = "预测性"

            # 计算距离触发价的差距
            if trigger_price > 0 and current > 0:
                if direction == 'below':
                    diff = current - trigger_price
                    diff_pct = diff / trigger_price * 100
                    if triggered:
                        distance_text = f"已跌破触发价 {abs(diff_pct):.1f}%"
                    else:
                        distance_text = f"距触发价还有 {diff_pct:.1f}%"
                else:  # above
                    diff = trigger_price - current
                    diff_pct = diff / trigger_price * 100
                    if triggered:
                        distance_text = f"已突破触发价 {abs(diff_pct):.1f}%"
                    else:
                        distance_text = f"距触发价还有 {diff_pct:.1f}%"
            else:
                distance_text = ""

            lines.append(f"{status_emoji} [{status_text}] ML预测: {data.get('prediction', '未知')} "
                        f"(置信度 {data.get('probability', 0):.1%})")
            if distance_text:
                lines.append(f"   📊 {distance_text} (当前¥{current:.2f} / 触发价¥{trigger_price:.2f})")

    # RL 信号
    for alert in alerts:
        if alert['type'] == AlertType.RL.value:
            data = alert['data']
            if data.get('rl_suggestion'):
                lines.append(f"🧠 RL建议: {data.get('rl_suggestion')}")
                if data.get('position_status'):
                    lines.append(f"   持仓: {data.get('position_status')} "
                               f"| 盈亏: {data.get('position_pnl', '0%')}")
            else:
                lines.append(f"🧠 RL信号: {data.get('action', '未知')}")

    if suppressed_count:
        lines.append(f"… 另有 {suppressed_count} 条低优先级重复信号已合并")

    lines.append("")
    lines.append("⏰ 以上信号在同一扫描周期内触发")

    return title, "\n".join(lines)
