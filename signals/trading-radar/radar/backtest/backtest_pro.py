#!/usr/bin/env python3
"""
专业回测引擎 (Pro版)
支持：可视化、多策略对比、参数优化、详细绩效指标
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from radar.trading_assumptions import TradingCostModel

logger = logging.getLogger(__name__)


class OrderType(Enum):
    """订单类型"""
    MARKET = "市价单"
    LIMIT = "限价单"
    STOP = "止损单"


class PositionType(Enum):
    """持仓类型"""
    LONG = "多仓"
    SHORT = "空仓"


@dataclass
class Trade:
    """完整交易记录"""
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    direction: str = 'long'
    shares: int = 0
    pnl: float = 0
    pnl_pct: float = 0
    exit_reason: str = ''
    holding_days: int = 0
    max_profit_pct: float = 0  # 最大浮盈
    max_loss_pct: float = 0    # 最大浮亏
    commission: float = 0
    slippage: float = 0

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl <= 0


@dataclass
class BacktestResult:
    """完整回测结果"""
    # 基本收益指标
    total_return: float = 0
    annualized_return: float = 0
    max_drawdown: float = 0
    max_drawdown_duration: int = 0

    # 风险调整收益
    sharpe_ratio: float = 0
    sortino_ratio: float = 0
    calmar_ratio: float = 0

    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0
    profit_factor: float = 0
    expectancy: float = 0  # 期望收益

    # 收益分布
    avg_trade_return: float = 0
    avg_win: float = 0
    avg_loss: float = 0
    largest_win: float = 0
    largest_loss: float = 0

    # 持仓统计
    avg_holding_days: float = 0
    max_holding_days: int = 0
    min_holding_days: int = 0

    # 连续统计
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # 数据
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    drawdown_curve: pd.Series = field(default_factory=pd.Series)
    daily_returns: pd.Series = field(default_factory=pd.Series)

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'total_return': f"{self.total_return:.2%}",
            'annualized_return': f"{self.annualized_return:.2%}",
            'max_drawdown': f"{self.max_drawdown:.2%}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'sortino_ratio': f"{self.sortino_ratio:.2f}",
            'calmar_ratio': f"{self.calmar_ratio:.2f}",
            'win_rate': f"{self.win_rate:.2%}",
            'profit_factor': f"{self.profit_factor:.2f}",
            'expectancy': f"{self.expectancy:.2f}",
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'avg_holding_days': f"{self.avg_holding_days:.1f}",
            'avg_trade_return': f"{self.avg_trade_return:.2%}",
        }


class Strategy:
    """策略基类"""

    def __init__(self, name: str = "BaseStrategy"):
        self.name = name

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        raise NotImplementedError

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float) -> Tuple[bool, str]:
        raise NotImplementedError

    def calculate_position_size(self, capital: float, price: float) -> int:
        """计算仓位大小，默认20%仓位"""
        position_value = capital * 0.2
        return int(position_value / price)


class PriceTriggerStrategy(Strategy):
    """价格触发策略"""

    def __init__(self, trigger_price: float, direction: str = 'below',
                 stop_loss_pct: float = 0.05, take_profit_pct: float = 0.10,
                 position_pct: float = 0.2):
        super().__init__(f"PriceTrigger_{direction}_{trigger_price}")
        self.trigger_price = trigger_price
        self.direction = direction
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.position_pct = position_pct

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        current_price = data['price']
        if self.direction == 'below':
            return current_price <= self.trigger_price
        else:
            return current_price >= self.trigger_price

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float) -> Tuple[bool, str]:
        current_price = data['price']

        if self.direction == 'below':
            if current_price >= entry_price * (1 + self.take_profit_pct):
                return True, '止盈'
            if current_price <= entry_price * (1 - self.stop_loss_pct):
                return True, '止损'
        else:
            if current_price <= entry_price * (1 - self.take_profit_pct):
                return True, '止盈'
            if current_price >= entry_price * (1 + self.stop_loss_pct):
                return True, '止损'

        return False, ''

    def calculate_position_size(self, capital: float, price: float) -> int:
        position_value = capital * self.position_pct
        return int(position_value / price)


class MovingAverageStrategy(Strategy):
    """均线策略"""

    def __init__(self, fast_period: int = 5, slow_period: int = 20,
                 stop_loss_pct: float = 0.05):
        super().__init__(f"MA_Cross_{fast_period}_{slow_period}")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.stop_loss_pct = stop_loss_pct
        self.prices = []

    def should_enter(self, data: pd.Series, context: Dict) -> bool:
        self.prices.append(data['price'])
        if len(self.prices) < self.slow_period:
            return False

        fast_ma = np.mean(self.prices[-self.fast_period:])
        slow_ma = np.mean(self.prices[-self.slow_period:])
        prev_fast = np.mean(self.prices[-self.fast_period-1:-1])
        prev_slow = np.mean(self.prices[-self.slow_period-1:-1])

        # 金叉
        return prev_fast <= prev_slow and fast_ma > slow_ma

    def should_exit(self, data: pd.Series, context: Dict, entry_price: float) -> Tuple[bool, str]:
        current_price = data['price']

        if len(self.prices) >= self.slow_period:
            fast_ma = np.mean(self.prices[-self.fast_period:])
            slow_ma = np.mean(self.prices[-self.slow_period:])

            # 死叉或止损
            if fast_ma < slow_ma:
                return True, '死叉出场'

        if current_price <= entry_price * (1 - self.stop_loss_pct):
            return True, '止损'

        return False, ''


class BacktestEngine:
    """专业回测引擎"""

    def __init__(self,
                 initial_capital: float = 100000,
                 commission_rate: float = 0.0003,  # 万3佣金
                 stamp_duty: float = 0.001,        # 千1印花税（卖出时）
                 transfer_fee: float = 0.00002,    # 过户费
                 slippage: float = 0.001,          # 滑点
                 risk_free_rate: float = 0.03,     # 无风险利率3%
                 logger: Optional[logging.Logger] = None):

        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_duty = stamp_duty
        self.transfer_fee = transfer_fee
        self.cost_model = TradingCostModel(
            commission_rate=commission_rate,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
        )
        self.slippage = slippage
        self.risk_free_rate = risk_free_rate
        self.logger = logger or logging.getLogger(__name__)

    def calculate_cost(self, price: float, shares: int, is_buy: bool) -> float:
        """计算交易成本"""
        return self.cost_model.calculate(price, shares, is_buy)

    def _update_position_pnl(self, position: Trade, current_price: float) -> None:
        """更新持仓浮盈浮亏"""
        unrealized_pct = (current_price - position.entry_price) / position.entry_price * 100
        position.max_profit_pct = max(position.max_profit_pct, unrealized_pct)
        position.max_loss_pct = min(position.max_loss_pct, unrealized_pct)

    def _close_position(self, position: Trade, current_price: float, exit_reason: str,
                        date: datetime, capital: float, trades: List[Trade]) -> Tuple[float, Optional[Trade]]:
        """平仓并返回更新后的资金"""
        exit_price = current_price * (1 - self.slippage)
        gross_pnl = position.shares * (exit_price - position.entry_price)
        pnl_pct = gross_pnl / (position.shares * position.entry_price) * 100 if position.shares else 0.0
        cost = self.calculate_cost(exit_price, position.shares, is_buy=False)
        pnl = gross_pnl - cost
        proceeds = position.shares * exit_price - cost
        capital += proceeds

        position.exit_time = date
        position.exit_price = exit_price
        position.pnl = pnl
        position.pnl_pct = pnl_pct
        position.exit_reason = exit_reason
        position.holding_days = (date - position.entry_time).days
        position.commission += cost

        trades.append(position)
        return capital, None

    def _open_position(self, row: pd.Series, strategy: Strategy, date: datetime,
                       capital: float) -> Tuple[float, Optional[Trade]]:
        """开仓并返回更新后的资金和持仓"""
        entry_price = row['price'] * (1 + self.slippage)
        shares = strategy.calculate_position_size(capital, entry_price)
        if shares <= 0:
            return capital, None

        cost = self.calculate_cost(entry_price, shares, is_buy=True)
        total_cost = shares * entry_price + cost
        if capital < total_cost:
            return capital, None

        position = Trade(
            entry_time=date,
            entry_price=entry_price,
            direction='long',
            shares=shares,
            commission=cost
        )
        return capital - total_cost, position

    def run(self, df: pd.DataFrame, strategy: Strategy, execution_mode: str = "next_bar") -> BacktestResult:
        """运行回测。

        execution_mode 默认 next_bar：t 产生信号，t+1 成交；legacy_same_bar 保留旧同 bar 成交行为。
        """
        self.logger.info(f"开始回测: {strategy.name}")

        if execution_mode not in {"next_bar", "legacy_same_bar"}:
            raise ValueError("execution_mode must be 'next_bar' or 'legacy_same_bar'")

        capital = self.initial_capital
        position: Optional[Trade] = None
        trades: List[Trade] = []
        daily_data = []
        pending_entry = False
        pending_exit: Optional[str] = None

        for i, (idx, row) in enumerate(df.iterrows()):
            date = idx if isinstance(idx, datetime) else pd.to_datetime(idx)
            current_price = row['price']

            if execution_mode == "next_bar":
                if pending_exit is not None and position is not None:
                    capital, position = self._close_position(
                        position, current_price, pending_exit, date, capital, trades
                    )
                    pending_exit = None

                if pending_entry and position is None:
                    capital, position = self._open_position(row, strategy, date, capital)
                    pending_entry = False

            context = {
                'index': i,
                'total': len(df),
                'date': date,
                'cash': capital,
                'position_shares': position.shares if position else 0,
                'position_cost': (position.shares * position.entry_price) if position else 0.0,
                'current_price': current_price,
                'execution_mode': execution_mode,
                'df': df.iloc[:i+1]
            }

            # 处理出场
            if position is not None and pending_exit is None:
                self._update_position_pnl(position, current_price)
                should_exit, exit_reason = strategy.should_exit(row, context, position.entry_price)
                if should_exit:
                    if execution_mode == "next_bar":
                        pending_exit = exit_reason
                    else:
                        capital, position = self._close_position(
                            position, current_price, exit_reason, date, capital, trades
                        )

            # 处理入场
            if position is None and not pending_entry and pending_exit is None and strategy.should_enter(row, context):
                if execution_mode == "next_bar":
                    pending_entry = True
                else:
                    capital, position = self._open_position(row, strategy, date, capital)

            # 记录每日状态
            position_value = position.shares * current_price if position else 0
            daily_data.append({
                'date': date,
                'total_value': capital + position_value,
                'cash': capital,
                'position_value': position_value,
                'price': current_price,
                'execution_mode': execution_mode,
            })

        # 计算绩效指标
        result = self._calculate_metrics(trades, daily_data)
        result.trades = trades

        self.logger.info(f"回测完成: 总收益 {result.total_return:.2%}, 交易次数 {result.total_trades}")

        return result

    def _calc_trade_stats(self, trades: List[Trade]) -> Dict:
        """计算交易统计指标"""
        total = len(trades)
        returns = [t.pnl_pct for t in trades]
        wins = [t.pnl_pct for t in trades if t.is_win]
        losses = [t.pnl_pct for t in trades if t.is_loss]
        total_wins_pnl = sum(t.pnl for t in trades if t.is_win)
        total_losses_pnl = abs(sum(t.pnl for t in trades if t.is_loss))

        return {
            'total_trades': total,
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': len(wins) / total if total > 0 else 0,
            'avg_trade_return': np.mean(returns) if returns else 0,
            'avg_win': np.mean(wins) if wins else 0,
            'avg_loss': np.mean(losses) if losses else 0,
            'largest_win': max(wins) if wins else 0,
            'largest_loss': min(losses) if losses else 0,
            'profit_factor': total_wins_pnl / total_losses_pnl if total_losses_pnl > 0 else float('inf'),
        }

    def _calc_drawdown_metrics(self, equity: pd.Series) -> Dict:
        """计算回撤指标"""
        peak = equity.expanding().max()
        drawdown = (equity - peak) / peak

        in_drawdown = drawdown < 0
        durations = []
        current = 0
        for is_dd in in_drawdown:
            if is_dd:
                current += 1
            else:
                if current > 0:
                    durations.append(current)
                current = 0

        return {
            'max_drawdown': drawdown.min(),
            'drawdown_curve': drawdown,
            'max_drawdown_duration': max(durations) if durations else 0,
        }

    def _calc_risk_ratios(self, daily_returns: pd.Series, annualized_return: float) -> Dict:
        """计算风险比率（夏普、Sortino、Calmar）"""
        result = {}

        if len(daily_returns) > 1:
            excess = daily_returns - self.risk_free_rate / 252
            std = daily_returns.std()
            result['sharpe_ratio'] = excess.mean() / std * np.sqrt(252) if std > 0 else 0

            downside = daily_returns[daily_returns < 0]
            downside_std = downside.std() if len(downside) > 0 else 0
            result['sortino_ratio'] = (daily_returns.mean() * 252 - self.risk_free_rate) / (downside_std * np.sqrt(252)) if downside_std > 0 else 0

        return result

    def _calc_consecutive_stats(self, trades: List[Trade]) -> Dict:
        """计算连续盈亏统计"""
        consecutive_wins = 0
        consecutive_losses = 0
        max_wins = 0
        max_losses = 0

        for trade in trades:
            if trade.is_win:
                consecutive_wins += 1
                consecutive_losses = 0
                max_wins = max(max_wins, consecutive_wins)
            else:
                consecutive_losses += 1
                consecutive_wins = 0
                max_losses = max(max_losses, consecutive_losses)

        return {
            'max_consecutive_wins': max_wins,
            'max_consecutive_losses': max_losses,
        }

    def _calculate_metrics(self, trades: List[Trade], daily_data: List[Dict]) -> BacktestResult:
        """计算详细绩效指标"""
        result = BacktestResult()

        if not trades or not daily_data:
            if daily_data:
                equity_df = pd.DataFrame(daily_data)
                equity_df.set_index('date', inplace=True)
                result.equity_curve = equity_df
                final_value = equity_df['total_value'].iloc[-1]
                result.total_return = (final_value - self.initial_capital) / self.initial_capital
            return result

        # 转换为DataFrame
        equity_df = pd.DataFrame(daily_data)
        equity_df.set_index('date', inplace=True)
        result.equity_curve = equity_df
        result.daily_returns = equity_df['total_value'].pct_change().dropna()

        # 交易统计
        trade_stats = self._calc_trade_stats(trades)
        for k, v in trade_stats.items():
            setattr(result, k, v)

        # 总收益
        final_value = equity_df['total_value'].iloc[-1]
        result.total_return = (final_value - self.initial_capital) / self.initial_capital

        # 年化收益
        days = (equity_df.index[-1] - equity_df.index[0]).days
        if days > 0:
            result.annualized_return = (1 + result.total_return) ** (365 / days) - 1

        # 期望收益
        result.expectancy = result.avg_win * result.win_rate + result.avg_loss * (1 - result.win_rate)

        # 持仓统计
        holding_days = [t.holding_days for t in trades]
        result.avg_holding_days = np.mean(holding_days) if holding_days else 0
        result.max_holding_days = max(holding_days) if holding_days else 0
        result.min_holding_days = min(holding_days) if holding_days else 0

        # 回撤指标
        dd_metrics = self._calc_drawdown_metrics(equity_df['total_value'])
        for k, v in dd_metrics.items():
            setattr(result, k, v)

        # 风险比率
        risk_ratios = self._calc_risk_ratios(result.daily_returns, result.annualized_return)
        for k, v in risk_ratios.items():
            setattr(result, k, v)

        # Calmar比率
        if result.max_drawdown != 0:
            result.calmar_ratio = result.annualized_return / abs(result.max_drawdown)

        # 连续统计
        consecutive = self._calc_consecutive_stats(trades)
        for k, v in consecutive.items():
            setattr(result, k, v)

        return result

    def plot_results(self, result: BacktestResult, save_path: str = None):
        """绘制回测图表"""
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'SimHei', 'Arial Unicode MS']
        plt.rcParams['axes.unicode_minus'] = False

        fig, axes = plt.subplots(3, 1, figsize=(14, 10))

        equity = result.equity_curve

        # 图1: 资金曲线
        ax1 = axes[0]
        ax1.plot(equity.index, equity['total_value'], label='Total Value', linewidth=2, color='blue')
        ax1.plot(equity.index, equity['cash'], label='Cash', linewidth=1, color='gray', alpha=0.7)
        ax1.set_title('Backtest Result - Equity Curve', fontsize=14, fontweight='bold')
        ax1.set_ylabel('Value (CNY)')
        ax1.legend(loc='upper left')
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)

        # 图2: 回撤曲线
        ax2 = axes[1]
        drawdown_pct = result.drawdown_curve * 100
        ax2.fill_between(equity.index, drawdown_pct, 0, color='red', alpha=0.3)
        ax2.plot(equity.index, drawdown_pct, color='red', linewidth=1)
        ax2.set_title('Drawdown Curve', fontsize=14, fontweight='bold')
        ax2.set_ylabel('Drawdown (%)')
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45)

        # 图3: 每日收益分布
        ax3 = axes[2]
        daily_returns_pct = result.daily_returns * 100
        ax3.hist(daily_returns_pct, bins=50, color='steelblue', alpha=0.7, edgecolor='black')
        ax3.axvline(daily_returns_pct.mean(), color='red', linestyle='--',
                   label=f'Mean: {daily_returns_pct.mean():.2f}%')
        ax3.set_title('Daily Returns Distribution', fontsize=14, fontweight='bold')
        ax3.set_xlabel('Daily Return (%)')
        ax3.set_ylabel('Frequency')
        ax3.legend()
        ax3.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            self.logger.info(f"图表已保存: {save_path}")

        return fig

    def _build_header_section(self, result: BacktestResult) -> str:
        """构建报告头部的绩效指标段。"""
        return f"""
{'='*70}
📊 专业回测报告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{'='*70}

💰 收益指标
  总收益率:           {result.total_return:>10.2%}
  年化收益率:         {result.annualized_return:>10.2%}
  最大回撤:           {result.max_drawdown:>10.2%}
  最大回撤天数:       {result.max_drawdown_duration:>10}天

📈 风险调整收益
  夏普比率:           {result.sharpe_ratio:>10.2f}
  Sortino比率:        {result.sortino_ratio:>10.2f}
  Calmar比率:         {result.calmar_ratio:>10.2f}

📊 交易统计
  总交易次数:         {result.total_trades:>10}
  盈利次数:           {result.winning_trades:>10}
  亏损次数:           {result.losing_trades:>10}
  胜率:               {result.win_rate:>10.2%}
  盈亏比:             {result.profit_factor:>10.2f}
  期望收益:           {result.expectancy:>10.2f}%

📉 收益分布
  平均交易收益:       {result.avg_trade_return:>10.2%}
  平均盈利:           {result.avg_win:>10.2%}
  平均亏损:           {result.avg_loss:>10.2%}
  最大单笔盈利:       {result.largest_win:>10.2%}
  最大单笔亏损:       {result.largest_loss:>10.2%}

⏱️ 持仓统计
  平均持仓天数:       {result.avg_holding_days:>10.1f}天
  最长持仓:           {result.max_holding_days:>10}天
  最短持仓:           {result.min_holding_days:>10}天

🔥 连续统计
  最大连续盈利:       {result.max_consecutive_wins:>10}次
  最大连续亏损:       {result.max_consecutive_losses:>10}次

{'='*70}
"""

    def _build_trade_list(self, trades: list) -> str:
        """构建最近交易列表段。"""
        if not trades:
            return ""
        lines = ["\n📝 最近10笔交易:"]
        lines.append(f"{'日期':<12} {'方向':<6} {'收益':<10} {'原因':<10} {'持仓天数':<8}")
        lines.append("-" * 60)
        for trade in trades[-10:]:
            date_str = trade.exit_time.strftime('%m-%d') if trade.exit_time else '持仓中'
            emoji = "✅" if trade.is_win else "❌"
            lines.append(
                f"{emoji} {date_str:<10} {trade.direction:<6} "
                f"{trade.pnl_pct:+.2f}% {'':<5} {trade.exit_reason[:8]:<10} "
                f"{trade.holding_days}天"
            )
        return "\n".join(lines)

    def generate_report(self, result: BacktestResult, output_file: str = None) -> str:
        """生成详细回测报告。"""
        report = self._build_header_section(result)
        report += self._build_trade_list(result.trades)

        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)

        return report

    def export_trades(self, result: BacktestResult, csv_path: str):
        """导出交易记录到CSV"""
        if not result.trades:
            return

        trades_data = []
        for i, trade in enumerate(result.trades, 1):
            trades_data.append({
                '序号': i,
                '入场日期': trade.entry_time.strftime('%Y-%m-%d'),
                '入场价格': round(trade.entry_price, 2),
                '出场日期': trade.exit_time.strftime('%Y-%m-%d') if trade.exit_time else '',
                '出场价格': round(trade.exit_price, 2) if trade.exit_price else '',
                '方向': trade.direction,
                '数量': trade.shares,
                '盈亏金额': round(trade.pnl, 2),
                '盈亏比例': f"{trade.pnl_pct:.2f}%",
                '持仓天数': trade.holding_days,
                '最大浮盈': f"{trade.max_profit_pct:.2f}%",
                '最大浮亏': f"{trade.max_loss_pct:.2f}%",
                '交易成本': round(trade.commission, 2),
                '出场原因': trade.exit_reason
            })

        df = pd.DataFrame(trades_data)
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        self.logger.info(f"交易记录已导出: {csv_path}")


# 参数优化工具
class StrategyOptimizer:
    """策略参数优化器"""

    def __init__(self, engine: BacktestEngine):
        self.engine = engine

    def grid_search(self, df: pd.DataFrame, strategy_class: type,
                    param_grid: Dict[str, List],
                    metric: str = 'sharpe_ratio') -> Tuple[Dict, BacktestResult]:
        """网格搜索最佳参数"""

        from itertools import product

        # 生成参数组合
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(product(*values))

        best_metric = -np.inf
        best_params = None
        best_result = None

        results = []

        for combo in combinations:
            params = dict(zip(keys, combo))
            strategy = strategy_class(**params)

            result = self.engine.run(df, strategy)

            current_metric = getattr(result, metric, 0)

            results.append({
                'params': params,
                'metric': current_metric,
                'total_return': result.total_return,
                'max_drawdown': result.max_drawdown,
                'sharpe_ratio': result.sharpe_ratio
            })

            if current_metric > best_metric:
                best_metric = current_metric
                best_params = params
                best_result = result

        logger.info("%s", "\n" + "=" * 60)
        logger.info("网格搜索结果 (优化指标: %s)", metric)
        logger.info("%s", "=" * 60)
        logger.info("最佳参数:")
        for k, v in best_params.items():
            logger.info("  %s: %s", k, v)
        logger.info("最佳%s: %.4f", metric, best_metric)

        return best_params, best_result
