#!/usr/bin/env python3
"""向量化回测: 5个策略 + A股规则(T+1/涨跌停/手续费)。

规则:
- 信号日 D 收盘后确认 → D+1 开盘买入(无前视)
- T+1: 持有 >= 1 天
- 涨停不能买(close==涨停价则跳过)
- 手续费: 佣金0.025%×2 + 印花税0.05%(卖出)
- 持有 N 日后收盘卖出
"""
import os
import numpy as np
import pandas as pd

from qlib_io import load_qlib_features, read_qlib_day_bin

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
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


# ---- 信号函数(返回 bool Series, 每天) ----
def sig_turtle(df):
    high_20 = df["high"].shift(1).rolling(20).max()
    return (df["close"] > high_20) & (df["amount"] > 1e5) & (df["close"] > df["open"]) & (df["close"] > df["close"].shift(1))


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


def sig_limit_up_shakeout(df):
    pc = df["close"].shift(1)
    p2c = df["close"].shift(2)
    pv = df["volume"].shift(1)
    return (pc >= p2c * 1.095) & (df["close"] < df["open"]) & (df["volume"] > pv * 2.0) & (df["low"] >= pc)


def sig_uptrend_limit_down(df):
    ma20 = df["close"].rolling(20).mean()
    ma60 = df["close"].rolling(60).mean()
    vol_ma20 = df["volume"].rolling(20).mean()
    return (ma20.shift(1) > ma60.shift(1)) & (df["close"] <= df["close"].shift(1) * 0.905) & (df["volume"] > vol_ma20 * 2.0)


def backtest(sym, sig_fn, hold_days=5, commission=0.00025, stamp_tax=0.0005):
    df = load_df(sym)
    if df is None or len(df) < 60:
        return []
    sig = sig_fn(df).fillna(False).values
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
        # 涨停不能买: close == 涨停价(round(prev_close*(1+lim),2)) 且 high==close
        lp = round(close[buy_day - 1] * (1 + lim), 2)
        if abs(close[buy_day] - lp) < 0.005 and abs(high[buy_day] - close[buy_day]) < 0.005:
            continue
        sell_day = buy_day + hold_days
        if sell_day >= n:
            continue
        sell_price = close[sell_day]
        if sell_price <= 0 or np.isnan(sell_price):
            continue
        ret = (sell_price - buy_price) / buy_price
        cost = commission * 2 + stamp_tax
        rets.append(ret - cost)
    return rets


def main():
    insts = [l.split()[0].lower() for l in open(os.path.join(QLIB, "instruments/all.txt")) if len(l.split()) >= 3]
    strategies = {"海龟突破": sig_turtle, "均线放量": sig_ma_volume, "高窄旗形": sig_high_tight_flag,
                  "涨停洗盘": sig_limit_up_shakeout, "上升跌停": sig_uptrend_limit_down}
    for name, fn in strategies.items():
        all_rets = []
        for sym in insts:
            all_rets.extend(backtest(sym, fn))
        if not all_rets:
            print(f"{name}: 无信号")
            continue
        r = np.array(all_rets)
        r = r[np.isfinite(r)]
        if len(r) == 0:
            print(f"{name}: 无有效信号")
            continue
        win = (r > 0).mean()
        avg = r.mean()
        total = (1 + r).prod() - 1
        # 置信区间(均值)
        se = r.std() / np.sqrt(len(r))
        print(f"{name}: {len(r)}笔 胜率{win*100:.1f}% 平均{avg*100:.2f}%±{1.96*se*100:.2f}% 累计{total*100:.1f}%")


if __name__ == "__main__":
    main()
