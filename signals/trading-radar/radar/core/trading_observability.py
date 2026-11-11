"""
战局统筹层 — 把 battlefield / MonitorEngine 运行时投影到 Prometheus。

阵地状态机语义（与 battlefield_state + RL + 冷却对齐）：
- empty_wait：标的池中持仓股数为 0
- has_position：持仓股数 > 0（视作「已建底仓 / 持仓运作」）
- rl_takeprofit_cooldown：Telegram 合并告警使用的 RL 通道正处于冷却（AlertCooldownManager key ``rl``；
  RL 止盈/减仓建议等多经该路径，可作「止盈冷却」近似）

盈亏：进程内追踪权益峰值以计算回撤比例；按 config.targets[].strategy_tag 汇总浮动盈亏。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from radar.core.monitor_engine import MonitorEngine

logger = logging.getLogger(__name__)

_peak_equity_cny: float = 0.0
_daily_cn_date: Optional[str] = None
_daily_baseline_equity_cny: float = 0.0


def reset_daily_pnl_state_for_tests() -> None:
    """测试用：重置北京时间当日盈亏基准。"""
    global _daily_cn_date, _daily_baseline_equity_cny
    _daily_cn_date = None
    _daily_baseline_equity_cny = 0.0


def reset_peak_equity_for_tests(value: float = 0.0) -> None:
    """测试用：重置峰值权益。"""
    global _peak_equity_cny
    _peak_equity_cny = float(value)


def _beijing_calendar_date_str() -> str:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


def _daily_pnl_vs_first_sample(eq_cny: float) -> float:
    """相对于北京时间当日进程首次采样权益的盈亏。"""
    global _daily_cn_date, _daily_baseline_equity_cny
    today = _beijing_calendar_date_str()
    if _daily_cn_date != today:
        _daily_cn_date = today
        _daily_baseline_equity_cny = float(eq_cny)
    return float(eq_cny) - _daily_baseline_equity_cny


def refresh_trading_observability(engine: MonitorEngine) -> None:
    """在每次扫描或行情事件处理后调用，刷新 /metrics 中的战局指标。"""
    global _peak_equity_cny

    try:
        from radar.core import prometheus_extra as pe

        if not getattr(pe, "_HAVE", False):
            return
    except (ImportError, AttributeError):
        # Prometheus未安装，安全退出  # 安全忽略
        return

    try:
        from radar.core.prometheus_extra import (
            observe_battlefield_lane_counts,
            observe_daily_pnl_cny,
            observe_portfolio_performance,
            observe_strategy_tag_open_unrealized_pnl,
        )
    except Exception as e:
        logger.debug("prometheus_extra observe 不可用: %s", e)
        return

    cfg = engine.config
    cd_min = int(cfg.cooldown_minutes)

    empty_wait = 0
    has_position = 0
    rl_tp_cd = 0

    portfolio = engine._portfolio
    positions = portfolio.positions

    for target in engine.targets:
        code = target.code
        pos = positions.get(code, {})
        shares = int(pos.get("shares", 0) or 0)

        if shares <= 0:
            empty_wait += 1
        else:
            has_position += 1

        # RL 合并告警冷却 — cooldown key 与 AlertType.RL.value 一致为 ``rl``
        try:
            if engine.cooldown.is_in_cooldown(code, "rl", cd_min):
                rl_tp_cd += 1
        except (ImportError, AttributeError):
            # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
            pass

    observe_battlefield_lane_counts(empty_wait, has_position, rl_tp_cd)

    eq = float(portfolio.get_portfolio_value())
    initial = float(portfolio.initial_cash)
    if _peak_equity_cny <= 0:
        _peak_equity_cny = max(initial, eq)
    else:
        _peak_equity_cny = max(_peak_equity_cny, eq)

    net = eq - initial
    dd = (_peak_equity_cny - eq) / _peak_equity_cny if _peak_equity_cny > 1e-9 else 0.0
    observe_portfolio_performance(eq, net, max(0.0, dd))
    observe_daily_pnl_cny(_daily_pnl_vs_first_sample(eq))

    tag_unreal: Dict[str, float] = defaultdict(float)
    for target in engine.targets:
        raw = getattr(target, "strategy_tag", "") or ""
        tag = raw.strip() or "untagged"
        pos = positions.get(target.code)
        if not pos:
            continue
        shares = int(pos.get("shares", 0) or 0)
        if shares <= 0:
            continue
        cost = float(pos.get("cost", 0.0) or 0.0)
        last_price = float(pos.get("last_price", cost) or cost)
        tag_unreal[tag] += shares * (last_price - cost)

    observe_strategy_tag_open_unrealized_pnl(dict(tag_unreal))

    try:
        from radar.core.alpha_benchmark import refresh_alpha_and_allocation_metrics

        refresh_alpha_and_allocation_metrics(engine)
    except Exception as e:
        logger.debug("[TradingObs] Alpha/L1 指标失败: %s", e)


def observe_fill_slippage(
    strategy_tag: str,
    expected_price: float,
    fill_price: float,
    side_buy: bool,
) -> None:
    """
    辅助函数：给定预期价与成交价，计算基点滑点并写入 Histogram。
    side_buy=True：做多开仓/加仓；(fill−expected)/expected*10000 — 正值表示买贵了。
    side_buy=False：卖出；(expected−fill)/expected*10000 — 正值表示卖便宜了（不利）。
    """
    try:
        from radar.core import prometheus_extra as pe

        if not getattr(pe, "_HAVE", False):
            return
    except (ImportError, AttributeError):
        # Prometheus未安装，安全退出  # 安全忽略
        return

    ep = float(expected_price)
    fp = float(fill_price)
    if ep <= 0:
        return

    if side_buy:
        bps = (fp - ep) / ep * 10000.0
    else:
        bps = (ep - fp) / ep * 10000.0

    try:
        from radar.core.prometheus_extra import observe_order_slippage_bps

        observe_order_slippage_bps(strategy_tag, bps)
    except (ImportError, AttributeError):
        # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
        pass
