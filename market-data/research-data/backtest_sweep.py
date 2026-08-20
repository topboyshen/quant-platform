#!/usr/bin/env python3
"""回测参数扫描: 涨停信号 + 持有天数扫描(1/3/5/10/20日)。

涨停信号 = screen.py 的核心(涨停打板), 资金流/PE 无法历史回测(仅当日有)。
"""
import os, struct
import numpy as np
import pandas as pd

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    with open(path, "rb") as f:
        f.read(16)
        return np.frombuffer(f.read(), dtype="<f4")


def load_df(sym):
    cols = {}
    for fld in ["open", "high", "low", "close", "volume", "amount"]:
        try:
            cols[fld] = read_bin(os.path.join(QLIB, "features", sym, fld + ".day.bin"))
        except FileNotFoundError:
            return None
    n = len(cols["close"])
    return pd.DataFrame({k: v[:n] for k, v in cols.items()})


def limit_pct(sym):
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


def sig_limit_up(df, lim):
    prev_close = df["close"].shift(1)
    limit_price = (prev_close * (1 + lim)).round(2)
    return (abs(df["close"] - limit_price) < 0.005) & (abs(df["high"] - df["close"]) < 0.005)


def sig_ma_volume(df):
    ma5 = df["close"].rolling(5).mean()
    ma20 = df["close"].rolling(20).mean()
    vol_ma20 = df["volume"].rolling(20).mean()
    return (ma5.shift(1) < ma20.shift(1)) & (ma5 > ma20) & (df["volume"] > vol_ma20 * 1.5)


def sig_high_tight_flag(df):
    high40 = df["high"].rolling(40).max()
    low40 = df["low"].rolling(40).min()
    high10 = df["high"].rolling(10).max()
    low10 = df["low"].rolling(10).min()
    vol_ma20 = df["volume"].rolling(20).mean()
    return (high40 / low40 > 1.6) & (high10 / low10 < 1.15) & (low10 >= high40 * 0.8) & (df["volume"] < vol_ma20 * 0.6)


def backtest(sym, sig, hold_days, commission=0.00025, stamp_tax=0.0005):
    df = load_df(sym)
    if df is None or len(df) < 60:
        return []
    sig = sig.fillna(False).values
    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    n = len(close)
    lim = limit_pct(sym)
    rets = []
    for i in range(n - 1):
        if not sig[i]:
            continue
        buy_day = i + 1
        buy_price = open_[buy_day]
        if buy_price <= 0 or np.isnan(buy_price):
            continue
        lp = round(close[buy_day - 1] * (1 + lim), 2)
        if abs(close[buy_day] - lp) < 0.005 and abs(high[buy_day] - close[buy_day]) < 0.005:
            continue
        sell_day = buy_day + hold_days
        if sell_day >= n:
            continue
        sell_price = close[sell_day]
        if sell_price <= 0 or np.isnan(sell_price):
            continue
        rets.append((sell_price - buy_price) / buy_price - commission * 2 - stamp_tax)
    return rets


def sweep(name, sig_fn, insts, holds):
    print(f"=== {name} 持有天数扫描 ===")
    for hold in holds:
        all_rets = []
        for sym in insts:
            df = load_df(sym)
            if df is None:
                continue
            sig = sig_fn(df, limit_pct(sym)) if name == "涨停打板" else sig_fn(df)
            all_rets.extend(backtest(sym, sig, hold))
        r = np.array([x for x in all_rets if np.isfinite(x)])
        if len(r) == 0:
            continue
        win = (r > 0).mean()
        avg = r.mean()
        se = r.std() / np.sqrt(len(r))
        print(f"  持有{hold:>2}日: {len(r):>6}笔 胜率{win*100:5.1f}% 平均{avg*100:6.2f}%±{1.96*se*100:.2f}%")


def main():
    insts = [l.split()[0].lower() for l in open(os.path.join(QLIB, "instruments/all.txt")) if len(l.split()) >= 3]
    holds = [1, 3, 5, 10, 20]
    sweep("涨停打板", sig_limit_up, insts, holds)
    sweep("均线放量", sig_ma_volume, insts, holds)
    sweep("高窄旗形", sig_high_tight_flag, insts, holds)


if __name__ == "__main__":
    main()
