#!/usr/bin/env python3
"""每日复盘视图: 读 snapshot.db。

用法:
  review.py             → 最新快照(实时量价 + 板块/活跃度 + 龙虎榜)
  review.py YYYY-MM-DD  → 指定历史日(qlib 量价 + 龙虎榜)
"""
import sqlite3, json, os, sys
import datetime as dt
from pathlib import Path
import snapshot_contract
from core_storage import CORE_DB, read_core

DB = os.path.expanduser("~/research-data/snapshot.db")
POLICY = Path(__file__).with_name("config") / "snapshot-contract.json"


def show_core(record):
    q = record["qlib"]
    print(f"=== A股核心行情摘要 {record['date']}（已封存，非实时、非完整快照） ===")
    _show_breadth(q["breadth"])
    coverage = q["coverage"]
    print(f"有效报价 {coverage['valid']}/{coverage['eligible']}，缺失 {coverage['invalid']}")
    print(f"涨停 {q.get('limitUpCount', '未提供')}  跌停 {q.get('limitDownCount', '未提供')}{_limit_note(q)}")
    print("研究附录未请求：不提供每日新闻、公告、资金流、板块或估值归档。")


def read_review(day=None, *, db_path=DB, core_db=CORE_DB, policy_path=POLICY, now=None):
    """Read the contract appropriate to the requested date, without any writer."""
    policy = snapshot_contract.read_policy(policy_path)
    snapshot_contract.assert_core_history(policy, core_db)
    now = now or dt.datetime.now(snapshot_contract.CST)
    selected = snapshot_contract.contract_for_day(policy, day) if day else snapshot_contract.select(policy, now)
    if selected == snapshot_contract.CORE:
        if day is None:
            path = Path(core_db)
            if not path.exists():
                return selected, None
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                days = [row[0] for row in conn.execute("SELECT date FROM core_market ORDER BY date DESC")]
            day = next((d for d in days if d <= now.date().isoformat()
                        and snapshot_contract.contract_for_day(policy, d) == selected), None)
        return selected, read_core(day, core_db) if day else None
    path = Path(db_path)
    if not path.exists():
        return selected, None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        values = conn.execute("SELECT date,payload FROM snapshot ORDER BY date DESC").fetchall()
    for found, raw in values:
        if (found == day if day else found <= now.date().isoformat()):
            if snapshot_contract.contract_for_day(policy, found) == selected:
                return selected, json.loads(raw)
    return selected, None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _limit_note(q):
    status = q.get("limitStatus")
    if not isinstance(status, dict):
        return "（旧口径，未核对原始价与板块规则）"
    if status.get("complete"):
        return ""
    return f"（下限值：另有 {status.get('unverified', '?')} 只无法核实，不计入）"


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


def _show_global_markets(gm):
    if isinstance(gm, dict) and gm and "error" not in gm:
        parts = []
        for name, v in gm.items():
            if isinstance(v, dict):
                chg = v.get("chg", 0)
                sign = "+" if chg >= 0 else ""
                parts.append(f"{name}{sign}{chg}%")
        if parts:
            print(f"【隔夜外盘】{' | '.join(parts)}")


def show_current(d):
    spot = d.get("spot", {})
    a = d.get("akshare", {})
    l = d.get("lhb", {})
    print("=== A股每日复盘 (实时) ===")
    _show_global_markets(d.get("globalMarkets", {}))
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
    print(f"涨停 {q['limitUpCount']}  跌停 {q['limitDownCount']}{_limit_note(q)}")
    print()
    _show_limitup(q["limitUp"], "涨停梯队(连板)")
    print("【领涨】", [(x["sym"], x["chg"]) for x in q["topGainers"][:5]])
    print("【领跌】", [(x["sym"], x["chg"]) for x in q["topLosers"][:5]])
    print()
    _show_lhb(l)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    contract, record = read_review(date)
    if record is None:
        print(f"无对应已封存产物（{contract}）；禁止用今日采集回填历史。")
        return 1
    if contract == snapshot_contract.CORE:
        show_core(record)
    else:
        print(f"已封存 full 记录日期：{record['date']}；不代表实时行情。")
        show_historical(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
