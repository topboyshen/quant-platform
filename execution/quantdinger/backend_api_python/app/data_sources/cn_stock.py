"""
中国A股数据源 — 多层 fallback

有 TWELVE_DATA_API_KEY:
  所有周期 → Twelve Data（主） → 腾讯日/周线 → Sina日线 → yfinance → AkShare

无 API Key:
  分钟/小时 → yfinance → AkShare
  日/周线 → 腾讯 fqkline → Sina → yfinance → AkShare
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.tencent import normalize_cn_code, fetch_quote, parse_quote_to_ticker, fetch_kline, tencent_kline_rows_to_dicts
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


class CNStockDataSource(BaseDataSource):
    """A股数据源（TwelveData + Tencent + Sina + yfinance + AkShare）"""

    name = "CNStock/multi-source"

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_cn_code(symbol)
        parts = fetch_quote(code)
        if not parts:
            return {"last": 0, "symbol": code}
        t = parse_quote_to_ticker(parts)
        return {
            "last": t.get("last", 0),
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 1: Twelve Data (paid, most reliable)
        rows = fetch_twelvedata_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 2: Tencent for daily/weekly (fast, free)
        if tf in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(tf, "day")
            raw_rows = fetch_kline(code, period=period, count=lim, adj="qfq")
            out = tencent_kline_rows_to_dicts(raw_rows)
            if out:
                return self.filter_and_limit(
                    out,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 2.5: Sina daily K (A-share daily only, before yfinance)
        if tf == "1D":
            try:
                sina_rows = self._fetch_sina_daily(code, limit=lim, before_time=before_time)
                if sina_rows:
                    logger.info(f"CNStock Sina daily fallback: {code} {len(sina_rows)} candles")
                    return self.filter_and_limit(
                        sina_rows,
                        limit=lim,
                        before_time=before_time,
                        after_time=after_time,
                        truncate=(after_time is None),
                    )
            except Exception as exc:
                logger.warning(f"CNStock Sina daily failed for {code}: {exc}")

        # Tier 3: yfinance (works when Yahoo not rate-limited)
        rows = fetch_yfinance_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 4: AkShare (fragile overseas, last resort)
        if tf in ("1m", "5m", "15m", "30m", "1H", "4H"):
            rows = fetch_akshare_minute_klines(
                is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
            )
        elif tf == "1W":
            rows = fetch_akshare_weekly_klines(
                is_hk=False, tencent_code=code, limit=lim, before_time=before_time
            )
        else:
            rows = []

        return self.filter_and_limit(
            rows,
            limit=lim,
            before_time=before_time,
            after_time=after_time,
            truncate=(after_time is None),
        )

    def _fetch_sina_daily(self, symbol: str, limit: int, before_time: Optional[int]) -> List[Dict]:
        """Fetch A-share daily K from Sina (JD repair pattern)."""
        import json
        import urllib.request
        from datetime import datetime
        from zoneinfo import ZoneInfo

        url = (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={limit}"
        )
        ua = "Mozilla/5.0 (compatible; QuantDinger/1.0)"
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(200_000)
        rows_json = json.loads(raw.decode("utf-8"))
        if not isinstance(rows_json, list):
            return []
        out = []
        for row in rows_json:
            if not isinstance(row, dict):
                continue
            day = str(row.get("day") or "")[:10]
            if not day or len(day) != 10:
                continue
            try:
                o = float(row.get("open"))
                h = float(row.get("high"))
                l = float(row.get("low"))
                c = float(row.get("close"))
                v = float(row.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            dt = datetime.strptime(day + " 15:00:00", "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
            timestamp = int(dt.timestamp())
            out.append({"time": timestamp, "open": o, "high": h, "low": l, "close": c, "volume": v})
        if before_time is not None:
            out = [r for r in out if r["time"] < before_time]
        return out[-limit:] if out else []
