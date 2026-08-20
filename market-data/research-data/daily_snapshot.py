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


def fund_flow_sina(codes, days=5):
    import urllib.request, json
    results = {}
    for code in codes:
        pre = ("bj" if code.startswith(("92", "8")) else "sh" if code.startswith(("6", "9")) else "sz") + code
        u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                t = r.read().decode("utf-8", "ignore")
            arr = json.loads(t[t.index("["):t.rindex("]") + 1])
            results[code] = [{"date": x.get("opendate"), "close": x.get("trade"),
                              "net_amount": x.get("netamount"), "turnover": x.get("turnover")} for x in arr]
        except Exception as e:
            results[code] = {"error": str(e)}
    return results


def lhb_official(date):
    import urllib.request, json, ssl
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    out = {"date": date, "szse": [], "sse": ""}
    su = (f"https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
          f"&CATALOGID=1842_xxpl&TABKEY=tab1&txtStart={date}&txtEnd={date}&random=0.9")
    req = urllib.request.Request(su, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/disclosure/supervision/dealinfo/index.html"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            d = json.loads(r.read())
        for row in d[0].get("data", []):
            out["szse"].append({"code": row.get("zqdm"), "name": row.get("zqjc"), "amount": row.get("cjje"), "reason": row.get("plyy")})
    except Exception as e:
        out["szse_error"] = str(e)
    eu = (f"https://query.sse.com.cn/infodisplay/showTradePublicFile.do?"
          f"jsonCallBack=cb&isPagination=false&dateTx={date}")
    req = urllib.request.Request(eu, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/disclosure/diclosure/public/"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            t = r.read().decode("utf-8", "ignore")
        out["sse"] = "\n".join(json.loads(t[t.index("(") + 1:t.rindex(")")]).get("fileContents", []))
    except Exception as e:
        out["sse_error"] = str(e)
    return out


def mootdx_quotes(codes):
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    df = client.quotes(symbol=codes)
    result = {}
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            result[code] = {
                "price": r.get("price"),
                "bid1": r.get("bid1"), "ask1": r.get("ask1"),
                "bid_vol1": r.get("bid_vol1"), "ask_vol1": r.get("ask_vol1"),
            }
    return result


def tencent_quote(codes):
    import urllib.request
    prefixed = []
    for c in codes:
        if c.startswith(("92", "8")):
            p = "bj" + c
        elif c.startswith(("6", "9", "5")):
            p = "sh" + c
        else:
            p = "sz" + c
        prefixed.append(p)
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    result = {}
    for line in data.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        result[key[2:]] = {
            "name": vals[1], "price": vals[3], "pe_ttm": vals[39], "pb": vals[46],
            "mcap_yi": vals[45], "float_mcap_yi": vals[44], "turnover_pct": vals[38],
            "limit_up": vals[47], "limit_down": vals[48],
        }
    return result


def baostock_valuation(codes, days=30):
    import baostock as bs
    import datetime
    import contextlib, io
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
    result = {}
    with contextlib.redirect_stdout(io.StringIO()):  # 抑制 baostock login/logout 的 stdout 输出
        bs.login()
        for code in codes:
            pre = ("sh." if code.startswith(("6", "9", "5")) else "sz." if code.startswith(("0", "3")) else "bj.") + code
            try:
                rs = bs.query_history_k_data_plus(pre, "date,code,close,peTTM,pbMRQ,psTTM,turn",
                                                  start_date=start, end_date=end, frequency="d", adjustflag="3")
                rows = []
                while rs.next():
                    r = rs.get_row_data()
                    rows.append({"date": r[0], "close": r[2], "pe_ttm": r[3], "pb": r[4], "ps": r[5], "turn": r[6]})
                result[code] = rows
            except Exception as e:
                result[code] = {"error": str(e)}
        bs.logout()
    return result


_CNINFO_ORGID_MAP = None


def cninfo_announcements(code, page_size=10):
    global _CNINFO_ORGID_MAP
    import requests
    if _CNINFO_ORGID_MAP is None:
        r = requests.get("http://www.cninfo.com.cn/new/data/szse_stock.json", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        _CNINFO_ORGID_MAP = {s["code"]: s["orgId"] for s in r.json().get("stockList", [])}
    org_id = _CNINFO_ORGID_MAP.get(code, f"gssz0{code}")
    payload = {"stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(page_size), "pageNum": "1",
               "column": "", "category": "", "plate": "", "seDate": "", "searchkey": "", "secid": "",
               "sortName": "", "sortType": "", "isHLtitle": "true"}
    r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", data=payload,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded",
                               "Referer": "https://www.cninfo.com.cn/new/disclosure", "Origin": "https://www.cninfo.com.cn"}, timeout=15)
    anns = r.json().get("announcements") or []
    import datetime
    out = []
    for a in anns:
        ts = a.get("announcementTime")
        date = ""
        if ts:
            try:
                ts = int(ts)
                if ts > 1e12:
                    ts = ts // 1000
                date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                date = str(ts)[:10]
        out.append({"title": a.get("announcementTitle"), "date": date})
    return out


def cls_telegraph(page_size=20):
    import requests, hashlib
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5", "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    r = requests.get(f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}",
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}, timeout=10)
    rolls = (r.json().get("data") or {}).get("roll_data") or []
    return [{"title": n.get("title", ""), "time": n.get("ctime")} for n in rolls]


def northbound_flow():
    import urllib.request, json
    req = urllib.request.Request("https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                                 headers={"User-Agent": "Mozilla/5.0", "Host": "data.hexin.cn",
                                          "Referer": "https://data.hexin.cn/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])
    hgt_close = hgt[-1] if hgt else None
    sgt_close = sgt[-1] if sgt else None
    hgt_open = hgt[0] if hgt else None
    sgt_open = sgt[0] if sgt else None
    return {"hgt": hgt_close, "sgt": sgt_close,
            "total": (hgt_close + sgt_close) if hgt and sgt else None,
            "hgt_trend": (hgt_close - hgt_open) if hgt else None,
            "sgt_trend": (sgt_close - sgt_open) if sgt else None}


def concept_blocks(code):
    import urllib.request, json
    url = ('https://finance.pae.baidu.com/api/getrelatedblock'
           f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]&finClientType=pc')
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Host": "finance.pae.baidu.com",
                                               "Referer": "https://gushitong.baidu.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    result = d.get("Result", {}).get(code, [])
    return {cat.get("name", ""): [i.get("name") for i in cat.get("list", [])] for cat in result}


def insider_transactions(code):
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    text = client.F10(symbol=code, name="股东研究")
    return (text or "")[:3000]  # 截断, 供 LLM 分析用


def hot_stocks():
    import urllib.request, json, datetime
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    rows = d.get("data") or []
    return [{"code": r.get("code"), "name": r.get("name"), "reason": r.get("reason", "")} for r in rows]


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
    # 资金流: watchlist + 涨停股
    try:
        cfg = json.load(open(os.path.expanduser("~/research-data/config/stocks.json")))
        watch_codes = [s["code"] for s in cfg.get("corePool", [])]
    except Exception:
        watch_codes = []
    limit_codes = [x["sym"][2:] for x in spot.get("limitUp", [])[:10]]
    ff_codes = list(dict.fromkeys(watch_codes + limit_codes))
    try:
        ff = fund_flow_sina(ff_codes, days=5)
    except Exception as e:
        ff = {"error": str(e)}
    try:
        lhb_off = lhb_official(q["date"])
    except Exception as e:
        lhb_off = {"date": q["date"], "error": str(e)}
    try:
        mq = mootdx_quotes(watch_codes)
    except Exception as e:
        mq = {"error": str(e)}
    try:
        tq = tencent_quote(watch_codes)
    except Exception as e:
        tq = {"error": str(e)}
    try:
        bv = baostock_valuation(watch_codes, days=30)
    except Exception as e:
        bv = {"error": str(e)}
    try:
        anns = {code: cninfo_announcements(code) for code in watch_codes}
    except Exception as e:
        anns = {"error": str(e)}
    try:
        news = cls_telegraph(20)
    except Exception as e:
        news = {"error": str(e)}
    try:
        nb = northbound_flow()
    except Exception as e:
        nb = {"error": str(e)}
    try:
        concepts = {code: concept_blocks(code) for code in watch_codes}
    except Exception as e:
        concepts = {"error": str(e)}
    try:
        insiders = {code: insider_transactions(code) for code in watch_codes}
    except Exception as e:
        insiders = {"error": str(e)}
    try:
        hot = hot_stocks()
    except Exception as e:
        hot = {"error": str(e)}
    snapshot = {
        "date": q["date"],
        "generatedAt": datetime.datetime.now().isoformat(),
        "qlib": q,
        "akshare": a,
        "lhb": lhb,
        "spot": spot,
        "fundFlow": ff,
        "lhbOfficial": lhb_off,
        "mootdx": mq,
        "tencent": tq,
        "baostock": bv,
        "announcements": anns,
        "news": news,
        "northbound": nb,
        "concepts": concepts,
        "insiders": insiders,
        "hotStocks": hot,
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
