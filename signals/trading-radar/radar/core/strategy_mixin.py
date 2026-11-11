"""
策略管理 Mixin
提取 MonitorEngine 中的策略相关方法
"""

from typing import Any, Dict, Optional

from radar.core.alert_payloads import MLPredictionAlertDict, PriceTriggerAlertDict
from radar.strategy.base import PriceTriggerStrategy
from radar.strategy.ml_strategy import MLStrategy
from radar.strategy.rl_strategy_v2 import RLStrategyV2


class StrategyMixin:
    """策略管理 Mixin"""

    def _init_strategies(self) -> None:
        """初始化策略存储"""
        self._price_strategies: Dict[str, PriceTriggerStrategy] = {}
        self._ml_strategies: Dict[str, MLStrategy] = {}
        self._rl_strategies: Dict[str, RLStrategyV2] = {}

    def _create_strategies_for_target(self, target) -> None:
        """为目标创建策略实例"""
        self._price_strategies[target.code] = PriceTriggerStrategy(
            trigger_price=target.trigger_price,
            direction=target.direction
        )

        if target.ml_enabled:
            self._ml_strategies[target.code] = MLStrategy(
                code=target.code,
                direction=target.direction,
                trigger_price=target.trigger_price,
                model_dir=self.config.ml_model_dir,
                threshold=self.config.ml_threshold,
                min_history_bars=self.config.ml_min_history_bars,
                logger=self.logger
            )

        if target.rl_enabled:
            rl_strategy = RLStrategyV2(
                code=target.code,
                direction=target.direction,
                trigger_price=target.trigger_price,
                model_dir=self.config.rl_model_dir,
                universal=False,
                use_real_account=False,
                use_external_features=True,
                logger=self.logger
            )
            account = self._portfolio.get_account_state(target.code)
            rl_strategy.set_real_account(account)
            self._rl_strategies[target.code] = rl_strategy

    def check_price_trigger(
        self, target, data: Dict[str, Any]
    ) -> Optional[PriceTriggerAlertDict]:
        """检查价格触发"""
        strategy = self._price_strategies.get(target.code)
        if not strategy:
            return None

        signal = strategy.get_signal_info(data)

        if signal['triggered']:
            return {
                'type': 'price_trigger',
                'price': signal['price'],
                'trigger_price': signal['trigger_price'],
                'direction': signal['direction'],
                'distance_pct': signal['distance_pct']
            }

        return None

    def check_ml_prediction(
        self, target, data: Dict[str, Any]
    ) -> Optional[MLPredictionAlertDict]:
        """检查ML预测"""
        if not target.ml_enabled:
            return None

        strategy = self._ml_strategies.get(target.code)
        if not strategy:
            return None

        prediction = strategy.should_alert(data)
        if prediction:
            return {
                'type': 'ml_prediction',
                'prediction': prediction['prediction'],
                'probability': prediction['probability'],
                'trigger_price': prediction['trigger_price'],
                'triggered': prediction.get('triggered', False),
                'current_price': prediction.get('current_price', 0.0),
            }

        return None

    def check_rl_signal(
        self, target, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """检查RL信号"""
        if not target.rl_enabled:
            return None

        strategy = self._rl_strategies.get(target.code)
        if not strategy:
            return None

        account = self._portfolio.get_account_state(target.code)
        strategy.set_real_account(account)

        if self.config.rl_mode == 'confirm':
            signal = strategy.check_signal_confirm(
                price=data.get('price', 0),
                trigger_price=target.trigger_price,
                direction=target.direction,
                volume=data.get('volume', 0)
            )
        else:
            signal = strategy.check_signal_independent(
                price=data.get('price', 0),
                volume=data.get('volume', 0)
            )

        return signal
