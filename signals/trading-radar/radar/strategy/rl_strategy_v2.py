"""
RL 策略 V2 - 解决 train-inference distribution shift
支持：
1. 真实账户状态注入（解决虚拟持仓问题）
2. RL 作为辅助确认信号（非独立入场）
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from radar.strategy.feature_extractor import IncrementalFeatureExtractor, get_global_predictor
from radar.strategy.rl_schema import build_live_observation
from radar.trading_assumptions import TradingCostModel, apply_slippage


@dataclass
class AccountState:
    """账户状态"""
    cash: float = 100000.0
    position_shares: int = 0
    position_cost: float = 0.0

    @property
    def has_position(self) -> bool:
        return self.position_shares > 0

    @property
    def position_value(self, current_price: float = 0) -> float:
        return self.position_shares * current_price

    def get_position_pnl_pct(self, current_price: float) -> float:
        """获取持仓盈亏百分比"""
        if self.position_shares <= 0 or self.position_cost <= 0:
            return 0.0
        return (current_price - self.position_cost) / self.position_cost * 100


class RLStrategyV2:
    """
    RL 策略 V2 - 支持真实账户状态

    两种使用模式：
    1. 独立信号模式：RL 自己做开平仓决策（需传入真实账户状态）
    2. 辅助确认模式：价格触发后，RL 提供仓位建议（推荐）
    """

    ACTION_NAMES = {0: 'hold', 1: 'open', 2: 'add', 3: 'reduce', 4: 'close'}
    ACTION_DESC = {
        0: '维持当前仓位',
        1: '建议开仓/加仓',
        2: '继续加仓',
        3: '减仓锁定利润',
        4: '建议平仓'
    }

    def __init__(self, code: str, direction: str, trigger_price: float,
                 model_dir: str = "rl_models", universal: bool = True,
                 use_real_account: bool = False,  # 是否使用真实账户
                 use_external_features: bool = False,  # 是否使用外部特征
                 logger: logging.Logger = None):
        self.code = code
        self.direction = direction
        self.trigger_price = trigger_price
        self.universal = universal
        self.use_real_account = use_real_account
        self.use_external_features = use_external_features
        self.model_suffix = "ext" if use_external_features else ""
        self.logger = logger or logging.getLogger(__name__)

        # 增量特征提取器（带持久化价格历史）
        self._feature_extractor = IncrementalFeatureExtractor(
            code=code,  # 传入 code 启用持久化
            window_sizes=[5, 10, 20],
            max_history=100
        )

        # 使用全局模型预测器
        self._predictor = get_global_predictor(model_dir)

        # 外部特征（维度在启动时固定，避免每拍 import / 分配）
        self._external_provider = None
        self._ext_names: tuple = ()
        self._ext_dim = 0
        if use_external_features:
            try:
                from rl_features import ExternalFeatureProvider, RLObservationBuilderWithExternal
                self._external_provider = ExternalFeatureProvider()
                self._ext_names = tuple(RLObservationBuilderWithExternal.EXTERNAL_FEATURE_NAMES)
                self._ext_dim = len(self._ext_names)
            except Exception as e:
                self.logger.warning(f"外部特征提供者初始化失败: {e}")
        # 单次推理复用观测向量，减少 concatenate / 小数组分配
        self._obs_dim = 15 + self._ext_dim
        self._obs_buf = np.zeros(self._obs_dim, dtype=np.float32)

        # 虚拟账户状态（当 use_real_account=False 时使用）
        self._virtual_account = AccountState()

        # 真实账户状态引用（外部注入）
        self._real_account: Optional[AccountState] = None

        # 上次动作（用于检测变化）
        self._last_action = 0

    def set_real_account(self, account: AccountState) -> None:
        """设置真实账户状态（解决 distribution shift）"""
        self._real_account = account
        self.use_real_account = True

    def update_real_account(self, cash: float = None, position_shares: int = None,
                           position_cost: float = None):
        """更新真实账户状态"""
        if self._real_account is None:
            self._real_account = AccountState()

        if cash is not None:
            self._real_account.cash = cash
        if position_shares is not None:
            self._real_account.position_shares = position_shares
        if position_cost is not None:
            self._real_account.position_cost = position_cost

        self.use_real_account = True

    def _get_account(self) -> AccountState:
        """获取当前使用的账户状态"""
        if self.use_real_account and self._real_account is not None:
            return self._real_account
        return self._virtual_account

    def _fill_external_tail(self, dst: np.ndarray) -> None:
        """将外部特征写入 dst（长度 _ext_dim）。"""
        dst.fill(0.0)
        if self._ext_dim == 0 or self._external_provider is None:
            return
        try:
            features_dict = self._external_provider.get_realtime_external_features(self.code)
            for i, name in enumerate(self._ext_names):
                dst[i] = float(features_dict.get(name, 0.0))
        except Exception as e:
            self.logger.warning(f"获取外部特征失败: {e}")

    def predict_action(self, price: float, volume: float = 0) -> tuple:
        """
        预测动作，返回 (action_id, confidence)
        :return: (动作ID, 置信度/特征值)
        """
        self._feature_extractor.update(price, volume)
        account = self._get_account()
        base = build_live_observation(
            prices=self._feature_extractor.get_price_history(100),
            volumes=list(self._feature_extractor._volumes),
            trigger_price=self.trigger_price,
            position_shares=account.position_shares,
            position_cost=account.position_cost,
        )
        np.copyto(self._obs_buf[:15], base)

        if self._ext_dim:
            self._fill_external_tail(self._obs_buf[15:15 + self._ext_dim])

        vec = self._obs_buf[: self._obs_dim]
        action = self._predictor.predict(
            self.direction, vec, self.universal, self.code, self.model_suffix
        )
        return action, vec

    def check_signal_independent(self, price: float, volume: float = 0) -> Optional[Dict]:
        """
        独立信号模式：RL 自己做开平仓决策
        注意：此模式必须使用真实账户状态，否则有 distribution shift
        """
        action, features = self.predict_action(price, volume)
        account = self._get_account()

        # 动作有效性检查（基于真实持仓）
        is_valid = self._validate_action(action, account)

        # 更新虚拟账户（如果不用真实账户）
        if not self.use_real_account:
            self._update_virtual_position(action, price)

        # 只在动作变化且有效时告警
        if action != self._last_action and is_valid:
            self._last_action = action
            return {
                'code': self.code,
                'action': self.ACTION_NAMES[action],
                'action_id': action,
                'action_desc': self.ACTION_DESC[action],
                'price': price,
                'using_real_account': self.use_real_account,
                'account_position': account.position_shares,
                'position_pnl': account.get_position_pnl_pct(price),
                'confidence': float(features[0]) if len(features) > 0 else 0.0
            }

        self._last_action = action
        return None

    def check_signal_confirm(self, price: float, trigger_price: float,
                            direction: str, volume: float = 0) -> Optional[Dict]:
        """
        辅助确认模式：价格触发后，RL 提供仓位建议
        推荐模式：避免独立信号与真实持仓脱节

        :param price: 当前价格
        :param trigger_price: 触发价格
        :param direction: 触发方向
        """
        action, features = self.predict_action(price, volume)
        account = self._get_account()

        # 根据动作生成建议
        suggestions = {
            0: "观望",      # hold
            1: "建议建仓",  # open
            2: "建议加仓",  # add
            3: "建议减仓",  # reduce
            4: "建议平仓"   # close
        }

        if action not in {1, 2}:
            return None

        risk_note = ""

        return {
            'code': self.code,
            'triggered': True,
            'trigger_price': trigger_price,
            'current_price': price,
            'rl_suggestion': suggestions.get(action, "未知"),
            'rl_action': self.ACTION_NAMES[action],
            'position_status': f"持仓{account.position_shares}股" if account.has_position else "空仓",
            'position_pnl': f"{account.get_position_pnl_pct(price):.2f}%",
            'risk_note': risk_note,
            'using_real_account': self.use_real_account
        }

    def _validate_action(self, action: int, account: AccountState) -> bool:
        """验证动作在真实账户状态下是否有效"""
        if action == 0:  # hold
            return True
        elif action == 1:  # open
            # 开仓需要空仓且有资金
            return not account.has_position and account.cash > 0
        elif action in [2, 3]:  # add/reduce
            # 加仓减仓需要已有持仓
            return account.has_position
        elif action == 4:  # close
            # 平仓需要已有持仓
            return account.has_position
        return False

    def _update_virtual_position(self, action: int, price: float) -> None:
        """更新虚拟账户状态（仅模拟）"""
        if action == 1 and not self._virtual_account.has_position:
            self._virtual_account.position_shares = 100  # 模拟100股
            self._virtual_account.position_cost = price
        elif action == 4 and self._virtual_account.has_position:
            self._virtual_account.position_shares = 0
            self._virtual_account.position_cost = 0.0

    def should_alert(self, data: Dict, mode: str = 'confirm') -> Optional[Dict]:
        """
        统一入口
        :param mode: 'independent' 独立模式 / 'confirm' 辅助确认模式
        """
        price = data.get('price', 0)
        volume = data.get('volume', 0)

        if mode == 'independent':
            return self.check_signal_independent(price, volume)
        else:
            trigger_price = data.get('trigger_price', self.trigger_price)
            direction = data.get('direction', self.direction)
            return self.check_signal_confirm(price, trigger_price, direction, volume)

    def reset(self) -> None:
        """重置策略状态"""
        self._feature_extractor.reset()
        self._last_action = 0
        self._virtual_account = AccountState()
        # 注意：不清除 _real_account，因为那是外部注入的


class PortfolioTracker:
    """
    组合持仓追踪器
    用于在监控引擎中维护真实账户状态
    """

    def __init__(
        self,
        initial_cash: float = 100000.0,
        cost_model: TradingCostModel = None,
        slippage: float = 0.0,
    ):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: Dict[str, Dict] = {}  # code -> {shares, cost, last_price}
        self.cost_model = cost_model or TradingCostModel()
        self.slippage = slippage

    def update_price(self, code: str, price: float) -> None:
        """更新价格（无持仓时创建空条目，用于跟踪价格变化）"""
        if code not in self.positions:
            self.positions[code] = {'shares': 0, 'cost': 0.0, 'last_price': price}
        else:
            self.positions[code]['last_price'] = price

    def set_position(self, code: str, shares: int, cost: float) -> None:
        """设置持仓（用于从外部配置文件加载真实持仓）"""
        self.positions[code] = {
            'shares': shares,
            'cost': cost,
            'last_price': cost
        }

    def get_account_state(self, code: str) -> AccountState:
        """获取某只股票的账户状态"""
        if code in self.positions:
            pos = self.positions[code]
            return AccountState(
                cash=self.cash,
                position_shares=pos.get('shares', 0),
                position_cost=pos.get('cost', 0.0)
            )
        return AccountState(cash=self.cash, position_shares=0, position_cost=0.0)

    def execute_trade(
        self,
        code: str,
        action: str,
        price: float,
        shares: int = 100,
        expected_price: Optional[float] = None,
        strategy_tag: str = "",
    ):
        """执行交易（模拟或真实）。expected_price 用于滑点 Histogram；strategy_tag 用于 Prometheus 分组。"""
        tag = strategy_tag.strip() or "untagged"

        if action in {'open', 'add'}:
            fill_price = apply_slippage(price, self.slippage, is_buy=True)
            fee = self.cost_model.calculate(fill_price, shares, is_buy=True)
            total_cost = fill_price * shares + fee
            if self.cash >= total_cost:
                existing = self.positions.get(code, {'shares': 0, 'cost': 0.0, 'last_price': fill_price})
                old_shares = int(existing.get('shares', 0))
                old_cost = float(existing.get('cost', 0.0))
                new_shares = old_shares + shares
                avg_cost = (
                    (old_cost * old_shares + fill_price * shares) / new_shares
                    if new_shares > 0 else 0.0
                )
                self.cash -= total_cost
                self.positions[code] = {
                    'shares': new_shares,
                    'cost': avg_cost,
                    'last_price': fill_price,
                }
                if expected_price is not None:
                    try:
                        from radar.core.trading_observability import observe_fill_slippage

                        observe_fill_slippage(tag, expected_price, fill_price, side_buy=True)
                    except (ValueError, TypeError):
                        pass
                return True
        elif action in {'reduce', 'close'} and code in self.positions:
            pos = self.positions[code]
            held = int(pos['shares'])
            if held <= 0:
                return False
            qty = held if action == 'close' else min(shares, held)
            fill_price = apply_slippage(price, self.slippage, is_buy=False)
            avg_cost = float(pos['cost'])
            fee = self.cost_model.calculate(fill_price, qty, is_buy=False)
            realized = (fill_price - avg_cost) * qty - fee
            proceeds = fill_price * qty - fee
            self.cash += proceeds
            remaining = held - qty
            if remaining > 0:
                pos['shares'] = remaining
                pos['last_price'] = fill_price
            else:
                del self.positions[code]
            if expected_price is not None:
                try:
                    from radar.core.trading_observability import observe_fill_slippage

                    observe_fill_slippage(tag, expected_price, fill_price, side_buy=False)
                except (ImportError, ValueError, TypeError):
                    pass
            try:
                from radar.core.prometheus_extra import observe_strategy_trade_outcome

                observe_strategy_trade_outcome(tag, realized > 0)
            except (ImportError, ValueError, TypeError):
                pass
            return True
        return False

    def get_portfolio_value(self) -> float:
        """获取组合总价值"""
        position_value = sum(
            pos['shares'] * pos.get('last_price', pos['cost'])
            for pos in self.positions.values()
        )
        return self.cash + position_value
