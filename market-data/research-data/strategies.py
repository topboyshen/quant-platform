#!/usr/bin/env python3
"""移植 Sequoia-X 的 6 个选股策略, 用 qlib 数据(26年历史)。

策略: 海龟突破 / 均线放量 / 高窄旗形 / 涨停洗盘 / 上升跌停 / RPS突破
"""
import os
import numpy as np

from qlib_io import load_qlib_features, read_qlib_day_bin
from signal_rules import latest_signal

QLIB = os.path.expanduser("~/qlib/data/cn_data")


def read_bin(path):
    return read_qlib_day_bin(path)


def load_stock(sym):
    try:
        _, fields = load_qlib_features(
            QLIB, sym, ("open", "high", "low", "close", "volume", "amount")
        )
    except FileNotFoundError:
        return None
    return fields


def turtle_trade(d):
    """海龟突破: close>20日high, 成交额>1亿, 阳线, 真涨"""
    return latest_signal("turtle_trade", d)


def ma_volume(d):
    """均线放量: 原金叉规则；至少21日，均量排除信号日。"""
    return latest_signal("ma_volume", d)


def high_tight_flag(d):
    """高窄旗形: 40日涨>60%, 10日收敛<15%, 高位抗跌, 缩量"""
    return latest_signal("high_tight_flag", d)


def limit_up_shakeout(d):
    """涨停洗盘: 昨日涨停, 今日收阴, 放量2倍, 支撑不破"""
    return latest_signal("limit_up_shakeout", d)


def uptrend_limit_down(d):
    """上升跌停: 原阈值；至少61日，均价和均量均排除信号日。"""
    return latest_signal("uptrend_limit_down", d)


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
