#!/usr/bin/env python3
"""每日复盘视图: 读 snapshot.db。

用法:
  review.py             → 最新快照(实时量价 + 板块/活跃度 + 龙虎榜)
  review.py YYYY-MM-DD  → 指定历史日(qlib 量价 + 龙虎榜)
"""
import sqlite3, json, os, sys

DB = os.path.expanduser("~/research-data/snapshot.db")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _show_breadth(b):
    print(f"涨跌家数: 涨{b['up']} / 跌{b['down']} / 平{b['flat']}  (总{b['total']})")


def _show_limitup(items, label):
    print(f"【{label}】")
    for x in items[:15]:
        name = x.get("name", "")
        streak = x.get("streak")
        if streak:
            print(f"  {streak}连板  {x['sym']} {name}  涨{x['chg']}%")
        else:
            print(f"  {x['sym']} {name}  涨{x['chg']}%")


def _show_lhb(l):
    print(f"【龙虎榜】({l['count']}条, 前10)")
    for r in l["rows"][:10]:
        print(f"  {r['code']} {r['name']}  {r['chg']}%  {r['reason']}")


def _show_sectors(a):
    sectors = sorted(a["sectors"], key=lambda s: -_f(s.get("chg")))
    print("【板块领涨】")
    for s in sectors[:5]:
        print(f"  {s['name']}  {s['chg']}%  领涨:{s['leader']}")
    print("【板块领跌】")
    for s in sectors[-5:][::-1]:
        print(f"  {s['name']}  {s['chg']}%")


def _show_activity(a):
    act = a.get("activity", {})
    print(f"【市场活跃度】{act.get('活跃度', '?')}  上涨{act.get('上涨', '?')} 下跌{act.get('下跌', '?')} "
          f"涨停{act.get('涨停', '?')} 跌停{act.get('跌停', '?')}")


def _show_fundflow(ff):
    print("【资金流】(watchlist+涨停股, 最新日净流入)")
    for code, data in ff.items():
        if isinstance(data, list) and data:
            na = _f(data[0].get("net_amount"))
            if abs(na) >= 1e8:
                s = f"{na/1e8:.2f}亿"
            elif abs(na) >= 1e4:
                s = f"{na/1e4:.0f}万"
            else:
                s = f"{na:.0f}"
            print(f"  {code} 净流入 {s}")
        else:
            print(f"  {code} {data}")


def show_current(d):
    spot = d.get("spot", {})
    a = d.get("akshare", {})
    l = d.get("lhb", {})
    print("=== A股每日复盘 (实时) ===")
    if "error" in spot:
        print(f"实时量价获取失败: {spot['error']}")
    else:
        _show_breadth(spot["breadth"])
        print(f"涨停 {spot['limitUpCount']}")
        _show_limitup(spot["limitUp"], "涨停(实时)")
        print("【领涨】", [(x["sym"], x["chg"]) for x in spot["topGainers"][:5]])
        print("【领跌】", [(x["sym"], x["chg"]) for x in spot["topLosers"][:5]])
    print()
    _show_lhb(l)
    print()
    _show_fundflow(d.get("fundFlow", {}))
    print()
    _show_sectors(a)
    print()
    _show_activity(a)


def show_historical(d):
    q = d["qlib"]
    l = d.get("lhb", {})
    print(f"=== A股每日复盘 {q['date']} (历史) ===")
    _show_breadth(q["breadth"])
    print(f"涨停 {q['limitUpCount']}  跌停 {q['limitDownCount']}")
    print()
    _show_limitup(q["limitUp"], "涨停梯队(连板)")
    print("【领涨】", [(x["sym"], x["chg"]) for x in q["topGainers"][:5]])
    print("【领跌】", [(x["sym"], x["chg"]) for x in q["topLosers"][:5]])
    print()
    _show_lhb(l)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect(DB)
    if date:
        row = conn.execute("select payload from snapshot where date=?", (date,)).fetchone()
        if not row:
            print(f"无 {date} 快照, 先跑: daily_snapshot.py {date}")
            return 1
        show_historical(json.loads(row[0]))
    else:
        row = conn.execute("select payload from snapshot order by date desc limit 1").fetchone()
        if not row:
            print("无快照, 先跑 daily_snapshot.py")
            return 1
        show_current(json.loads(row[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
