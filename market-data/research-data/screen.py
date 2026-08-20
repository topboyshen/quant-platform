#!/usr/bin/env python3
"""选股/信号: 涨停股 × 资金流 × 估值 横截面筛选。

逻辑: 取当天涨停股 → 逐个查主力净流入(新浪) + PE/PB(腾讯) → 筛出净流入>0 的,
按净流入降序输出。这是"涨停 + 资金流入 + 估值"的横截面信号。
"""
import os, json, time, sys

for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)


def get_limit_up():
    import akshare as ak
    df = ak.stock_zh_a_spot()
    limit_up = []
    for _, r in df.iterrows():
        full = str(r.get("代码"))
        code = full[2:] if full.startswith(("sh", "sz", "bj")) else full  # 去前缀, 留6位码
        try:
            chg = float(r.get("涨跌幅") or 0)
        except (TypeError, ValueError):
            chg = 0.0
        lim = 30 if code.startswith(("92", "8")) else (20 if code.startswith(("30", "68")) else 10)
        if chg >= lim - 0.5:
            limit_up.append({"code": code, "name": r.get("名称"), "chg": chg})
    return limit_up


def fund_flow(code):
    import urllib.request
    pre = ("bj" if code.startswith(("92", "8")) else "sh" if code.startswith(("6", "9")) else "sz") + code
    u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=1&sort=opendate&asc=0&daima={pre}")
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        t = r.read().decode("utf-8", "ignore")
    arr = json.loads(t[t.index("["):t.rindex("]") + 1])
    return float(arr[0].get("netamount") or 0) if arr else 0.0


def pe_pb(code):
    import urllib.request
    pre = ("bj" if code.startswith(("92", "8")) else "sh" if code.startswith(("6", "9", "5")) else "sz") + code
    url = f"https://qt.gtimg.cn/q={pre}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    vals = data.split('"')[1].split("~")
    return {"pe": float(vals[39] or 0), "pb": float(vals[46] or 0)}


def main():
    limit_up = get_limit_up()
    print(f"涨停股 {len(limit_up)} 只, 逐个查资金流+估值...")
    rows = []
    for s in limit_up:
        try:
            ff = fund_flow(s["code"])
        except Exception:
            ff = 0.0
        try:
            pp = pe_pb(s["code"])
        except Exception:
            pp = {"pe": 0.0, "pb": 0.0}
        rows.append({"code": s["code"], "name": s["name"], "chg": s["chg"],
                     "net_flow": ff, "pe": pp["pe"], "pb": pp["pb"]})
        time.sleep(0.03)
    inflow = [r for r in rows if r["net_flow"] > 0]
    inflow.sort(key=lambda r: -r["net_flow"])
    print(f"\n=== 涨停股中主力净流入 > 0 ({len(inflow)}/{len(rows)} 只) ===")
    print(f"{'代码':<8}{'名称':<10}{'涨幅%':>7}{'净流入(万)':>12}{'PE':>8}{'PB':>7}")
    for r in inflow[:20]:
        print(f"{r['code']:<8}{r['name']:<10}{r['chg']:>7.2f}{r['net_flow']/1e4:>12.0f}{r['pe']:>8.1f}{r['pb']:>7.2f}")
    # 低估值 + 净流入 的"价值型"信号
    value = [r for r in inflow if 0 < r["pe"] < 30]
    value.sort(key=lambda r: r["pe"])
    print(f"\n=== 净流入>0 且 PE<30 (价值型, {len(value)} 只) ===")
    for r in value[:10]:
        print(f"  {r['code']} {r['name']} PE={r['pe']:.1f} PB={r['pb']:.2f} 净流入{r['net_flow']/1e4:.0f}万")


if __name__ == "__main__":
    main()
