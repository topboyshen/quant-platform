"""Macro inputs for M4 (SHADOW): market total amount, margin balance, limit counts,
and the VIX / US10Y / crude risk state.

All parsers are pure functions so fixtures can lock the exact legacy semantics:

- total amount: Tencent (万元, quote date must equal the last bar) -> Sina (元, both
  indexes, GB18030) -> Eastmoney full-market snapshot (元); accepted range 1,000-80,000 亿
- margin: SSE queryMargin + SZSE ShowReport, same-date pair, 亿元
- limits: Eastmoney ZT/DT pools, ``data.tc``
- risk: Yahoo chart -> CBOE/FRED fallbacks; strict thresholds and fail-closed levels
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote as urlquote

from qp.market import http

TENCENT_QUOTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list=sh000001,sz399001"
EASTMONEY_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SSE_MARGIN_URL = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
SZSE_MARGIN_URL = "https://www.szse.cn/api/report/ShowReport/data"
ZT_POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
DT_POOL_URL = "https://push2ex.eastmoney.com/getTopicDTPool"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

TOTAL_AMOUNT_BOUNDS = (1000.0, 80000.0)
MARGIN_MAX_AGE_DAYS = 4
VIX_CRITICAL = 17.5
US10Y_BLOCK = 4.55
CRUDE_OIL_BLOCK = 88.5
RISK_STATE_MAX_AGE_SEC = 300
RISK_OBSERVATION_TTL_SEC = 900
FRED_MAX_AGE_SEC = 5 * 86400

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@dataclass
class FactorStatus:
    available: bool
    source: str = ""
    message: str = ""
    observed_date: str | None = None
    stale: bool = False
    anomalous: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "source": self.source, "message": self.message,
                "observed_date": self.observed_date, "stale": self.stale, "anomalous": self.anomalous}


# --------------------------------------------------------------------------- total amount
def _finite(value: Any, *, low: float = 0.0, high: float = math.inf, strict: bool = False) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and (number > low if strict else number >= low) and number <= high


def parse_tencent_total(payload: dict[str, Any]) -> tuple[float, str]:
    """Sum SH/SZ amounts (万元); both quote dates must equal both last bars."""
    data = payload.get("data") or {}
    total_wan = 0.0
    observed_dates: list[str] = []
    for code in ("sh000001", "sz399001"):
        entry = data.get(code) or {}
        quote = (entry.get("qt") or {}).get(code) or []
        if len(quote) < 38 or not re.fullmatch(r"\d{14}", str(quote[30] or "")):
            raise ValueError("bad_quote_time")
        quote_date = dt.datetime.strptime(str(quote[30]), "%Y%m%d%H%M%S").date().isoformat()
        amount_wan = float(quote[37])
        if not math.isfinite(amount_wan) or amount_wan <= 0:
            raise ValueError("bad_amount")
        bars = entry.get("qfqday") or entry.get("day") or []
        if not bars or not bars[-1]:
            raise ValueError("missing_bar")
        observed = str(bars[-1][0])
        if dt.date.fromisoformat(observed).isoformat() != observed:
            raise ValueError("bad_bar_date")
        if quote_date != observed:
            raise ValueError("date_mismatch")
        total_wan += amount_wan
        observed_dates.append(observed)
    if len(set(observed_dates)) != 1:
        raise ValueError("date_mismatch")
    return round(total_wan / 10000.0, 2), observed_dates[0]


def parse_sina_total(text: str) -> tuple[float, str]:
    """Parse ``var hq_str_sh000001="...";`` lines; field 9 is cumulative amount in 元."""
    components: dict[str, tuple[str, float]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r'var hq_str_(sh000001|sz399001)="([^"\r\n]*)";', line)
        if match is None:
            raise ValueError("unexpected_line")
        code, payload = match.groups()
        if code in components:
            raise ValueError("duplicate_component")
        fields = payload.split(",")
        if len(fields) < 32 or not fields[0].strip():
            raise ValueError("incomplete_component")
        amount = float(fields[9])
        if not _finite(amount, strict=True):
            raise ValueError("bad_component_amount")
        observed = fields[30].strip()
        if dt.date.fromisoformat(observed).isoformat() != observed:
            raise ValueError("bad_component_date")
        components[code] = (observed, amount)
    if set(components) != {"sh000001", "sz399001"}:
        raise ValueError("incomplete_component_set")
    dates = {value[0] for value in components.values()}
    if len(dates) != 1:
        raise ValueError("component_dates_differ")
    return round(sum(value[1] for value in components.values()) / 1e8, 2), dates.pop()


def fetch_tencent_total(fetch_fn: Callable[..., Any] = http.fetch) -> tuple[float, str]:
    payloads = {}
    for code in ("sh000001", "sz399001"):
        payloads[code] = fetch_fn(
            TENCENT_QUOTE_URL, params={"param": f"{code},day,,,3,qfq"},
            headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, proxy=False, timeout=8,
        ).json()
    merged: dict[str, Any] = {"data": {}}
    for code, payload in payloads.items():
        entry = (payload.get("data") or {}).get(code) or {}
        merged["data"][code] = entry
    return parse_tencent_total(merged)


def fetch_sina_total(fetch_fn: Callable[..., Any] = http.fetch) -> tuple[float, str]:
    resp = fetch_fn(SINA_QUOTE_URL, headers={
        "Accept": "application/javascript,text/plain;q=0.9",
        "Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0",
    }, proxy=False, timeout=8)
    return parse_sina_total(resp.body.decode("gb18030"))


def _eastmoney_snapshot_params(page: int, size: int) -> dict[str, str]:
    return {
        "pn": str(page), "pz": str(size), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f6",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81,m:1+t:1,m:0+t:7,m:1+t:3",
        "fields": "f12,f6", "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }


def fetch_eastmoney_total(fetch_fn: Callable[..., Any] = http.fetch,
                          observed_date: str | None = None) -> tuple[float, str]:
    first = fetch_fn(EASTMONEY_LIST_URL, params=_eastmoney_snapshot_params(1, 1),
                     headers={"User-Agent": UA}, proxy=False, timeout=15).json()
    total_raw = ((first.get("data") or {}).get("total"))
    if total_raw is None:
        raise ValueError("missing_total")
    pages = max(1, (int(total_raw) + 4999) // 5000)
    total_yuan = 0.0
    seen: set[str] = set()
    for page in range(1, pages + 1):
        payload = fetch_fn(EASTMONEY_LIST_URL, params=_eastmoney_snapshot_params(page, 5000),
                           headers={"User-Agent": UA}, proxy=False, timeout=15).json()
        data = payload.get("data")
        if not isinstance(data, dict) or not (data.get("diff") or []):
            raise ValueError("incomplete_page")
        for row in data["diff"]:
            code = str(row.get("f12") or "").strip()
            if not code or not code.startswith(("0", "3", "6")) or code in seen:
                continue
            amount = row.get("f6")
            if not _finite(amount):
                continue
            seen.add(code)
            total_yuan += float(amount)
    if len(seen) < 1000:
        raise ValueError("incomplete_universe")
    return round(total_yuan / 1e8, 2), observed_date or dt.date.today().isoformat()


def collect_total_amount(decision_date: str, fetch_fn: Callable[..., Any] = http.fetch) -> tuple[float | None, FactorStatus]:
    """Mirror legacy order: Tencent (stale allowed) -> Sina (same-day only) -> Eastmoney.

    Out-of-range values are rejected from the slice and only leave ``anomalous`` on the
    final status, matching ``MacroSnapshotCollector._collect_total_amount``.
    """
    errors: list[str] = []
    low, high = TOTAL_AMOUNT_BOUNDS
    for name, fn in (("tencent", fetch_tencent_total), ("sina", fetch_sina_total)):
        try:
            value, observed = fn(fetch_fn)
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
            continue
        if not low <= value <= high:
            errors.append(f"{name}_anomalous")
            continue
        if name == "sina" and observed != decision_date:
            # Real-time Sina quotes may only be labelled as the collection day.
            errors.append("sina:observed_date_mismatch")
            continue
        stale = observed != decision_date
        return value, FactorStatus(True, name, "" if not stale else "observed_date_mismatch",
                                   observed, stale=stale)
    try:
        value, observed = fetch_eastmoney_total(fetch_fn, decision_date)
        if low <= value <= high:
            return value, FactorStatus(True, "eastmoney", "", observed, stale=observed != decision_date)
        errors.append("eastmoney_anomalous")
    except Exception as exc:
        errors.append(f"eastmoney:{type(exc).__name__}")
    return None, FactorStatus(False, "tencent/sina/eastmoney", ";".join(errors)[:200],
                              anomalous=bool(errors))


# --------------------------------------------------------------------------- margin
def parse_sse_margin(payload: dict[str, Any]) -> list[tuple[str, float]]:
    rows = payload.get("result") or []
    out: list[tuple[str, float]] = []
    for row in rows:
        raw_date = str(row.get("opDate") or "")
        if not re.fullmatch(r"\d{8}", raw_date):
            continue
        balance = row.get("rzrqjyzl")
        if not _finite(balance, strict=True):
            continue
        out.append((f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}", round(float(balance) / 1e8, 2)))
    return sorted(out, reverse=True)


def parse_szse_margin(payload: Any) -> float:
    if not isinstance(payload, list) or not payload:
        raise ValueError("empty_payload")
    rows = payload[0].get("data") if isinstance(payload[0], dict) else None
    if not rows:
        raise ValueError("empty_rows")
    raw = str(rows[0].get("jrrzrjye") or "").replace(",", "")
    value = float(raw)
    if not _finite(value, strict=True):
        raise ValueError("bad_balance")
    return round(value, 2)


def fetch_margin_history(fetch_fn: Callable[..., Any] = http.fetch, *, days: int = 21,
                         max_dates: int = 6) -> list[tuple[str, float]]:
    today = dt.date.today()
    start = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    sse = fetch_fn(SSE_MARGIN_URL, params={
        "isPagination": "true", "beginDate": start, "endDate": today.strftime("%Y%m%d"),
        "tabType": "", "stockCode": "", "pageHelp.pageSize": "5000", "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1", "pageHelp.cacheSize": "1", "pageHelp.endPage": "5",
    }, headers={"User-Agent": UA, "Referer": "https://www.sse.com.cn/"}, proxy=False, timeout=15).json()
    sse_rows = parse_sse_margin(sse)
    out: list[tuple[str, float]] = []
    for observed, balance in sse_rows[:max_dates]:
        try:
            szse = fetch_fn(SZSE_MARGIN_URL, params={
                "SHOWTYPE": "JSON", "CATALOGID": "1837_xxpl", "txtDate": observed,
                "tab1PAGENO": "1", "random": "0.7425245522795993",
            }, headers={"User-Agent": UA,
                        "Referer": "https://www.szse.cn/disclosure/margin/object/index.html"},
               proxy=False, timeout=15).json()
            out.append((observed, round(balance + parse_szse_margin(szse), 2)))
        except Exception:
            continue
    return out


def collect_margin(decision_date: str, history: list[tuple[str, float]] | None,
                   error: str = "") -> tuple[float | None, float | None, FactorStatus, FactorStatus]:
    """Return balance/growth plus their statuses from a descending same-date history."""
    if not history:
        status = FactorStatus(False, "", error or "no_complete_pair")
        return None, None, status, FactorStatus(False, "", error or "no_previous_pair")
    observed, balance = history[0]
    age = (dt.date.fromisoformat(decision_date) - dt.date.fromisoformat(observed)).days
    stale = age < 0 or age > MARGIN_MAX_AGE_DAYS
    balance_status = FactorStatus(True, "sse+szse", "" if not stale else "publication_lag",
                                  observed, stale=stale)
    growth = None
    growth_message = "missing_previous_pair"
    growth_anomalous = False
    if len(history) >= 2:
        prev = history[1][1]
        if prev > 0:
            candidate = round((balance - prev) / prev * 100, 3)
            if -50 <= candidate <= 50:
                growth = candidate
            else:
                growth_message = "out_of_range"
                growth_anomalous = True
    growth_status = FactorStatus(growth is not None, "sse+szse",
                                 "" if growth is not None else growth_message,
                                 observed, stale=stale, anomalous=growth_anomalous)
    return balance, growth, balance_status, growth_status


# --------------------------------------------------------------------------- limits
def parse_limit_pool(payload: Any) -> int:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("missing_data")
    if "tc" in data and isinstance(data["tc"], int) and data["tc"] >= 0:
        return data["tc"]
    pool = data.get("pool")
    if isinstance(pool, list):
        return len(pool)
    raise ValueError("missing_count")


def fetch_limit_counts(decision_date: str, fetch_fn: Callable[..., Any] = http.fetch) -> tuple[int, int]:
    day = decision_date.replace("-", "")
    counts = []
    for url in (ZT_POOL_URL, DT_POOL_URL):
        payload = fetch_fn(url, params={
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": "0",
            "pagesize": "1", "sort": "fbt:asc", "date": day,
        }, headers={"User-Agent": UA}, proxy=False, timeout=15).json()
        counts.append(parse_limit_pool(payload))
    return counts[0], counts[1]


def collect_limits(decision_date: str, fetch_fn: Callable[..., Any] = http.fetch) -> tuple[int | None, int | None, FactorStatus]:
    try:
        up, down = fetch_limit_counts(decision_date, fetch_fn)
    except Exception as exc:
        return None, None, FactorStatus(False, "", f"eastmoney:{type(exc).__name__}")
    if up + down == 0:
        # Legacy rejects 0/0 because a non-trading day is indistinguishable from a
        # complete pool; both sides must be present and non-empty.
        return None, None, FactorStatus(False, "eastmoney", "empty_pool")
    return up, down, FactorStatus(True, "eastmoney", "", decision_date)


# --------------------------------------------------------------------------- orchestration
def collect_macro_slice(decision_date: str, *, fetch_fn: Callable[..., Any] = http.fetch,
                        margin_history: list[tuple[str, float]] | None = None,
                        margin_error: str = "") -> dict[str, Any]:
    total, total_status = collect_total_amount(decision_date, fetch_fn)
    if margin_history is None:
        try:
            margin_history = fetch_margin_history(fetch_fn)
        except Exception as exc:
            margin_error = f"sse+szse:{type(exc).__name__}"
            margin_history = []
    balance, growth, margin_status, growth_status = collect_margin(
        decision_date, margin_history, margin_error)
    up, down, limit_status = collect_limits(decision_date, fetch_fn)
    ratio = round(up / (down + 1), 3) if up is not None and down is not None else None
    if ratio is not None and not 0 <= ratio <= 1000:
        ratio = None
    sources = [s.source for s in (total_status, margin_status, limit_status) if s.available]
    return {
        "decision_date": decision_date,
        "total_amount": total,
        "margin_balance": balance,
        "margin_growth_pct": growth,
        "limit_up": up,
        "limit_down": down,
        "limit_ratio": ratio,
        "source": "+".join(dict.fromkeys(sources)) or "none",
        "collect_status": {
            "total_amount": total_status.as_dict(),
            "margin_balance": margin_status.as_dict(),
            "margin_growth_pct": growth_status.as_dict(),
            "limit_ratio": limit_status.as_dict(),
        },
    }


# --------------------------------------------------------------------------- risk state
def _risk_meta(source_ts: float, source: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {"source_timestamp": float(source_ts), "available_at": now, "source": source}


def fetch_yahoo_value(symbol: str, fetch_fn: Callable[..., Any] = http.fetch) -> tuple[float, dict[str, Any]]:
    payload = fetch_fn(f"{YAHOO_URL}{urlquote(symbol, safe='')}",
                       params={"interval": "1d", "range": "1d"},
                       headers={"User-Agent": UA}, proxy=True, timeout=8).json()
    result = payload["chart"]["result"][0]
    price = (result.get("meta") or {}).get("regularMarketPrice")
    if price is not None and _finite(price, strict=True):
        source_ts = float((result.get("meta") or {}).get("regularMarketTime") or time.time())
        return float(price), _risk_meta(source_ts, "yahoo_chart_regular_market")
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    timestamps = result.get("timestamp") or []
    for index, close in enumerate(reversed(closes)):
        if close is not None and _finite(close, strict=True):
            source_ts = float(timestamps[-(index + 1)]) if index < len(timestamps) else time.time()
            return float(close), _risk_meta(source_ts, "yahoo_chart_daily_close")
    raise ValueError("no_price")


def fetch_cboe_vix(fetch_fn: Callable[..., Any] = http.fetch, *, now: float | None = None) -> tuple[float, dict[str, Any]]:
    now = time.time() if now is None else now
    payload = fetch_fn(CBOE_VIX_URL, headers={"User-Agent": UA}, proxy=False, timeout=8).json()
    data = payload["data"]
    value = float(data["current_price"])
    source_ts = dt.datetime.strptime(str(payload["timestamp"]), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc).timestamp()
    age = now - source_ts
    if not _finite(value, low=0.0, high=200.0, strict=True) or age < -300 or age > 4 * 86400:
        raise ValueError("stale_or_out_of_range")
    return value, _risk_meta(source_ts, "cboe_vix_delayed", now)


def fetch_fred_value(series_id: str, fetch_fn: Callable[..., Any] = http.fetch,
                     *, now: float | None = None) -> tuple[float, dict[str, Any]]:
    now = time.time() if now is None else now
    start = dt.datetime.fromtimestamp(now - 14 * 86400, tz=dt.timezone.utc).date().isoformat()
    resp = fetch_fn(FRED_URL, params={"id": series_id, "cosd": start},
                    headers={"User-Agent": UA}, proxy=False, timeout=8)
    latest: tuple[float, float] | None = None
    for row in csv.DictReader(io.StringIO(resp.text())):
        date_text = str(row.get("observation_date") or row.get("DATE") or "").strip()
        value_text = str(row.get(series_id) or "").strip()
        if not date_text or not value_text or value_text == ".":
            continue
        value = float(value_text)
        upper = {"VIXCLS": 200.0, "DGS10": 25.0, "DCOILWTICO": 500.0}.get(series_id, 1_000_000.0)
        if not _finite(value, low=0.0, high=upper, strict=True):
            continue
        source = dt.date.fromisoformat(date_text)
        ts = dt.datetime.combine(source, dt.time.min, tzinfo=dt.timezone.utc).timestamp()
        if ts > now + 300:
            continue
        if latest is None or ts > latest[0]:
            latest = (ts, value)
    if latest is None or now - latest[0] > FRED_MAX_AGE_SEC:
        raise ValueError("no_recent_observation")
    source = ("fred_dcoilwtico_spot_daily" if series_id == "DCOILWTICO"
              else f"fred_{series_id.lower()}_daily")
    return latest[1], _risk_meta(latest[0], source, now)


def _wti_spot_fallback_enabled() -> bool:
    return os.getenv("QP_RISK_ALLOW_WTI_SPOT_FALLBACK", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def fetch_risk_values(fetch_fn: Callable[..., Any] = http.fetch, *,
                      now: float | None = None) -> dict[str, Any]:
    """Fetch VIX/US10Y/WTI with the same provider order as the legacy controller."""
    now = time.time() if now is None else now
    crude_attempts = [("yahoo", lambda: fetch_yahoo_value("CL=F", fetch_fn))]
    if _wti_spot_fallback_enabled():
        # Legacy gates the spot series behind an explicit env flag; production has it off.
        crude_attempts.append(("fred", lambda: fetch_fred_value("DCOILWTICO", fetch_fn, now=now)))
    plan = {
        "vix": [("yahoo", lambda: fetch_yahoo_value("^VIX", fetch_fn)),
                ("cboe", lambda: fetch_cboe_vix(fetch_fn, now=now)),
                ("fred", lambda: fetch_fred_value("VIXCLS", fetch_fn, now=now))],
        "us_10y": [("yahoo", lambda: fetch_yahoo_value("^TNX", fetch_fn)),
                   ("fred", lambda: fetch_fred_value("DGS10", fetch_fn, now=now))],
        "crude_oil": crude_attempts,
    }
    values: dict[str, float | None] = {}
    sources: dict[str, str] = {}
    source_timestamps: dict[str, float] = {}
    available_at: dict[str, float] = {}
    observed_at: dict[str, float] = {}
    for field, attempts in plan.items():
        values[field] = None
        for _name, attempt in attempts:
            try:
                value, meta = attempt()
            except Exception:
                continue
            values[field] = value
            sources[field] = meta["source"]
            source_timestamps[field] = meta["source_timestamp"]
            available_at[field] = meta["available_at"]
            observed_at[field] = now
            break
    missing = [field for field, value in values.items() if value is None]
    return {"values": values, "sources": sources, "source_timestamps": source_timestamps,
            "observed_at": observed_at, "available_at": available_at,
            "missing_fields": missing, "fetched_at": now}


def evaluate_risk(values: dict[str, float | None], *, previous_state: dict[str, Any] | None = None,
                  now: float | None = None,
                  ttl: float = RISK_OBSERVATION_TTL_SEC) -> dict[str, Any]:
    """Apply the legacy ordering and reuse a previous observation for up to ``ttl``.

    Cache substitution happens before threshold checks, exactly like
    ``MacroRiskControl.check``; ``missing_fields`` therefore excludes reused fields.
    """
    now = time.time() if now is None else now
    previous = previous_state or {}
    previous_observed = previous.get("observed_at") or {}
    effective: dict[str, float | None] = {}
    cached_fields: list[str] = []
    missing: list[str] = []
    for name in ("vix", "us_10y", "crude_oil"):
        value = values.get(name)
        if value is not None:
            effective[name] = float(value)
            continue
        cached = previous.get(name)
        observed = previous_observed.get(name)
        try:
            age = now - float(observed) if observed is not None else math.inf
        except (TypeError, ValueError):
            age = math.inf
        if cached is not None and math.isfinite(age) and 0 <= age <= ttl:
            effective[name] = float(cached)
            cached_fields.append(name)
        else:
            effective[name] = None
            missing.append(name)

    vix, us_10y, crude = effective["vix"], effective["us_10y"], effective["crude_oil"]
    if vix is not None and vix > VIX_CRITICAL:
        level = "critical"
        message = f"CRITICAL_WARNING: VIX={vix:.2f} > {VIX_CRITICAL}"
    elif vix is None and us_10y is None and crude is None:
        level = "warning"
        message = "DATA_UNAVAILABLE: VIX/US10Y/CRUDE all missing"
    elif (us_10y is not None and us_10y >= US10Y_BLOCK) or (
            crude is not None and crude > CRUDE_OIL_BLOCK):
        triggers = []
        if us_10y is not None and us_10y >= US10Y_BLOCK:
            triggers.append(f"US10Y={us_10y:.2f} >= {US10Y_BLOCK}")
        if crude is not None and crude > CRUDE_OIL_BLOCK:
            triggers.append(f"CRUDE={crude:.2f} > {CRUDE_OIL_BLOCK}")
        level = "growth_blocked"
        message = "GROWTH_BLOCK: " + " | ".join(triggers)
    elif missing:
        level = "warning"
        message = "DATA_PARTIAL: " + ",".join(missing)
    else:
        level = "ok"
        message = "SYSTEM_OK"

    sources = dict(previous.get("sources") or {})
    return {
        "level": level,
        "vix": vix,
        "us_10y": us_10y,
        "crude_oil": crude,
        "message": message,
        "timestamp": now,
        "observed_at": dict(previous_observed),
        "source_timestamps": dict(previous.get("source_timestamps") or {}),
        "available_at": dict(previous.get("available_at") or {}),
        "sources": sources,
        "cached_fields": cached_fields,
        "missing_fields": missing,
    }


def merge_risk_metadata(state: dict[str, Any], fetched: dict[str, Any]) -> dict[str, Any]:
    """Copy fresh provider metadata for fields that were fetched (not cached)."""
    for name in ("vix", "us_10y", "crude_oil"):
        if name in fetched["values"] and fetched["values"][name] is not None:
            state["sources"][name] = fetched["sources"][name]
            state["source_timestamps"][name] = fetched["source_timestamps"][name]
            state["available_at"][name] = fetched["available_at"][name]
            state["observed_at"][name] = fetched["observed_at"][name]
    return state


def risk_state_is_fresh(timestamp: float | None, *, now: float | None = None,
                        max_age: float = RISK_STATE_MAX_AGE_SEC) -> bool:
    now = time.time() if now is None else now
    if not timestamp:
        return False
    try:
        age = now - float(timestamp)
    except (TypeError, ValueError):
        return False
    return -1 <= age <= max_age
