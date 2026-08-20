#!/usr/bin/env python3
"""统一每日数据快照层: qlib 量价 + akshare 板块/活跃度 → SQLite append-only。

用法: python3 daily_snapshot.py [YYYY-MM-DD]   (缺省用 qlib 日历最新交易日)
输出: 打印摘要 JSON; 全量快照写入 ~/research-data/snapshot.db (表 snapshot, date 主键)
"""
import os, json, struct, sqlite3, datetime, sys

# akshare 需直连中国源, 去掉代理环境变量
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

QLIB = os.path.expanduser("~/qlib/data/cn_data")
DB = os.path.expanduser("~/research-data/snapshot.db")


def read_bin(path):
    with open(path, "rb") as f:
        hdr = f.read(16)
        start = int(struct.unpack("<f", hdr[:4])[0])
        data = f.read()
    n = len(data) // 4
    return start, struct.unpack("<%df" % n, data)


def limit_pct(sym):
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith("30") or code.startswith("68"):
        return 0.20
    return 0.10


def qlib_market(date):
    cal = [l.strip() for l in open(os.path.join(QLIB, "calendars/day.txt")) if l.strip()]
    if date is None:
        # 日历可能领先特征数据 3-5 天; 用参考股票的实际最新数据日期
        ref_syms = ["sh600000", "sz000001", "sh600519", "sz300750"]
        latest = None
        for rs in ref_syms:
            try:
                s, close = read_bin(os.path.join(QLIB, "features", rs, "close.day.bin"))
                last_idx = s + len(close) - 1
                if 0 <= last_idx < len(cal):
                    d = cal[last_idx]
                    if latest is None or d > latest:
                        latest = d
            except FileNotFoundError:
                pass
        date = latest or cal[-1]
    elif date not in cal:
        date = cal[-1]
    di = cal.index(date)
    insts = []
    for line in open(os.path.join(QLIB, "instruments/all.txt")):
        p = line.split()
        if len(p) >= 3:
            insts.append((p[0], p[1], p[2]))
    up = down = flat = 0
    limit_up, limit_down, rows = [], [], []
    for sym, sd, ed in insts:
        sym = sym.lower()  # all.txt 大写, feature 目录小写
        try:
            s, close = read_bin(os.path.join(QLIB, "features", sym, "close.day.bin"))
            _, high = read_bin(os.path.join(QLIB, "features", sym, "high.day.bin"))
            _, low = read_bin(os.path.join(QLIB, "features", sym, "low.day.bin"))
            _, amount = read_bin(os.path.join(QLIB, "features", sym, "amount.day.bin"))
        except FileNotFoundError:
            continue
        li = di - s
        if li < 1 or li >= len(close):
            continue
        chg = close[li] / close[li - 1] - 1
        if chg > 1e-4:
            up += 1
        elif chg < -1e-4:
            down += 1
        else:
            flat += 1
        lim = limit_pct(sym)
        lp = round(close[li - 1] * (1 + lim), 2)
        if abs(close[li] - lp) < 0.005 and abs(high[li] - close[li]) < 0.005:
            streak, j = 0, li
            while j >= 1:
                lpj = round(close[j - 1] * (1 + lim), 2)
                if abs(close[j] - lpj) < 0.005 and abs(high[j] - close[j]) < 0.005:
                    streak += 1
                    j -= 1
                else:
                    break
            limit_up.append({"sym": sym, "chg": round(chg * 100, 2), "streak": streak, "close": round(close[li], 2)})
        lpd = round(close[li - 1] * (1 - lim), 2)
        if abs(close[li] - lpd) < 0.005 and abs(low[li] - close[li]) < 0.005:
            limit_down.append({"sym": sym, "chg": round(chg * 100, 2), "close": round(close[li], 2)})
        rows.append((sym, chg, amount[li], close[li]))
    rows.sort(key=lambda r: -r[1])
    return {
        "date": date,
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "limitUpCount": len(limit_up),
        "limitDownCount": len(limit_down),
        "limitUp": sorted(limit_up, key=lambda x: -x["streak"])[:30],
        "limitDown": limit_down[:10],
        "topGainers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "amount": round(r[2], 0)} for r in sorted(rows, key=lambda r: -r[2])[:10]],
    }


def akshare_market():
    import akshare as ak
    sectors = ak.stock_board_industry_summary_ths()
    activity = ak.stock_market_activity_legu()
    sector_list = []
    for _, r in sectors.iterrows():
        sector_list.append({
            "name": r.get("板块"),
            "chg": r.get("涨跌幅"),
            "netInflow": r.get("净流入"),
            "upCount": r.get("上涨家数"),
            "downCount": r.get("下跌家数"),
            "leader": r.get("领涨股"),
        })
    act = {}
    for _, r in activity.iterrows():
        act[str(r["item"])] = r["value"]
    return {"sectors": sector_list, "activity": act}


def akshare_lhb(date):
    import akshare as ak
    d = date.replace("-", "")
    df = ak.stock_lhb_detail_daily_sina(date=d)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "code": str(r.get("股票代码")),
            "name": r.get("股票名称"),
            "close": r.get("收盘价"),
            "chg": r.get("对应值"),
            "reason": r.get("指标"),
        })
    return {"date": date, "count": len(rows), "rows": rows}


def spot_market():
    import akshare as ak
    df = ak.stock_zh_a_spot()  # sina 全A现货(实时)
    up = down = flat = 0
    limit_up = []
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码"))
        name = r.get("名称")
        try:
            chg = float(r.get("涨跌幅"))
        except (TypeError, ValueError):
            chg = 0.0
        if chg > 0.01:
            up += 1
        elif chg < -0.01:
            down += 1
        else:
            flat += 1
        lim = 30 if code.startswith("bj") else (20 if code[2:].startswith(("30", "68")) else 10)
        if chg >= lim - 0.5:
            limit_up.append({"sym": code, "name": name, "chg": chg})
        try:
            amt = float(r.get("成交额"))
        except (TypeError, ValueError):
            amt = 0.0
        rows.append((code, name, chg, amt))
    rows.sort(key=lambda r: -r[2])
    return {
        "date": "current",
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "limitUpCount": len(limit_up),
        "limitUp": limit_up[:30],
        "topGainers": [{"sym": r[0], "name": r[1], "chg": r[2]} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "name": r[1], "chg": r[2]} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "name": r[1], "amount": r[3]} for r in sorted(rows, key=lambda r: -r[3])[:10]],
    }


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    q = qlib_market(date)
    try:
        a = akshare_market()
    except Exception as e:
        a = {"sectors": [], "activity": {}, "error": str(e)}
    try:
        lhb = akshare_lhb(q["date"])
    except Exception as e:
        lhb = {"date": q["date"], "count": 0, "rows": [], "error": str(e)}
    try:
        spot = spot_market()
    except Exception as e:
        spot = {"date": "current", "error": str(e)}
    snapshot = {
        "date": q["date"],
        "generatedAt": datetime.datetime.now().isoformat(),
        "qlib": q,
        "akshare": a,
        "lhb": lhb,
        "spot": spot,
    }
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)")
    conn.execute("INSERT OR REPLACE INTO snapshot(date, generated_at, payload) VALUES(?,?,?)",
                 (q["date"], snapshot["generatedAt"], json.dumps(snapshot, ensure_ascii=False)))
    conn.commit()
    conn.close()
    print(json.dumps({
        "date": q["date"],
        "breadth": q["breadth"],
        "limitUp": q["limitUpCount"],
        "limitDown": q["limitDownCount"],
        "sectors": len(a["sectors"]),
        "activity": a.get("activity", {}),
        "lhb": lhb.get("count", 0),
        "spot": {"limitUp": spot.get("limitUpCount", 0), "breadth": spot.get("breadth", {})},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
