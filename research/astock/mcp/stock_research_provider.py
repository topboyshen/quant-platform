"""Read-only, normalized AkShare provider for stock-research MCP tools.

The provider deliberately exposes a small fixed surface instead of forwarding
arbitrary AkShare function names or arguments.  Public methods accept only a
validated A-share symbol, ISO dates, bounded limits, and documented enum
values.  AkShare itself is imported lazily so unit tests can inject a fake API
without network access or an installed AkShare package.

Every response uses the same evidence envelope and is safe for strict JSON
serialization: non-finite pandas/numpy values become ``None``; every numeric
record carries ``dataAsOf``, ``units`` and one or more ``sourceIds``; upstream
gaps are reported in ``warnings`` rather than silently fabricated.
"""

from __future__ import annotations

import hashlib
import math
import re
import sys
from contextlib import redirect_stdout
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "1.0"
PROVIDER_NAME = "akshare"
CHINA_TZ = ZoneInfo("Asia/Shanghai")

MAX_PRICE_LIMIT = 500
MAX_NEWS_LIMIT = 100
MAX_SECTOR_LIMIT = 100
MAX_CAPITAL_FLOW_LIMIT = 100
MAX_FINANCIAL_REPORTS = 20
MAX_INSTITUTION_HOLDINGS = 100
MAX_MAJOR_SHAREHOLDERS = 100
MAX_DATE_SPAN_DAYS = 20 * 366

VALID_ADJUSTMENTS = {"", "qfq", "hfq"}
VALID_SECTOR_TYPES = {"industry", "concept"}
VALID_FLOW_INDICATORS = {"今日", "3日", "5日", "10日"}

PRICE_UNITS = {
    "open": "CNY_per_share",
    "close": "CNY_per_share",
    "high": "CNY_per_share",
    "low": "CNY_per_share",
    "volume": "lot_100_shares",
    "turnover": "CNY",
    "amplitude": "percentage_point",
    "changePercent": "percentage_point",
    "change": "CNY_per_share",
    "turnoverRate": "percentage_point",
}

TECHNICAL_UNITS = {
    "ma5": "CNY_per_share",
    "ma10": "CNY_per_share",
    "ma20": "CNY_per_share",
    "ma60": "CNY_per_share",
    "rsi14": "percentage_point",
    "macdDif": "CNY_per_share",
    "macdDea": "CNY_per_share",
    "macdHistogram": "CNY_per_share",
    "volumeMa5": "lot_100_shares",
    "volumeRatio5": "ratio",
}

FINANCIAL_UNITS = {
    "eps": "CNY_per_share",
    "deductedEps": "CNY_per_share",
    "dilutedEps": "CNY_per_share",
    "bookValuePerShare": "CNY_per_share",
    "operatingCashFlowPerShare": "CNY_per_share",
    "revenue": "CNY",
    "grossProfit": "CNY",
    "parentNetProfit": "CNY",
    "deductedNetProfit": "CNY",
    "revenueYoY": "percentage_point",
    "parentNetProfitYoY": "percentage_point",
    "deductedNetProfitYoY": "percentage_point",
    "roeWeighted": "percentage_point",
    "grossMargin": "percentage_point",
    "netMargin": "percentage_point",
    "debtRatio": "percentage_point",
}

INSTITUTION_UNITS = {
    "sharesHeld": "ten_thousand_shares",
    "latestSharesHeld": "ten_thousand_shares",
    "holdingRatio": "percentage_point",
    "latestHoldingRatio": "percentage_point",
    "floatShareRatio": "percentage_point",
    "latestFloatShareRatio": "percentage_point",
    "holdingRatioChange": "percentage_point",
    "floatShareRatioChange": "percentage_point",
}

SHAREHOLDER_UNITS = {
    "sharesHeld": "share",
    "holdingRatio": "percentage_point",
    "shareholderCount": "count",
    "averageSharesHeld": "share",
}

SECTOR_UNITS = {
    "last": "index_point",
    "change": "index_point",
    "changePercent": "percentage_point",
    "marketCap": "CNY",
    "turnoverRate": "percentage_point",
    "advancers": "company_count",
    "decliners": "company_count",
    "leaderChangePercent": "percentage_point",
}

CAPITAL_FLOW_UNITS = {
    "close": "CNY_per_share",
    "last": "CNY_per_share",
    "changePercent": "percentage_point",
    "mainNetInflow": "CNY",
    "mainNetInflowRatio": "percentage_point",
    "superLargeNetInflow": "CNY",
    "superLargeNetInflowRatio": "percentage_point",
    "largeNetInflow": "CNY",
    "largeNetInflowRatio": "percentage_point",
    "mediumNetInflow": "CNY",
    "mediumNetInflowRatio": "percentage_point",
    "smallNetInflow": "CNY",
    "smallNetInflowRatio": "percentage_point",
}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if type(value).__name__ in {"NAType", "NaTType"}:
        return True
    if isinstance(value, float):
        return not math.isfinite(value)
    try:
        marker = value != value
        return isinstance(marker, bool) and marker
    except Exception:
        return False


def sanitize_json(value: Any) -> Any:
    """Convert pandas/numpy scalars and non-finite values to strict JSON data."""

    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, datetime):
        parsed = _aware_china(value)
        return parsed.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "to_pydatetime"):
        try:
            return sanitize_json(value.to_pydatetime())
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return sanitize_json(value.item())
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        value = value.to_dict(orient="records")
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"预期表格或记录列表，实际为 {type(value).__name__}")
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _pick(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and not _is_missing(row[name]):
            return row[name]
    return None


def _text(value: Any, *, maximum: int | None = None) -> str | None:
    if _is_missing(value):
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:maximum] if maximum is not None else result


def _number(value: Any) -> int | float | None:
    if _is_missing(value) or isinstance(value, bool):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if _is_missing(value) or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


def _aware_china(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ)


def _parse_upstream_date(value: Any) -> date | None:
    if _is_missing(value):
        return None
    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except Exception:
            pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("/", "-")
    for candidate in (raw, raw[:10]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            pass
    if re.fullmatch(r"\d{8}", raw):
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            pass
    return None


def _parse_upstream_datetime(value: Any) -> datetime | None:
    if _is_missing(value):
        return None
    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except Exception:
            pass
    if isinstance(value, datetime):
        return _aware_china(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, CHINA_TZ)
    raw = str(value).strip().replace("/", "-").replace("Z", "+00:00")
    try:
        return _aware_china(datetime.fromisoformat(raw))
    except ValueError:
        parsed_date = _parse_upstream_date(raw)
        return datetime.combine(parsed_date, time.min, CHINA_TZ) if parsed_date else None


def _market_close(day: date) -> datetime:
    return datetime.combine(day, time(15, 0), CHINA_TZ)


def _iso(value: datetime | None) -> str | None:
    return _aware_china(value).isoformat(timespec="seconds") if value else None


def _latest_iso(values: Sequence[str | None]) -> str | None:
    parsed: list[datetime] = []
    for value in values:
        item = _parse_upstream_datetime(value)
        if item is not None:
            parsed.append(item)
    return _iso(max(parsed)) if parsed else None


def _validate_iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} 不是有效日期") from exc


def _validate_limit(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < 1 or value > maximum:
        raise ValueError(f"{name} 必须在 1 到 {maximum} 之间")
    return value


def _infer_market(code: str) -> str | None:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    if code.startswith(("4", "8", "920")):
        return "bj"
    return None


def normalize_stock_symbol(value: Any) -> tuple[str, str]:
    """Validate one A-share stock symbol and return ``(code, market)``.

    Accepted forms are ``600519``, ``sh600519`` and ``600519.SH`` (case
    insensitive).  The exchange marker, if supplied, must match the code.  No
    fuzzy digit extraction is used, which keeps command/path-like strings out.
    """

    if not isinstance(value, str) or value != value.strip():
        raise ValueError("symbol 必须是严格的 A 股代码字符串")
    match = re.fullmatch(
        r"(?:(?P<prefix>sh|sz|bj)(?P<prefixed>\d{6})|(?P<plain>\d{6})|"
        r"(?P<suffixed>\d{6})\.(?P<suffix>sh|sz|bj))",
        value.lower(),
    )
    if not match:
        raise ValueError("symbol 仅支持六位 A 股代码及可选的 sh/sz/bj 市场标识")
    code = match.group("prefixed") or match.group("plain") or match.group("suffixed")
    market = _infer_market(code)
    if market is None:
        raise ValueError("symbol 不是受支持的沪深京 A 股代码")
    supplied_market = match.group("prefix") or match.group("suffix")
    if supplied_market is not None and supplied_market != market:
        raise ValueError("symbol 的市场标识与股票代码不一致")
    return code, market


def _parse_quarter(value: Any) -> tuple[str, str, date]:
    if not isinstance(value, str):
        raise ValueError("quarter 必须是 YYYYQ1 至 YYYYQ4 格式")
    match = re.fullmatch(r"(?P<year>\d{4})Q(?P<quarter>[1-4])", value)
    if not match:
        raise ValueError("quarter 必须是 YYYYQ1 至 YYYYQ4 格式")
    year = int(match.group("year"))
    quarter = int(match.group("quarter"))
    if year < 2005 or year > 9999:
        raise ValueError("quarter 年份必须不早于 2005")
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    return value, f"{year}{quarter}", date(year, *month_day)


def _quarter_for_date(day: date) -> tuple[str, str, date]:
    quarter = (day.month - 1) // 3 + 1
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    period_end = date(day.year, *month_day)
    return f"{day.year}Q{quarter}", f"{day.year}{quarter}", period_end


def _latest_completed_quarter(day: date) -> tuple[str, str, date]:
    current_q = (day.month - 1) // 3 + 1
    if current_q == 1:
        return _parse_quarter(f"{day.year - 1}Q4")
    return _parse_quarter(f"{day.year}Q{current_q - 1}")


def _source_id(endpoint: str, key: str) -> str:
    digest = hashlib.sha256(f"{endpoint}|{key}".encode("utf-8")).hexdigest()[:16]
    return f"akshare:{endpoint}:{digest}"


def _source(
    endpoint: str,
    *,
    key: str,
    title: str,
    url: str,
    source_type: str,
    retrieved_at: str,
    data_as_of: str | None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": _source_id(endpoint, key),
        "provider": PROVIDER_NAME,
        "endpoint": endpoint,
        "title": title,
        "url": url,
        "sourceType": source_type,
        "retrievedAt": retrieved_at,
        "dataAsOf": data_as_of,
    }
    result.update(extra)
    return result


def _deduplicate_sources(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        source_id = str(source.get("id", ""))
        if source_id and source_id not in seen:
            result.append(dict(source))
            seen.add(source_id)
    return result


def _response(
    tool: str,
    *,
    status: str,
    data: Any,
    sources: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    data_as_of: str | None,
    units: Mapping[str, Any],
    source_name: str,
) -> dict[str, Any]:
    if status not in {"live", "partial", "unavailable"}:
        raise ValueError("内部 status 非法")
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": tool,
        "status": status,
        "provider": PROVIDER_NAME,
        "source": source_name,
        "dataAsOf": data_as_of,
        "units": dict(units),
        "data": data,
        "sources": _deduplicate_sources(sources),
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
    }
    return sanitize_json(payload)


def _sma(values: Sequence[int | float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        window = values[max(0, index - period + 1) : index + 1]
        if len(window) < period or any(value is None for value in window):
            result.append(None)
        else:
            result.append(round(sum(float(value) for value in window) / period, 6))
    return result


def _ema(values: Sequence[int | float | None], period: int) -> list[float | None]:
    alpha = 2.0 / (period + 1.0)
    state: float | None = None
    result: list[float | None] = []
    for value in values:
        if value is None:
            result.append(None)
            continue
        state = float(value) if state is None else alpha * float(value) + (1 - alpha) * state
        result.append(round(state, 6))
    return result


def _rsi(values: Sequence[int | float | None], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    avg_gain: float | None = None
    avg_loss: float | None = None
    for index in range(1, len(values)):
        previous, current = values[index - 1], values[index]
        if previous is None or current is None:
            gains.append(0.0)
            losses.append(0.0)
            continue
        change = float(current) - float(previous)
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
        if index == period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
        elif index > period and avg_gain is not None and avg_loss is not None:
            avg_gain = (avg_gain * (period - 1) + gains[-1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[-1]) / period
        if index >= period and avg_gain is not None and avg_loss is not None:
            if avg_gain == 0 and avg_loss == 0:
                result[index] = 50.0
            elif avg_loss == 0:
                result[index] = 100.0
            else:
                result[index] = round(100 - 100 / (1 + avg_gain / avg_loss), 6)
    return result


def _technical_series(prices: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    closes = [_number(item.get("close")) for item in prices]
    volumes = [_number(item.get("volume")) for item in prices]
    ma5, ma10 = _sma(closes, 5), _sma(closes, 10)
    ma20, ma60 = _sma(closes, 20), _sma(closes, 60)
    rsi14 = _rsi(closes, 14)
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif = [
        round(float(fast) - float(slow), 6) if fast is not None and slow is not None else None
        for fast, slow in zip(ema12, ema26)
    ]
    dea = _ema(dif, 9)
    histogram = [
        round(2 * (float(dif_value) - float(dea_value)), 6)
        if dif_value is not None and dea_value is not None
        else None
        for dif_value, dea_value in zip(dif, dea)
    ]
    volume_ma5 = _sma(volumes, 5)
    volume_ratio5 = [
        round(float(volume) / float(average), 6)
        if volume is not None and average not in {None, 0}
        else None
        for volume, average in zip(volumes, volume_ma5)
    ]
    result: list[dict[str, Any]] = []
    for index, price in enumerate(prices):
        result.append(
            {
                "date": price["date"],
                "ma5": ma5[index],
                "ma10": ma10[index],
                "ma20": ma20[index],
                "ma60": ma60[index],
                "rsi14": rsi14[index],
                "macdDif": dif[index],
                "macdDea": dea[index],
                "macdHistogram": histogram[index],
                "volumeMa5": volume_ma5[index],
                "volumeRatio5": volume_ratio5[index],
                "dataAsOf": price["dataAsOf"],
                "units": TECHNICAL_UNITS,
                "sourceIds": list(price["sourceIds"]),
            }
        )
    return result


class StockResearchProvider:
    """AkShare-backed implementation of the five read-only research tools."""

    def __init__(
        self,
        api: Any | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if api is None:
            import akshare as api  # type: ignore[no-redef]

        self.api = api
        self.clock = clock or (lambda: datetime.now(CHINA_TZ))

    def _now(self) -> datetime:
        return _aware_china(self.clock())

    def _call(self, endpoint: str, warnings: list[str], **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        try:
            # AkShare upstreams do not all set a requests timeout and a few
            # interfaces print diagnostics.  Reuse the morning provider's
            # carefully restored 15-second default and keep MCP stdout a clean
            # protocol stream by redirecting provider chatter to stderr.
            from akshare_provider import _requests_default_timeout

            function = getattr(self.api, endpoint)
            with redirect_stdout(sys.stderr), _requests_default_timeout():
                return _records(function(**kwargs)), False
        except Exception as exc:
            warnings.append(f"AkShare {endpoint} 调用失败（{type(exc).__name__}）；该数据模块不可用。")
            return [], True

    def get_stock_price(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 120,
        adjust: str = "qfq",
    ) -> dict[str, Any]:
        code, market = normalize_stock_symbol(symbol)
        limit = _validate_limit(limit, "limit", MAX_PRICE_LIMIT)
        if not isinstance(adjust, str) or adjust not in VALID_ADJUSTMENTS:
            raise ValueError("adjust 仅支持空字符串、qfq 或 hfq")

        now = self._now()
        end = _validate_iso_date(end_date, "end_date") if end_date is not None else now.date()
        lookback = min(MAX_DATE_SPAN_DAYS, max(365, limit * 3))
        start = _validate_iso_date(start_date, "start_date") if start_date is not None else end - timedelta(days=lookback)
        if start > end:
            raise ValueError("start_date 不得晚于 end_date")
        if (end - start).days > MAX_DATE_SPAN_DAYS:
            raise ValueError(f"日期跨度不得超过 {MAX_DATE_SPAN_DAYS} 天")
        if end > now.date():
            raise ValueError("end_date 不得晚于当前日期")

        warnings: list[str] = []
        primary_warnings: list[str] = []
        raw_rows, primary_failed = self._call(
            "stock_zh_a_hist",
            primary_warnings,
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=adjust,
        )
        fallback_used = False
        failed = primary_failed
        endpoint = "stock_zh_a_hist"
        if primary_failed or not raw_rows:
            fallback_warnings: list[str] = []
            fallback_rows, fallback_failed = self._call(
                "stock_zh_a_hist_tx",
                fallback_warnings,
                symbol=f"{market}{code}",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=adjust,
            )
            if fallback_rows:
                raw_rows = fallback_rows
                endpoint = "stock_zh_a_hist_tx"
                fallback_used = True
                failed = False
                if primary_failed:
                    warnings.extend(
                        warning.replace("该数据模块不可用", "东方财富主行情源不可用")
                        for warning in primary_warnings
                    )
                else:
                    warnings.append("东方财富主行情源未返回记录。")
                warnings.append("已回退到 AkShare 腾讯历史行情；来源和字段口径已显式标注。")
            else:
                warnings.extend(primary_warnings)
                warnings.extend(fallback_warnings)
                failed = primary_failed or fallback_failed
        else:
            warnings.extend(primary_warnings)
        retrieved_at = _iso(now)
        source_id = _source_id(endpoint, f"{code}|{start}|{end}|{adjust}")
        prices: list[dict[str, Any]] = []
        skipped = 0
        has_intraday_partial = False
        for row in raw_rows:
            trading_day = _parse_upstream_date(_pick(row, "日期", "date", "trade_date"))
            if trading_day is None or trading_day < start or trading_day > end:
                skipped += 1
                continue
            is_intraday_partial = trading_day == now.date() and now.timetz().replace(tzinfo=None) < time(15, 0)
            has_intraday_partial = has_intraday_partial or is_intraday_partial
            volume = _number(_pick(row, "成交量", "volume"))
            turnover_amount = _number(_pick(row, "成交额", "amount"))
            turnover_rate = _number(_pick(row, "换手率", "turnover_rate"))
            if fallback_used:
                # AkShare 1.18.81 stock_zh_a_hist_tx documents volume in
                # shares and amount in CNY.  Normalize volume to the provider's
                # common lot-of-100-shares contract.  The TX ``turnover``
                # column is a ratio with a different scale, so leave
                # turnoverRate null rather than silently asserting a unit.
                volume = volume / 100 if volume is not None else None
                turnover_rate = None
            prices.append(
                {
                    "date": trading_day.isoformat(),
                    "open": _number(_pick(row, "开盘", "open")),
                    "close": _number(_pick(row, "收盘", "close")),
                    "high": _number(_pick(row, "最高", "high")),
                    "low": _number(_pick(row, "最低", "low")),
                    "volume": volume,
                    "turnover": turnover_amount,
                    "amplitude": _number(_pick(row, "振幅", "amplitude")),
                    "changePercent": _number(_pick(row, "涨跌幅", "change_percent", "pct_change")),
                    "change": _number(_pick(row, "涨跌额", "change")),
                    "turnoverRate": turnover_rate,
                    "dataAsOf": _iso(now if is_intraday_partial else _market_close(trading_day)),
                    "sessionStatus": "intraday_partial" if is_intraday_partial else "closed_session",
                    "units": PRICE_UNITS,
                    "sourceIds": [source_id],
                }
            )
        prices.sort(key=lambda item: item["date"])
        technical = _technical_series(prices)
        if len(prices) > limit:
            prices = prices[-limit:]
            technical = technical[-limit:]
        if skipped:
            warnings.append(f"有 {skipped} 条行情记录因日期缺失或超出请求范围而被排除。")
        if prices and any(item["close"] is None for item in prices):
            warnings.append("部分 K 线缺少收盘价，相应技术指标为 null。")
        if prices and len(raw_rows) < 60:
            warnings.append("历史样本少于 60 条，MA60 等长周期技术指标可能为 null。")
        if has_intraday_partial:
            warnings.append(
                "最新日 K 为盘中未收盘记录；技术指标仅供观察，Kronos 输入会排除该记录。"
            )
        if fallback_used:
            warnings.append(
                "腾讯历史行情的 volume 已由股换算为手；其 turnover 比率口径未并入换手率，"
                "turnoverRate 及该源不提供的涨跌字段保持 null。"
            )
        if not prices and not failed:
            warnings.append("AkShare stock_zh_a_hist 未返回请求区间内的行情；不得用其他日期价格替代。")

        data_as_of = prices[-1]["dataAsOf"] if prices else None
        sources: list[dict[str, Any]] = []
        if prices:
            sources.append(
                _source(
                    endpoint,
                    key=f"{code}|{start}|{end}|{adjust}",
                    title=(
                        f"腾讯证券 {code} A股历史行情（AkShare）"
                        if fallback_used
                        else f"东方财富 {code} A股历史行情（AkShare）"
                    ),
                    url=(
                        f"https://gu.qq.com/{market}{code}/gp"
                        if fallback_used
                        else f"https://quote.eastmoney.com/{market}{code}.html"
                    ),
                    source_type="market_data_aggregation",
                    retrieved_at=retrieved_at,
                    data_as_of=data_as_of,
                )
            )
        status = "unavailable" if not prices else "partial" if fallback_used or failed or skipped or len(raw_rows) < 60 or has_intraday_partial else "live"
        return _response(
            "get_stock_price",
            status=status,
            data={
                "symbol": code,
                "market": market,
                "period": "daily",
                "adjust": adjust,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "count": len(prices),
                "prices": prices,
                "technicalIndicators": {
                    "parameters": {"sma": [5, 10, 20, 60], "rsi": 14, "macd": [12, 26, 9]},
                    "method": "derived_from_returned_adjusted_daily_history",
                    "latest": technical[-1] if technical else None,
                    "series": technical,
                },
            },
            sources=sources,
            warnings=warnings,
            data_as_of=data_as_of,
            units={"prices": PRICE_UNITS, "technicalIndicators": TECHNICAL_UNITS},
            source_name=f"AkShare.{endpoint}",
        )

    def get_financial_report(
        self,
        symbol: str,
        as_of: str | None = None,
        quarter: str | None = None,
    ) -> dict[str, Any]:
        code, market = normalize_stock_symbol(symbol)
        now = self._now()
        cutoff_day = _validate_iso_date(as_of, "as_of") if as_of is not None else now.date()
        if cutoff_day > now.date():
            raise ValueError("as_of 不得晚于当前日期")
        cutoff = min(datetime.combine(cutoff_day, time.max, CHINA_TZ), now)

        requested_quarter: str | None = None
        institute_quarter: str | None = None
        quarter_end: date | None = None
        if quarter is not None:
            requested_quarter, institute_quarter, quarter_end = _parse_quarter(quarter)
            if quarter_end > cutoff_day:
                raise ValueError("quarter 的报告期末不得晚于 as_of")

        warnings: list[str] = []
        retrieved_at = _iso(now)
        financial_endpoint = "stock_financial_analysis_indicator_em"
        financial_rows, financial_failed = self._call(
            financial_endpoint,
            warnings,
            symbol=f"{code}.{market.upper()}",
            indicator="按报告期",
        )
        financial_source_id = _source_id(financial_endpoint, code)
        reports: list[dict[str, Any]] = []
        excluded_financial = 0
        for row in financial_rows:
            report_day = _parse_upstream_date(_pick(row, "REPORT_DATE", "报告日期", "reportDate"))
            if report_day is None or report_day > cutoff_day:
                excluded_financial += 1
                continue
            row_quarter, _, _ = _quarter_for_date(report_day)
            if requested_quarter is not None and row_quarter != requested_quarter:
                continue
            notice_at = _parse_upstream_datetime(_pick(row, "NOTICE_DATE", "公告日期", "noticeAt"))
            updated_at = _parse_upstream_datetime(_pick(row, "UPDATE_DATE", "更新日期", "updatedAt"))
            available_at = max(item for item in (notice_at, updated_at) if item is not None) if notice_at or updated_at else None
            if available_at is None or available_at > cutoff:
                excluded_financial += 1
                continue
            reports.append(
                {
                    "reportDate": report_day.isoformat(),
                    "quarter": row_quarter,
                    "periodName": _text(_pick(row, "REPORT_DATE_NAME", "REPORT_TYPE", "报告期", "periodName"), maximum=100),
                    "noticeAt": _iso(notice_at),
                    "updatedAt": _iso(updated_at),
                    "eps": _number(_pick(row, "EPSJB", "基本每股收益", "eps")),
                    "deductedEps": _number(_pick(row, "EPSKCJB", "扣非每股收益", "deductedEps")),
                    "dilutedEps": _number(_pick(row, "EPSXS", "稀释每股收益", "dilutedEps")),
                    "bookValuePerShare": _number(_pick(row, "BPS", "每股净资产", "bookValuePerShare")),
                    "operatingCashFlowPerShare": _number(_pick(row, "MGJYXJJE", "每股经营现金流", "operatingCashFlowPerShare")),
                    "revenue": _number(_pick(row, "TOTALOPERATEREVE", "营业总收入", "revenue")),
                    "grossProfit": _number(_pick(row, "MLR", "毛利润", "grossProfit")),
                    "parentNetProfit": _number(_pick(row, "PARENTNETPROFIT", "归属净利润", "parentNetProfit")),
                    "deductedNetProfit": _number(_pick(row, "KCFJCXSYJLR", "扣非净利润", "deductedNetProfit")),
                    "revenueYoY": _number(_pick(row, "TOTALOPERATEREVETZ", "营业总收入同比增长", "revenueYoY")),
                    "parentNetProfitYoY": _number(_pick(row, "PARENTNETPROFITTZ", "归属净利润同比增长", "parentNetProfitYoY")),
                    "deductedNetProfitYoY": _number(_pick(row, "KCFJCXSYJLRTZ", "扣非净利润同比增长", "deductedNetProfitYoY")),
                    "roeWeighted": _number(_pick(row, "ROEJQ", "净资产收益率(加权)", "roeWeighted")),
                    "grossMargin": _number(_pick(row, "XSMLL", "毛利率", "grossMargin")),
                    "netMargin": _number(_pick(row, "XSJLL", "净利率", "netMargin")),
                    "debtRatio": _number(_pick(row, "ZCFZL", "资产负债率", "debtRatio")),
                    "dataAsOf": _iso(available_at),
                    "units": FINANCIAL_UNITS,
                    "sourceIds": [financial_source_id],
                }
            )
        reports.sort(key=lambda item: (item["reportDate"], item["dataAsOf"]), reverse=True)
        if len(reports) > MAX_FINANCIAL_REPORTS:
            reports = reports[:MAX_FINANCIAL_REPORTS]
            warnings.append(f"财务报告最多返回 {MAX_FINANCIAL_REPORTS} 期，较早记录已截断。")
        if excluded_financial:
            warnings.append(f"有 {excluded_financial} 条财务记录因报告期/披露时间缺失或晚于 as_of 而被排除。")
        if not reports and not financial_failed:
            warnings.append("没有可证明在 as_of 截止前已披露的财务报告。")

        if requested_quarter is None:
            if reports:
                requested_quarter, institute_quarter, quarter_end = _parse_quarter(reports[0]["quarter"])
            else:
                requested_quarter, institute_quarter, quarter_end = _latest_completed_quarter(cutoff_day)

        institution_endpoint = "stock_institute_hold_detail"
        institution_rows, institution_failed = self._call(
            institution_endpoint,
            warnings,
            stock=code,
            quarter=institute_quarter,
        )
        institution_source_id = _source_id(institution_endpoint, f"{code}|{institute_quarter}")
        institution_as_of = _iso(_market_close(quarter_end)) if quarter_end else None
        institutions: list[dict[str, Any]] = []
        for row in institution_rows[:MAX_INSTITUTION_HOLDINGS]:
            institutions.append(
                {
                    "institutionType": _text(_pick(row, "持股机构类型", "institutionType"), maximum=100),
                    "institutionCode": _text(_pick(row, "持股机构代码", "institutionCode"), maximum=50),
                    "shortName": _text(_pick(row, "持股机构简称", "shortName"), maximum=200),
                    "fullName": _text(_pick(row, "持股机构全称", "fullName"), maximum=500),
                    "sharesHeld": _number(_pick(row, "持股数", "sharesHeld")),
                    "latestSharesHeld": _number(_pick(row, "最新持股数", "latestSharesHeld")),
                    "holdingRatio": _number(_pick(row, "持股比例", "holdingRatio")),
                    "latestHoldingRatio": _number(_pick(row, "最新持股比例", "latestHoldingRatio")),
                    "floatShareRatio": _number(_pick(row, "占流通股比例", "floatShareRatio")),
                    "latestFloatShareRatio": _number(_pick(row, "最新占流通股比例", "latestFloatShareRatio")),
                    "holdingRatioChange": _number(_pick(row, "持股比例增幅", "holdingRatioChange")),
                    "floatShareRatioChange": _number(_pick(row, "占流通股比例增幅", "floatShareRatioChange")),
                    "quarter": requested_quarter,
                    "dataAsOf": institution_as_of,
                    "dateMeaning": "holding_period_end",
                    "availabilityAsOf": None,
                    "availabilityStatus": "unknown_no_publication_timestamp",
                    "units": INSTITUTION_UNITS,
                    "sourceIds": [institution_source_id],
                }
            )
        if len(institution_rows) > MAX_INSTITUTION_HOLDINGS:
            warnings.append(f"机构持股最多返回 {MAX_INSTITUTION_HOLDINGS} 条，其余已截断。")
        if not institutions and not institution_failed:
            warnings.append(f"AkShare 未返回 {requested_quarter} 的机构持股详情。")
        if institutions:
            warnings.append(
                "stock_institute_hold_detail 不提供公开披露时间；机构持股 dataAsOf 仅表示持股期末，"
                "availabilityAsOf 为 null，不能证明该数据在历史 as_of 时点已经可得。"
            )

        shareholder_endpoint = "stock_main_stock_holder"
        shareholder_rows, shareholder_failed = self._call(
            shareholder_endpoint,
            warnings,
            stock=code,
        )
        shareholder_source_id = _source_id(shareholder_endpoint, code)
        eligible_shareholders: list[tuple[date, dict[str, Any]]] = []
        excluded_shareholders = 0
        for row in shareholder_rows:
            holding_day = _parse_upstream_date(_pick(row, "截至日期", "holdingDate"))
            announced_at = _parse_upstream_datetime(_pick(row, "公告日期", "announcedAt"))
            if holding_day is None or announced_at is None or holding_day > cutoff_day or announced_at > cutoff:
                excluded_shareholders += 1
                continue
            if quarter is not None and holding_day != quarter_end:
                continue
            eligible_shareholders.append(
                (
                    holding_day,
                    {
                        "rank": _integer(_pick(row, "编号", "序号", "rank")),
                        "name": _text(_pick(row, "股东名称", "name"), maximum=500),
                        "sharesHeld": _number(_pick(row, "持股数量", "sharesHeld")),
                        "holdingRatio": _number(_pick(row, "持股比例", "holdingRatio")),
                        "shareType": _text(_pick(row, "股本性质", "shareType"), maximum=200),
                        "holdingDate": holding_day.isoformat(),
                        "announcedAt": _iso(announced_at),
                        "description": _text(_pick(row, "股东说明", "description"), maximum=1000),
                        "shareholderCount": _number(_pick(row, "股东总数", "shareholderCount")),
                        "averageSharesHeld": _number(_pick(row, "平均持股数", "averageSharesHeld")),
                        "dataAsOf": _iso(_market_close(holding_day)),
                        "units": SHAREHOLDER_UNITS,
                        "sourceIds": [shareholder_source_id],
                    },
                )
            )
        if quarter is None and eligible_shareholders:
            latest_holding_day = max(item[0] for item in eligible_shareholders)
            eligible_shareholders = [item for item in eligible_shareholders if item[0] == latest_holding_day]
        eligible_shareholders.sort(key=lambda item: (item[0], -(item[1]["rank"] or 10**9)), reverse=True)
        shareholders = [item[1] for item in eligible_shareholders[:MAX_MAJOR_SHAREHOLDERS]]
        if len(eligible_shareholders) > MAX_MAJOR_SHAREHOLDERS:
            warnings.append(f"主要股东最多返回 {MAX_MAJOR_SHAREHOLDERS} 条，其余已截断。")
        if excluded_shareholders:
            warnings.append(f"有 {excluded_shareholders} 条主要股东记录因日期缺失或晚于 as_of 而被排除。")
        if not shareholders and not shareholder_failed:
            warnings.append("没有可证明在 as_of 截止前已公告的主要股东记录。")

        sources: list[dict[str, Any]] = []
        report_as_of = _latest_iso([item["dataAsOf"] for item in reports])
        shareholder_as_of = _latest_iso([item["dataAsOf"] for item in shareholders])
        if reports:
            sources.append(
                _source(
                    financial_endpoint,
                    key=code,
                    title=f"东方财富 {code} 主要财务指标（AkShare）",
                    url=f"https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code={market.upper()}{code}#/cwfx",
                    source_type="financial_data_aggregation",
                    retrieved_at=retrieved_at,
                    data_as_of=report_as_of,
                )
            )
        if institutions:
            sources.append(
                _source(
                    institution_endpoint,
                    key=f"{code}|{institute_quarter}",
                    title=f"新浪财经 {code} {requested_quarter} 机构持股详情（AkShare）",
                    url="https://vip.stock.finance.sina.com.cn/q/go.php/vComStockHold/kind/jgcg/index.phtml",
                    source_type="institution_holding_aggregation",
                    retrieved_at=retrieved_at,
                    data_as_of=institution_as_of,
                    dateMeaning="holding_period_end",
                    availabilityAsOf=None,
                    availabilityStatus="unknown_no_publication_timestamp",
                )
            )
        if shareholders:
            sources.append(
                _source(
                    shareholder_endpoint,
                    key=code,
                    title=f"新浪财经 {code} 主要股东（AkShare）",
                    url=f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_StockHolder/stockid/{code}.phtml",
                    source_type="major_shareholder_aggregation",
                    retrieved_at=retrieved_at,
                    data_as_of=shareholder_as_of,
                )
            )

        available_sections = sum(bool(section) for section in (reports, institutions, shareholders))
        any_failed = financial_failed or institution_failed or shareholder_failed
        # Institution rows have a holding-period date but no publication
        # timestamp, so even three populated sections cannot be represented as
        # fully point-in-time complete.
        status = "unavailable" if available_sections == 0 else "partial" if available_sections < 3 or any_failed or institutions else "live"
        data_as_of = _latest_iso([report_as_of, institution_as_of if institutions else None, shareholder_as_of])
        return _response(
            "get_financial_report",
            status=status,
            data={
                "symbol": code,
                "market": market,
                "asOf": cutoff_day.isoformat(),
                "quarter": requested_quarter,
                "reports": reports,
                "institutionHoldings": institutions,
                "majorShareholders": shareholders,
                "sectionStatus": {
                    "reports": "unavailable" if not reports else "available",
                    "institutionHoldings": "unavailable" if not institutions else "available",
                    "majorShareholders": "unavailable" if not shareholders else "available",
                },
            },
            sources=sources,
            warnings=warnings,
            data_as_of=data_as_of,
            units={
                "reports": FINANCIAL_UNITS,
                "institutionHoldings": INSTITUTION_UNITS,
                "majorShareholders": SHAREHOLDER_UNITS,
            },
            source_name="AkShare financial + institution/shareholder endpoints",
        )

    def get_news(
        self,
        symbol: str,
        limit: int = 10,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        code, market = normalize_stock_symbol(symbol)
        limit = _validate_limit(limit, "limit", MAX_NEWS_LIMIT)
        now = self._now()
        cutoff_day = _validate_iso_date(as_of, "as_of") if as_of is not None else now.date()
        if cutoff_day > now.date():
            raise ValueError("as_of 不得晚于当前日期")
        cutoff = min(datetime.combine(cutoff_day, time.max, CHINA_TZ), now)
        warnings = ["stock_news_em 提供媒体新闻聚合，不等同于交易所或上市公司官方公告。"]
        rows, failed = self._call("stock_news_em", warnings, symbol=code)
        retrieved_at = _iso(now)
        candidates: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
        excluded = 0
        for row in rows:
            published_at = _parse_upstream_datetime(_pick(row, "发布时间", "publishedAt", "date"))
            if published_at is None or published_at > cutoff:
                excluded += 1
                continue
            title = _text(_pick(row, "新闻标题", "标题", "title"), maximum=500)
            if not title:
                excluded += 1
                continue
            url = _text(_pick(row, "新闻链接", "链接", "url"), maximum=2048)
            if url:
                try:
                    parsed_url = urlparse(url)
                    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                        url = None
                except ValueError:
                    url = None
            original_publisher = _text(_pick(row, "文章来源", "来源", "publisher"), maximum=200)
            is_eastmoney = False
            if url:
                hostname = (urlparse(url).hostname or "").lower().rstrip(".")
                is_eastmoney = hostname == "eastmoney.com" or hostname.endswith(".eastmoney.com")
            source_url = url or f"https://so.eastmoney.com/news/s?keyword={code}"
            source_id = _source_id("stock_news_em", f"{code}|{source_url}|{_iso(published_at)}|{title}")
            source = _source(
                "stock_news_em",
                key=f"{code}|{source_url}|{_iso(published_at)}|{title}",
                title=title,
                url=source_url,
                source_type="media_aggregation" if is_eastmoney or url is None else "media_article",
                retrieved_at=retrieved_at,
                data_as_of=_iso(published_at),
                publisher=original_publisher or ("东方财富" if is_eastmoney or url is None else None),
                originalPublisher=original_publisher,
                aggregator="东方财富" if is_eastmoney or url is None else None,
            )
            item = {
                "title": title,
                "summary": _text(_pick(row, "新闻内容", "摘要", "content", "summary"), maximum=4000),
                "publishedAt": _iso(published_at),
                "publisher": original_publisher,
                "url": url,
                "sourceType": source["sourceType"],
                "dataAsOf": _iso(published_at),
                "units": {"publishedAt": "ISO-8601 Asia/Shanghai timestamp"},
                "sourceIds": [source_id],
            }
            candidates.append((published_at, item, source))
        candidates.sort(key=lambda item: item[0], reverse=True)
        candidates = candidates[:limit]
        items = [item[1] for item in candidates]
        sources = [item[2] for item in candidates]
        if excluded:
            warnings.append(f"有 {excluded} 条新闻因标题/发布时间缺失或晚于 as_of 而被排除。")
        if not items and not failed:
            warnings.append("没有可核验发布时间且不晚于 as_of 的媒体新闻。")
        data_as_of = _iso(candidates[0][0]) if candidates else None
        status = "unavailable" if not items else "partial" if failed or excluded else "live"
        return _response(
            "get_news",
            status=status,
            data={
                "symbol": code,
                "market": market,
                "asOf": cutoff_day.isoformat(),
                "count": len(items),
                "items": items,
                "officialAnnouncementsAvailable": False,
            },
            sources=sources,
            warnings=warnings,
            data_as_of=data_as_of,
            units={"publishedAt": "ISO-8601 Asia/Shanghai timestamp", "count": "article_count"},
            source_name="AkShare.stock_news_em",
        )

    def get_sector_rank(self, sector_type: str = "industry", limit: int = 10) -> dict[str, Any]:
        if not isinstance(sector_type, str) or sector_type not in VALID_SECTOR_TYPES:
            raise ValueError("sector_type 仅支持 industry 或 concept")
        limit = _validate_limit(limit, "limit", MAX_SECTOR_LIMIT)
        endpoint = {
            "industry": "stock_board_industry_name_em",
            "concept": "stock_board_concept_name_em",
        }[sector_type]
        now = self._now()
        observed_at = _iso(now)
        warnings: list[str] = []
        rows, failed = self._call(endpoint, warnings)
        source_id = _source_id(endpoint, observed_at or "snapshot")
        rankings: list[dict[str, Any]] = []
        skipped = 0
        for row in rows:
            name = _text(_pick(row, "板块名称", "板块", "name"), maximum=200)
            if not name:
                skipped += 1
                continue
            rankings.append(
                {
                    "rank": _integer(_pick(row, "排名", "序号", "rank")),
                    "name": name,
                    "code": _text(_pick(row, "板块代码", "code"), maximum=50),
                    "last": _number(_pick(row, "最新价", "last")),
                    "change": _number(_pick(row, "涨跌额", "change")),
                    "changePercent": _number(_pick(row, "涨跌幅", "changePercent")),
                    "marketCap": _number(_pick(row, "总市值", "marketCap")),
                    "turnoverRate": _number(_pick(row, "换手率", "turnoverRate")),
                    "advancers": _integer(_pick(row, "上涨家数", "advancers")),
                    "decliners": _integer(_pick(row, "下跌家数", "decliners")),
                    "leader": _text(_pick(row, "领涨股票", "领涨股", "leader"), maximum=200),
                    "leaderChangePercent": _number(_pick(row, "领涨股票-涨跌幅", "领涨股-涨跌幅", "leaderChangePercent")),
                    "dataAsOf": observed_at,
                    "units": SECTOR_UNITS,
                    "sourceIds": [source_id],
                }
            )
        rankings.sort(
            key=lambda item: (
                item["rank"] is None,
                item["rank"] if item["rank"] is not None else -float(item["changePercent"] or -math.inf),
            )
        )
        rankings = rankings[:limit]
        if skipped:
            warnings.append(f"有 {skipped} 条板块记录因名称缺失而被排除。")
        if not rankings and not failed:
            warnings.append(f"AkShare {endpoint} 未返回板块排行快照。")
        sources = []
        if rankings:
            sources.append(
                _source(
                    endpoint,
                    key=observed_at or "snapshot",
                    title=f"东方财富 {'行业' if sector_type == 'industry' else '概念'}板块实时排行（AkShare）",
                    url=f"https://quote.eastmoney.com/center/boardlist.html#{sector_type}_board",
                    source_type="sector_market_data_aggregation",
                    retrieved_at=observed_at,
                    data_as_of=observed_at,
                )
            )
        status = "unavailable" if not rankings else "partial" if failed or skipped else "live"
        return _response(
            "get_sector_rank",
            status=status,
            data={"sectorType": sector_type, "count": len(rankings), "rankings": rankings},
            sources=sources,
            warnings=warnings,
            data_as_of=observed_at if rankings else None,
            units=SECTOR_UNITS,
            source_name=f"AkShare.{endpoint}",
        )

    def get_capital_flow(
        self,
        symbol: str | None = None,
        indicator: str = "5日",
        limit: int = 10,
    ) -> dict[str, Any]:
        if not isinstance(indicator, str) or indicator not in VALID_FLOW_INDICATORS:
            raise ValueError("indicator 仅支持 今日、3日、5日 或 10日")
        limit = _validate_limit(limit, "limit", MAX_CAPITAL_FLOW_LIMIT)
        code: str | None = None
        market: str | None = None
        if symbol is not None:
            code, market = normalize_stock_symbol(symbol)
        now = self._now()
        retrieved_at = _iso(now)
        warnings = ["资金流为东方财富数据供应方口径，不代表资金真实意图或交易信号。"]

        if code is not None and market is not None:
            endpoint = "stock_individual_fund_flow"
            rows, failed = self._call(endpoint, warnings, stock=code, market=market)
            source_id = _source_id(endpoint, code)
            candidates: list[tuple[date, dict[str, Any]]] = []
            skipped = 0
            for row in rows:
                trading_day = _parse_upstream_date(_pick(row, "日期", "date"))
                if trading_day is None or trading_day > now.date():
                    skipped += 1
                    continue
                candidates.append(
                    (
                        trading_day,
                        {
                            "date": trading_day.isoformat(),
                            "close": _number(_pick(row, "收盘价", "close")),
                            "changePercent": _number(_pick(row, "涨跌幅", "changePercent")),
                            "mainNetInflow": _number(_pick(row, "主力净流入-净额", "mainNetInflow")),
                            "mainNetInflowRatio": _number(_pick(row, "主力净流入-净占比", "mainNetInflowRatio")),
                            "superLargeNetInflow": _number(_pick(row, "超大单净流入-净额", "superLargeNetInflow")),
                            "superLargeNetInflowRatio": _number(_pick(row, "超大单净流入-净占比", "superLargeNetInflowRatio")),
                            "largeNetInflow": _number(_pick(row, "大单净流入-净额", "largeNetInflow")),
                            "largeNetInflowRatio": _number(_pick(row, "大单净流入-净占比", "largeNetInflowRatio")),
                            "mediumNetInflow": _number(_pick(row, "中单净流入-净额", "mediumNetInflow")),
                            "mediumNetInflowRatio": _number(_pick(row, "中单净流入-净占比", "mediumNetInflowRatio")),
                            "smallNetInflow": _number(_pick(row, "小单净流入-净额", "smallNetInflow")),
                            "smallNetInflowRatio": _number(_pick(row, "小单净流入-净占比", "smallNetInflowRatio")),
                            "dataAsOf": _iso(_market_close(trading_day)),
                            "units": CAPITAL_FLOW_UNITS,
                            "sourceIds": [source_id],
                        },
                    )
                )
            candidates.sort(key=lambda item: item[0], reverse=True)
            candidates = candidates[:limit]
            items = [item[1] for item in candidates]
            if skipped:
                warnings.append(f"有 {skipped} 条个股资金流因日期缺失或晚于当前日期而被排除。")
            if not items and not failed:
                warnings.append("AkShare 未返回可核验日期的个股资金流历史。")
            data_as_of = items[0]["dataAsOf"] if items else None
            sources = []
            if items:
                sources.append(
                    _source(
                        endpoint,
                        key=code,
                        title=f"东方财富 {code} 个股资金流历史（AkShare）",
                        url=f"https://data.eastmoney.com/zjlx/{market}{code}.html",
                        source_type="capital_flow_aggregation",
                        retrieved_at=retrieved_at,
                        data_as_of=data_as_of,
                    )
                )
            status = "unavailable" if not items else "partial" if failed or skipped else "live"
            data = {
                "mode": "stock_history",
                "symbol": code,
                "market": market,
                "indicator": indicator,
                "indicatorApplied": False,
                "count": len(items),
                "items": items,
            }
        else:
            endpoint = "stock_individual_fund_flow_rank"
            rows, failed = self._call(endpoint, warnings, indicator=indicator)
            source_id = _source_id(endpoint, f"{indicator}|{retrieved_at}")
            items = []
            skipped = 0
            for row in rows:
                item_code = _text(_pick(row, "代码", "symbol", "code"), maximum=20)
                if item_code is None:
                    skipped += 1
                    continue
                normalized_code = item_code.zfill(6) if item_code.isdigit() and len(item_code) < 6 else item_code
                prefix = indicator
                items.append(
                    {
                        "rank": _integer(_pick(row, "序号", "排名", "rank")),
                        "symbol": normalized_code,
                        "name": _text(_pick(row, "名称", "name"), maximum=200),
                        "last": _number(_pick(row, "最新价", "last")),
                        "changePercent": _number(_pick(row, f"{prefix}涨跌幅", "今日涨跌幅", "涨跌幅", "changePercent")),
                        "mainNetInflow": _number(_pick(row, f"{prefix}主力净流入-净额", "主力净流入-净额", "mainNetInflow")),
                        "mainNetInflowRatio": _number(_pick(row, f"{prefix}主力净流入-净占比", "主力净流入-净占比", "mainNetInflowRatio")),
                        "superLargeNetInflow": _number(_pick(row, f"{prefix}超大单净流入-净额", "超大单净流入-净额", "superLargeNetInflow")),
                        "superLargeNetInflowRatio": _number(_pick(row, f"{prefix}超大单净流入-净占比", "超大单净流入-净占比", "superLargeNetInflowRatio")),
                        "largeNetInflow": _number(_pick(row, f"{prefix}大单净流入-净额", "大单净流入-净额", "largeNetInflow")),
                        "largeNetInflowRatio": _number(_pick(row, f"{prefix}大单净流入-净占比", "大单净流入-净占比", "largeNetInflowRatio")),
                        "mediumNetInflow": _number(_pick(row, f"{prefix}中单净流入-净额", "中单净流入-净额", "mediumNetInflow")),
                        "mediumNetInflowRatio": _number(_pick(row, f"{prefix}中单净流入-净占比", "中单净流入-净占比", "mediumNetInflowRatio")),
                        "smallNetInflow": _number(_pick(row, f"{prefix}小单净流入-净额", "小单净流入-净额", "smallNetInflow")),
                        "smallNetInflowRatio": _number(_pick(row, f"{prefix}小单净流入-净占比", "小单净流入-净占比", "smallNetInflowRatio")),
                        "dataAsOf": retrieved_at,
                        "units": CAPITAL_FLOW_UNITS,
                        "sourceIds": [source_id],
                    }
                )
            items.sort(key=lambda item: (item["rank"] is None, item["rank"] or 10**9))
            items = items[:limit]
            if skipped:
                warnings.append(f"有 {skipped} 条资金流排行因股票代码缺失而被排除。")
            if not items and not failed:
                warnings.append("AkShare 未返回指定 indicator 的个股资金流排行。")
            data_as_of = retrieved_at if items else None
            sources = []
            if items:
                sources.append(
                    _source(
                        endpoint,
                        key=f"{indicator}|{retrieved_at}",
                        title=f"东方财富个股资金流 {indicator} 排行（AkShare）",
                        url="https://data.eastmoney.com/zjlx/",
                        source_type="capital_flow_aggregation",
                        retrieved_at=retrieved_at,
                        data_as_of=data_as_of,
                    )
                )
            status = "unavailable" if not items else "partial" if failed or skipped else "live"
            data = {
                "mode": "market_ranking",
                "symbol": None,
                "market": None,
                "indicator": indicator,
                "indicatorApplied": True,
                "count": len(items),
                "items": items,
            }

        return _response(
            "get_capital_flow",
            status=status,
            data=data,
            sources=sources,
            warnings=warnings,
            data_as_of=data_as_of,
            units=CAPITAL_FLOW_UNITS,
            source_name=f"AkShare.{endpoint}",
        )


_DEFAULT_PROVIDER: StockResearchProvider | None = None


def _default_provider() -> StockResearchProvider:
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = StockResearchProvider()
    return _DEFAULT_PROVIDER


def get_stock_price(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 120,
    adjust: str = "qfq",
) -> dict[str, Any]:
    return _default_provider().get_stock_price(symbol, start_date, end_date, limit, adjust)


def get_financial_report(
    symbol: str,
    as_of: str | None = None,
    quarter: str | None = None,
) -> dict[str, Any]:
    return _default_provider().get_financial_report(symbol, as_of, quarter)


def get_news(symbol: str, limit: int = 10, as_of: str | None = None) -> dict[str, Any]:
    return _default_provider().get_news(symbol, limit, as_of)


def get_sector_rank(sector_type: str = "industry", limit: int = 10) -> dict[str, Any]:
    return _default_provider().get_sector_rank(sector_type, limit)


def get_capital_flow(
    symbol: str | None = None,
    indicator: str = "5日",
    limit: int = 10,
) -> dict[str, Any]:
    return _default_provider().get_capital_flow(symbol, indicator, limit)


__all__ = [
    "MAX_CAPITAL_FLOW_LIMIT",
    "MAX_NEWS_LIMIT",
    "MAX_PRICE_LIMIT",
    "MAX_SECTOR_LIMIT",
    "StockResearchProvider",
    "get_capital_flow",
    "get_financial_report",
    "get_news",
    "get_sector_rank",
    "get_stock_price",
    "normalize_stock_symbol",
    "sanitize_json",
]
