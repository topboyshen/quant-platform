#!/usr/bin/env python3
"""每日复盘视图: 读 snapshot.db 最新快照, 一屏输出当天市场全景。"""
import sqlite3, json, os

DB = os.path.expanduser("~/research-data/snapshot.db")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def main():
    conn = sqlite3.connect(DB)
    row = conn.execute("select payload from snapshot order by date desc limit 1").fetchone()
    if not row:
        print("无快照, 先跑 daily_snapshot.py")
        return 1
    d = json.loads(row[0])
    q = d["qlib"]
    a = d["akshare"]
    l = d["lhb"]
    b = q["breadth"]

    print(f"=== A股每日复盘 {q['date']} ===")
    print(f"涨跌家数: 涨{b['up']} / 跌{b['down']} / 平{b['flat']}  (总{b['total']})")
    print(f"涨停 {q['limitUpCount']}  跌停 {q['limitDownCount']}")
    print()

    print("【涨停梯队】(连板降序)")
    for x in q["limitUp"][:15]:
        print(f"  {x['streak']}连板  {x['sym']}  涨{x['chg']}%")
    print()

    print(f"【龙虎榜】({l['count']}条, 前10)")
    for r in l["rows"][:10]:
        print(f"  {r['code']} {r['name']}  {r['chg']}%  {r['reason']}")
    print()

    sectors = sorted(a["sectors"], key=lambda s: -_f(s.get("chg")))
    print("【板块领涨】")
    for s in sectors[:5]:
        print(f"  {s['name']}  {s['chg']}%  领涨:{s['leader']}")
    print("【板块领跌】")
    for s in sectors[-5:][::-1]:
        print(f"  {s['name']}  {s['chg']}%")
    print()

    act = a.get("activity", {})
    print(f"【市场活跃度】{act.get('活跃度', '?')}  上涨{act.get('上涨', '?')} 下跌{act.get('下跌', '?')} "
          f"涨停{act.get('涨停', '?')} 跌停{act.get('跌停', '?')}")
    print()
    print(f"注: 量价/龙虎榜={q['date']}(qlib滞后3日) | 板块/活跃度={act.get('统计日期', '当前')}(实时)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
