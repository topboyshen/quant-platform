#!/usr/bin/env python3
"""五规则的逐笔日线价格情景，不是已验证成交或资金组合回测。

信号在 D 收盘后确认，情景入场价为 D+1 开盘；开盘准入只使用前收盘
与该开盘价，不读取入场日最终 high/close/volume。至少再经过一个观测
交易日后，按指定日收盘价计算条件收益。涨跌幅与费用沿用旧假设，尚未
按历史日期、ST/IPO、复权或真实成交规则核实；开盘低于估算涨停价和有
退出收盘价均不保证可成交。停牌、流动性、滑点和退出跌停尚未建模。

各信号可能重叠且共享资金，未指定资金、仓位、复投或持仓冲突规则，
因此不计算组合/累计收益或独立同分布置信区间，也不构成策略有效性证据。
"""
import json
from numbers import Integral, Real
import os
import numpy as np
import pandas as pd

from qlib_io import load_qlib_features, read_qlib_day_bin
from signal_rules import (sig_turtle, sig_ma_volume, sig_high_tight_flag,
                          sig_limit_up_shakeout, sig_uptrend_limit_down)

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    return read_qlib_day_bin(path)[1]


def load_df(sym):
    try:
        _, cols = load_qlib_features(
            QLIB, sym, ("open", "high", "low", "close", "volume", "amount")
        )
    except FileNotFoundError:
        return None
    return pd.DataFrame(cols)


def limit_pct(sym):
    """Legacy board-based proxy only; not a dated exchange-rule authority."""
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


def validate_backtest_parameters(hold_days, commission, stamp_tax):
    """Reject malformed inputs; do not introduce a position/risk policy."""
    if isinstance(hold_days, (bool, np.bool_)) or not isinstance(hold_days, Integral) or hold_days < 1:
        raise ValueError("hold_days must be a positive nonboolean integer")
    for name, value in (("commission", commission), ("stamp_tax", stamp_tax)):
        if (isinstance(value, (bool, np.bool_)) or not isinstance(value, Real)
                or not np.isfinite(value) or value < 0):
            raise ValueError(f"{name} must be finite and nonnegative")
    if not np.isfinite(float(commission) * 2 + float(stamp_tax)):
        raise ValueError("aggregate assumed cost must be finite")


def opening_entry_allowed(previous_close, opening_price, assumed_limit_pct):
    """Conservative price-scenario filter, NOT evidence of an executable fill.

Only these three arguments are available to this decision. The inherited
rounding/board proxy is not a historical rule engine. Daily bars alone cannot
establish auction liquidity or that this opening price was attainable.
"""
    values = (previous_close, opening_price, assumed_limit_pct)
    if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, Real)
           or not np.isfinite(value) or value <= 0 for value in values):
        return False
    estimated_limit = round(float(previous_close) * (1 + float(assumed_limit_pct)), 2)
    return np.isfinite(estimated_limit) and opening_price < estimated_limit - 0.005


def _aligned_signal(frame, signal):
    if isinstance(signal, pd.Series):
        if not signal.index.equals(frame.index):
            raise ValueError("signal index must exactly match frame index")
        values = signal.to_numpy()
    else:
        # Preserve bools beside NaN/None; numpy's default float coercion would
        # turn valid False values into numeric 0.0 and lose the input contract.
        values = np.asarray(signal, dtype=object)
    if values.ndim != 1 or len(values) != len(frame):
        raise ValueError("one signal is required per frame row")
    # Missing signal means no decision; numeric/string truthiness is not a signal.
    result = []
    for value in values:
        if isinstance(value, (bool, np.bool_)):
            result.append(bool(value))
        elif value is None or value is pd.NA or (isinstance(value, Real) and np.isnan(value)):
            result.append(False)
        else:
            raise ValueError("signals must be booleans or explicitly missing")
    return result


def trade_returns_from_frame(df, signal, sym, hold_days=5, commission=0.00025, stamp_tax=0.0005):
    """Pure in-memory conditional per-entry net-price observations.

Rows must already be chronological, aligned observations for one instrument.
This helper neither loads data nor verifies its calendar/adjustment provenance.
Eligible opening scenarios with no priced exit are retained as NaN, never
filled with zero; describe_trade_returns reports their excluded count. A final
row signal has no subsequent open and is therefore not an entry scenario.
"""
    validate_backtest_parameters(hold_days, commission, stamp_tax)
    # Keep numpy integer arithmetic from overflowing a future row index.
    hold_days = int(hold_days)
    cost = float(commission) * 2 + float(stamp_tax)
    sig = _aligned_signal(df, signal)
    close = pd.to_numeric(df["close"], errors="raise").to_numpy(dtype=float)
    open_ = pd.to_numeric(df["open"], errors="raise").to_numpy(dtype=float)
    n = len(df)
    lim = limit_pct(sym)
    rets = []
    for i in range(n - 1):
        if not sig[i]:
            continue
        buy_day = i + 1
        buy_price = open_[buy_day]
        if not opening_entry_allowed(close[i], buy_price, lim):
            continue
        sell_day = buy_day + hold_days
        if sell_day >= n:
            rets.append(float("nan"))
            continue
        sell_price = close[sell_day]
        if sell_price <= 0 or not np.isfinite(sell_price):
            rets.append(float("nan"))
            continue
        ret = (sell_price - buy_price) / buy_price
        value = ret - cost
        rets.append(float(value) if np.isfinite(value) else float("nan"))
    return rets


def backtest(sym, sig_fn, hold_days=5, commission=0.00025, stamp_tax=0.0005):
    """Legacy loader wrapper; each canonical signal enforces its own warmup."""
    validate_backtest_parameters(hold_days, commission, stamp_tax)
    df = load_df(sym)
    if df is None or len(df) == 0:
        return []
    return trade_returns_from_frame(df, sig_fn(df), sym, hold_days, commission, stamp_tax)


def describe_trade_returns(values):
    """Descriptive finite subset only; not funded or independent observations."""
    observations = np.asarray(values, dtype=float)
    if observations.ndim != 1:
        raise ValueError("trade observations must be one dimensional")
    priced = observations[np.isfinite(observations)]

    def finite_or_none(value):
        return float(value) if np.isfinite(value) else None

    return {
        "scope": "conditional_single_trade_price_scenarios",
        "scenario_count": int(len(observations)),
        "priced_count": int(len(priced)),
        "excluded_unpriced_count": int(len(observations) - len(priced)),
        "mean_trade_return": finite_or_none(priced.mean()) if len(priced) else None,
        "median_trade_return": finite_or_none(np.median(priced)) if len(priced) else None,
        "win_fraction_priced": float((priced > 0).mean()) if len(priced) else None,
        "min_trade_return": float(priced.min()) if len(priced) else None,
        "max_trade_return": float(priced.max()) if len(priced) else None,
        "portfolio_return": None,
        "portfolio_status": "not_modeled",
        "confidence_interval": None,
        "confidence_interval_status": "not_estimated_non_independent_observations",
        "limitations": ["overlapping_signals_not_capital_allocated", "fills_not_verified",
                        "historical_limit_and_fee_rules_not_validated",
                        "liquidity_slippage_and_exit_constraints_not_modeled",
                        "priced_subset_may_have_selection_bias"],
    }


def main():
    insts = [l.split()[0].lower() for l in open(os.path.join(QLIB, "instruments/all.txt")) if len(l.split()) >= 3]
    strategies = {"海龟突破": sig_turtle, "均线放量": sig_ma_volume, "高窄旗形": sig_high_tight_flag,
                  "涨停洗盘": sig_limit_up_shakeout, "上升跌停": sig_uptrend_limit_down}
    for name, fn in strategies.items():
        all_rets = []
        for sym in insts:
            all_rets.extend(backtest(sym, fn))
        report = {"strategy": name, **describe_trade_returns(all_rets)}
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
