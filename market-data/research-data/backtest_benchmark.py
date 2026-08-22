#!/usr/bin/env python3
"""回测 + 沪深300基准对比: 区分 alpha vs beta。

对每个交易, 计算策略收益和同期沪深300收益, 超额收益 = 策略 - 基准。
超额收益≈0 = 纯beta(无alpha); 超额>0 = 有alpha。
"""
import os, contextlib, io
import numpy as np
import pandas as pd

from qlib_io import load_qlib_features, read_qlib_day_bin

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    return read_qlib_day_bin(path)


def load_df(sym):
    try:
        start, cols = load_qlib_features(
            QLIB, sym, ("open", "high", "low", "close", "volume", "amount")
        )
    except FileNotFoundError:
        return None, None
    return pd.DataFrame(cols), start


def limit_pct(sym):
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


def get_index():
    import baostock as bs
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
        rs = bs.query_history_k_data_plus("sh.000300", "date,close",
                                          start_date="2000-01-01", end_date="2026-12-31",
                                          frequency="d", adjustflag="3")
        rows = {}
        while rs.next():
            r = rs.get_row_data()
            rows[r[0]] = float(r[1])
        bs.logout()
    return rows


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


def backtest_benchmark(sym, sig_fn, hold_days, index, cal, commission=0.00025, stamp_tax=0.0005):
    df, start = load_df(sym)
    if df is None or len(df) < 60:
        return []
    sig = sig_fn(df).fillna(False).values
    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    n = len(close)
    lim = limit_pct(sym)
    out = []
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
        strat = (sell_price - buy_price) / buy_price - commission * 2 - stamp_tax
        # 基准收益(同期沪深300)
        bd = cal[start + buy_day]
        sd = cal[start + sell_day]
        ib = index.get(bd)
        is_ = index.get(sd)
        if ib and is_ and ib > 0:
            bench = (is_ - ib) / ib
            out.append((strat, bench))
    return out


def main():
    cal = [l.strip() for l in open(os.path.join(QLIB, "calendars/day.txt")) if l.strip()]
    index = get_index()
    print(f"沪深300 基准数据: {len(index)} 天")
    insts = [l.split()[0].lower() for l in open(os.path.join(QLIB, "instruments/all.txt")) if len(l.split()) >= 3]
    for name, fn in [("均线放量", sig_ma_volume), ("高窄旗形", sig_high_tight_flag)]:
        for hold in [5, 10, 20]:
            pairs = []
            for sym in insts:
                pairs.extend(backtest_benchmark(sym, fn, hold, index, cal))
            if not pairs:
                continue
            strat = np.array([p[0] for p in pairs])
            bench = np.array([p[1] for p in pairs])
            excess = strat - bench
            se = excess.std() / np.sqrt(len(excess))
            print(f"{name} 持有{hold:>2}日: {len(excess)}笔 策略{strat.mean()*100:.2f}% 基准{bench.mean()*100:.2f}% "
                  f"超额{excess.mean()*100:.2f}%±{1.96*se*100:.2f}%")


if __name__ == "__main__":
    main()
