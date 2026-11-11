#!/usr/bin/env python3
"""Legacy exploratory helpers, not a capital-constrained portfolio.

Shared signal and opening assumptions live in backtest.py. Parameter search
requires a separately approved protocol; the automatic sweep is blocked.
"""
import os
import pandas as pd
from qlib_io import load_qlib_features, read_qlib_day_bin
from backtest import (limit_pct, sig_ma_volume, sig_high_tight_flag,
                      trade_returns_from_frame, validate_backtest_parameters)

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    return read_qlib_day_bin(path)[1]


def load_df(sym):
    try:
        _, cols = load_qlib_features(
            QLIB, sym, ("open", "high", "low", "close", "volume", "amount"))
    except FileNotFoundError:
        return None
    return pd.DataFrame(cols)


def sig_limit_up(df, lim):
    """Signal-day close/high are available after that day's close."""
    prev_close = df["close"].shift(1)
    limit_price = (prev_close * (1 + lim)).round(2)
    return ((abs(df["close"] - limit_price) < 0.005)
            & (abs(df["high"] - df["close"]) < 0.005))


def backtest(sym, sig, hold_days, commission=0.00025, stamp_tax=0.0005):
    validate_backtest_parameters(hold_days, commission, stamp_tax)
    df = load_df(sym)
    if df is None or len(df) == 0:
        return []
    return trade_returns_from_frame(df, sig, sym, hold_days, commission, stamp_tax)


def sweep(name, sig_fn, insts, holds):
    raise RuntimeError("Parameter search blocked: approved research protocol required")


def main():
    raise SystemExit("BLOCKED: automatic parameter search is not approved; no data loaded")


if __name__ == "__main__":
    main()
