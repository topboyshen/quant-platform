#!/usr/bin/env python3
"""
RL 策略 - 集成到回测引擎
加载训练好的 PPO 模型，在回测/实盘时做决策
"""

from typing import Dict

import pandas as pd

from backtest_pro import Strategy
from rl_agent import RLAgent
from rl_features import (
    ExternalFeatureProvider,
    RLObservationBuilder,
    RLObservationBuilderWithExternal,
    prepare_external_features,
)


class RLStrategy(Strategy):
    """
    强化学习策略

    使用训练好的 PPO 模型决定每日动作。
    inference 时使用 RLObservationBuilder 生成状态，并注入真实账户持仓。
    支持 universal 通用策略模型，以及外部特征（市场情绪、资金流）。
    """

    def __init__(self, code: str, direction: str, trigger_price: float,
                 model_dir: str = "rl_models", position_pct: float = 0.2,
                 universal: bool = False, cache_dir: str = "rl_feature_cache",
                 use_external_features: bool = False):
        super().__init__(f"RL_PPO_{code}_{direction}")
        self.code = code
        self.direction = direction
        self.trigger_price = trigger_price
        self.position_pct = position_pct
        self.universal = universal
        self.cache_dir = cache_dir
        self.use_external_features = use_external_features
        self.model_suffix = "ext" if use_external_features else ""

        self.agent = RLAgent(model_dir=model_dir)
        self.obs_builder = None

        # live 模式缓存
        self.price_history: list = []
        self.volume_history: list = []
        self._live_builder = None

        # 实时外部特征提供者
        self._external_provider = ExternalFeatureProvider()

        self._last_index: int = -1
        self._last_action: int = 0

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        """RL 模型决定入场"""
        action = self._predict_action(data, context)
        return action in [1, 2]

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float):
        """RL 模型决定出场"""
        action = self._predict_action(data, context)
        return action in [3, 4], self._action_to_reason(action)

    def calculate_position_size(self, capital: float, price: float) -> int:
        """RL 策略使用固定仓位比例"""
        position_value = capital * self.position_pct
        return int(position_value / price)

    def _action_to_reason(self, action: int) -> str:
        reasons = {
            0: "保持空仓",
            1: "RL建仓",
            2: "RL加仓",
            3: "RL减仓",
            4: "RL清仓"
        }
        return reasons.get(action, "未知")

    def _get_external_features(self) -> Dict:
        """获取实时外部特征（Live 模式），返回已归一化的字典"""
        return self._external_provider.get_realtime_external_features(self.code)

    def _predict_action(self, data: pd.Series, context: Dict) -> int:
        """
        从当前市场数据构建状态并预测动作。
        Backtest 模式下使用 builder + 真实账户状态；
        Live 模式下用缓存历史构建临时 builder，并注入实时外部特征。
        """
        index = context.get('index', 0)

        if self._last_index == index:
            return self._last_action

        df = context.get('df')
        if df is not None:
            # Backtest 模式
            if self.obs_builder is None:
                if self.use_external_features:
                    external_df = prepare_external_features(df, self.code)
                    self.obs_builder = RLObservationBuilderWithExternal(
                        df, self.trigger_price, cache_dir=self.cache_dir,
                        external_features=external_df
                    )
                else:
                    self.obs_builder = RLObservationBuilder(df, self.trigger_price, cache_dir=self.cache_dir)

            position_shares = context.get('position_shares', 0)
            position_cost = context.get('position_cost', 0.0)

            if self.use_external_features and isinstance(self.obs_builder, RLObservationBuilderWithExternal):
                # backtest 模式下使用预计算的外部特征，不调用实时 API
                obs = self.obs_builder.get_observation(index, position_shares, position_cost)
            else:
                obs = self.obs_builder.get_observation(index, position_shares, position_cost)
        else:
            # Live 模式
            price = data.get('price', 0)
            self.price_history.append(price)
            if 'volume' in data:
                self.volume_history.append(data['volume'])

            tmp_df = pd.DataFrame({
                'price': self.price_history,
                'volume': self.volume_history if self.volume_history else [0] * len(self.price_history)
            })

            if self._live_builder is None or len(tmp_df) != len(self._live_builder.df):
                if self.use_external_features:
                    self._live_builder = RLObservationBuilderWithExternal(
                        tmp_df, self.trigger_price, live_mode=True
                    )
                else:
                    self._live_builder = RLObservationBuilder(tmp_df, self.trigger_price)

            idx = len(tmp_df) - 1
            if self.use_external_features and isinstance(self._live_builder, RLObservationBuilderWithExternal):
                external_feats = self._get_external_features()
                obs = self._live_builder.get_observation(idx, 0, 0.0, external_feats)
            else:
                obs = self._live_builder.get_observation(idx, 0, 0.0)

        action = self.agent.predict(
            self.code, self.direction, obs,
            universal=self.universal, model_suffix=self.model_suffix
        )
        if action is None:
            action = 0

        self._last_index = index
        self._last_action = action
        return action
