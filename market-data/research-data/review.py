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


def _show_baostock(bv):
    print("【估值】(watchlist, 最新 PE/PB/PS)")
    for code, data in bv.items():
        if isinstance(data, list) and data:
            latest = data[-1]
            print(f"  {code} PE={latest.get('pe_ttm')} PB={latest.get('pb')} PS={latest.get('ps')} 换手={latest.get('turn')}%")
        else:
            print(f"  {code} {data}")


def _show_announcements(anns):
    print("【公告】(watchlist)")
    for code, items in anns.items():
        if isinstance(items, list):
            for a in items[:3]:
                print(f"  {code} {a.get('date', '')} {a.get('title', '')[:40]}")
        else:
            print(f"  {code} {items}")


def _show_news(news):
    print("【财联社快讯】")
    for n in news[:8]:
        print(f"  {n.get('title', '')[:60]}")


def _show_northbound(nb):
    if isinstance(nb, dict) and "error" not in nb:
        hgt_trend = nb.get("hgt_trend")
        sgt_trend = nb.get("sgt_trend")
        trend_s = ""
        if hgt_trend is not None and sgt_trend is not None:
            t = hgt_trend + sgt_trend
            trend_s = f" 盘中{'流入' if t > 0 else '流出'}{abs(t):.0f}亿"
        print(f"【北向资金】沪股通{nb.get('hgt')}亿 深股通{nb.get('sgt')}亿 总{nb.get('total')}亿{trend_s}")


def _show_history_compare(conn, current):
    prev = conn.execute("select payload from snapshot where date < ? order by date desc limit 1",
                        (current["date"],)).fetchone()
    if not prev:
        print("【历史对比】无历史快照(等 cron 累积几天)")
        return
    p = json.loads(prev[0])
    cur_spot = current.get("spot", {})
    prev_spot = p.get("spot", {})
    cur_lu = cur_spot.get("limitUpCount", 0)
    prev_lu = prev_spot.get("limitUpCount", 0)
    cur_nb = current.get("northbound", {}).get("total", 0) or 0
    prev_nb = p.get("northbound", {}).get("total", 0) or 0
    print(f"【历史对比】vs {p['date']}")
    print(f"  涨停: {prev_lu} → {cur_lu} ({'+' if cur_lu >= prev_lu else ''}{cur_lu - prev_lu})")
    print(f"  北向: {prev_nb:.0f}亿 → {cur_nb:.0f}亿 ({'+' if cur_nb >= prev_nb else ''}{cur_nb - prev_nb:.0f}亿)")


def _show_hot_stocks(hot):
    if isinstance(hot, list) and hot:
        print(f"【涨停题材归因】({len(hot)}只)")
        for h in hot[:10]:
            print(f"  {h['code']} {h['name']} {h['reason'][:40]}")


def _show_concepts(concepts):
    if isinstance(concepts, dict):
        print("【概念板块】(watchlist)")
        for code, blocks in concepts.items():
            if isinstance(blocks, dict):
                print(f"  {code}: {'/'.join(blocks.get('概念', [])[:5])}")


def _show_sentiment(spot, nb):
    b = spot.get("breadth", {})
    up, down = b.get("up", 0), b.get("down", 0)
    total_nb = nb.get("total", 0) or 0
    limit_up = spot.get("limitUpCount", 0)
    if up > down * 2 and total_nb > 0:
        mood = "强势(普涨+北向流入)"
    elif down > up * 2:
        mood = "弱势(普跌)"
    else:
        mood = "震荡"
    print(f"【市场情绪】{mood} | 涨{up}/跌{down} | 涨停{limit_up} | 北向{total_nb:.0f}亿")


def _show_theme_mainline(hot):
    if not isinstance(hot, list) or not hot:
        return
    from collections import Counter
    themes = Counter()
    for h in hot:
        for t in (h.get("reason", "") or "").split("+"):
            t = t.strip()
            if t:
                themes[t] += 1
    top = themes.most_common(10)
    print(f"【题材主线】(涨停股题材聚合)")
    print("  " + " | ".join(f"{t}({c})" for t, c in top))


def show_current(d):
    spot = d.get("spot", {})
    a = d.get("akshare", {})
    l = d.get("lhb", {})
    print("=== A股每日复盘 (实时) ===")
    _show_sentiment(spot, d.get("northbound", {}))
    print()
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
    _show_northbound(d.get("northbound", {}))
    print()
    _show_hot_stocks(d.get("hotStocks", []))
    print()
    _show_theme_mainline(d.get("hotStocks", []))
    print()
    _show_concepts(d.get("concepts", {}))
    print()
    _show_baostock(d.get("baostock", {}))
    print()
    _show_announcements(d.get("announcements", {}))
    print()
    _show_news(d.get("news", []))
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
        _show_history_compare(conn, json.loads(row[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
