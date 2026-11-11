#!/usr/bin/env python3
"""
强化学习环境 - 股票交易 RL Env (Gymnasium)
训练时沙盒，特征计算复用 rl_features.RLObservationBuilder
"""

import logging
from typing import Dict, List, Optional

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

from rl_features import RLObservationBuilder, RLObservationBuilderWithExternal

logger = logging.getLogger(__name__)


class StockTradingEnv(gym.Env):
    """
    股票交易强化学习环境

    状态空间 (15维):
        - 收益率特征 (4维): return_1d, 5d, 10d, 20d
        - 技术指标 (6维): rsi_14, volatility_20d, momentum_10, momentum_20,
                         ma5_deviation, ma20_deviation
        - 价格位置 (1维): price_position (0-100)
        - 持仓状态 (2维): 是否持仓, 持仓盈亏比例
        - 目标距离 (1维): (当前价 - 触发价) / 触发价
        - 成交量趋势 (1维): volume_trend

    动作空间 (离散 5个):
        0: 保持空仓
        1: 建仓/满仓 (投入20%可用资金)
        2: 加仓 (再投入10%)
        3: 减仓 (卖出50%持仓)
        4: 清仓 (全部卖出)

    奖励函数:
        r = daily_return - 0.5 * drawdown_penalty + 0.1 * sharpe_bonus
    """

    metadata = {'render_modes': ['human']}

    def __init__(self, df: pd.DataFrame, trigger_price: float, direction: str = 'below',
                 initial_capital: float = 100000, position_pct: float = 0.2,
                 commission_rate: float = 0.0003, slippage: float = 0.001,
                 split: str = 'train', train_ratio: float = 0.8,
                 cache_dir: str = "rl_feature_cache",
                 drawdown_penalty_weight: float = 0.5,
                 sharpe_bonus_weight: float = 0.1,
                 trading_penalty_weight: float = 0.01,
                 max_trades_per_episode: int = 10,
                 external_features: pd.DataFrame = None,
                 live_mode: bool = False):
        super().__init__()

        full_df = df.reset_index(drop=True)
        n = len(full_df)
        train_n = int(n * train_ratio)

        if split == 'train':
            self.df = full_df.iloc[:train_n].reset_index(drop=True)
        elif split == 'val':
            self.df = full_df.iloc[train_n:].reset_index(drop=True)
        else:
            self.df = full_df

        self.trigger_price = trigger_price
        self.direction = direction
        self.initial_capital = initial_capital
        self.position_pct = position_pct
        self.commission_rate = commission_rate
        self.slippage = slippage
        self._cache_dir = cache_dir
        self.drawdown_penalty_weight = drawdown_penalty_weight
        self.sharpe_bonus_weight = sharpe_bonus_weight
        self.trading_penalty_weight = trading_penalty_weight
        self.max_trades_per_episode = max_trades_per_episode
        self.live_mode = live_mode

        # 反过度交易：滑动窗口参数
        self._recent_trade_window = 10       # 最近10步内统计交易频率
        self._max_recent_trades = 3          # 10步内最多3笔交易

        # 保存原始数据，用于 reset() 随机截取
        self.full_df = self.df.copy()
        self.min_episode_len = 30

        # 预计算完整数据的特征 builder，reset 时直接切片（避免重复计算）
        if external_features is not None or live_mode:
            # 使用带外部特征的 builder
            self._full_builder = RLObservationBuilderWithExternal(
                self.full_df, self.trigger_price, cache_dir=cache_dir,
                external_features=external_features, live_mode=live_mode
            )
        else:
            # 使用基础 builder
            self._full_builder = RLObservationBuilder(self.full_df, self.trigger_price, cache_dir=cache_dir)

        # 动态确定观察空间维度
        obs_dim = self._full_builder.obs_dim if hasattr(self._full_builder, 'obs_dim') else 15
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float32
        )
        self.action_space = spaces.Discrete(5)

        # 运行状态
        self.current_step = 0
        self.cash = initial_capital
        self.position_shares = 0
        self.position_cost_basis = 0.0    # 持仓成本
        self.position_market_value = 0.0  # 持仓市值
        self.total_value = initial_capital
        self.peak_value = initial_capital

        # 历史记录
        self.value_history: List[float] = []
        self.return_history: List[float] = []
        self.trades: List[Dict] = []

        # 特征生成器
        self.obs_builder = self._full_builder.slice(0, len(self.df))

    def reset(self, seed: Optional[int] = None, options: Optional[dict] = None):
        super().reset(seed=seed)

        # 数据增强：随机截取子区间作为新 episode
        n = len(self.full_df)
        if n > self.min_episode_len:
            # 随机选择 episode 长度和起始位置
            max_start = n - self.min_episode_len
            start = self.np_random.integers(0, max_start + 1)
            # 结束位置在 [start+min_len, n] 之间随机
            min_end = start + self.min_episode_len
            end = self.np_random.integers(min_end, n + 1)
            self.df = self.full_df.iloc[start:end].reset_index(drop=True)
            # 从预计算的 full_builder 切片，避免重新计算技术指标
            self.obs_builder = self._full_builder.slice(start, end)
        else:
            self.df = self.full_df.copy()
            self.obs_builder = self._full_builder.slice(0, n)

        self.current_step = 0
        self.cash = self.initial_capital
        self.position_shares = 0
        self.position_cost_basis = 0.0
        self.position_market_value = 0.0
        self.total_value = self.initial_capital
        self.peak_value = self.initial_capital
        self.value_history = [self.initial_capital]
        self.return_history = []
        self.trades = []
        self.trades_count = 0

        obs = self._get_observation()
        info = {}
        return obs, info

    def _get_observation(self, external_feats: Dict = None) -> np.ndarray:
        """构建状态向量 - 使用独立特征生成器"""
        if self.live_mode and external_feats:
            return self.obs_builder.get_observation(
                index=self.current_step,
                position_shares=self.position_shares,
                position_value=self.position_cost_basis,
                external_feats=external_feats
            )
        return self.obs_builder.get_observation(
            index=self.current_step,
            position_shares=self.position_shares,
            position_value=self.position_cost_basis
        )

    def _calculate_cost(self, amount: float, is_buy: bool) -> float:
        commission = max(amount * self.commission_rate, 5)
        transfer = amount * 0.00002
        if is_buy:
            return commission + transfer
        else:
            stamp = amount * 0.001
            return commission + transfer + stamp

    def _is_action_valid(self, action: int) -> bool:
        """判断当前动作是否合法"""
        if action == 0:
            return True
        elif action == 1:
            return self.position_shares == 0
        elif action in (2, 3, 4):
            return self.position_shares > 0
        return False

    def action_masks(self) -> np.ndarray:
        """返回动作掩码 (兼容 MaskablePPO): 1=可用, 0=不可用"""
        mask = np.ones(5, dtype=np.int8)
        if self.position_shares == 0:
            mask[2] = 0  # 加仓无效
            mask[3] = 0  # 减仓无效
            mask[4] = 0  # 清仓无效
        else:
            mask[1] = 0  # 建仓无效（已持仓）
        return mask

    def step(self, action: int):
        current_price = self.df['price'].iloc[self.current_step]
        trade_executed = False

        # 执行动作（无效动作会被静默忽略）
        if action == 0:
            pass  # 保持
        elif action == 1 and self.position_shares == 0:
            # 建仓: 投入 position_pct 资金
            invest_amount = self.cash * self.position_pct
            entry_price = current_price * (1 + self.slippage)
            shares = int(invest_amount / entry_price)
            if shares > 0:
                cost = self._calculate_cost(shares * entry_price, True)
                if self.cash >= shares * entry_price + cost:
                    self.cash -= (shares * entry_price + cost)
                    self.position_shares = shares
                    self.position_cost_basis = shares * entry_price
                    trade_executed = True
        elif action == 2 and self.position_shares > 0:
            # 加仓: 再投入 10% 资金
            invest_amount = self.cash * 0.1
            entry_price = current_price * (1 + self.slippage)
            shares = int(invest_amount / entry_price)
            if shares > 0:
                cost = self._calculate_cost(shares * entry_price, True)
                if self.cash >= shares * entry_price + cost:
                    self.cash -= (shares * entry_price + cost)
                    self.position_shares += shares
                    self.position_cost_basis += shares * entry_price
                    trade_executed = True
        elif action == 3 and self.position_shares > 0:
            # 减仓: 卖出 50%
            sell_shares = self.position_shares // 2
            if sell_shares > 0:
                exit_price = current_price * (1 - self.slippage)
                amount = sell_shares * exit_price
                cost = self._calculate_cost(amount, False)
                self.cash += (amount - cost)
                self.position_shares -= sell_shares
                if self.position_shares == 0:
                    self.position_cost_basis = 0.0
                else:
                    self.position_cost_basis = self.position_cost_basis * (self.position_shares / (self.position_shares + sell_shares))
                trade_executed = True
        elif action == 4 and self.position_shares > 0:
            # 清仓
            exit_price = current_price * (1 - self.slippage)
            amount = self.position_shares * exit_price
            cost = self._calculate_cost(amount, False)
            self.cash += (amount - cost)
            self.position_shares = 0
            self.position_cost_basis = 0.0
            trade_executed = True

        if trade_executed:
            self.trades_count += 1
            self.trades.append({
                'step': self.current_step,
                'action': action,
                'price': current_price,
            })

        # 标记动作是否合法
        is_valid = self._is_action_valid(action)

        # 计算总资产
        self.position_market_value = self.position_shares * current_price
        self.total_value = self.cash + self.position_market_value
        self.peak_value = max(self.peak_value, self.total_value)

        self.value_history.append(self.total_value)

        # 计算奖励
        reward = self._calculate_reward()

        # 无效动作惩罚
        if not is_valid:
            reward -= 0.01

        self.current_step += 1
        terminated = self.current_step >= len(self.df) - 1
        truncated = False

        obs = self._get_observation()
        info = {
            'step': self.current_step,
            'total_value': self.total_value,
            'cash': self.cash,
            'position_shares': self.position_shares,
            'price': current_price
        }

        return obs, reward, terminated, truncated, info

    def _calculate_reward(self) -> float:
        """计算奖励: 日收益 + 风险调整 + 反过度交易惩罚。

        设计原则：
        1. 日收益权重适度（×5），避免放大微小价差激励高频翻转。
        2. 滑动窗口交易计数：在最近 N 步内的交易都被惩罚，防止
           Agent 利用长 episode 规避全局交易计数。
        3. 持仓不动的韧性奖励：Agent 可以选择不交易并仍然获得正反馈。
        4. 大额回撤加重惩罚（二次），抑制赌博式建仓。
        """
        if len(self.value_history) < 2:
            return 0.0

        raw_daily = (self.value_history[-1] - self.value_history[-2]) / max(self.value_history[-2], 1e-8)
        self.return_history.append(raw_daily)

        # ── 1. 收益项 ──────────────────────────────────────────
        # 降权到 ×5（原 ×10 放大了微小波动），减少对投机式交易的激励
        step_return = raw_daily * 5.0

        # ── 2. 回撤惩罚（二次） ─────────────────────────────────
        drawdown = (self.peak_value - self.total_value) / max(self.peak_value, 1e-8)
        dd_penalty = -self.drawdown_penalty_weight * (drawdown ** 2)

        # ── 3. 夏普奖励 ────────────────────────────────────────
        sharpe_bonus = 0.0
        if len(self.return_history) >= 20:
            recent_returns = np.array(self.return_history[-20:])
            std = np.std(recent_returns)
            # 允许 std==0 的情况获得最大夏普奖励（零波动 + 正收益）
            if std > 0:
                sharpe = np.mean(recent_returns) / std * np.sqrt(252)
            elif np.mean(recent_returns) > 0:
                sharpe = 10.0  # 零波动正收益 → 高夏普
            else:
                sharpe = 0.0
            sharpe_bonus = self.sharpe_bonus_weight * np.tanh(sharpe)

        # ── 4. 反过度交易惩罚（滑动窗口） ────────────────────────
        # 核心修复：用滑动窗口（trade_window=10）替代全局计数，
        # 防止 Agent 在长 episode 中频繁建仓→清仓→建仓循环。
        sliding_trade_count = 0
        max(0, len(self.trades) - self._recent_trade_window)
        for t in self.trades[-self._recent_trade_window:]:
            sliding_trade_count += 1

        trading_penalty = 0.0
        if sliding_trade_count > self._max_recent_trades:
            excess = sliding_trade_count - self._max_recent_trades
            # 渐进式惩罚：超出越多越重
            trading_penalty = -self.trading_penalty_weight * (excess ** 1.5)

        # ── 5. 持仓不动的韧性奖励 ──────────────────────────────
        # 当且仅当本步未交易且 Agent 通过不操作避免了亏损时给予小奖励
        hold_bonus = 0.0
        latest_trade_was_this_step = (
            self.trades
            and self.trades[-1].get('step', -1) == self.current_step
        )
        if not latest_trade_was_this_step:
            # 跟踪最近几次奖励的稳定性
            if len(self.return_history) >= 5:
                recent = self.return_history[-5:]
                if abs(np.mean(recent)) < 0.005 and np.std(recent) < 0.01:
                    # 稳定不交易 → 给予微小韧性奖励
                    hold_bonus = 0.005
                elif np.mean(recent) > 0:
                    # 持仓盈利中 → 鼓励继续持有
                    hold_bonus = 0.01

        # ── 6. 连续盈利的额外奖励（保留） ────────────────────────
        streak_bonus = 0.0
        if len(self.return_history) >= 3 and raw_daily > 0:
            if all(r > 0 for r in self.return_history[-3:]):
                streak_bonus = 0.02

        reward = (
            step_return
            + dd_penalty
            + sharpe_bonus
            + trading_penalty
            + hold_bonus
            + streak_bonus
        )
        return float(reward)

    def render(self):
        logger.info(
            "Step: %s, Price: %.2f, Value: %.2f, Cash: %.2f, Shares: %s",
            self.current_step,
            self.df['price'].iloc[self.current_step],
            self.total_value,
            self.cash,
            self.position_shares,
        )
