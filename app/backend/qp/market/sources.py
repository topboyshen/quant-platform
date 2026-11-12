"""Data-source adapters mirroring the endpoints the legacy code uses (read-only public GET/POST).
  - Dolt (chenditc/investment_data on DoltHub SQL API): trading calendar, latest EOD date  [qlib_dolt_incremental]
  - Tencent fqkline + Sina KLine: index closes                                            [index_close_backend_probe]
  - Tencent quote: instrument names (Eastmoney push2 clist is the fallback)              [akshare stock_zh_a_spot_em]
  - akshare tool_trade_date_hist_sina: sina klc_td_sh.txt (reachability only)             [update_trading_calendar]
  - CNINFO hisAnnouncement/query: announcement counts (direct, legacy uses no proxy)       [precollect_cninfo]
  - GitHub releases (chenditc/investment_data): Qlib release-fallback tag                 [release_fallback]
  - baostock: TCP reachability only (raw socket protocol, no HTTP proxy support)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any

from qp.market.http import FetchError, fetch, tcp_probe

DOLT = "https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master"
# DoltHub's SQL API regularly takes ~60 s; use the same budget as the real EOD query
# and retry a transient failure once instead of recording a hard source failure.
DOLT_PROBE_TIMEOUT = 180.0
DOLT_PROBE_ATTEMPTS = 2
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_CAL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
GH_RELEASE = "https://api.github.com/repos/chenditc/investment_data/releases/latest"
BAOSTOCK = ("public-api.baostock.com", 10030)
INDEXES = ("sh000001", "sh000300", "sz399001", "sz399006")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TX_SYMBOL = re.compile(r"^(SH|SZ|BJ)\d{6}$")
_TX_QUOTE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


def dolt_sql(sql: str, timeout: float = 150) -> list[dict[str, Any]]:
    r = fetch(DOLT, params={"q": sql}, timeout=timeout)
    d = r.json()
    if d.get("query_execution_status") != "Success":
        raise FetchError("dolt_query_" + str(d.get("query_execution_status") or "unknown").lower())
    return d.get("rows") or []


def dolt_calendar(start: dt.date, end: dt.date) -> list[tuple[dt.date, bool]]:
    """SSE calendar rows from Dolt ts_trade_day_calendar, one query per <=366-day window (< 1000 rows each).
    DoltHub is slow (~60 s per query observed), so callers keep the range small after the initial seed."""
    out: dict[dt.date, bool] = {}
    cur = start
    while cur <= end:
        nxt = min(end, cur + dt.timedelta(days=365))
        rows = dolt_sql("SELECT date,is_open FROM ts_trade_day_calendar WHERE exchange='SSE' "
                        f"AND date>='{cur.isoformat()}' AND date<='{nxt.isoformat()}' ORDER BY date")
        for r in rows:
            d = str(r.get("date") or "")[:10]
            if _DATE.match(d):
                out[dt.date.fromisoformat(d)] = str(r.get("is_open")) == "1"
        cur = nxt + dt.timedelta(days=1)
    return sorted(out.items())


def dolt_latest_eod() -> str | None:
    rows = dolt_sql("SELECT MAX(tradedate) AS latest FROM final_a_stock_eod_price", timeout=180)
    v = rows[0].get("latest") if rows else None
    return str(v)[:10] if v else None


def tencent_index(symbol: str, count: int = 10) -> list[tuple[str, float]]:
    r = fetch(TENCENT_KLINE, params={"param": f"{symbol},day,,,{count},qfq"}, headers={"Referer": "https://gu.qq.com/"},
              timeout=15)
    try:
        d = r.json()
    except ValueError:
        raise FetchError("tencent_not_json") from None
    node = ((d.get("data") or {}).get(symbol) or {})
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 3 and _DATE.match(str(row[0])):
            out.append((row[0], float(row[2])))
    if not out:
        raise FetchError("tencent_empty")
    return out


def sina_index(symbol: str, count: int = 10) -> list[tuple[str, float]]:
    r = fetch(SINA_KLINE, params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": count},
              headers={"Referer": "https://finance.sina.com.cn/"}, timeout=15)
    try:
        rows = json.loads(r.text())
    except ValueError:
        raise FetchError("sina_not_json") from None
    out = [(x["day"][:10], float(x["close"])) for x in rows or [] if isinstance(x, dict) and _DATE.match(str(x.get("day", ""))[:10])]
    if not out:
        raise FetchError("sina_empty")
    return out


def eastmoney_names(page_size: int = 100, max_pages: int = 80) -> dict[str, str]:
    """Fallback A-share code -> name from Eastmoney clist (SH/SZ/BJ). Returns {SH600000: '浦发银行', ...}."""
    names: dict[str, str] = {}
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    for pn in range(1, max_pages + 1):
        r = fetch(EM_CLIST, params={"pn": pn, "pz": page_size, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
                                    "fs": fs, "fields": "f12,f13,f14"}, headers={"Referer": "https://quote.eastmoney.com/"},
                  timeout=15)
        try:
            d = (r.json() or {}).get("data") or {}
        except ValueError:
            raise FetchError("eastmoney_not_json") from None
        diff = d.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        for x in diff:
            code, mkt, name = str(x.get("f12", "")), x.get("f13"), str(x.get("f14", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            ex = "SH" if mkt == 1 else ("BJ" if code[0] in "48" or code.startswith("92") else "SZ")
            names[ex + code] = name
        total = int(d.get("total") or 0)
        if not diff or pn * page_size >= total:
            break
    if not names:
        raise FetchError("eastmoney_empty")
    return names


def tencent_names(symbols: list[str], batch_size: int = 200, min_ratio: float = 0.9) -> dict[str, str]:
    """Primary code -> name map from Tencent quote (GBK), batched.

    Only the instruments qp already knows are requested; this is not a full-market
    listing.  Eastmoney clist remains the fallback, but it can reject the production
    egress path while this endpoint stays reachable.  A response that covers less than
    ``min_ratio`` of the requested symbols is treated as a partial failure (delisted
    codes are a handful, not a whole batch).
    """
    wanted = list(dict.fromkeys(s.upper() for s in symbols if _TX_SYMBOL.match(s.upper())))
    names: dict[str, str] = {}
    for i in range(0, len(wanted), batch_size):
        batch = wanted[i:i + batch_size]
        q = ",".join(s.lower() for s in batch)
        for attempt in (1, 2):
            try:
                text = fetch(TENCENT_QUOTE + q, headers={"Referer": "https://gu.qq.com/"}, timeout=15).text("gbk")
                break
            except FetchError as e:
                if attempt == 2:
                    raise FetchError(f"tencent_names_batch_{i // batch_size + 1}:{e}") from None
                time.sleep(1)
        for code, payload in _TX_QUOTE.findall(text):
            parts = payload.split("~")
            if len(parts) > 1 and parts[1].strip():
                names[code[:2].upper() + code[2:]] = parts[1].strip()
    if not names:
        raise FetchError("tencent_names_empty")
    if len(names) < len(wanted) * min_ratio:
        raise FetchError(f"tencent_names_partial:{len(names)}/{len(wanted)}")
    return names


def cninfo_count(day: dt.date) -> int:
    """Total announcements for one day across SZSE+SSE (legacy collector is direct, no proxy)."""
    r = fetch(CNINFO_QUERY, data={"pageNum": 1, "pageSize": 1, "column": "szse", "tabName": "fulltext", "plate": "",
                                  "stock": "", "searchkey": "", "secid": "", "category": "", "trade": "",
                                  "seDate": f"{day}~{day}", "sortName": "", "sortType": "", "isHLtitle": "true"},
              headers={"Referer": "https://www.cninfo.com.cn/new/disclosure", "Origin": "https://www.cninfo.com.cn",
                       "X-Requested-With": "XMLHttpRequest"}, proxy=False, timeout=20)
    try:
        d = r.json()
    except ValueError:
        raise FetchError("cninfo_not_json") from None
    total = d.get("totalAnnouncement", d.get("totalRecordNum"))
    if not isinstance(total, int):
        raise FetchError("cninfo_shape")
    return total


def github_release_tag() -> str | None:
    r = fetch(GH_RELEASE, headers={"Accept": "application/vnd.github+json"}, timeout=20)
    return (r.json() or {}).get("tag_name")


def _retryable_dolt_error(e: FetchError) -> bool:
    """Only retry transport/5xx failures; a rejected query should fail immediately."""
    code = str(e)
    return code in {"TimeoutError", "URLError", "ConnectionError", "OSError", "ConnectionResetError",
                    "RemoteDisconnected"} or code.startswith("http_5")


def probe_dolt() -> dict[str, Any]:
    """Dolt reachability probe aligned with the real fetch budget (180 s, one retry)."""
    last: FetchError | None = None
    for attempt in range(1, DOLT_PROBE_ATTEMPTS + 1):
        try:
            rows = dolt_sql("SELECT 1 AS one", timeout=DOLT_PROBE_TIMEOUT)
            return {"rows": len(rows), "attempts": attempt}
        except FetchError as e:
            last = e
            if attempt >= DOLT_PROBE_ATTEMPTS or not _retryable_dolt_error(e):
                raise
            time.sleep(2)
    raise last or FetchError("dolt_probe_failed")


def probe_all() -> list[dict[str, Any]]:
    """Light reachability + freshness probe for each legacy data source. One request each."""
    out: list[dict[str, Any]] = []

    def run(name: str, proxy: bool, f):
        t0 = time.monotonic()
        try:
            detail = f() or {}
            out.append({"source": name, "ok": True, "via_proxy": proxy, "ms": round((time.monotonic() - t0) * 1000, 1),
                        "detail": detail})
        except FetchError as e:
            out.append({"source": name, "ok": False, "via_proxy": proxy, "ms": round((time.monotonic() - t0) * 1000, 1),
                        "detail": {"error": str(e)}})
        except Exception as e:  # noqa: BLE001 - classify only
            out.append({"source": name, "ok": False, "via_proxy": proxy, "ms": round((time.monotonic() - t0) * 1000, 1),
                        "detail": {"error": type(e).__name__}})

    # The names check keeps the Tencent primary path (and its Eastmoney fallback claim) honest.
    run("tencent", True, lambda: {"last": tencent_index("sh000001", 2)[-1][0],
                                  "names": len(tencent_names(["SH600000", "SZ000001", "BJ430017"]))})
    run("sina", True, lambda: {"last": sina_index("sh000001", 2)[-1][0]})
    run("akshare_sina_calendar", True, lambda: {"bytes": len(fetch(SINA_CAL, timeout=15).body)})
    run("eastmoney", True, lambda: {"total": (fetch(EM_CLIST, params={"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                                                                     "fid": "f12", "fs": "m:1+t:2", "fields": "f12"},
                                                    headers={"Referer": "https://quote.eastmoney.com/"},
                                                    timeout=15).json().get("data") or {}).get("total")})
    run("cninfo", False, lambda: {"today_total": cninfo_count(dt.date.today())})
    run("dolt", True, probe_dolt)
    run("github_release", True, lambda: {"tag": github_release_tag()})

    def bao():
        ok, ms, err = tcp_probe(*BAOSTOCK)
        if not ok:
            raise FetchError(err or "tcp_failed")
        return {"tcp": True}
    run("baostock", False, bao)
    return out
