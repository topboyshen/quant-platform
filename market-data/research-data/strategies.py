#!/usr/bin/env python3
"""移植 Sequoia-X 的 6 个选股策略, 用 qlib 数据(26年历史)。

策略: 海龟突破 / 均线放量 / 高窄旗形 / 涨停洗盘 / 上升跌停 / RPS突破
注: qlib 特征滞后3天, 策略基于最新可用数据(非当天)。
"""
import os, struct
import numpy as np

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    with open(path, "rb") as f:
        hdr = f.read(16)
        start = int(struct.unpack("<f", hdr[:4])[0])
        data = f.read()
    return start, np.frombuffer(data, dtype="<f4")


def load_stock(sym):
    fields = {}
    for fld in ["open", "high", "low", "close", "volume", "amount"]:
        try:
            _, v = read_bin(os.path.join(QLIB, "features", sym, fld + ".day.bin"))
            fields[fld] = v
        except FileNotFoundError:
            return None
    return fields


def turtle_trade(d):
    """海龟突破: close>20日high, 成交额>1亿, 阳线, 真涨"""
    if len(d["close"]) < 21:
        return False
    high_20 = d["high"][-21:-1].max()
    last, prev = d["close"][-1], d["close"][-2]
    return (last > high_20 and d["amount"][-1] > 1e5 and  # amount单位千元, 1e5=1亿
            last > d["open"][-1] and last > prev)


def ma_volume(d):
    """均线放量: 5日ma上穿20日ma(金叉), 放量1.5倍"""
    if len(d["close"]) < 20:
        return False
    close = d["close"]
    ma5_prev, ma20_prev = close[-6:-1].mean(), close[-21:-1].mean()
    ma5_today, ma20_today = close[-5:].mean(), close[-20:].mean()
    vol_ma20 = d["volume"][-21:-1].mean()
    return (ma5_prev < ma20_prev and ma5_today > ma20_today and
            d["volume"][-1] > vol_ma20 * 1.5)


def high_tight_flag(d):
    """高窄旗形: 40日涨>60%, 10日收敛<15%, 高位抗跌, 缩量"""
    if len(d["close"]) < 40:
        return False
    high40, low40 = d["high"][-40:].max(), d["low"][-40:].min()
    high10, low10 = d["high"][-10:].max(), d["low"][-10:].min()
    vol_ma20 = d["volume"][-21:-1].mean()
    return (high40 / low40 > 1.6 and high10 / low10 < 1.15 and
            low10 >= high40 * 0.8 and d["volume"][-1] < vol_ma20 * 0.6)


def limit_up_shakeout(d):
    """涨停洗盘: 昨日涨停, 今日收阴, 放量2倍, 支撑不破"""
    if len(d["close"]) < 3:
        return False
    prev2, prev1, today = d["close"][-3], d["close"][-2], d["close"][-1]
    return (prev1 >= prev2 * 1.095 and today < d["open"][-1] and
            d["volume"][-1] > d["volume"][-2] * 2.0 and d["low"][-1] >= prev1)


def uptrend_limit_down(d):
    """上升跌停: ma20>ma60, 跌停, 放量2倍"""
    if len(d["close"]) < 60:
        return False
    close = d["close"]
    ma20, ma60 = close[-21:-1].mean(), close[-61:-1].mean()
    vol_ma20 = d["volume"][-21:-1].mean()
    return (ma20 > ma60 and close[-1] <= close[-2] * 0.905 and
            d["volume"][-1] > vol_ma20 * 2.0)


def main():
    insts = [l.split()[0].lower() for l in open(os.path.join(QLIB, "instruments/all.txt")) if len(l.split()) >= 3]
    per_stock = {"海龟突破": turtle_trade, "均线放量": ma_volume, "高窄旗形": high_tight_flag,
                 "涨停洗盘": limit_up_shakeout, "上升跌停": uptrend_limit_down}
    results = {name: [] for name in per_stock}
    rps_data = []  # (sym, 120日涨幅, 120日high, close)
    for sym in insts:
        d = load_stock(sym)
        if d is None:
            continue
        for name, fn in per_stock.items():
            try:
                if fn(d):
                    results[name].append(sym)
            except Exception:
                pass
        # RPS: 120日涨幅 + 120日high
        if len(d["close"]) >= 120:
            c = d["close"]
            pct = (c[-1] - c[-120]) / c[-120]
            rps_data.append((sym, pct, d["high"][-120:].max(), c[-1]))
    # RPS 突破: 120日涨幅排名>=90% 且 close>=120日high*0.9
    if rps_data:
        pcts = np.array([x[1] for x in rps_data])
        rps_rank = (pcts < pcts[:, None]).sum(axis=1) / len(pcts) * 100  # 百分位
        rps_selected = [rps_data[i][0] for i in range(len(rps_data))
                        if rps_rank[i] >= 90 and rps_data[i][3] >= rps_data[i][2] * 0.9]
    else:
        rps_selected = []
    results["RPS突破"] = rps_selected
    for name, syms in results.items():
        print(f"{name}: {len(syms)} 只")
        for s in syms[:8]:
            print(f"  {s}")


if __name__ == "__main__":
    main()
