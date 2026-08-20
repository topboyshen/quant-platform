#!/usr/bin/env python3
"""样本外验证: 高窄旗形 20日, 分前后两半看 alpha 是否稳健。

前13年(2000-2012) vs 后13年(2013-2026), 各自算超额收益。
两半都正 = 稳健; 一半正一半负 = 过拟合/偶然。
"""
import os, struct, contextlib, io
import numpy as np
import pandas as pd

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    with open(path, "rb") as f:
        hdr = f.read(16)
        start = int(struct.unpack("<f", hdr[:4])[0])
        return start, np.frombuffer(f.read(), dtype="<f4")


def load_df(sym):
    cols = {}
    start = None
    for fld in ["open", "high", "low", "close", "volume", "amount"]:
        try:
            s, v = read_bin(os.path.join(QLIB, "features", sym, fld + ".day.bin"))
            cols[fld] = v
            start = s
        except FileNotFoundError:
            return None, None
    n = len(cols["close"])
    return pd.DataFrame({k: v[:n] for k, v in cols.items()}), start


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


def sig_high_tight_flag(df):
    high40 = df["high"].rolling(40).max()
    low40 = df["low"].rolling(40).min()
    high10 = df["high"].rolling(10).max()
    low10 = df["low"].rolling(10).min()
    vol_ma20 = df["volume"].rolling(20).mean()
    return (high40 / low40 > 1.6) & (high10 / low10 < 1.15) & (low10 >= high40 * 0.8) & (df["volume"] < vol_ma20 * 0.6)


def backtest_oos(sym, hold_days, index, cal, split_idx, commission=0.00025, stamp_tax=0.0005):
    df, start = load_df(sym)
    if df is None or len(df) < 60:
        return [], []
    sig = sig_high_tight_flag(df).fillna(False).values
    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    n = len(close)
    lim = limit_pct(sym)
    first, second = [], []
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
        bd = cal[start + buy_day]
        sd = cal[start + sell_day]
        ib, is_ = index.get(bd), index.get(sd)
        if not (ib and is_ and ib > 0):
            continue
        bench = (is_ - ib) / ib
        excess = strat - bench
        # 按 buy_day 的日历索引分前后两半
        if (start + buy_day) < split_idx:
            first.append(excess)
        else:
            second.append(excess)
    return first, second


def main():
    cal = [l.strip() for l in open(os.path.join(QLIB, "calendars/day.txt")) if l.strip()]
    split_idx = len(cal) // 2
    split_date = cal[split_idx]
    print(f"样本外切分: 前段 2000~{split_date}, 后段 {split_date}~2026")
    index = get_index()
    insts = [l.split()[0].lower() for l in open(os.path.join(QLIB, "instruments/all.txt")) if len(l.split()) >= 3]
    first, second = [], []
    for sym in insts:
        f, s = backtest_oos(sym, 20, index, cal, split_idx)
        first.extend(f)
        second.extend(s)
    for label, data in [("前13年", first), ("后13年", second)]:
        r = np.array(data)
        if len(r) == 0:
            print(f"{label}: 无信号")
            continue
        se = r.std() / np.sqrt(len(r))
        print(f"{label}: {len(r)}笔 超额{r.mean()*100:.2f}%±{1.96*se*100:.2f}% 胜率{(r>0).mean()*100:.1f}%")


if __name__ == "__main__":
    main()
