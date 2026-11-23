"""Daily K-line sources for M4 (SHADOW): Eastmoney -> Tencent -> Sina -> akshare.

Pure stdlib + ``qp.market.http``. The normalisation rules mirror the legacy fetcher:
ascending unique dates (last wins), positive closes, and an exact ``required_date``
check for EOD. A source that only has the previous session is treated as a
publication lag and may trigger one bounded wait per decision date.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from qp.market import http
from qp.market.http import FetchError

EASTMONEY_URL = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
MIN_SOURCE_ROWS = 30
DEFAULT_HISTORY_DAYS = 250

EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://quote.eastmoney.com/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://stockpage.10jqka.com.cn/",
}
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


@dataclass(frozen=True)
class DailyBar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class KlineResult:
    bars: list[DailyBar] = field(default_factory=list)
    source: str = ""
    attempts: list[dict] = field(default_factory=list)
    publication_lag: bool = False


def resolve_secid(code: str) -> str:
    """Eastmoney secid: Shanghai 5/6/11 prefixes, everything else Shenzhen."""
    full = str(code).strip().zfill(6)
    return f"1.{full}" if full[0] in ("5", "6") or full.startswith("11") else f"0.{full}"


def exchange_prefixed_code(code: str) -> str:
    full = str(code).strip().zfill(6)
    return ("sh" if resolve_secid(full).startswith("1.") else "sz") + full


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        raise ValueError("bad_date") from None


def _num(text, *, positive: bool = False) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError("bad_number") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("bad_number")
    return value


def _bar(date: str, o, h, low, c, v) -> DailyBar:
    return DailyBar(
        date=_date(date), open=_num(o), high=_num(h), low=_num(low), close=_num(c, positive=True), volume=_num(v)
    )


def parse_eastmoney_klines(klines: list[str]) -> list[DailyBar]:
    """Rows are \"date,open,close,high,low,volume,...\"; require >= 30 rows."""
    bars: list[DailyBar] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            raise ValueError("malformed_kline")
        bars.append(_bar(parts[0], parts[1], parts[3], parts[4], parts[2], parts[5]))
    if len(bars) < MIN_SOURCE_ROWS:
        raise ValueError("insufficient_rows")
    return bars


def parse_tencent_klines(items: list[list]) -> list[DailyBar]:
    """Rows are [date, open, close, high, low, volume, ...]; require >= 30 rows."""
    bars: list[DailyBar] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            raise ValueError("malformed_kline")
        bars.append(_bar(item[0], item[1], item[3], item[4], item[2], item[5]))
    if len(bars) < MIN_SOURCE_ROWS:
        raise ValueError("insufficient_rows")
    return bars


def parse_sina_klines(items: list[dict]) -> list[DailyBar]:
    """Rows are ``{day, open, high, low, close, volume}`` from Sina."""
    bars: list[DailyBar] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("malformed_kline")
        try:
            bars.append(_bar(item["day"], item["open"], item["high"], item["low"], item["close"], item["volume"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed_kline") from exc
    if len(bars) < MIN_SOURCE_ROWS:
        raise ValueError("insufficient_rows")
    return bars


def normalize_bars(bars: list[DailyBar]) -> list[DailyBar]:
    seen: dict[dt.date, DailyBar] = {}
    for bar in sorted(bars, key=lambda b: b.date):
        if bar.close > 0 and math.isfinite(bar.close):
            seen[bar.date] = bar
    return list(seen.values())


def fetch_eastmoney_bars(code: str, days: int = DEFAULT_HISTORY_DAYS) -> list[DailyBar]:
    params = {
        "secid": resolve_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": dt.date.today().strftime("%Y%m%d"),
        "lmt": str(days + 30),
    }
    try:
        payload = http.fetch(EASTMONEY_URL, params=params, headers=EASTMONEY_HEADERS, proxy=False, timeout=30).json()
    except (FetchError, ValueError, json.JSONDecodeError) as exc:
        raise FetchError(f"eastmoney_kline:{type(exc).__name__}") from None
    klines = (payload.get("data") or {}).get("klines") or []
    try:
        return parse_eastmoney_klines(klines)
    except ValueError as exc:
        raise FetchError(f"eastmoney_kline:{exc}") from None


def fetch_tencent_bars(code: str, days: int = DEFAULT_HISTORY_DAYS) -> list[DailyBar]:
    tencent_code = exchange_prefixed_code(code)
    url = f"{TENCENT_URL}?param={tencent_code},day,,,{days + 30},qfq"
    try:
        payload = http.fetch(url, headers=TENCENT_HEADERS, proxy=False, timeout=30).json()
    except (FetchError, ValueError, json.JSONDecodeError) as exc:
        raise FetchError(f"tencent_kline:{type(exc).__name__}") from None
    if payload.get("code") != 0:
        raise FetchError("tencent_kline:bad_code")
    entry = (payload.get("data") or {}).get(tencent_code) or {}
    items = entry.get("qfqday") or entry.get("day") or []
    try:
        return parse_tencent_klines(items)
    except ValueError as exc:
        raise FetchError(f"tencent_kline:{exc}") from None


def fetch_sina_bars(code: str, days: int = DEFAULT_HISTORY_DAYS) -> list[DailyBar]:
    params = {
        "symbol": exchange_prefixed_code(code),
        "scale": "240",
        "ma": "no",
        "datalen": str(days + 30),
    }
    try:
        payload = http.fetch(SINA_URL, params=params, headers=SINA_HEADERS, proxy=False, timeout=30).json()
    except (FetchError, ValueError, json.JSONDecodeError) as exc:
        raise FetchError(f"sina_kline:{type(exc).__name__}") from None
    if not isinstance(payload, list):
        raise FetchError("sina_kline:bad_payload")
    try:
        return parse_sina_klines(payload)
    except ValueError as exc:
        raise FetchError(f"sina_kline:{exc}") from None


def fetch_akshare_bars(code: str, days: int = DEFAULT_HISTORY_DAYS) -> list[DailyBar]:
    """akshare is optional in the qp runtime; absence fails over, it never fabricates data."""
    raise FetchError("akshare_unavailable")


DEFAULT_SOURCES: list[tuple[str, Callable[[str, int], list[DailyBar]]]] = [
    ("eastmoney_push2his", fetch_eastmoney_bars),
    ("tencent_kline", fetch_tencent_bars),
    ("sina_kline", fetch_sina_bars),
    ("akshare_kline", fetch_akshare_bars),
]


class KlineFetcher:
    """Sequential source fallback with one bounded publication-lag wait per date."""

    def __init__(
        self,
        days: int = DEFAULT_HISTORY_DAYS,
        *,
        wait_seconds: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        sources: list[tuple[str, Callable[[str, int], list[DailyBar]]]] | None = None,
    ):
        self.days = days
        self.wait_seconds = int(os.getenv("QP_EOD_STALE_RETRY_SECONDS", "45")) if wait_seconds is None else wait_seconds
        self.sleep = sleep
        self.sources = sources if sources is not None else DEFAULT_SOURCES
        self._waited: set[dt.date] = set()

    def get(
        self, code: str, *, required_date: dt.date | None = None, previous_session: dt.date | None = None
    ) -> KlineResult:
        result = self._attempt(code, required_date, previous_session)
        if (
            result.bars
            or not result.publication_lag
            or required_date is None
            or self.wait_seconds <= 0
            or required_date in self._waited
        ):
            return result
        self._waited.add(required_date)
        self.sleep(self.wait_seconds)
        return self._attempt(code, required_date, previous_session)

    def _attempt(self, code: str, required_date: dt.date | None, previous_session: dt.date | None) -> KlineResult:
        out = KlineResult()
        for name, fetch in self.sources:
            attempt = {"source": name, "rows": 0, "latest_date": None, "error": ""}
            try:
                bars = normalize_bars(fetch(code, self.days))
            except Exception as exc:  # source isolation: one adapter never kills the run
                attempt["error"] = type(exc).__name__
                out.attempts.append(attempt)
                continue
            attempt["rows"], attempt["latest_date"] = len(bars), (bars[-1].date.isoformat() if bars else None)
            out.attempts.append(attempt)
            if not bars:
                attempt["error"] = "empty"
                continue
            if required_date is not None and bars[-1].date != required_date:
                if previous_session is not None and bars[-1].date == previous_session:
                    out.publication_lag = True
                attempt["error"] = "date_mismatch"
                continue
            out.bars, out.source = bars, name
            return out
        return out
