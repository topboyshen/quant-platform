"""
策略层 - 基础策略接口
职责：定义统一策略接口
"""

from abc import ABC, abstractmethod
from typing import Any, Dict


class Strategy(ABC):
    """策略基类"""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def should_enter(self, data: Dict, context: Dict = None) -> bool:
        """是否应该入场"""
        pass

    @abstractmethod
    def should_exit(self, data: Dict, context: Dict = None, entry_price: float = None) -> bool:
        """是否应该出场"""
        pass


class PriceTriggerStrategy(Strategy):
    """价格触发策略（支持动态触发价）"""

    def __init__(self, trigger_price: float, direction: str = 'below',
                 stop_loss_pct: float = 0.05, take_profit_pct: float = 0.10,
                 adaptive_manager=None):
        super().__init__(f"PriceTrigger_{direction}")
        self._original_trigger = trigger_price
        self.trigger_price = trigger_price
        self.direction = direction
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._adaptive = adaptive_manager

    def sync_trigger(self, code: str, current_price: float) -> float:
        """同步动态触发价，返回当前有效触发价"""
        if self._adaptive is not None:
            self.trigger_price = self._adaptive.sync(
                code, self._original_trigger, self.direction, current_price
            )
        return self.trigger_price

    def should_enter(self, data: Dict, context: Dict = None) -> bool:
        """检查是否触发入场条件"""
        return self.is_triggered(data)

    def is_triggered(self, data: Dict, trigger_price: float = None) -> bool:
        """按指定触发价判断是否触发；默认使用当前有效触发价。"""
        price = data.get('price', 0)
        effective_trigger = self.trigger_price if trigger_price is None else trigger_price

        if self.direction == 'below':
            return price <= effective_trigger
        else:  # above
            return price >= effective_trigger

    def should_exit(self, data: Dict, context: Dict = None, entry_price: float = None) -> bool:
        """检查是否触发离场条件（止损/止盈）"""
        if entry_price is None:
            return False

        price = data.get('price', 0)

        # 止损
        if price <= entry_price * (1 - self.stop_loss_pct):
            return True

        # 止盈
        if price >= entry_price * (1 + self.take_profit_pct):
            return True

        return False

    def get_signal_info(self, data: Dict, trigger_price: float = None) -> Dict[str, Any]:
        """获取信号详情"""
        price = data.get('price', 0)
        effective_trigger = self.trigger_price if trigger_price is None else trigger_price

        if self.direction == 'below':
            triggered = price <= effective_trigger
            distance = ((price - effective_trigger) / effective_trigger) * 100
        else:
            triggered = price >= effective_trigger
            distance = ((effective_trigger - price) / effective_trigger) * 100

        return {
            'triggered': triggered,
            'price': price,
            'trigger_price': effective_trigger,
            'current_trigger': self.trigger_price,
            'original_trigger': self._original_trigger,
            'direction': self.direction,
            'distance_pct': distance,
            'is_adaptive': self._adaptive is not None,
        }
