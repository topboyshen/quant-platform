#!/usr/bin/env python3
"""
回测引擎
测试策略在历史数据上的表现
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class Trade:
    """单笔交易记录"""
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    direction: str = 'long'  # long/short
    shares: int = 0
    pnl: float = 0
    pnl_pct: float = 0
    exit_reason: str = ''


@dataclass
class BacktestResult:
    """回测结果"""
    total_return: float = 0
    annualized_return: float = 0
    max_drawdown: float = 0
    sharpe_ratio: float = 0
    win_rate: float = 0
    profit_factor: float = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_trade_return: float = 0
    avg_win: float = 0
    avg_loss: float = 0
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'total_return': f"{self.total_return:.2%}",
            'annualized_return': f"{self.annualized_return:.2%}",
            'max_drawdown': f"{self.max_drawdown:.2%}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'win_rate': f"{self.win_rate:.2%}",
            'profit_factor': f"{self.profit_factor:.2f}",
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'avg_trade_return': f"{self.avg_trade_return:.2%}",
            'avg_win': f"{self.avg_win:.2%}",
            'avg_loss': f"{self.avg_loss:.2%}"
        }


class Strategy:
    """策略基类"""

    def __init__(self, name: str = "BaseStrategy"):
        self.name = name

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        """是否应该入场"""
        raise NotImplementedError

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float) -> Tuple[bool, str]:
        """是否应该出场，返回(是否出场, 出场原因)"""
        raise NotImplementedError


class PriceTriggerStrategy(Strategy):
    """价格触发策略"""

    def __init__(self, trigger_price: float, direction: str = 'below',
                 stop_loss_pct: float = 0.05, take_profit_pct: float = 0.10):
        super().__init__(f"PriceTrigger_{direction}")
        self.trigger_price = trigger_price
        self.direction = direction
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        """价格触发入场"""
        current_price = data['price']

        if self.direction == 'below':
            return current_price <= self.trigger_price
        else:  # above
            return current_price >= self.trigger_price

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float) -> Tuple[bool, str]:
        """止损止盈出场"""
        current_price = data['price']

        if self.direction == 'below':  # 买入后上涨
            # 止盈
            if current_price >= entry_price * (1 + self.take_profit_pct):
                return True, 'take_profit'
            # 止损
            if current_price <= entry_price * (1 - self.stop_loss_pct):
                return True, 'stop_loss'
        else:  # 卖出后下跌（做空）
            # 止盈（价格下跌）
            if current_price <= entry_price * (1 - self.take_profit_pct):
                return True, 'take_profit'
            # 止损（价格上涨）
            if current_price >= entry_price * (1 + self.stop_loss_pct):
                return True, 'stop_loss'

        return False, ''


class MLEnsembleStrategy(Strategy):
    """机器学习集成策略"""

    def __init__(self, predictor, code: str, direction: str,
                 probability_threshold: float = 0.7,
                 stop_loss_pct: float = 0.05, take_profit_pct: float = 0.10):
        super().__init__(f"ML_{code}_{direction}")
        self.predictor = predictor
        self.code = code
        self.direction = direction
        self.probability_threshold = probability_threshold
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.price_history: List[float] = []
        self.volume_history: List[float] = []

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        """ML预测触发入场"""
        self.price_history.append(data['price'])
        if 'volume' in data:
            self.volume_history.append(data['volume'])

        if len(self.price_history) < 30:
            return False

        prediction = self.predictor.predict(
            self.code, self.direction,
            self.price_history, self.volume_history
        )

        if prediction is None:
            return False

        # 高概率且方向匹配
        if prediction['confidence'] in ['high', 'medium']:
            if self.direction == 'below' and prediction['will_trigger']:
                return True
            if self.direction == 'above' and prediction['will_trigger']:
                return True

        return False

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float) -> Tuple[bool, str]:
        """止损止盈出场"""
        current_price = data['price']

        if self.direction == 'below':
            if current_price >= entry_price * (1 + self.take_profit_pct):
                return True, 'take_profit'
            if current_price <= entry_price * (1 - self.stop_loss_pct):
                return True, 'stop_loss'
        else:
            if current_price <= entry_price * (1 - self.take_profit_pct):
                return True, 'take_profit'
            if current_price >= entry_price * (1 + self.stop_loss_pct):
                return True, 'stop_loss'

        return False, ''


class BacktestEngine:
    """回测引擎"""

    def __init__(self, initial_capital: float = 100000,
                 commission_rate: float = 0.0003,
                 slippage: float = 0.001,
                 logger: Optional[logging.Logger] = None):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage = slippage
        self.logger = logger or logging.getLogger(__name__)

    def run(self, df: pd.DataFrame, strategy: Strategy,
            position_size: float = 0.2,
            execution_mode: str = "next_bar") -> BacktestResult:
        """
        运行回测

        Args:
            df: 历史数据 DataFrame (需包含 price, volume 等列)
            strategy: 策略实例
            position_size: 每次交易仓位比例 (0-1)
            execution_mode: 成交假设，默认 next_bar（t 产生信号，t+1 成交）；legacy_same_bar 保留旧行为

        Returns:
            BacktestResult 回测结果
        """
        self.logger.info(f"开始回测: {strategy.name}")

        if execution_mode not in {"next_bar", "legacy_same_bar"}:
            raise ValueError("execution_mode must be 'next_bar' or 'legacy_same_bar'")

        capital = self.initial_capital
        position = None  # 当前持仓
        trades: List[Trade] = []
        equity_curve = [capital]
        pending_entry = None
        pending_exit = None

        def _as_dt(index_value):
            return index_value if isinstance(index_value, datetime) else pd.to_datetime(index_value)

        def _mark_to_market_cash(current_price: float) -> float:
            if position is None:
                return capital
            if position.direction == 'long':
                return capital + position.shares * current_price
            return capital + position.shares * (2 * position.entry_price - current_price)

        for i, (idx, row) in enumerate(df.iterrows()):
            current_price = float(row['price'])
            date = _as_dt(idx)

            # next_bar: 先用当前 bar 执行上一 bar 产生的订单，再用当前 bar 产生下一 bar 订单。
            if execution_mode == "next_bar":
                if pending_exit is not None and position is not None:
                    exit_reason = pending_exit
                    exit_price = current_price * (1 - self.slippage) if position.direction == 'long' else current_price * (1 + self.slippage)
                    commission = position.shares * exit_price * self.commission_rate

                    if position.direction == 'long':
                        gross_pnl = position.shares * (exit_price - position.entry_price)
                        capital += position.shares * exit_price - commission
                    else:
                        gross_pnl = position.shares * (position.entry_price - exit_price)
                        capital += gross_pnl - commission

                    pnl = gross_pnl - commission
                    pnl_pct = gross_pnl / (position.shares * position.entry_price) if position.shares else 0.0

                    position.exit_time = date
                    position.exit_price = exit_price
                    position.pnl = pnl
                    position.pnl_pct = pnl_pct * 100
                    position.exit_reason = exit_reason
                    trades.append(position)
                    self.logger.debug(f"出场: {exit_reason}, 收益: {pnl_pct:.2%}")
                    position = None
                    pending_exit = None

                if pending_entry is not None and position is None:
                    direction = pending_entry
                    entry_price = current_price * (1 + self.slippage) if direction == 'long' else current_price * (1 - self.slippage)
                    position_value = capital * position_size
                    shares = int(position_value / entry_price)

                    if shares > 0:
                        commission = shares * entry_price * self.commission_rate
                        if direction == 'long':
                            total_cost = shares * entry_price + commission
                            if capital >= total_cost:
                                capital -= total_cost
                                position = Trade(
                                    entry_time=date,
                                    entry_price=entry_price,
                                    direction=direction,
                                    shares=shares,
                                )
                                self.logger.debug(f"入场: 价格 {entry_price:.2f}, 数量 {shares}")
                        else:
                            capital -= commission
                            position = Trade(
                                entry_time=date,
                                entry_price=entry_price,
                                direction=direction,
                                shares=shares,
                            )
                            self.logger.debug(f"入场: 价格 {entry_price:.2f}, 数量 {shares}")
                    pending_entry = None

            context = {
                'index': i,
                'total': len(df),
                'prev_data': df.iloc[i-1] if i > 0 else None,
                'execution_mode': execution_mode,
                'df': df.iloc[:i+1],
            }

            if position is not None and pending_exit is None:
                should_exit, exit_reason = strategy.should_exit(row, context, position.entry_price)

                if should_exit:
                    if execution_mode == "next_bar":
                        pending_exit = exit_reason
                    else:
                        exit_price = current_price * (1 - self.slippage) if position.direction == 'long' else current_price * (1 + self.slippage)
                        commission = position.shares * exit_price * self.commission_rate

                        if position.direction == 'long':
                            gross_pnl = position.shares * (exit_price - position.entry_price)
                            capital += position.shares * exit_price - commission
                        else:
                            gross_pnl = position.shares * (position.entry_price - exit_price)
                            capital += gross_pnl - commission

                        pnl = gross_pnl - commission
                        pnl_pct = gross_pnl / (position.shares * position.entry_price) if position.shares else 0.0

                        position.exit_time = date
                        position.exit_price = exit_price
                        position.pnl = pnl
                        position.pnl_pct = pnl_pct * 100
                        position.exit_reason = exit_reason
                        trades.append(position)
                        self.logger.debug(f"出场: {exit_reason}, 收益: {pnl_pct:.2%}")
                        position = None

            if position is None and pending_entry is None and pending_exit is None:
                if strategy.should_enter(row, context):
                    direction = 'long' if getattr(strategy, 'direction', 'below') == 'below' else 'short'
                    if execution_mode == "next_bar":
                        pending_entry = direction
                    else:
                        entry_price = current_price * (1 + self.slippage) if direction == 'long' else current_price * (1 - self.slippage)
                        position_value = capital * position_size
                        shares = int(position_value / entry_price)

                        if shares > 0:
                            commission = shares * entry_price * self.commission_rate
                            if direction == 'long':
                                total_cost = shares * entry_price + commission
                                if capital >= total_cost:
                                    capital -= total_cost
                                    position = Trade(
                                        entry_time=date,
                                        entry_price=entry_price,
                                        direction=direction,
                                        shares=shares,
                                    )
                                    self.logger.debug(f"入场: 价格 {entry_price:.2f}, 数量 {shares}")
                            else:
                                capital -= commission
                                position = Trade(
                                    entry_time=date,
                                    entry_price=entry_price,
                                    direction=direction,
                                    shares=shares,
                                )
                                self.logger.debug(f"入场: 价格 {entry_price:.2f}, 数量 {shares}")

            equity_curve.append(_mark_to_market_cash(current_price))

        # 计算结果指标
        result = self._calculate_metrics(trades, equity_curve)
        result.trades = trades
        result.equity_curve = equity_curve

        self.logger.info(f"回测完成: 总收益 {result.total_return:.2%}, 交易次数 {result.total_trades}")

        return result

    def _calculate_metrics(self, trades: List[Trade], equity_curve: List[float]) -> BacktestResult:
        """计算绩效指标"""
        result = BacktestResult()

        if not trades:
            if equity_curve:
                result.total_return = (equity_curve[-1] - self.initial_capital) / self.initial_capital
            return result

        # 基本统计
        result.total_trades = len(trades)
        result.winning_trades = sum(1 for t in trades if t.pnl > 0)
        result.losing_trades = result.total_trades - result.winning_trades
        result.win_rate = result.winning_trades / result.total_trades

        # 收益统计
        final_equity = equity_curve[-1] if equity_curve else self.initial_capital
        result.total_return = (final_equity - self.initial_capital) / self.initial_capital

        wins = [t.pnl_pct for t in trades if t.pnl > 0]
        losses = [abs(t.pnl_pct) for t in trades if t.pnl <= 0]

        result.avg_win = np.mean(wins) if wins else 0
        result.avg_loss = np.mean(losses) if losses else 0
        result.avg_trade_return = np.mean([t.pnl_pct for t in trades])

        # 盈亏比
        total_wins = sum(t.pnl for t in trades if t.pnl > 0)
        total_losses = abs(sum(t.pnl for t in trades if t.pnl <= 0))
        result.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

        # 最大回撤
        peak = equity_curve[0]
        max_dd = 0
        for value in equity_curve:
            if value > peak:
                peak = value
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
        result.max_drawdown = max_dd

        # 年化收益（假设数据是日频）
        days = len(equity_curve)
        if days > 0:
            result.annualized_return = (1 + result.total_return) ** (252 / days) - 1

        # 夏普比率（简化版，假设无风险利率0）
        returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
        if len(returns) > 1 and np.std(returns) > 0:
            result.sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252)

        return result

    def compare_strategies(self, df: pd.DataFrame, strategies: List[Strategy],
                          position_size: float = 0.2) -> pd.DataFrame:
        """对比多个策略"""
        results = []

        for strategy in strategies:
            result = self.run(df, strategy, position_size)
            result_dict = result.to_dict()
            result_dict['strategy'] = strategy.name
            results.append(result_dict)

        return pd.DataFrame(results)

    def generate_report(self, result: BacktestResult, output_file: str = None) -> str:
        """生成回测报告"""
        report = f"""
{'='*60}
📊 回测报告
{'='*60}

💰 收益指标
  总收益率:      {result.total_return:>10.2%}
  年化收益率:    {result.annualized_return:>10.2%}
  最大回撤:      {result.max_drawdown:>10.2%}
  夏普比率:      {result.sharpe_ratio:>10.2f}

📈 交易统计
  总交易次数:    {result.total_trades:>10}
  盈利次数:      {result.winning_trades:>10}
  亏损次数:      {result.losing_trades:>10}
  胜率:          {result.win_rate:>10.2%}
  盈亏比:        {result.profit_factor:>10.2f}

📊 平均表现
  平均交易收益:  {result.avg_trade_return:>10.2%}
  平均盈利:      {result.avg_win:>10.2%}
  平均亏损:      {result.avg_loss:>10.2%}

{'='*60}
"""

        # 最近10笔交易
        if result.trades:
            report += "\n📝 最近10笔交易:\n"
            for i, trade in enumerate(result.trades[-10:], 1):
                status = "✅" if trade.pnl > 0 else "❌"
                report += f"  {status} {trade.entry_time.strftime('%m-%d %H:%M')} "
                report += f"→ {trade.exit_time.strftime('%m-%d %H:%M') if trade.exit_time else '持仓中'} "
                report += f"收益: {trade.pnl_pct:+.2f}% ({trade.exit_reason})\n"

        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)

        return report
