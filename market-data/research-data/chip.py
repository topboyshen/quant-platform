#!/usr/bin/env python3
"""筹码分布 CYQ: 本地计算(获利比例/平均成本/成本区间/筹码峰)。

输入: baostock 前复权 OHLC + 换手率(120交易日)。零外部依赖(仅 numpy)。
"""
import os, json, sys
import numpy as np

for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)


def _triangular_weights(grid, low, high, avg):
    w = np.zeros_like(grid)
    if not np.isfinite([low, high, avg]).all() or high < low:
        return w
    if high - low < 1e-9:
        w[np.argmin(np.abs(grid - low))] = 1.0
        return w
    avg = min(max(avg, low), high)
    left = (grid >= low) & (grid <= avg)
    right = (grid > avg) & (grid <= high)
    if avg - low > 1e-9:
        w[left] = (grid[left] - low) / (avg - low)
    else:
        w[left] = 1.0
    if high - avg > 1e-9:
        w[right] = (high - grid[right]) / (high - avg)
    else:
        w[right] = 1.0
    total = w.sum()
    if total > 0:
        return w / total
    w[np.argmin(np.abs(grid - avg))] = 1.0
    return w


def chip_distribution(rows, grid_size=300, decay=1.0):
    d = [r for r in rows if r["high"] > 0 and r["turn"] is not None]
    d.sort(key=lambda r: r["date"])
    if not d:
        return None
    lo = min(r["low"] for r in d)
    hi = max(r["high"] for r in d)
    pad = (hi - lo) * 0.02 or max(lo * 0.02, 0.01)
    grid = np.linspace(lo - pad, hi + pad, grid_size)
    chips = None
    for r in d:
        t = min(max(float(r["turn"]) / 100.0 * decay, 0.0), 1.0)
        avg = (float(r["high"]) + float(r["low"]) + float(r["close"])) / 3.0
        w = _triangular_weights(grid, float(r["low"]), float(r["high"]), avg)
        if w.sum() <= 0:
            continue
        if chips is None:
            chips = w.copy()
            continue
        chips = chips * (1.0 - t) + w * t
    if chips is None:
        return None
    chips = chips / chips.sum()
    price = float(d[-1]["close"])
    cum = np.cumsum(chips)

    def price_at(q):
        return float(np.interp(q, cum, grid))

    p05, p15, p85, p95 = (price_at(q) for q in (0.05, 0.15, 0.85, 0.95))
    peak_i = int(np.argmax(chips))
    return {
        "price": price,
        "profit_ratio": float(chips[grid <= price].sum()),
        "avg_cost": float((grid * chips).sum()),
        "cost_90": (p05, p95),
        "cost_70": (p15, p85),
        "concentration_90": float((p95 - p05) / (p95 + p05)) if p95 + p05 else None,
        "concentration_70": float((p85 - p15) / (p85 + p15)) if p85 + p15 else None,
        "peak_price": float(grid[peak_i]),
    }


def baostock_ohlc_turn(code, days=120):
    import baostock as bs
    import datetime, contextlib, io
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
    pre = ("sh." if code.startswith(("6", "9", "5")) else "sz." if code.startswith(("0", "3")) else "bj.") + code
    rows = []
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
        rs = bs.query_history_k_data_plus(pre, "date,high,low,close,turn,tradestatus",
                                          start_date=start, end_date=end, frequency="d", adjustflag="2")
        while rs.next():
            r = rs.get_row_data()
            if r[5] == "1":
                rows.append({"date": r[0], "high": float(r[1]), "low": float(r[2]),
                             "close": float(r[3]), "turn": float(r[4])})
        bs.logout()
    return rows


def main():
    cfg = json.load(open(os.path.expanduser("~/research-data/config/stocks.json")))
    watch = [s["code"] for s in cfg.get("corePool", [])]
    for code in watch:
        rows = baostock_ohlc_turn(code, days=120)
        r = chip_distribution(rows)
        if r:
            print(f"{code}: 现价{r['price']:.2f} 获利比例{r['profit_ratio']*100:.1f}% "
                  f"平均成本{r['avg_cost']:.2f} 筹码峰{r['peak_price']:.2f} "
                  f"集中度90={r['concentration_90']*100:.1f}%")


if __name__ == "__main__":
    main()
