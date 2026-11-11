"""Resilient AkShare-backed morning-context provider.

The provider implements the existing ``morning-context-v1`` bridge contract.
Every remote module is isolated behind a last-good JSON cache so that a
temporary failure at one upstream site does not discard data from the others.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import math
import multiprocessing
import os
import re
import socket
import sys
import tempfile
import threading
import time as monotonic_time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from index_turnover_http import IndexFetchError, fetch_kline_isolated, validate_unit_declarations

from us_equity_calendar import (
    US_EQUITY_CALENDAR_ID,
    latest_completed_us_session,
    us_equity_session_close,
    us_index_freshness,
    validate_us_index_return,
)


SCHEMA_VERSION = "1.0"
CACHE_FORMAT_VERSION = "source-provenance-v1"
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
DEFAULT_MEDIA_SOURCE_BUDGET_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
DEFAULT_NEWS_MAX_AGE_HOURS = 72.0
MAX_WARNING_ERROR_DETAIL_CHARS = 240
MAX_MEDIA_SUBPROCESS_BYTES = 4 * 1024 * 1024
TASK_NAME = "a_share_morning_context"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
NEW_YORK_TZ = ZoneInfo("America/New_York")
SECTION_NAMES = (
    "tradingCalendar",
    "overnightGlobal",
    "previousAShareSession",
    "policyAndCalendar",
    "sectors",
    "capitalFlows",
    "watchlist",
    "announcementsAndNews",
)
US_INDEXES = {".DJI": "道琼斯工业指数", ".INX": "标普500指数", ".IXIC": "纳斯达克综合指数"}
A_SHARE_INDEXES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
# Same-session index turnover supplement (Eastmoney daily kline, CNY yuan). One request
# per index per attempt: identity comes from the response itself (code + market), not from
# a separate full market-map download.
INDEX_TURNOVER_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
INDEX_TURNOVER_SECIDS = {"sh000001": (1, "000001"), "sz399001": (0, "399001"), "sz399006": (0, "399006")}
INDEX_TURNOVER_MAX_ATTEMPTS = 3            # per index; first attempts = 3 requests, hard cap = 9
INDEX_TURNOVER_TOTAL_BUDGET_SECONDS = 45.0  # wall-clock for ALL three indices together
INDEX_TURNOVER_ATTEMPT_TIMEOUT_SECONDS = (5.0, 8.0)  # (connect, read), further capped by the budget
INDEX_TURNOVER_RETRY_DELAY_SECONDS = 1.5
# Unit ANOMALY guard (not a proof): a CNY-yuan daily index turnover is far above 1e10 and below 1e14,
# so 万元/亿元/手 mix-ups usually fall outside this range.  An in-range number is NOT thereby proven
# to be in yuan; the response carries no unit field and the yuan unit rests on the documented API.
INDEX_TURNOVER_YUAN_RANGE = (1e10, 1e14)
INDEX_TURNOVER_MAX_BODY_BYTES = 65_536
UNSPECIFIED_UNIT = "provider_native_unspecified"
A_SHARE_QUOTE_UNITS = {
    "last": "CNY_per_share",
    "change": "CNY_per_share",
    "changePercent": "percentage_point",
    "previousClose": "CNY_per_share",
    "open": "CNY_per_share",
    "high": "CNY_per_share",
    "low": "CNY_per_share",
    "volume": UNSPECIFIED_UNIT,
    "turnover": "CNY",
}
A_SHARE_INDEX_UNITS = {
    "close": "index_point",
    "change": "index_point",
    "changePercent": "percentage_point",
    "previousClose": "index_point",
    "open": "index_point",
    "high": "index_point",
    "low": "index_point",
    "volume": UNSPECIFIED_UNIT,
    "turnover": "CNY",
}
US_INDEX_UNITS = {
    "open": "index_point",
    "high": "index_point",
    "low": "index_point",
    "close": "index_point",
    "change": "index_point",
    "changePercent": "percentage_point",
    "volume": UNSPECIFIED_UNIT,
    "amount": UNSPECIFIED_UNIT,
}
SECTOR_UNITS = {
    "changePercent": "percentage_point",
    "volume": "ten_thousand_lots",
    "turnover": "CNY_100_million",
    "netInflow": "CNY_100_million",
    "advancers": "company_count",
    "decliners": "company_count",
    "leaderPrice": "CNY_per_share",
    "leaderChangePercent": "percentage_point",
}
LHB_ENTRY_UNITS = {
    "close": "CNY_per_share",
    "changePercent": "percentage_point",
    "netBuy": "CNY",
    "buyAmount": "CNY",
    "sellAmount": "CNY",
}
FINANCIAL_REQUIRED_FIELD_GROUPS = (
    ("reportDate",),
    ("noticeAt", "updatedAt"),
    ("revenue",),
    ("revenueYoY",),
    ("parentNetProfit",),
    ("parentNetProfitYoY",),
    ("deductedNetProfit",),
    ("deductedNetProfitYoY",),
    ("grossMargin",),
    ("netMargin",),
    ("roeWeighted", "roeDiluted"),
    ("eps",),
)
HISTORICAL_LOOKBACK_DAYS = 32


# AkShare calls several requests-based upstreams which do not consistently set
# a timeout.  Patching Session.request is necessarily process-wide, so the lock
# serializes our short-lived patches and the thread check leaves unrelated
# concurrent requests untouched.  The original method is always restored.
_REQUEST_PATCH_LOCK = threading.RLock()


class _MediaBudgetExceeded(TimeoutError):
    """Raised after an isolated media loader is killed at its wall-clock limit."""


class _MediaLoaderFailure(RuntimeError):
    """Serializable child-process failure with preserved transport classification."""

    def __init__(self, detail: str, *, transport_failure: bool = False) -> None:
        super().__init__(detail)
        self.transport_failure = transport_failure


class _MediaIsolationUnavailable(RuntimeError):
    """Raised when production media isolation cannot be established safely."""


_SENSITIVE_DIAGNOSTIC_KEY = (
    r"(?:api[_-]?key|client[_-]?secret|access[_-]?token|token|secret|password|passwd|"
    r"authorization|cookie)"
)


def _warning_error_detail(error: BaseException) -> str:
    """Return a bounded, single-line exception summary safe for public metadata."""

    try:
        raw_message = str(error)
    except Exception:
        raw_message = "unprintable exception detail"
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", raw_message)
    message = re.sub(r"\s+", " ", message).strip()
    # Redact URL userinfo before generic key/value handling so credentials that
    # contain punctuation cannot be split and partially retained.
    message = re.sub(
        r"(?i)(https?://)[^/?#@\s]+@",
        r"\1[REDACTED]@",
        message,
    )
    # JSON/logfmt forms, including quoted keys and values with spaces.
    message = re.sub(
        rf'''(?i)(["']?)({_SENSITIVE_DIAGNOSTIC_KEY})\1(\s*[:=]\s*)'''
        r'''(["'])(?:\\.|(?!\4).)*\4''',
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(1)}{match.group(3)}"
            f"{match.group(4)}[REDACTED]{match.group(4)}"
        ),
        message,
    )
    message = re.sub(
        r"(?i)\b(bearer|basic)\s+[^\s,;\"']+",
        lambda match: f"{match.group(1)} [REDACTED]",
        message,
    )
    message = re.sub(
        rf'''(?i)(["']?)({_SENSITIVE_DIAGNOSTIC_KEY})\1(\s*[:=]\s*)'''
        r'''(?!["'])([^\s,;&}\]]+)''',
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(1)}{match.group(3)}[REDACTED]"
        ),
        message,
    )
    message = re.sub(
        r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b",
        "[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)-----BEGIN [^-]{0,48}PRIVATE KEY-----.*?"
        r"(?:-----END [^-]{0,48}PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        message,
    )
    detail = f"{type(error).__name__}: {message}" if message else type(error).__name__
    if len(detail) > MAX_WARNING_ERROR_DETAIL_CHARS:
        detail = detail[: MAX_WARNING_ERROR_DETAIL_CHARS - 1].rstrip() + "…"
    return detail


@contextlib.contextmanager
def _requests_default_timeout(
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    *,
    cap_explicit_timeout: bool = False,
):
    """Give requests calls made by the current AkShare call a default timeout.

    An upstream timeout supplied either by keyword or positional argument wins
    unless ``cap_explicit_timeout`` is requested.  The cap is used only for the
    shared media budget; other AkShare calls retain their existing semantics.
    Imports stay local so injectable unit-test APIs do not require AkShare at
    import time.
    """

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("HTTP timeout 必须为正数")

    import requests

    session_class = requests.sessions.Session
    owner_thread = threading.get_ident()
    with _REQUEST_PATCH_LOCK:
        original_request = session_class.request
        try:
            request_signature = inspect.signature(original_request)
        except (TypeError, ValueError):
            request_signature = None

        timeout_position: int | None = None
        if request_signature is not None and "timeout" in request_signature.parameters:
            parameters = list(request_signature.parameters)
            timeout_position = parameters.index("timeout") - 1  # ``args`` excludes ``session``.

        def capped_timeout(value: Any) -> Any:
            def cap_component(component: Any) -> float:
                try:
                    numeric = float(component)
                except (TypeError, ValueError):
                    return timeout_seconds
                if not math.isfinite(numeric) or numeric <= 0:
                    return timeout_seconds
                return min(numeric, timeout_seconds)

            if isinstance(value, tuple):
                return tuple(cap_component(component) for component in value)
            return cap_component(value)

        def request_with_default_timeout(session: Any, *args: Any, **kwargs: Any) -> Any:
            call_args = list(args)
            explicit_timeout = "timeout" in kwargs
            if not explicit_timeout and request_signature is not None:
                try:
                    explicit_timeout = "timeout" in request_signature.bind_partial(
                        session, *args, **kwargs
                    ).arguments
                except TypeError:
                    # Let the original method raise its normal argument error.
                    explicit_timeout = False
            if threading.get_ident() == owner_thread:
                if not explicit_timeout:
                    kwargs["timeout"] = timeout_seconds
                elif cap_explicit_timeout:
                    if "timeout" in kwargs:
                        kwargs["timeout"] = capped_timeout(kwargs["timeout"])
                    elif timeout_position is not None and len(call_args) > timeout_position:
                        call_args[timeout_position] = capped_timeout(call_args[timeout_position])
            return original_request(session, *call_args, **kwargs)

        session_class.request = request_with_default_timeout
        try:
            yield
        finally:
            # Do not overwrite an independent replacement installed while the
            # AkShare call was running; otherwise restore our exact predecessor.
            if session_class.request is request_with_default_timeout:
                session_class.request = original_request


@dataclass
class ModulePayload:
    data: Any
    sources: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ModuleResult:
    data: Any
    sources: list[dict[str, Any]]
    status: str  # live | cache | stale | unavailable

    @property
    def score(self) -> int:
        return {"live": 100, "cache": 50, "stale": 0, "unavailable": 0}[self.status]


@dataclass
class RunState:
    warnings: list[str] = field(default_factory=list)
    cache_hits: list[str] = field(default_factory=list)
    live_modules: int = 0
    eastmoney_media_deadline: float | None = None
    eastmoney_media_circuit_reason: str | None = None


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return not math.isfinite(value)
    try:
        marker = value != value
        return isinstance(marker, bool) and marker
    except Exception:
        return False


def sanitize_json(value: Any) -> Any:
    """Convert pandas/numpy values and non-finite floats to strict JSON values."""
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=CHINA_TZ)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
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


def normalize_stock_code(value: Any) -> str | None:
    """Return a six-digit A-share code, removing exchange affixes."""
    if _is_missing(value):
        return None
    raw = str(value).strip().lower()
    groups = re.findall(r"\d+", raw)
    if not groups:
        return None
    digits = groups[-1]
    if len(digits) < 6:
        digits = digits.zfill(6)
    elif len(digits) > 6:
        digits = digits[-6:]
    return digits if re.fullmatch(r"\d{6}", digits) else None


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


def _text_or_none(value: Any) -> str | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _is_eastmoney_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "eastmoney.com" or hostname.endswith(".eastmoney.com")


def _media_provenance(url: str | None, original_publisher: str | None) -> dict[str, str | None]:
    """Describe the linked page without pretending an aggregator is the original outlet."""

    original = _text_or_none(original_publisher)
    aggregator = "东方财富" if _is_eastmoney_url(url) else None
    return {
        "publisher": original or aggregator,
        "sourceType": "media_aggregation" if aggregator else "media_article",
        "originalPublisher": original,
        "aggregator": aggregator,
    }


def _number(value: Any) -> float | int | None:
    if _is_missing(value):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _positive_number(value: Any) -> float | int | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _rounded_number(value: float, digits: int = 10) -> float | int:
    return _number(round(value, digits)) or 0


def _closed_daily_fields(
    rows: Sequence[Mapping[str, Any]],
    session_day: date,
    expected_previous_day: date,
    *,
    label: str,
    warnings: list[str],
) -> dict[str, float | int | None]:
    """Return one verified closed-session row and derive its change fields.

    ``previousClose`` is always the close from the trading-calendar-declared
    immediately preceding session.  Provider-supplied change fields are
    treated as cross-checks only; they can never override the arithmetic
    derived from the two closes.
    """

    dated_rows = sorted(
        (
            (row_date, row)
            for row in rows
            if (row_date := _parse_date(_pick(row, "date", "日期", "trade_date")))
            and row_date <= session_day
        ),
        key=lambda item: item[0],
    )
    current = next((row for row_date, row in dated_rows if row_date == session_day), None)
    previous = next(
        (row for row_date, row in dated_rows if row_date == expected_previous_day),
        None,
    )
    if current is None:
        raise ValueError(f"{label} 未返回 {session_day.isoformat()} 日线；不得用其他日期替代")
    if expected_previous_day >= session_day:
        raise ValueError(f"{label} 交易日历给出的前序交易日无效")
    if previous is None:
        raise ValueError(
            f"{label} 缺少交易日历指定的紧邻前序交易日 {expected_previous_day.isoformat()} 日线；"
            "不得使用更老日期作为 previousClose"
        )
    close = _positive_number(_pick(current, "close", "收盘", "最新价"))
    previous_close = _positive_number(_pick(previous, "close", "收盘", "最新价"))
    open_price = _positive_number(_pick(current, "open", "开盘", "今开"))
    high = _positive_number(_pick(current, "high", "最高"))
    low = _positive_number(_pick(current, "low", "最低"))
    if None in {close, previous_close, open_price, high, low}:
        raise ValueError(f"{label} 日线核心价格字段缺失、非有限数或不大于 0")
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise ValueError(f"{label} 日线 OHLC 算术关系无效")

    change_value = float(close) - float(previous_close)
    percent_value = change_value / float(previous_close) * 100
    change = _rounded_number(change_value)
    change_percent = _rounded_number(percent_value)

    raw_change = _number(_pick(current, "change", "涨跌额", "pricechange"))
    if raw_change is not None and not math.isclose(
        float(raw_change), float(change), rel_tol=1e-6, abs_tol=0.011
    ):
        warnings.append(f"{label} 上游涨跌额与相邻日线收盘价不一致，已采用收盘价重算值。")
    raw_percent = _number(_pick(current, "change_percent", "涨跌幅", "pct_change"))
    if raw_percent is not None and not math.isclose(
        float(raw_percent), float(change_percent), rel_tol=1e-6, abs_tol=0.011
    ):
        warnings.append(f"{label} 上游涨跌幅与相邻日线收盘价不一致，已采用收盘价重算值。")

    volume = _positive_number(_pick(current, "volume", "成交量"))
    turnover = _positive_number(_pick(current, "amount", "成交额"))
    if volume is None:
        warnings.append(f"{label} {session_day.isoformat()} 成交量缺失或不大于 0，已置为 null。")
    if turnover is None:
        warnings.append(f"{label} {session_day.isoformat()} 成交额缺失或不大于 0，已置为 null。")

    return {
        "close": close,
        "change": change,
        "changePercent": change_percent,
        "previousClose": previous_close,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "turnover": turnover,
    }


def _arithmetic_is_coherent(item: Mapping[str, Any], close_field: str) -> bool:
    close = _positive_number(item.get(close_field))
    previous_close = _positive_number(item.get("previousClose"))
    change = _number(item.get("change"))
    change_percent = _number(item.get("changePercent"))
    if close is None or previous_close is None or change is None or change_percent is None:
        return False
    expected_change = float(close) - float(previous_close)
    expected_percent = expected_change / float(previous_close) * 100
    return math.isclose(float(change), expected_change, rel_tol=1e-9, abs_tol=1e-8) and math.isclose(
        float(change_percent), expected_percent, rel_tol=1e-9, abs_tol=1e-8
    )


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _parse_date(value: Any) -> date | None:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: Any, assumed_tz: ZoneInfo = CHINA_TZ) -> datetime | None:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assumed_tz)
    return parsed


def _china_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ).isoformat(timespec="seconds")


def _instant_iso(value: datetime) -> str:
    """Serialise a real observation instant without losing its microseconds.

    ``_china_iso`` deliberately rounds display/market timestamps to whole
    seconds. A query/observation instant is evidence that is compared against
    other recorded instants (``observedThroughAt <= queriedAt``, recorded
    ``queriedAt == payload queriedAt``); truncating it can move it *before*
    an instant that kept its fraction and break those proofs.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ).isoformat()


def _session_iso(day: date | None, clock: str = "15:00:00") -> str | None:
    if day is None:
        return None
    try:
        parsed_time = time.fromisoformat(str(clock)[:8])
    except ValueError:
        parsed_time = time(15, 0)
    return _china_iso(datetime.combine(day, parsed_time, CHINA_TZ))


def _latest_iso(values: Sequence[str | None]) -> str | None:
    parsed = [(_parse_datetime(value), value) for value in values if value]
    valid = [(dt, value) for dt, value in parsed if dt is not None]
    return max(valid, key=lambda pair: pair[0])[1] if valid else None


def _disclosure_datetime(value: Any) -> datetime | None:
    """Parse a disclosure timestamp conservatively.

    Eastmoney often exposes NOTICE_DATE/UPDATE_DATE at 00:00:00 even though the
    underlying field only has day precision.  Treat such values as end-of-day,
    so a morning cutoff cannot accidentally see a report released later that
    same day.
    """

    parsed = _parse_datetime(value, CHINA_TZ)
    if parsed is None:
        return None
    parsed = parsed.astimezone(CHINA_TZ)
    if parsed.time() == time.min:
        return datetime.combine(parsed.date(), time(23, 59, 59), CHINA_TZ)
    return parsed


def _collect_source_ids(value: Any, output: set[str]) -> None:
    if isinstance(value, Mapping):
        source_id = value.get("sourceId")
        if isinstance(source_id, str) and source_id:
            output.add(source_id)
        source_ids = value.get("sourceIds")
        if isinstance(source_ids, Sequence) and not isinstance(source_ids, (str, bytes, bytearray)):
            output.update(item for item in source_ids if isinstance(item, str) and item)
        for child in value.values():
            _collect_source_ids(child, output)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _collect_source_ids(child, output)


def _latest_evidence_datetime(value: Any) -> datetime | None:
    latest: datetime | None = None
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"dataAsOf", "publishedAt"}:
                parsed = _parse_datetime(child, CHINA_TZ)
                if parsed is not None:
                    parsed = parsed.astimezone(CHINA_TZ)
                    latest = parsed if latest is None or parsed > latest else latest
            else:
                child_latest = _latest_evidence_datetime(child)
                if child_latest is not None:
                    latest = child_latest if latest is None or child_latest > latest else latest
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            child_latest = _latest_evidence_datetime(child)
            if child_latest is not None:
                latest = child_latest if latest is None or child_latest > latest else latest
    return latest


def _source_id(prefix: str, discriminator: str = "") -> str:
    suffix = hashlib.sha256(discriminator.encode("utf-8")).hexdigest()[:12] if discriminator else ""
    return f"akshare:{prefix}" + (f":{suffix}" if suffix else "")


def _configured_news_max_age_hours(project_root: Path) -> float:
    """Read the report news threshold, retaining a fail-safe documented default."""

    try:
        document = json.loads((project_root / "config" / "risk.json").read_text(encoding="utf-8"))
        value = float(document["newsMaxAgeHours"])
        if math.isfinite(value) and value > 0:
            return value
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return DEFAULT_NEWS_MAX_AGE_HOURS


def _is_transport_failure(error: BaseException) -> bool:
    """Recognize network failures without treating content/shape errors as outages."""

    if isinstance(error, _MediaLoaderFailure):
        return error.transport_failure

    try:
        import requests

        requests_transport_errors: tuple[type[BaseException], ...] = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ProxyError,
            requests.exceptions.SSLError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        )
    except (ImportError, AttributeError):
        requests_transport_errors = ()

    network_type_names = {
        "ConnectTimeoutError",
        "MaxRetryError",
        "NewConnectionError",
        "ProtocolError",
        "ReadTimeoutError",
        "RemoteDisconnected",
    }
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError, socket.timeout, socket.gaierror)):
            return True
        if requests_transport_errors and isinstance(current, requests_transport_errors):
            return True
        if type(current).__name__ in network_type_names and type(current).__module__.startswith(
            ("requests", "urllib3", "socket", "http.client")
        ):
            return True
        current = current.__cause__ or (
            None if getattr(current, "__suppress_context__", False) else current.__context__
        )
    return False


def _validate_request(request: Mapping[str, Any]) -> tuple[date, datetime, list[dict[str, Any]]]:
    if request.get("schemaVersion") != SCHEMA_VERSION or request.get("task") != TASK_NAME:
        raise ValueError("仅支持 schemaVersion=1.0、task=a_share_morning_context")
    if request.get("timezone") != "Asia/Shanghai":
        raise ValueError("timezone 必须为 Asia/Shanghai")
    target_date = _parse_date(request.get("targetDate"))
    requested_at = _parse_datetime(request.get("requestedAt"), CHINA_TZ)
    if target_date is None or requested_at is None:
        raise ValueError("targetDate 或 requestedAt 格式无效")
    raw_universe = request.get("universe")
    if not isinstance(raw_universe, list) or not raw_universe:
        raise ValueError("universe 必须是非空数组")
    universe: list[dict[str, Any]] = []
    for raw in raw_universe:
        if not isinstance(raw, Mapping):
            raise ValueError("universe 项必须是对象")
        code = normalize_stock_code(raw.get("code"))
        if code is None:
            raise ValueError(f"无效股票代码: {raw.get('code')!r}")
        exchange = str(raw.get("exchange") or "").upper()
        if exchange not in {"SH", "SZ", "BJ"}:
            raise ValueError(f"无效交易所: {exchange!r}")
        universe.append(
            {
                "code": code,
                "exchange": exchange,
                "symbol": str(raw.get("symbol") or f"{code}.{exchange}"),
                "name": str(raw.get("name") or code),
            }
        )
    return target_date, requested_at, universe


def _reject_unclear_units(data: Mapping[str, Any], attempts: int) -> None:
    """Use the same explicit-unit rule as the HTTP envelope/data worker checks."""
    try:
        validate_unit_declarations(data)
    except IndexFetchError as error:
        raise IndexTurnoverError(error.stage, error.detail, attempts) from None


class IndexTurnoverError(Exception):
    """Per-stage failure of the index turnover supplement (never silently swallowed)."""

    def __init__(self, stage: str, detail: str, attempts: int = 0, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail
        self.attempts = attempts
        self.retryable = retryable


def _is_retryable_index_fetch_error(error: BaseException) -> bool:
    """Only connection-level/5xx/429 failures are retried; content or identity errors never are."""

    try:
        import requests

        if isinstance(error, requests.exceptions.ContentDecodingError):
            return False
        if isinstance(error, requests.exceptions.HTTPError):
            status = getattr(error.response, "status_code", None)
            return status is not None and (500 <= status < 600 or status == 429)
    except (ImportError, AttributeError):
        pass
    return _is_transport_failure(error)


class AkShareMorningProvider:
    """Build the contract response from an injectable AkShare-compatible API."""

    def __init__(
        self,
        api: Any | None = None,
        *,
        cache_dir: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        media_source_budget_seconds: float = DEFAULT_MEDIA_SOURCE_BUDGET_SECONDS,
        news_max_age_hours: float | None = None,
        isolate_media_loaders: bool = True,
        cninfo_fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
        cninfo_window_fetcher: Callable[..., dict[str, Any] | None] | None = None,
        collect_documents: bool = True,
        document_fetcher: Callable[..., dict[str, Any]] | None = None,
        staging_dir: str | Path | None = None,
        index_kline_fetcher: Callable[..., Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
        index_clock: Callable[[], float] | None = None,
    ) -> None:
        uses_real_api = api is None
        if uses_real_api and index_kline_fetcher is not None:
            raise ValueError("真实 AkShare 路径不得绕过成交额进程隔离；注入取数器仅用于离线 API")
        if api is None:
            import akshare as api  # Lazy import keeps unit tests independent of the network package.

        self.api = api
        self.clock = clock or (lambda: datetime.now(CHINA_TZ))
        self.monotonic_clock = monotonic_clock or monotonic_time.monotonic
        self.isolate_media_loaders = bool(isolate_media_loaders)
        self.cninfo_fetcher = cninfo_fetcher
        self.cninfo_window_fetcher = cninfo_window_fetcher
        # The HTTP kline fetcher is only the default for the real AkShare provider; an injected
        # (test/offline) api never reaches the network unless a fetcher is injected explicitly.
        self.index_kline_fetcher = index_kline_fetcher or (
            self._fetch_index_kline if uses_real_api else self._offline_index_kline)
        self._sleep = sleep or monotonic_time.sleep
        # Separate from the media budget clock so the two budgets never consume each other's ticks.
        self._index_clock = index_clock or monotonic_time.monotonic
        self._index_isolation_failed = False
        self.collect_documents = collect_documents
        self.document_fetcher = document_fetcher
        self._document_deadline = None
        project_root = Path(__file__).resolve().parents[1]
        self.cache_dir = Path(cache_dir) if cache_dir else project_root / "data" / "inbox" / "akshare-cache"
        self.staging_dir = (Path(staging_dir) if staging_dir
                            else project_root / "work" / "cninfo-staging")
        self._staging_loaded: dict[str, Any] | bool | None = None
        # The request watchlist for this run: staging must cover it exactly so
        # pre-collected and live coverage can never be mixed silently.
        self._request_codes: list[str] | None = None
        self.media_source_budget_seconds = float(media_source_budget_seconds)
        self.news_max_age_hours = float(
            news_max_age_hours
            if news_max_age_hours is not None
            else _configured_news_max_age_hours(project_root)
        )
        if not math.isfinite(self.media_source_budget_seconds) or self.media_source_budget_seconds <= 0:
            raise ValueError("media_source_budget_seconds must be a positive finite number")
        if not math.isfinite(self.news_max_age_hours) or self.news_max_age_hours <= 0:
            raise ValueError("news_max_age_hours must be a positive finite number")

    def _now(self) -> datetime:
        value = self.clock()
        return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)

    def _call(
        self,
        name: str,
        *args: Any,
        _http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        _cap_http_timeout: bool = False,
        **kwargs: Any,
    ) -> Any:
        function = getattr(self.api, name, None)
        if not callable(function):
            raise AttributeError(f"AkShare 接口不存在: {name}")
        # Some upstream helpers print progress. Preserve the bridge's stdout JSON invariant.
        with _requests_default_timeout(
            _http_timeout_seconds,
            cap_explicit_timeout=_cap_http_timeout,
        ):
            with contextlib.redirect_stdout(sys.stderr):
                return function(*args, **kwargs)

    def _cache_path(self, module: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", module)
        return self.cache_dir / f"{safe_name}.json"

    def _write_cache(self, module: str, key: str, payload: ModulePayload) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_path(module)
        temp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "cacheFormatVersion": CACHE_FORMAT_VERSION,
            "module": module,
            "key": key,
            "savedAt": _china_iso(self._now()),
            "data": sanitize_json(payload.data),
            "sources": sanitize_json(payload.sources),
        }
        try:
            temp_path.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temp_path.replace(cache_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _read_cache(self, module: str, key: str) -> ModulePayload | None:
        try:
            document = json.loads(self._cache_path(module).read_text(encoding="utf-8"))
            if not isinstance(document, Mapping):
                return None
            if (
                document.get("schemaVersion") != SCHEMA_VERSION
                or document.get("cacheFormatVersion") != CACHE_FORMAT_VERSION
                or document.get("key") != key
            ):
                return None
            sources = document.get("sources")
            if not isinstance(sources, list) or any(
                not isinstance(source, Mapping) for source in sources
            ):
                return None
            return ModulePayload(
                data=document.get("data"),
                sources=[dict(source) for source in sources],
            )
        except (OSError, ValueError, TypeError):
            return None

    def _module(
        self,
        state: RunState,
        module: str,
        key: str,
        loader: Callable[[], ModulePayload],
        default: Any,
    ) -> ModuleResult:
        try:
            payload = loader()
            payload = ModulePayload(sanitize_json(payload.data), sanitize_json(payload.sources), payload.warnings)
            if isinstance(payload.data, Mapping) and payload.data.get("freshnessStatus") in {"stale", "unverified"}:
                state.warnings.extend(payload.warnings)
                state.warnings.append(f"{module} 未通过应有交易日校验；保留实际日期但不计为当前可用，不写 last-good 缓存。")
                return ModuleResult(payload.data, payload.sources, "stale")
            self._write_cache(module, key, payload)
            state.live_modules += 1
            state.warnings.extend(payload.warnings)
            return ModuleResult(payload.data, payload.sources, "live")
        except Exception as error:
            cached = self._read_cache(module, key)
            detail = _warning_error_detail(error)
            if cached is not None:
                state.cache_hits.append(module)
                state.warnings.append(f"{module} 实时拉取失败，已使用 last-good 缓存（{detail}）。")
                if module.startswith("us_index_"):
                    try:
                        request_cutoff = _parse_datetime(key.removeprefix("us-session-v1|"), CHINA_TZ)
                        if request_cutoff is None or not isinstance(cached.data, Mapping):
                            raise ValueError("cache request/session proof missing")
                        requested_symbol = module.removeprefix("us_index_")
                        if (cached.data.get("symbol") != requested_symbol
                                or cached.data.get("marketSymbol") != requested_symbol):
                            raise ValueError("cache index identity does not match request")
                        if us_index_freshness(cached.data, request_cutoff) != "current":
                            raise ValueError("cache session does not match expected session")
                        validate_us_index_return(cached.data)
                    except (ValueError, TypeError, KeyError):
                        state.warnings.append(f"{module} 缓存没有有效交易日及完整价格/紧邻前收证明，不计为当前可用。")
                        data = {**cached.data, "freshnessStatus": "unverified"} if isinstance(cached.data, Mapping) else cached.data
                        return ModuleResult(data, cached.sources, "stale")
                return ModuleResult(cached.data, cached.sources, "cache")
            state.warnings.append(f"{module} 不可用且无匹配缓存（{detail}）。")
            return ModuleResult(default, [], "unavailable")

    def _validated_media_cache(
        self,
        module: str,
        key: str,
        cutoff: datetime,
    ) -> ModulePayload | None:
        """Return only point-in-time-safe, non-stale cached media evidence."""

        cached = self._read_cache(module, key)
        if cached is None or not isinstance(cached.data, list) or not cached.data:
            return None
        cutoff = cutoff.astimezone(CHINA_TZ)
        oldest_allowed = cutoff - timedelta(hours=self.news_max_age_hours)

        def eligible_timestamp(value: Any) -> bool:
            parsed = _parse_datetime(value, CHINA_TZ)
            if parsed is None:
                return False
            parsed = parsed.astimezone(CHINA_TZ)
            return oldest_allowed <= parsed <= cutoff

        sources_by_id = {
            str(source["id"]): source
            for source in cached.sources
            if isinstance(source, Mapping) and isinstance(source.get("id"), str)
        }
        accepted_items: list[dict[str, Any]] = []
        accepted_source_ids: set[str] = set()
        for raw_item in cached.data:
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            item_source_type = item.get("sourceType")
            if not isinstance(item_source_type, str) or item_source_type not in {
                "media_aggregation",
                "media_article",
            }:
                continue
            if not eligible_timestamp(item.get("publishedAt")) or not eligible_timestamp(
                item.get("dataAsOf")
            ):
                continue
            raw_source_ids = item.get("sourceIds")
            if (
                not isinstance(raw_source_ids, list)
                or not raw_source_ids
                or any(
                    not isinstance(source_id, str) or not source_id.strip()
                    for source_id in raw_source_ids
                )
                or len(set(raw_source_ids)) != len(raw_source_ids)
            ):
                continue
            source_ids = list(raw_source_ids)
            if not source_ids or any(source_id not in sources_by_id for source_id in source_ids):
                continue
            source_records = [sources_by_id[source_id] for source_id in source_ids]
            if any(
                not isinstance(source.get("sourceType"), str)
                or source.get("sourceType") not in {"media_aggregation", "media_article"}
                or not eligible_timestamp(source.get("publishedAt"))
                or not eligible_timestamp(source.get("dataAsOf"))
                for source in source_records
            ):
                continue
            accepted_items.append(item)
            accepted_source_ids.update(source_ids)

        if not accepted_items:
            return None
        accepted_sources = [
            dict(source)
            for source in cached.sources
            if isinstance(source, Mapping) and source.get("id") in accepted_source_ids
        ]
        if len(accepted_sources) != len(accepted_source_ids):
            return None
        return ModulePayload(accepted_items, accepted_sources)

    def _media_fallback(
        self,
        state: RunState,
        module: str,
        key: str,
        cutoff: datetime,
        default: Any,
        detail: str,
        *,
        skipped: bool,
    ) -> ModuleResult:
        cached = self._validated_media_cache(module, key, cutoff)
        action = "已跳过实时请求" if skipped else "实时拉取失败"
        if cached is not None:
            state.cache_hits.append(module)
            state.warnings.append(
                f"{module} {action}；已使用可证明早于截止时间且未超过 "
                f"{self.news_max_age_hours:g} 小时阈值的媒体缓存（{detail}）。"
            )
            return ModuleResult(cached.data, cached.sources, "cache")
        state.warnings.append(
            f"{module} {action}；无满足截止时间与 {self.news_max_age_hours:g} 小时新闻阈值的缓存，"
            f"本模块标记 unavailable（{detail}）。"
        )
        return ModuleResult(default, [], "unavailable")

    def _run_media_loader_isolated(
        self,
        loader: Callable[[float], ModulePayload],
        timeout_seconds: float,
        deadline: float,
    ) -> ModulePayload:
        """Execute media work behind a killable POSIX process boundary.

        The child never writes caches or mutates run state.  It returns a
        bounded JSON envelope through a mode-0600 temporary file, so the child
        can never block on a full pipe while the parent waits.  The parent
        validates the completed file and is solely responsible for accepting
        and persisting a result.  A timed-out child is SIGKILLed and joined so
        no live worker or zombie remains.
        """

        if not self.isolate_media_loaders:
            return loader(timeout_seconds)
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
            or "fork" not in multiprocessing.get_all_start_methods()
        ):
            raise _MediaIsolationUnavailable(
                "safe POSIX main-thread fork boundary is unavailable; live media skipped"
            )

        process_context = multiprocessing.get_context("fork")
        result_fd, result_name = tempfile.mkstemp(prefix="astock-media-", suffix=".json")
        os.close(result_fd)
        os.chmod(result_name, 0o600)

        def child_main() -> None:
            try:
                payload = loader(timeout_seconds)
                envelope: dict[str, Any] = {
                    "status": "ok",
                    "data": sanitize_json(payload.data),
                    "sources": sanitize_json(payload.sources),
                    "warnings": sanitize_json(payload.warnings),
                }
            except BaseException as error:
                envelope = {
                    "status": "error",
                    "detail": _warning_error_detail(error),
                    "transportFailure": _is_transport_failure(error),
                }
            try:
                encoded = json.dumps(
                    envelope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(encoded) > MAX_MEDIA_SUBPROCESS_BYTES:
                    encoded = json.dumps(
                        {
                            "status": "isolation_error",
                            "detail": "media result exceeded bounded IPC limit",
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                with open(result_name, "wb") as result_file:
                    result_file.write(encoded)
                    result_file.flush()
                    os.fsync(result_file.fileno())
            except BaseException:
                # Parent observes an empty/invalid file and fails closed.
                pass

        process = process_context.Process(target=child_main, daemon=True)
        process_started = False
        try:
            process.start()
            process_started = True
            remaining = deadline - self.monotonic_clock()
            if remaining <= 0:
                raise _MediaBudgetExceeded("shared media wall-clock budget expired")
            process.join(remaining)
            if process.is_alive():
                raise _MediaBudgetExceeded("shared media wall-clock budget expired")
            if self.monotonic_clock() >= deadline:
                raise _MediaBudgetExceeded("shared media wall-clock budget expired")
            try:
                result_path = Path(result_name)
                result_size = result_path.stat().st_size
                if result_size <= 0 or result_size > MAX_MEDIA_SUBPROCESS_BYTES:
                    raise ValueError("isolated result size is outside the allowed bound")
                encoded = result_path.read_bytes()
            except (OSError, ValueError) as error:
                raise _MediaIsolationUnavailable(
                    "isolated media loader produced no bounded result"
                ) from error
            try:
                envelope = json.loads(encoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise _MediaIsolationUnavailable(
                    "isolated media loader returned an invalid JSON envelope"
                ) from error
            if not isinstance(envelope, Mapping) or not isinstance(envelope.get("status"), str):
                raise _MediaIsolationUnavailable(
                    "isolated media loader returned an invalid envelope shape"
                )
            if envelope["status"] == "error":
                detail = envelope.get("detail")
                transport_failure = envelope.get("transportFailure")
                if not isinstance(detail, str) or not isinstance(transport_failure, bool):
                    raise _MediaIsolationUnavailable(
                        "isolated media loader returned invalid failure metadata"
                    )
                raise _MediaLoaderFailure(
                    detail,
                    transport_failure=transport_failure,
                )
            if envelope["status"] != "ok":
                raise _MediaIsolationUnavailable(
                    "isolated media loader rejected an unsafe or oversized result"
                )
            sources = envelope.get("sources")
            warnings = envelope.get("warnings")
            if (
                not isinstance(sources, list)
                or not isinstance(warnings, list)
                or any(not isinstance(warning, str) for warning in warnings)
            ):
                raise _MediaIsolationUnavailable(
                    "isolated media loader returned invalid payload metadata"
                )
            return ModulePayload(envelope.get("data"), sources, warnings)
        finally:
            if process_started and process.is_alive():
                # kill() is SIGKILL on POSIX, so a loader that catches
                # TimeoutError/Exception/BaseException cannot evade the limit.
                process.kill()
            if process_started:
                process.join()
                process.close()
            try:
                os.unlink(result_name)
            except FileNotFoundError:
                pass

    def _media_module(
        self,
        state: RunState,
        module: str,
        key: str,
        loader: Callable[[float], ModulePayload],
        default: Any,
        cutoff: datetime,
    ) -> ModuleResult:
        """Run one Eastmoney media module inside a shared per-run budget."""

        if state.eastmoney_media_deadline is None:
            state.eastmoney_media_deadline = (
                self.monotonic_clock() + self.media_source_budget_seconds
            )
        if state.eastmoney_media_circuit_reason is not None:
            return self._media_fallback(
                state,
                module,
                key,
                cutoff,
                default,
                f"东方财富媒体熔断已开启：{state.eastmoney_media_circuit_reason}",
                skipped=True,
            )

        remaining = state.eastmoney_media_deadline - self.monotonic_clock()
        if remaining <= 0:
            state.eastmoney_media_circuit_reason = (
                f"共享媒体预算 {self.media_source_budget_seconds:g} 秒已耗尽"
            )
            state.warnings.append(
                "东方财富媒体熔断已开启：本轮共享媒体时间预算已耗尽，"
                "剩余 global_news/stock_news 不再访问上游。"
            )
            return self._media_fallback(
                state,
                module,
                key,
                cutoff,
                default,
                state.eastmoney_media_circuit_reason,
                skipped=True,
            )

        timeout_seconds = max(0.001, min(DEFAULT_HTTP_TIMEOUT_SECONDS, remaining))
        try:
            payload = self._run_media_loader_isolated(
                loader,
                timeout_seconds,
                state.eastmoney_media_deadline,
            )
            completed_at = self.monotonic_clock()
            if completed_at >= state.eastmoney_media_deadline:
                state.eastmoney_media_circuit_reason = (
                    f"{module} 返回时共享媒体预算 {self.media_source_budget_seconds:g} 秒已耗尽"
                )
                state.warnings.append(
                    "东方财富媒体熔断已开启：媒体 loader 返回时已达到共享 wall-clock 截止时间，"
                    "该结果已丢弃且不会写入缓存或标记 live。"
                )
                return self._media_fallback(
                    state,
                    module,
                    key,
                    cutoff,
                    default,
                    state.eastmoney_media_circuit_reason,
                    skipped=False,
                )
            payload = ModulePayload(
                sanitize_json(payload.data),
                sanitize_json(payload.sources),
                payload.warnings,
            )
            self._write_cache(module, key, payload)
            state.live_modules += 1
            state.warnings.extend(payload.warnings)
            return ModuleResult(payload.data, payload.sources, "live")
        except Exception as error:
            detail = _warning_error_detail(error)
            if isinstance(error, _MediaBudgetExceeded):
                state.eastmoney_media_circuit_reason = (
                    f"{module} 达到共享媒体 wall-clock 截止时间"
                )
                state.warnings.append(
                    "东方财富媒体熔断已开启：本轮共享媒体时间预算已耗尽，"
                    "当前请求已终止，剩余 global_news/stock_news 不再访问上游。"
                )
            elif isinstance(error, _MediaIsolationUnavailable):
                state.eastmoney_media_circuit_reason = (
                    f"{module} 无法建立安全媒体进程边界（{detail}）"
                )
                state.warnings.append(
                    "东方财富媒体熔断已开启：无法建立或验证可终止的媒体子进程边界，"
                    "已 fail-closed 跳过本轮剩余 global_news/stock_news。"
                )
            elif _is_transport_failure(error):
                state.eastmoney_media_circuit_reason = f"{module} 网络失败（{detail}）"
                state.warnings.append(
                    "东方财富媒体熔断已开启：检测到明确的超时或传输故障，"
                    "本轮剩余 global_news/stock_news 不再访问上游。"
                )
            return self._media_fallback(
                state,
                module,
                key,
                cutoff,
                default,
                detail,
                skipped=False,
            )

    def _calendar(self, target: date, now: datetime) -> ModulePayload:
        rows = _records(self._call("tool_trade_date_hist_sina"))
        days = sorted({day for row in rows if (day := _parse_date(_pick(row, "trade_date", "交易日")))})
        if not days:
            raise ValueError("交易日历为空")
        previous = max((day for day in days if day < target), default=None)
        previous_previous = max((day for day in days if previous and day < previous), default=None)
        following = min((day for day in days if day > target), default=None)
        source_id = _source_id("sina:trade-calendar")
        data_as_of = _china_iso(now)
        return ModulePayload(
            {
                "targetDate": target.isoformat(),
                "status": "trading_day" if target in days else "non_trading_day",
                "previousTradingDate": previous.isoformat() if previous else None,
                "previousSessionPreviousTradingDate": previous_previous.isoformat() if previous_previous else None,
                "nextTradingDate": following.isoformat() if following else None,
                "dataAsOf": data_as_of,
                "sourceIds": [source_id],
            },
            [
                {
                    "id": source_id,
                    "title": "新浪财经交易日历",
                    "url": "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt",
                    "publisher": "新浪财经",
                    "sourceType": "trading_calendar_data",
                    "publishedAt": None,
                    "dataAsOf": data_as_of,
                }
            ],
        )

    def _a_history(
        self,
        universe: list[dict[str, Any]],
        session_day: date | None,
        expected_previous_day: date | None,
    ) -> ModulePayload:
        if session_day is None or expected_previous_day is None:
            raise ValueError("交易日历未提供上一交易日及其紧邻前序交易日，无法请求个股日线")
        start = min(
            expected_previous_day,
            session_day - timedelta(days=HISTORICAL_LOOKBACK_DAYS),
        )
        start_text = start.strftime("%Y%m%d")
        end_text = session_day.strftime("%Y%m%d")
        warnings: list[str] = []
        quotes: dict[str, dict[str, Any] | None] = {}
        sources: list[dict[str, Any]] = []
        for stock in universe:
            code = stock["code"]
            prefixed_symbol = f"{stock['exchange'].lower()}{code}"
            attempts = (
                (
                    "stock_zh_a_daily",
                    {"symbol": prefixed_symbol, "start_date": start_text, "end_date": end_text, "adjust": ""},
                    "sina:a-share-daily",
                    "新浪财经",
                    f"https://finance.sina.com.cn/realstock/company/{prefixed_symbol}/nc.shtml",
                ),
                (
                    "stock_zh_a_hist",
                    {"symbol": code, "period": "daily", "start_date": start_text, "end_date": end_text, "adjust": ""},
                    "eastmoney:a-share-daily",
                    "东方财富",
                    f"https://quote.eastmoney.com/{prefixed_symbol}.html",
                ),
            )
            fields: dict[str, float | int | None] | None = None
            selected: tuple[str, str, str] | None = None
            errors: list[str] = []
            for endpoint, kwargs, source_prefix, publisher, url in attempts:
                try:
                    rows = _records(self._call(endpoint, **kwargs))
                    fields = _closed_daily_fields(
                        rows,
                        session_day,
                        expected_previous_day,
                        label=f"{code} {stock['name']}",
                        warnings=warnings,
                    )
                    selected = (source_prefix, publisher, url)
                    if endpoint != "stock_zh_a_daily":
                        warnings.append(f"{code} 新浪日线不可用，已回退到东方财富未复权日线。")
                    break
                except Exception as error:
                    errors.append(f"{endpoint}: {_warning_error_detail(error)}")
            if fields is None or selected is None:
                quotes[code] = None
                warnings.append(f"{code} 上一交易日日线不可用（{'；'.join(errors)}）。")
                continue

            source_prefix, publisher, url = selected
            source_id = _source_id(source_prefix, f"{code}|{session_day.isoformat()}|unadjusted")
            data_as_of = _session_iso(session_day)
            quotes[code] = {
                "code": code,
                "name": stock["name"],
                "last": fields["close"],
                "change": fields["change"],
                "changePercent": fields["changePercent"],
                "previousClose": fields["previousClose"],
                "previousCloseDate": expected_previous_day.isoformat(),
                "open": fields["open"],
                "high": fields["high"],
                "low": fields["low"],
                "volume": fields["volume"],
                "turnover": fields["turnover"],
                "units": dict(A_SHARE_QUOTE_UNITS),
                "dataAsOf": data_as_of,
                "sourceIds": [source_id],
            }
            sources.append(
                {
                    "id": source_id,
                    "title": f"{publisher}{stock['name']}未复权历史日线",
                    "url": url,
                    "publisher": publisher,
                    "sourceType": "market_data",
                    "publishedAt": None,
                    "dataAsOf": data_as_of,
                }
            )
        if not any(quote is not None for quote in quotes.values()):
            raise ValueError(f"股票池均无可核验的上一交易日日线（{'；'.join(warnings)}）")
        source_ids = [source["id"] for source in sources]
        return ModulePayload(
            {
                "quotes": quotes,
                "dataAsOf": _session_iso(session_day),
                "sourceIds": source_ids,
            },
            sources,
            warnings,
        )

    @staticmethod
    def _fetch_index_kline(
        secid: str,
        day_text: str,
        timeout: tuple[float, float],
        total_seconds: float | None = None,
        *,
        absolute_deadline: float | None = None,
    ) -> Mapping[str, Any]:
        """One request in an exec-isolated process, with bounded kill/reap and IPC.

        DNS, response headers and slow body reads are all inside the killable boundary.
        Unlike a daemon thread, the worker is terminated before this call returns on
        timeout. No proxy, redirect, adapter retry or alternate endpoint is permitted.
        """
        cap = float(total_seconds) if total_seconds is not None else float(timeout[0]) + float(timeout[1])
        try:
            return fetch_kline_isolated(
                secid, day_text, timeout, cap,
                url=INDEX_TURNOVER_KLINE_URL, max_body_bytes=INDEX_TURNOVER_MAX_BODY_BYTES,
                absolute_deadline=absolute_deadline,
            )
        except IndexFetchError as error:
            raise IndexTurnoverError(error.stage, error.detail, retryable=error.retryable) from None

    @staticmethod
    def _offline_index_kline(
        secid: str, day_text: str, timeout: tuple[float, float], total_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        raise IndexTurnoverError("kline_request", "未配置成交额 kline 取数器（离线/注入 API）")

    def _fetch_with_deadline(
        self, secid: str, day_text: str, timeout: tuple[float, float], remaining: float, deadline: float,
    ) -> Mapping[str, Any]:
        """Use the isolated real fetcher, or an explicitly injected offline fixture.

        Fixed unit-test fixtures stay in-process so call accounting is observable;
        they do not prove termination. Real-api construction rejects such injection,
        and default-path HTTP/process tests exercise the actual exec boundary.
        """
        if self._index_isolation_failed:
            raise IndexTurnoverError("isolation", "此前取数隔离失败，本轮不再发送请求")
        try:
            if self.index_kline_fetcher is self._fetch_index_kline:
                # Production's built-in transport (also used by process tests)
                # retains the same absolute deadline through exec and cleanup.
                value = self.index_kline_fetcher(
                    secid, day_text, timeout, total_seconds=remaining, absolute_deadline=deadline,
                )
            else:
                # Explicit offline fixtures keep their existing callable contract;
                # real-api construction rejects this injected path above.
                value = self.index_kline_fetcher(secid, day_text, timeout, total_seconds=remaining)
        except IndexTurnoverError as error:
            if error.stage == "isolation":
                self._index_isolation_failed = True
            raise
        if self._index_clock() >= deadline:
            raise IndexTurnoverError("budget", "请求在总预算截止之后才返回，结果丢弃")
        return value

    def _index_turnover_enrichment(
        self,
        symbol: str,
        session_day: date,
        primary_fields: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> ModulePayload:
        """Same-index, same-session CNY amount from one bounded kline request per attempt.

        Sina's valid OHLC stays authoritative.  Index identity is taken from the response
        (``code`` and ``market`` must equal the declared index), the single row must be the
        requested session, its OHLC must match the primary source, and the amount must be a
        positive CNY-yuan figure in the plausible range.  Nothing is estimated or reused from
        another day; every failure carries its stage and attempt count.
        """

        if symbol not in A_SHARE_INDEXES:
            raise IndexTurnoverError("input", "成交额补源仅允许已声明的三大 A 股指数")
        expected_market, code = INDEX_TURNOVER_SECIDS[symbol]
        exchange = "SH" if expected_market == 1 else "SZ"
        secid = f"{expected_market}.{code}"
        day_text = session_day.strftime("%Y%m%d")
        if deadline is None:
            deadline = self._index_clock() + INDEX_TURNOVER_TOTAL_BUDGET_SECONDS

        data: Mapping[str, Any] | None = None
        attempts = 0
        last_error: BaseException | None = None
        while attempts < INDEX_TURNOVER_MAX_ATTEMPTS:
            if self._index_isolation_failed:
                raise IndexTurnoverError("isolation", "此前取数隔离失败，本轮不再发送请求", attempts)
            remaining = deadline - self._index_clock()
            if remaining <= 0:
                raise IndexTurnoverError(
                    "budget",
                    f"总执行预算 {INDEX_TURNOVER_TOTAL_BUDGET_SECONDS:g}s 已用尽"
                    + (f"，最后错误 {type(last_error).__name__}" if last_error else ""),
                    attempts,
                )
            attempts += 1
            connect_timeout, read_timeout = INDEX_TURNOVER_ATTEMPT_TIMEOUT_SECONDS
            try:
                data = self._fetch_with_deadline(
                    secid, day_text, (min(connect_timeout, remaining), min(read_timeout, remaining)),
                    remaining, deadline,
                )
                break
            except IndexTurnoverError as error:
                last_error = error
                if not error.retryable or attempts >= INDEX_TURNOVER_MAX_ATTEMPTS:
                    raise IndexTurnoverError(error.stage, error.detail, attempts) from None
                delay = min(INDEX_TURNOVER_RETRY_DELAY_SECONDS, max(0.0, deadline - self._index_clock()))
                if delay > 0:
                    self._sleep(delay)
            except Exception as error:  # noqa: BLE001 - classified below
                last_error = error
                if not _is_retryable_index_fetch_error(error) or attempts >= INDEX_TURNOVER_MAX_ATTEMPTS:
                    raise IndexTurnoverError(
                        "kline_request", _warning_error_detail(error), attempts,
                    ) from None
                delay = min(INDEX_TURNOVER_RETRY_DELAY_SECONDS, max(0.0, deadline - self._index_clock()))
                if delay > 0:
                    self._sleep(delay)
        if data is None:
            raise IndexTurnoverError("kline_request", "未取得响应", attempts)

        # Stage: identity (from the response itself).
        response_code = str(data.get("code") or "").strip()
        response_market = data.get("market")
        if (response_code != code or isinstance(response_market, bool)
                or not isinstance(response_market, (int, float, str))
                or str(response_market).strip() != str(expected_market)):
            raise IndexTurnoverError(
                "identity", f"响应身份 code={response_code!r} market={response_market!r} 与 {symbol} 不符", attempts,
            )
        _reject_unclear_units(data, attempts)

        # Stage: trading day (exactly one row for the requested session).
        klines = data.get("klines")
        if not isinstance(klines, Sequence) or isinstance(klines, (str, bytes)):
            raise IndexTurnoverError("trading_day", "响应缺少 klines 列表", attempts)
        rows = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) >= 7 and _parse_date(parts[0]) == session_day:
                rows.append(parts)
        if len(rows) != 1:
            raise IndexTurnoverError("trading_day", f"缺少或重复当日 {session_day.isoformat()} 行（{len(rows)} 行）", attempts)
        _, open_text, close_text, high_text, low_text, _, amount_text = rows[0][:7]

        # Stage: OHLC cross-check against the primary source (same index, same session).
        for field_name, text in (("open", open_text), ("high", high_text), ("low", low_text), ("close", close_text)):
            secondary = _positive_number(text)
            primary = _positive_number(primary_fields.get(field_name))
            if secondary is None or primary is None or not math.isclose(
                float(secondary), float(primary), rel_tol=1e-6, abs_tol=0.011,
            ):
                raise IndexTurnoverError("ohlc_crosscheck", f"{field_name} 与主源同日指数不一致", attempts)

        # Stage: amount + unit plausibility (CNY yuan only; never estimated).
        amount = _positive_number(amount_text)
        if amount is None:
            raise IndexTurnoverError("amount", "缺少有效的人民币元成交额", attempts)
        low_bound, high_bound = INDEX_TURNOVER_YUAN_RANGE
        if not low_bound <= float(amount) <= high_bound:
            raise IndexTurnoverError("unit", f"成交额 {amount} 不在人民币元的合理范围内", attempts)

        source_id = _source_id(
            "eastmoney:a-share-index-turnover", f"{symbol}|{session_day.isoformat()}|CNY",
        )
        data_as_of = _session_iso(session_day)
        return ModulePayload(
            {"turnover": amount, "dataAsOf": data_as_of, "sourceIds": [source_id]},
            [{
                "id": source_id,
                "title": f"东方财富{A_SHARE_INDEXES[symbol]}同日指数成交额",
                "url": f"https://quote.eastmoney.com/zs{code}.html",
                "publisher": "东方财富",
                "sourceType": "market_data",
                "publishedAt": None,
                "dataAsOf": data_as_of,
                "api": "eastmoney_index_kline",
                "apiContract": INDEX_TURNOVER_KLINE_URL,
                "symbol": symbol,
                "exchange": exchange,
                "marketId": expected_market,
                "sessionDate": session_day.isoformat(),
                "fields": ["turnover"],
                "units": {"turnover": "CNY"},
                "identityCheck": "kline_response_code_market_and_same_session_ohlc",
            }],
        )

    def _index_history(
        self,
        session_day: date | None,
        expected_previous_day: date | None,
    ) -> ModulePayload:
        if session_day is None or expected_previous_day is None:
            raise ValueError("交易日历未提供上一交易日及其紧邻前序交易日，无法请求指数日线")
        start_text = min(
            expected_previous_day,
            session_day - timedelta(days=HISTORICAL_LOOKBACK_DAYS),
        ).strftime("%Y%m%d")
        end_text = session_day.strftime("%Y%m%d")
        warnings: list[str] = []
        normalized: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        turnover_deadline: float | None = None  # one shared wall-clock budget for all three indices
        for symbol, name in A_SHARE_INDEXES.items():
            attempts = (
                (
                    "stock_zh_index_daily",
                    {"symbol": symbol},
                    "sina:a-share-index-daily",
                    "新浪财经",
                    f"https://finance.sina.com.cn/realstock/company/{symbol}/nc.shtml",
                ),
                (
                    "stock_zh_index_daily_em",
                    {"symbol": symbol, "start_date": start_text, "end_date": end_text},
                    "eastmoney:a-share-index-daily",
                    "东方财富",
                    f"https://quote.eastmoney.com/zs{symbol[2:]}.html",
                ),
            )
            fields: dict[str, float | int | None] | None = None
            selected: tuple[str, str, str] | None = None
            errors: list[str] = []
            for endpoint, kwargs, source_prefix, publisher, url in attempts:
                try:
                    rows = _records(self._call(endpoint, **kwargs))
                    fields = _closed_daily_fields(
                        rows,
                        session_day,
                        expected_previous_day,
                        label=f"{symbol} {name}",
                        warnings=warnings,
                    )
                    selected = (source_prefix, publisher, url)
                    if endpoint != "stock_zh_index_daily":
                        warnings.append(f"{symbol} 新浪指数日线不可用，已回退到东方财富指数日线。")
                    break
                except Exception as error:
                    errors.append(f"{endpoint}: {_warning_error_detail(error)}")
            if fields is None or selected is None:
                warnings.append(f"{symbol} {name} 上一交易日日线不可用（{'；'.join(errors)}）。")
                continue

            source_prefix, publisher, url = selected
            source_id = _source_id(source_prefix, f"{symbol}|{session_day.isoformat()}")
            data_as_of = _session_iso(session_day)
            turnover_source_ids = [source_id] if fields["turnover"] is not None else []
            if fields["turnover"] is None:
                if turnover_deadline is None:
                    turnover_deadline = self._index_clock() + INDEX_TURNOVER_TOTAL_BUDGET_SECONDS
                try:
                    enriched = self._index_turnover_enrichment(
                        symbol, session_day, fields, deadline=turnover_deadline)
                    fields["turnover"] = enriched.data["turnover"]
                    turnover_source_ids = list(enriched.data["sourceIds"])
                    sources.extend(enriched.sources)
                    missing_warning = (
                        f"{symbol} {name} {session_day.isoformat()} "
                        "成交额缺失或不大于 0，已置为 null。"
                    )
                    warnings[:] = [warning for warning in warnings if warning != missing_warning]
                except IndexTurnoverError as error:
                    warnings.append(
                        f"{symbol} 指数成交额补源失败[阶段={error.stage}，尝试={error.attempts}次]，"
                        f"保留主源价格并将成交额保持 null（{error.detail}）。"
                    )
                except Exception as error:
                    warnings.append(
                        f"{symbol} 指数成交额补源失败[阶段=unexpected，尝试=未知]，保留主源价格并将成交额保持 null"
                        f"（{type(error).__name__}: {_warning_error_detail(error)}）。"
                    )
            normalized.append(
                {
                    "code": symbol,
                    "name": name,
                    **fields,
                    "previousCloseDate": expected_previous_day.isoformat(),
                    "units": dict(A_SHARE_INDEX_UNITS),
                    "dataAsOf": data_as_of,
                    "sourceIds": [source_id, *(
                        turnover_id for turnover_id in turnover_source_ids if turnover_id != source_id
                    )],
                    "priceSourceIds": [source_id],
                    "turnoverSourceIds": turnover_source_ids,
                    "fieldSourceIds": {
                        "open": [source_id], "high": [source_id],
                        "low": [source_id], "close": [source_id],
                        "previousClose": [source_id], "volume": [source_id],
                        "turnover": turnover_source_ids,
                    },
                }
            )
            sources.append(
                {
                    "id": source_id,
                    "title": f"{publisher}{name}历史日线",
                    "url": url,
                    "publisher": publisher,
                    "sourceType": "market_data",
                    "publishedAt": None,
                    "dataAsOf": data_as_of,
                }
            )
        if not normalized:
            raise ValueError(f"三大指数均无可核验的上一交易日日线（{'；'.join(warnings)}）")
        return ModulePayload(normalized, sources, warnings)

    def _market_turnover(self, session_day: date | None) -> ModulePayload:
        if session_day is None:
            raise ValueError("交易日历未提供上一交易日，无法请求交易所成交概况")
        day_text = session_day.strftime("%Y%m%d")
        sse_rows = _records(self._call("stock_sse_deal_daily", date=day_text))
        sse_row = next(
            (row for row in sse_rows if str(_pick(row, "单日情况", "项目") or "").strip() == "成交金额"),
            None,
        )
        if sse_row is None:
            raise ValueError("上交所每日概况缺少成交金额行")
        main_a = _positive_number(_pick(sse_row, "主板A", "主板A股"))
        star = _positive_number(_pick(sse_row, "科创板"))
        if main_a is None or star is None:
            raise ValueError("上交所每日概况缺少主板A或科创板成交金额")
        # 上交所每日概况金额以亿元展示；排除 B 股和股票回购。
        shanghai = (float(main_a) + float(star)) * 100_000_000

        szse_rows = _records(self._call("stock_szse_summary", date=day_text))
        category_amounts = [
            (str(_pick(row, "证券类别") or "").strip(), _number(_pick(row, "成交金额")))
            for row in szse_rows
        ]

        def exact_category_amount(category_name: str, *, allow_zero: bool = False) -> float:
            matches = [amount for category, amount in category_amounts if category == category_name]
            if len(matches) != 1 or matches[0] is None:
                raise ValueError(f"深交所市场总貌缺少或重复必需类别：{category_name}")
            amount = float(matches[0])
            if not math.isfinite(amount) or amount < 0 or (amount == 0 and not allow_zero):
                raise ValueError(f"深交所 {category_name} 成交金额无效")
            return amount

        sz_main_a = exact_category_amount("主板A股")
        chinext_a = exact_category_amount("创业板A股")
        stock_total = exact_category_amount("股票")
        b_categories = [
            (category, amount)
            for category, amount in category_amounts
            if "B股" in category
        ]
        if len(b_categories) != 1 or b_categories[0][1] is None:
            raise ValueError("深交所市场总貌缺少或重复B股成交金额类别")
        b_amount = float(b_categories[0][1])
        if not math.isfinite(b_amount) or b_amount < 0:
            raise ValueError("深交所B股成交金额无效")

        shenzhen = sz_main_a + chinext_a
        implied_a_share = stock_total - b_amount
        if implied_a_share <= 0 or not math.isclose(
            shenzhen,
            implied_a_share,
            rel_tol=1e-6,
            abs_tol=100.0,
        ):
            raise ValueError("深交所主板A股+创业板A股与股票-B股成交金额口径不闭合")
        total = shanghai + shenzhen
        if not math.isfinite(total) or total <= 0:
            raise ValueError("两市成交额汇总无效")

        data_as_of = _session_iso(session_day)
        sse_source = _source_id("sse:daily-market-summary", session_day.isoformat())
        szse_source = _source_id("szse:daily-market-summary", session_day.isoformat())
        return ModulePayload(
            {
                "totalTurnover": _rounded_number(total, 2),
                "byExchange": {
                    "SH": _rounded_number(shanghai, 2),
                    "SZ": _rounded_number(shenzhen, 2),
                },
                "dataAsOf": data_as_of,
                "sourceIds": [sse_source, szse_source],
            },
            [
                {
                    "id": sse_source,
                    "title": "上海证券交易所每日股票成交概况",
                    "url": "https://www.sse.com.cn/market/stockdata/overview/day/",
                    "publisher": "上海证券交易所",
                    "sourceType": "market_data",
                    "publishedAt": None,
                    "dataAsOf": data_as_of,
                },
                {
                    "id": szse_source,
                    "title": "深圳证券交易所证券类别统计",
                    "url": "https://www.szse.cn/market/overview/index.html",
                    "publisher": "深圳证券交易所",
                    "sourceType": "market_data",
                    "publishedAt": None,
                    "dataAsOf": data_as_of,
                },
            ],
        )

    def _market_breadth(self, session_day: date | None) -> ModulePayload:
        if session_day is None:
            raise ValueError("交易日历未提供上一交易日，无法请求市场广度")
        rows = _records(self._call("stock_market_activity_legu"))
        values = {
            str(_pick(row, "item", "项目", "名称") or "").strip(): _pick(row, "value", "数值")
            for row in rows
        }
        measured_at = _parse_datetime(values.get("统计日期"), CHINA_TZ)
        if measured_at is None or measured_at.astimezone(CHINA_TZ).date() != session_day:
            raise ValueError("市场广度统计日期与上一交易日不一致")
        breadth = {
            "advancers": _integer(values.get("上涨")),
            "decliners": _integer(values.get("下跌")),
            "unchanged": _integer(values.get("平盘")),
        }
        counts = list(breadth.values())
        if any(value is None or value < 0 for value in counts) or sum(counts) <= 0:
            raise ValueError("市场广度核心字段缺失、为负数或合计为 0")
        if (breadth["advancers"] or 0) + (breadth["decliners"] or 0) <= 0:
            raise ValueError("市场广度上涨与下跌家数同时为 0")
        source_id = _source_id("legulegu:market-activity", session_day.isoformat())
        data_as_of = _china_iso(measured_at)
        return ModulePayload(
            {"breadth": breadth, "dataAsOf": data_as_of, "sourceIds": [source_id]},
            [
                {
                    "id": source_id,
                    "title": "乐咕乐股赚钱效应收盘统计",
                    "url": "https://www.legulegu.com/stockdata/market-activity",
                    "publisher": "乐咕乐股",
                    "sourceType": "market_data",
                    "publishedAt": None,
                    "dataAsOf": data_as_of,
                }
            ],
        )

    def _us_index(self, symbol: str, requested_at: datetime) -> ModulePayload:
        expected_session = latest_completed_us_session(requested_at)
        rows = _records(self._call("index_us_stock_sina", symbol=symbol))
        candidates: list[tuple[datetime, Mapping[str, Any]]] = []
        for row in rows:
            row_date = _parse_date(_pick(row, "date", "日期"))
            if row_date is None:
                continue
            # Sina can return decades of history. Only the recent comparison
            # window is needed; unsupported ancient years must not discard an
            # otherwise valid latest session.
            if row_date < expected_session - timedelta(days=HISTORICAL_LOOKBACK_DAYS):
                continue
            try:
                close_time = us_equity_session_close(row_date)
            except ValueError:
                # The request's expected session already checked calendar
                # coverage above. Ignore out-of-scope historical/future rows.
                continue
            if close_time is None:
                continue
            close_time = close_time.astimezone(CHINA_TZ)
            if close_time <= requested_at.astimezone(CHINA_TZ):
                candidates.append((close_time, row))
        if not candidates:
            raise ValueError(f"{symbol} 无截止时间之前的日线")
        candidates.sort(key=lambda item: item[0])
        close_time, row = candidates[-1]
        session_day = close_time.astimezone(NEW_YORK_TZ).date()
        if sum(candidate_close == close_time for candidate_close, _ in candidates) != 1:
            raise ValueError(f"{symbol} 同一交易日 {session_day.isoformat()} 日线重复，不能选择任意一条")
        price_fields = {
            "open": _positive_number(_pick(row, "open", "开盘")),
            "high": _positive_number(_pick(row, "high", "最高")),
            "low": _positive_number(_pick(row, "low", "最低")),
            "close": _positive_number(_pick(row, "close", "收盘")),
        }
        if any(value is None for value in price_fields.values()):
            raise ValueError(f"{symbol} 当日 OHLC 缺失、非有限数或不大于 0")
        close, open_price, high, low = (
            price_fields["close"], price_fields["open"], price_fields["high"], price_fields["low"],
        )
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError(f"{symbol} 当日 OHLC 算术关系无效")

        # A daily return may only compare adjacent calendar-declared sessions;
        # the last available older row is not necessarily yesterday's close.
        previous_session = latest_completed_us_session(close_time - timedelta(microseconds=1))
        previous_rows = [
            previous_row for previous_time, previous_row in candidates
            if previous_time.astimezone(NEW_YORK_TZ).date() == previous_session
        ]
        if len(previous_rows) > 1:
            raise ValueError(f"{symbol} 紧邻前序交易日 {previous_session.isoformat()} 日线重复")
        previous_close = (
            _positive_number(_pick(previous_rows[0], "close", "收盘")) if previous_rows else None
        )
        change = close - previous_close if previous_close is not None else None
        change_percent = change / previous_close * 100 if previous_close is not None else None
        source_id = _source_id(f"sina:us-index:{symbol}")
        data_as_of = _china_iso(close_time)
        freshness_status = "current" if session_day == expected_session else "stale"
        warnings = []
        if freshness_status == "stale":
            warnings.append(
                f"{US_INDEXES[symbol]}仅取得 {session_day.isoformat()} 收盘；"
                f"截止前最近应完成交易日为 {expected_session.isoformat()}，保留旧值并标记陈旧，不计实时成功。"
            )
        if previous_close is None:
            freshness_status = "unverified"
            warnings.append(
                f"{US_INDEXES[symbol]}缺少有效的紧邻前序交易日 {previous_session.isoformat()} 收盘；"
                "保留当前价格，前收与日涨跌置为 null，不用更早日期冒充前收、不计实时成功。"
            )
        session_evidence = {
            "marketCalendar": US_EQUITY_CALENDAR_ID,
            "marketSymbol": symbol,
            "sessionDate": session_day.isoformat(),
            "expectedSessionDate": expected_session.isoformat(),
            "freshnessStatus": freshness_status,
        }
        item = {
            "symbol": symbol,
            "name": US_INDEXES[symbol],
            **session_evidence,
            **price_fields,
            "previousClose": previous_close,
            "previousCloseDate": previous_session.isoformat(),
            "change": _number(change),
            "changePercent": _number(change_percent),
            "volume": _number(_pick(row, "volume", "成交量")),
            "amount": _number(_pick(row, "amount", "成交额")),
            "units": {**US_INDEX_UNITS, "previousClose": "index_point"},
            "dataAsOf": data_as_of,
            "sourceIds": [source_id],
        }
        return ModulePayload(
            item,
            [{"id": source_id, "title": f"新浪财经{US_INDEXES[symbol]}日线", "url": f"https://stock.finance.sina.com.cn/usstock/quotes/{symbol}.html", "publisher": "新浪财经", "sourceType": "market_data", "publishedAt": None, "dataAsOf": data_as_of, **session_evidence}],
            warnings,
        )

    def _global_news(
        self,
        requested_at: datetime,
        *,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> ModulePayload:
        rows = _records(
            self._call(
                "stock_info_global_em",
                _http_timeout_seconds=timeout_seconds,
                _cap_http_timeout=True,
            )
        )
        items: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        keywords = ("政策", "国务院", "央行", "证监会", "交易所", "统计局", "会议", "发布", "监管", "关税")
        for row in rows:
            published = _parse_datetime(_pick(row, "发布时间", "published_at"), CHINA_TZ)
            if published is None or published > requested_at:
                continue
            title = str(_pick(row, "标题", "title") or "").strip()
            url = str(_pick(row, "链接", "url") or "").strip() or None
            original_publisher = _text_or_none(_pick(row, "文章来源", "来源", "媒体名称", "publisher", "source"))
            link_url = url or "https://kuaixun.eastmoney.com/7_24.html"
            provenance = _media_provenance(link_url, original_publisher)
            source_id = _source_id("eastmoney:global-news", f"{url}|{title}|{published.isoformat()}")
            published_iso = _china_iso(published)
            items.append(
                {
                    "type": "policy" if any(word in title for word in keywords) else "global_financial_news",
                    "title": title,
                    "summary": str(_pick(row, "摘要", "summary") or "").strip(),
                    **provenance,
                    "publishedAt": published_iso,
                    "url": link_url,
                    "dataAsOf": published_iso,
                    "sourceIds": [source_id],
                }
            )
            sources.append({"id": source_id, "title": title or "东方财富全球财经快讯", "url": link_url, **provenance, "publishedAt": published_iso, "dataAsOf": published_iso})
            if len(items) >= 30:
                break
        return ModulePayload(items, sources)

    def _sectors(self, session_day: date | None) -> ModulePayload:
        rows = _records(self._call("stock_board_industry_summary_ths"))
        if not rows:
            raise ValueError("行业板块数据为空")
        ranked = sorted(rows, key=lambda row: float(_number(_pick(row, "涨跌幅")) or 0), reverse=True)
        selected = ranked[:10] + ranked[-10:]
        source_id = _source_id("ths:industry-summary")
        data_as_of = _session_iso(session_day)
        items = []
        seen: set[str] = set()
        for row in selected:
            name = str(_pick(row, "板块", "行业", "name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            change = _number(_pick(row, "涨跌幅"))
            items.append(
                {
                    "rank": _integer(_pick(row, "序号", "rank")),
                    "direction": "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat",
                    "name": name,
                    "changePercent": change,
                    "volume": _number(_pick(row, "总成交量")),
                    "turnover": _number(_pick(row, "总成交额")),
                    "netInflow": _number(_pick(row, "净流入")),
                    "advancers": _integer(_pick(row, "上涨家数")),
                    "decliners": _integer(_pick(row, "下跌家数")),
                    "leader": _pick(row, "领涨股"),
                    "leaderPrice": _number(_pick(row, "领涨股-最新价")),
                    "leaderChangePercent": _number(_pick(row, "领涨股-涨跌幅")),
                    "units": dict(SECTOR_UNITS),
                    "dataAsOf": data_as_of,
                    "sourceIds": [source_id],
                }
            )
        return ModulePayload(
            items,
            [{"id": source_id, "title": "同花顺行业板块一览", "url": "https://q.10jqka.com.cn/thshy/", "publisher": "同花顺", "sourceType": "industry_market_data", "publishedAt": None, "dataAsOf": data_as_of}],
        )

    def _lhb(self, session_day: date | None, watch_codes: set[str]) -> ModulePayload:
        query_day = session_day or self._now().date()
        value = query_day.strftime("%Y%m%d")
        rows = _records(self._call("stock_lhb_detail_em", start_date=value, end_date=value))
        rows.sort(key=lambda row: abs(float(_number(_pick(row, "龙虎榜净买额")) or 0)), reverse=True)
        chosen = rows[:20]
        for row in rows:
            if normalize_stock_code(_pick(row, "代码")) in watch_codes and row not in chosen:
                chosen.append(row)
        source_id = _source_id("eastmoney:dragon-tiger-list", query_day.isoformat())
        data_as_of = _session_iso(query_day)
        entries = []
        for row in chosen:
            code = normalize_stock_code(_pick(row, "代码"))
            if code is None:
                continue
            listed_day = _parse_date(_pick(row, "上榜日")) or query_day
            entries.append(
                {
                    "code": code,
                    "name": str(_pick(row, "名称") or code),
                    "listedDate": listed_day.isoformat(),
                    "interpretation": _pick(row, "解读"),
                    "close": _number(_pick(row, "收盘价")),
                    "changePercent": _number(_pick(row, "涨跌幅")),
                    "netBuy": _number(_pick(row, "龙虎榜净买额")),
                    "buyAmount": _number(_pick(row, "龙虎榜买入额")),
                    "sellAmount": _number(_pick(row, "龙虎榜卖出额")),
                    "reason": _pick(row, "上榜原因"),
                    "units": dict(LHB_ENTRY_UNITS),
                    "dataAsOf": _session_iso(listed_day),
                    "sourceIds": [source_id],
                }
            )
        return ModulePayload(
            {"sessionDate": query_day.isoformat(), "entries": entries, "dataAsOf": data_as_of, "sourceIds": [source_id]},
            [{"id": source_id, "title": "东方财富龙虎榜详情", "url": "https://data.eastmoney.com/stock/tradedetail.html", "publisher": "东方财富", "sourceType": "dragon_tiger_list_aggregation", "publishedAt": None, "dataAsOf": data_as_of}],
        )

    def _financial(self, stock: Mapping[str, Any], requested_at: datetime) -> ModulePayload:
        rows = _records(self._call("stock_financial_analysis_indicator_em", symbol=stock["symbol"], indicator="按报告期"))
        cutoff = requested_at.astimezone(CHINA_TZ)
        candidates: list[tuple[date, datetime, datetime | None, datetime | None, Mapping[str, Any]]] = []
        for row in rows:
            report_date = _parse_date(_pick(row, "REPORT_DATE", "报告期"))
            notice_at = _disclosure_datetime(_pick(row, "NOTICE_DATE", "公告日期", "披露日期"))
            updated_at = _disclosure_datetime(_pick(row, "UPDATE_DATE", "更新日期"))
            availability_times = [value for value in (notice_at, updated_at) if value is not None]
            if not report_date or report_date > cutoff.date() or not availability_times:
                continue
            available_at = max(availability_times)
            if available_at <= cutoff:
                candidates.append((report_date, available_at, notice_at, updated_at, row))
        if not candidates:
            raise ValueError(f"{stock['code']} 无可核验且在截止时间前披露/更新的财务指标")
        report_date, available_at, notice_at, updated_at, row = max(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        source_id = _source_id("eastmoney:financial", stock["code"])
        notice_iso = _china_iso(notice_at) if notice_at is not None else None
        updated_iso = _china_iso(updated_at) if updated_at is not None else None
        data_as_of = _china_iso(available_at)
        currency = str(_pick(row, "CURRENCY", "币种") or "").strip().upper()
        amount_unit = currency if re.fullmatch(r"[A-Z]{3}", currency) else UNSPECIFIED_UNIT
        per_share_unit = f"{amount_unit}_per_share" if amount_unit != UNSPECIFIED_UNIT else UNSPECIFIED_UNIT
        financial_units = {
            "revenue": amount_unit,
            "revenueYoY": "percentage_point",
            "parentNetProfit": amount_unit,
            "parentNetProfitYoY": "percentage_point",
            "deductedNetProfit": amount_unit,
            "deductedNetProfitYoY": "percentage_point",
            "grossMargin": "percentage_point",
            "netMargin": "percentage_point",
            "roeWeighted": "percentage_point",
            "roeDiluted": "percentage_point",
            "eps": per_share_unit,
        }
        result = {
            "reportDate": report_date.isoformat(),
            "seasonLabel": _pick(row, "REPORT_DATE_NAME", "REPORT_TYPE", "SEASON_LABEL"),
            "noticeAt": notice_iso,
            "updatedAt": updated_iso,
            "revenue": _number(_pick(row, "TOTALOPERATEREVE")),
            "revenueYoY": _number(_pick(row, "TOTALOPERATEREVETZ")),
            "revenueBasis": {
                "provider": "eastmoney",
                "amountField": "TOTALOPERATEREVE",
                "yoyField": "TOTALOPERATEREVETZ",
                "verification": "unverified_against_filing",
            },
            "parentNetProfit": _number(_pick(row, "PARENTNETPROFIT")),
            "parentNetProfitYoY": _number(_pick(row, "PARENTNETPROFITTZ")),
            "deductedNetProfit": _number(_pick(row, "KCFJCXSYJLR", "DEDU_PARENT_PROFIT")),
            "deductedNetProfitYoY": _number(_pick(row, "KCFJCXSYJLRTZ", "DPNP_YOY_RATIO")),
            "grossMargin": _number(_pick(row, "XSMLL", "GROSS_PROFIT_RATIO")),
            "netMargin": _number(_pick(row, "XSJLL", "NET_PROFIT_RATIO")),
            "roeWeighted": _number(_pick(row, "ROEJQ")),
            "roeDiluted": _number(_pick(row, "ROE_DILUTED")),
            "eps": _number(_pick(row, "EPSJB")),
            "units": financial_units,
            "dataAsOf": data_as_of,
            "sourceIds": [source_id],
        }
        exchange_code = f"{stock['exchange']}{stock['code']}"
        url = f"https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code={exchange_code}&color=b#/cwfx"
        return ModulePayload(result, [{"id": source_id, "title": f"东方财富{stock['name']}主要财务指标", "url": url, "publisher": "东方财富", "sourceType": "financial_data_aggregation", "publishedAt": notice_iso, "dataAsOf": data_as_of}])

    def _stock_news(
        self,
        stock: Mapping[str, Any],
        requested_at: datetime,
        *,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> ModulePayload:
        rows = _records(
            self._call(
                "stock_news_em",
                symbol=stock["code"],
                _http_timeout_seconds=timeout_seconds,
                _cap_http_timeout=True,
            )
        )
        items: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for row in rows:
            published = _parse_datetime(_pick(row, "发布时间", "published_at"), CHINA_TZ)
            if published is None or published > requested_at:
                continue
            title = str(_pick(row, "新闻标题", "标题", "title") or "").strip()
            url = str(_pick(row, "新闻链接", "链接", "url") or "").strip() or None
            original_publisher = _text_or_none(_pick(row, "文章来源", "来源", "媒体名称", "publisher", "source"))
            link_url = url or f"https://so.eastmoney.com/news/s?keyword={stock['code']}"
            provenance = _media_provenance(link_url, original_publisher)
            source_id = _source_id("eastmoney:stock-news", f"{url}|{title}|{published.isoformat()}")
            published_iso = _china_iso(published)
            items.append(
                {
                    "type": "news",
                    "code": stock["code"],
                    "name": stock["name"],
                    "title": title,
                    "summary": str(_pick(row, "新闻内容", "摘要", "summary") or "").strip(),
                    **provenance,
                    "publishedAt": published_iso,
                    "url": link_url,
                    "dataAsOf": published_iso,
                    "sourceIds": [source_id],
                }
            )
            sources.append({"id": source_id, "title": title or f"{stock['name']}相关新闻", "url": link_url, **provenance, "publishedAt": published_iso, "dataAsOf": published_iso})
            if len(items) >= 10:
                break
        return ModulePayload(items, sources)

    def _staged(self, requested_at: datetime) -> dict[str, Any] | None:
        """Validated 07:50 pre-collection for this target date, else None.

        The staging file is only a transport for already-collected rows: it is
        accepted solely when intact, for the same target date, covering exactly
        the request watchlist, and observed no later than the request cutoff.
        Anything else falls back to a live query that records its own, later
        observation time.
        """
        if self._staging_loaded is None:
            try:
                from cninfo_staging import load_staging
                self._staging_loaded = load_staging(
                    self.staging_dir, requested_at.date().isoformat(), requested_at,
                    expected_codes=self._request_codes) or False
            except Exception:  # a staging problem must never break the run
                self._staging_loaded = False
        return self._staging_loaded or None

    def _fetch_cninfo_rows(self, code: str) -> list[dict[str, Any]]:
        if self.cninfo_fetcher is not None:
            rows = self.cninfo_fetcher(code)
            if not isinstance(rows, list):
                raise ValueError("cninfo query result must be a list")
            return rows
        from cninfo_source import CninfoClient

        with CninfoClient() as client:
            return client.announcements(code, page_size=30)

    def _fetch_cninfo_window(self, code: str, previous_day: date | None,
                             requested_at: datetime) -> dict[str, Any] | None:
        if previous_day is None:
            return None
        if self.cninfo_window_fetcher is not None:
            return self.cninfo_window_fetcher(code, window_start=previous_day, cutoff=requested_at)
        if self.cninfo_fetcher is not None:
            # A legacy injected first-page adapter has no pagination proof.
            return None
        from cninfo_source import CninfoClient
        with CninfoClient() as client:
            return client.announcement_window(code, window_start=previous_day,
                                              cutoff=requested_at, page_size=30, max_pages=4)

    def _financial_summary_document(self, rows, stock, requested_at):
        if not self.collect_documents:
            return None, None
        candidates = [row for row in rows if isinstance(row, Mapping)
                      and re.fullmatch(r"20\d{2}年半年度报告摘要", str(row.get("title", "")).strip())
                      and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date", "")))
                      and str(row["date"]) <= requested_at.date().isoformat()]
        if not candidates:
            return {"status": "unavailable", "reason": "summary_not_found_in_limited_query"}, None
        row = max(candidates, key=lambda item: item["date"])
        try:
            from cninfo_evidence import collect_summary
            staged_document = None
            staged = self._staged(requested_at)
            if staged is not None:
                candidate = staged["documents"].get(str(stock["code"]))
                if (isinstance(candidate, Mapping) and candidate.get("title") == row["title"]
                        and candidate.get("disclosureDate") == row["date"]):
                    staged_document = dict(candidate)
            if staged_document is not None:
                document = staged_document
            else:
                if self._document_deadline is None:
                    self._document_deadline = self.monotonic_clock() + 12
                remaining = self._document_deadline - self.monotonic_clock()
                if remaining <= 0:
                    raise ValueError("pdf_budget_exhausted")
                document = (self.document_fetcher(row, stock, self.cache_dir / "cninfo-pdf")
                            if self.document_fetcher is not None else
                            collect_summary(row, stock, self.cache_dir / "cninfo-pdf", timeout_seconds=min(10, remaining)))
                if self.monotonic_clock() > self._document_deadline:
                    raise ValueError("pdf_budget_exhausted")
            source_id = _source_id("cninfo:announcement", f"{stock['code']}|{row.get('announcementId')}|{row['title']}|{row['date']}")
            document["sourceIds"] = [source_id]
            source = {"id": source_id, "title": document["title"], "url": document["url"],
                      "publisher": "巨潮资讯网", "sourceType": "official_announcement",
                      "publishedAt": None, "timePrecision": "date", "disclosureDate": document["disclosureDate"],
                      "observedAt": document["observedAt"], "dataAsOf": document["observedAt"],
                      "pdfSha256": document["pdfSha256"]}
            return document, source
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]{1,64}", str(error)) else "pdf_collection_or_extraction_failed"
            return {"status": "unavailable", "reason": reason}, None

    def _official_announcements(
        self,
        stock: Mapping[str, Any],
        requested_at: datetime,
        previous_day: date | None,
    ) -> ModulePayload:
        code = str(stock["code"])
        staged = self._staged(requested_at)
        staged_entry = staged["codes"].get(code) if staged is not None else None
        if staged_entry is not None:
            rows = staged_entry["rows"]
            # That stock's own query instant, not the batch completion time.
            completed_at = staged_entry["queriedAt"]
            window_query = staged_entry.get("windowQuery")
        else:
            rows = self._fetch_cninfo_rows(code)
            completed_at = self._now()
            try:
                window_query = self._fetch_cninfo_window(code, previous_day, requested_at)
            except Exception as error:
                window_query = {"rows": [], "queryCoverage": {"kind": "window", "complete": False,
                                "reason": "window_query_failed"}}
        query_coverage = {"kind": "first_page", "limit": 30, "returned": len(rows), "complete": False}
        if isinstance(window_query, Mapping) and isinstance(window_query.get("queryCoverage"), Mapping):
            query_coverage = dict(window_query["queryCoverage"])
            window_rows = window_query.get("rows")
            if not isinstance(window_rows, list) or len(window_rows) > 120:
                window_rows = []
                query_coverage.update(complete=False, reason="window_rows_invalid")
            if query_coverage.get("returned") != len(window_rows):
                query_coverage.update(complete=False, reason="window_rows_count_mismatch")
            window_observed = _parse_datetime(query_coverage.get("queriedAt"), CHINA_TZ)
            window_through = _parse_datetime(query_coverage.get("observedThroughAt"), CHINA_TZ)
            coverage_start = _parse_datetime(query_coverage.get("windowStartAt"), CHINA_TZ)
            # An adapter must not discard malformed rows yet keep a complete
            # pagination proof. Valid historical rows can still be displayed.
            for window_row in window_rows:
                row_day = _parse_date(window_row.get("date")) if isinstance(window_row, Mapping) else None
                row_observed = _parse_datetime(window_row.get("observedAt"), CHINA_TZ) if isinstance(window_row, Mapping) else None
                if (row_day is None or coverage_start is None or window_observed is None
                        or not coverage_start.date() <= row_day <= requested_at.date()
                        or row_observed is None or window_through is None
                        or not coverage_start <= window_through <= row_observed <= window_observed <= requested_at):
                    query_coverage.update(complete=False, reason="window_row_evidence_invalid")
                    break
            history_observed = completed_at
            # Distinct real observations: an old financial row must not acquire
            # the later window query time just because both lists are joined.
            merged_rows, identities = [], set()
            for raw_row, observation in [
                *[(row, window_observed) for row in window_rows],
                *[(row, history_observed) for row in rows],
            ]:
                if not isinstance(raw_row, Mapping) or observation is None:
                    continue
                row = {**raw_row, "observedAt": raw_row.get("observedAt") or _instant_iso(observation)}
                identity = (code, row.get("url"), row.get("date"))
                if identity in identities:
                    continue
                identities.add(identity)
                merged_rows.append(row)
            rows = merged_rows
            if window_observed is not None:
                completed_at = window_observed
        observed_iso = _instant_iso(completed_at)
        query_source_id = _source_id("cninfo:query", f"{code}|{observed_iso}")
        query_source = {
            "id": query_source_id,
            "title": f"巨潮资讯{stock['name']}信息披露查询",
            "url": "https://www.cninfo.com.cn/new/disclosure",
            "publisher": "巨潮资讯网",
            "sourceType": "official_disclosure_query",
            "dataAsOf": observed_iso,
            "queriedAt": observed_iso,
            "queryCoverage": query_coverage,
        }
        window_start = previous_day
        items: list[dict[str, Any]] = []
        background: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = [query_source]
        latest: date | None = None
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                disclosure_day = date.fromisoformat(str(row.get("date") or ""))
            except ValueError:
                continue
            if disclosure_day > requested_at.date():
                continue
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                continue
            row_observed = _parse_datetime(row.get("observedAt"), CHINA_TZ) or completed_at
            if row_observed > completed_at:
                raise ValueError("cninfo observation time is later than query completion")
            # CNINFO returns a date label, not an exact publication time. A
            # same-day label collected after cutoff cannot prove pre-cutoff availability.
            date_end = datetime.combine(disclosure_day + timedelta(days=1), datetime.min.time(), CHINA_TZ)
            if min(row_observed, date_end) > requested_at:
                continue
            if latest is None or disclosure_day > latest:
                latest = disclosure_day
            source_id = _source_id(
                "cninfo:announcement",
                f"{code}|{row.get('announcementId')}|{title}|{disclosure_day.isoformat()}",
            )
            record = {
                "type": "official_announcement",
                "sourceType": "official_announcement",
                "code": code,
                "name": stock["name"],
                "title": title,
                "publishedAt": None,
                "url": url,
                "dataAsOf": _instant_iso(row_observed),
                "observedAt": _instant_iso(row_observed),
                "disclosureDate": disclosure_day.isoformat(),
                "timePrecision": "date",
                "dateKind": "source_disclosure_date",
                "sourceIds": [source_id],
            }
            source = {
                "id": source_id,
                "title": title,
                "url": url,
                "publisher": "巨潮资讯网",
                "sourceType": "official_announcement",
                "publishedAt": None,
                "disclosureDate": disclosure_day.isoformat(),
                "timePrecision": "date",
                "observedAt": _instant_iso(row_observed),
                "dataAsOf": _instant_iso(row_observed),
            }
            in_window = window_start is None or disclosure_day >= window_start
            if in_window:
                if len(items) >= 5:
                    continue
                items.append(record)
                sources.append(source)
            else:
                if len(background) >= 3:
                    continue
                background.append(record)
                sources.append(source)
        document, document_source = self._financial_summary_document(rows, stock, requested_at)
        if document_source is not None:
            # One URL/identity remains one citable source. PDF observation is
            # later than query observation; never copy the query cutoff into it.
            sources = [source for source in sources if source["id"] != document_source["id"]]
            sources.append(document_source)
            for record in items + background:
                if record["sourceIds"] == document["sourceIds"]:
                    record["observedAt"] = record["dataAsOf"] = document["observedAt"]
        return ModulePayload(
            {
                "status": "available",
                "financialSummary": document,
                "latestDisclosureDate": latest.isoformat() if latest else None,
                "queriedAt": observed_iso,
                "queryCutoffAt": _china_iso(requested_at),
                "queryCoverage": query_coverage,
                "items": items,
                "recentBackground": background,
                "sourceIds": [query_source_id],
            },
            sources,
        )

    @staticmethod
    def _coverage_average(values: Sequence[float | int]) -> int:
        """Round a bounded coverage mean without rounding away any real gap.

        Only an all-100 input may produce 100. Invalid/out-of-domain components
        are missing coverage (0), never a reason to drop a denominator item.
        """
        if not values:
            return 0
        components = [
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 100 and math.isfinite(value) else 0.0
            for value in values
        ]
        result = round(sum(components) / len(components))
        return min(result, 99) if any(value < 100 for value in components) else 100

    @staticmethod
    def _average(results: Sequence[ModuleResult]) -> int:
        return AkShareMorningProvider._coverage_average([result.score for result in results])

    @staticmethod
    def _financial_content_score(result: ModuleResult) -> int:
        if not isinstance(result.data, Mapping):
            return 0
        covered = sum(
            1
            for group in FINANCIAL_REQUIRED_FIELD_GROUPS
            if any(not _is_missing(result.data.get(field)) for field in group)
        )
        coverage = covered / len(FINANCIAL_REQUIRED_FIELD_GROUPS)
        return round(result.score * coverage)

    @staticmethod
    def _market_item_content_score(item: Any, close_field: str) -> int:
        if not isinstance(item, Mapping):
            return 0
        open_price = _positive_number(item.get("open"))
        high = _positive_number(item.get("high"))
        low = _positive_number(item.get("low"))
        close = _positive_number(item.get(close_field))
        ohlc_valid = (
            None not in {open_price, high, low, close}
            and high >= max(open_price, close)
            and low <= min(open_price, close)
            and high >= low
        )
        provenance_valid = (
            _parse_datetime(item.get("dataAsOf"), CHINA_TZ) is not None
            and bool(item.get("sourceIds"))
        )
        groups = (
            _arithmetic_is_coherent(item, close_field),
            ohlc_valid,
            _positive_number(item.get("volume")) is not None,
            _positive_number(item.get("turnover")) is not None,
            provenance_valid,
        )
        return round(sum(bool(value) for value in groups) / len(groups) * 100)

    @classmethod
    def _index_content_score(cls, result: ModuleResult) -> int:
        items = {
            item.get("code"): item
            for item in result.data
            if isinstance(item, Mapping) and item.get("code") in A_SHARE_INDEXES
        } if isinstance(result.data, list) else {}
        content = cls._coverage_average([
            cls._market_item_content_score(items.get(symbol), "close")
            for symbol in A_SHARE_INDEXES
        ])
        return cls._coverage_average([content * result.score / 100])

    @classmethod
    def _quote_content_scores(
        cls,
        result: ModuleResult,
        universe: Sequence[Mapping[str, Any]],
    ) -> list[int]:
        quotes = result.data.get("quotes") if isinstance(result.data, Mapping) else None
        quotes = quotes if isinstance(quotes, Mapping) else {}
        return [
            cls._coverage_average([
                cls._market_item_content_score(quotes.get(stock["code"]), "last") * result.score / 100
            ])
            for stock in universe
        ]

    @staticmethod
    def _aggregate_content_score(turnover: ModuleResult, breadth: ModuleResult) -> int:
        turnover_data = turnover.data if isinstance(turnover.data, Mapping) else {}
        by_exchange = turnover_data.get("byExchange")
        turnover_valid = (
            _positive_number(turnover_data.get("totalTurnover")) is not None
            and isinstance(by_exchange, Mapping)
            and all(_positive_number(by_exchange.get(exchange)) is not None for exchange in ("SH", "SZ"))
            and _parse_datetime(turnover_data.get("dataAsOf"), CHINA_TZ) is not None
            and bool(turnover_data.get("sourceIds"))
        )
        breadth_data = breadth.data if isinstance(breadth.data, Mapping) else {}
        counts = breadth_data.get("breadth")
        breadth_valid = False
        if isinstance(counts, Mapping):
            values = [_integer(counts.get(name)) for name in ("advancers", "decliners", "unchanged")]
            breadth_valid = (
                all(value is not None and value >= 0 for value in values)
                and sum(values) > 0
                and values[0] + values[1] > 0
                and _parse_datetime(breadth_data.get("dataAsOf"), CHINA_TZ) is not None
                and bool(breadth_data.get("sourceIds"))
            )
        turnover_score = turnover.score if turnover_valid else 0
        breadth_score = breadth.score if breadth_valid else 0
        return AkShareMorningProvider._coverage_average([turnover_score, breadth_score])

    def get_morning_context(self, request: Mapping[str, Any]) -> dict[str, Any]:
        target, requested_at, universe = _validate_request(request)
        self._document_deadline = None
        self._request_codes = [str(stock["code"]) for stock in universe]
        now = self._now()
        cutoff = requested_at.astimezone(CHINA_TZ)
        state = RunState()
        universe_key = ",".join(sorted(stock["code"] for stock in universe))

        calendar_as_of = min(now, cutoff)
        calendar = self._module(state, "trading_calendar", target.isoformat(), lambda: self._calendar(target, calendar_as_of), None)
        calendar_data = calendar.data if isinstance(calendar.data, Mapping) else {}
        previous_day = _parse_date(calendar_data.get("previousTradingDate"))
        expected_previous_day = _parse_date(calendar_data.get("previousSessionPreviousTradingDate"))

        history_day_key = previous_day.isoformat() if previous_day else target.isoformat()
        previous_close_day_key = expected_previous_day.isoformat() if expected_previous_day else "unknown"
        a_history = self._module(
            state,
            "a_share_history",
            f"closed-history-v2|{history_day_key}|{previous_close_day_key}|{universe_key}",
            lambda: self._a_history(universe, previous_day, expected_previous_day),
            None,
        )
        index_history = self._module(
            state,
            "a_share_indices_history",
            f"closed-history-v2|{history_day_key}|{previous_close_day_key}",
            lambda: self._index_history(previous_day, expected_previous_day),
            [],
        )
        market_turnover = self._module(
            state,
            "a_share_market_turnover",
            f"exchange-summary-v1|{history_day_key}",
            lambda: self._market_turnover(previous_day),
            None,
        )
        market_breadth = self._module(
            state,
            "a_share_market_breadth",
            f"dated-close-v1|{history_day_key}",
            lambda: self._market_breadth(previous_day),
            None,
        )
        sectors = self._module(state, "sectors", f"units-v2|{target.isoformat()}", lambda: self._sectors(previous_day), [])
        lhb_key = previous_day.isoformat() if previous_day else target.isoformat()
        lhb = self._module(state, "capital_flows", f"units-v1|{lhb_key}", lambda: self._lhb(previous_day, {stock["code"] for stock in universe}), None)

        us_results = [self._module(state, f"us_index_{symbol}", f"us-session-v1|{_china_iso(requested_at)}", lambda symbol=symbol: self._us_index(symbol, requested_at), None) for symbol in US_INDEXES]
        financial_results = [self._module(state, f"financial_{stock['code']}", f"v3|units-v1|{target.isoformat()}", lambda stock=stock: self._financial(stock, requested_at), None) for stock in universe]
        # Keep the Eastmoney media phase contiguous.  Core LHB, US-index and
        # financial work above must not consume this phase's shared deadline.
        global_news = self._media_module(
            state,
            "global_news",
            target.isoformat(),
            lambda timeout: self._global_news(requested_at, timeout_seconds=timeout),
            [],
            cutoff,
        )
        news_results = [
            self._media_module(
                state,
                f"stock_news_{stock['code']}",
                target.isoformat(),
                lambda timeout, stock=stock: self._stock_news(
                    stock,
                    requested_at,
                    timeout_seconds=timeout,
                ),
                [],
                cutoff,
            )
            for stock in universe
        ]
        official_results = [
            self._module(
                state,
                f"cninfo_{stock['code']}",
                f"cninfo-v4-window-evidence|{_china_iso(requested_at)}|{stock['code']}",
                lambda stock=stock: self._official_announcements(stock, requested_at, previous_day),
                {"status": "unavailable", "latestDisclosureDate": None, "items": [], "sourceIds": []},
            )
            for stock in universe
        ]

        cutoff_financial_results: list[ModuleResult] = []
        for stock, result in zip(universe, financial_results):
            data_as_of = _parse_datetime(result.data.get("dataAsOf"), CHINA_TZ) if isinstance(result.data, Mapping) else None
            if isinstance(result.data, Mapping) and data_as_of is not None and data_as_of <= cutoff:
                cutoff_financial_results.append(result)
            else:
                if result.data is not None:
                    state.warnings.append(f"financial_{stock['code']} 缓存/响应无法证明在 requestedAt 截止前可用，已排除。")
                cutoff_financial_results.append(ModuleResult(None, [], "unavailable"))
        financial_results = cutoff_financial_results

        history_data = a_history.data if isinstance(a_history.data, Mapping) else {}
        indices = index_history.data if isinstance(index_history.data, list) else []
        turnover_data = market_turnover.data if isinstance(market_turnover.data, Mapping) else {}
        breadth_data = market_breadth.data if isinstance(market_breadth.data, Mapping) else {}
        previous_session = None
        if history_data or indices or turnover_data or breadth_data:
            breadth = breadth_data.get("breadth")
            if isinstance(breadth, Mapping):
                breadth = {
                    **breadth,
                    "sourceIds": list(breadth_data.get("sourceIds") or []),
                }
            previous_session = {
                "sessionDate": previous_day.isoformat() if previous_day else None,
                "indices": indices,
                "marketBreadth": breadth,
                "totalTurnover": turnover_data.get("totalTurnover"),
                "turnoverByExchange": turnover_data.get("byExchange"),
                "turnoverSourceIds": list(turnover_data.get("sourceIds") or []),
                "units": {
                    "totalTurnover": "CNY",
                    "turnoverByExchange.SH": "CNY",
                    "turnoverByExchange.SZ": "CNY",
                    "marketBreadth.advancers": "company_count",
                    "marketBreadth.decliners": "company_count",
                    "marketBreadth.unchanged": "company_count",
                },
                "dataAsOf": _latest_iso(
                    [
                        history_data.get("dataAsOf"),
                        turnover_data.get("dataAsOf"),
                        breadth_data.get("dataAsOf"),
                        *[item.get("dataAsOf") for item in indices if isinstance(item, Mapping)],
                    ]
                ),
                "sourceIds": list(
                    dict.fromkeys(
                        [
                            *(history_data.get("sourceIds") or []),
                            *(turnover_data.get("sourceIds") or []),
                            *(breadth_data.get("sourceIds") or []),
                            *[
                                source_id
                                for item in indices
                                if isinstance(item, Mapping)
                                for source_id in item.get("sourceIds", [])
                            ],
                        ]
                    )
                ),
            }

        quotes = history_data.get("quotes") if isinstance(history_data.get("quotes"), Mapping) else {}
        watchlist = []
        for stock, financial, official in zip(universe, financial_results, official_results):
            quote = quotes.get(stock["code"])
            official_payload = official.data if isinstance(official.data, Mapping) else {}
            official_status = (
                "available"
                if official.status in {"live", "cache"} and official_payload.get("status") == "available"
                else "unavailable"
            )
            official_source_ids = list(official_payload.get("sourceIds") or []) if official_status == "available" else []
            source_ids = list(dict.fromkeys([*(quote.get("sourceIds", []) if isinstance(quote, Mapping) else []), *((financial.data or {}).get("sourceIds", []) if isinstance(financial.data, Mapping) else []), *official_source_ids]))
            watchlist.append(
                {
                    **stock,
                    "status": "available" if quote is not None and financial.data is not None else "partial" if quote is not None or financial.data is not None else "unavailable",
                    "marketData": quote,
                    "financials": financial.data,
                    "officialDisclosure": {
                        "status": official_status,
                        "latestDisclosureDate": official_payload.get("latestDisclosureDate"),
                        "queriedAt": official_payload.get("queriedAt") if official_status == "available" else None,
                        "queryCutoffAt": official_payload.get("queryCutoffAt"),
                        "queryCoverage": official_payload.get("queryCoverage"),
                        "recentBackground": list(official_payload.get("recentBackground") or []) if official_status == "available" else [],
                        "financialSummary": official_payload.get("financialSummary") if official_status == "available" else None,
                        "sourceIds": official_source_ids,
                    },
                    "dataAsOf": _latest_iso([quote.get("dataAsOf") if isinstance(quote, Mapping) else None, financial.data.get("dataAsOf") if isinstance(financial.data, Mapping) else None]),
                    "sourceIds": source_ids,
                }
            )

        policy = [item for item in (global_news.data or []) if (_parse_datetime(item.get("publishedAt")) or datetime.min.replace(tzinfo=CHINA_TZ)) <= cutoff]
        stock_news = [item for result in news_results for item in (result.data or []) if (_parse_datetime(item.get("publishedAt")) or datetime.min.replace(tzinfo=CHINA_TZ)) <= cutoff]
        official_items = [
            item
            for result in official_results
            if isinstance(result.data, Mapping)
            for item in (result.data.get("items") or [])
            if isinstance(item, Mapping)
        ]
        overnight = [result.data for result in us_results if isinstance(result.data, Mapping)]
        capital_flows = [
            {
                "type": "northbound_realtime",
                "status": "unavailable",
                "availabilityReason": "not_disclosed",
                "requiredForDeclaredCoverage": False,
                "reason": "AkShare 当前可靠接口不提供可核验的北向资金实时口径；不以历史值或估算值替代。",
                "dataAsOf": None,
                "sourceIds": [],
            }
        ]
        if isinstance(lhb.data, Mapping):
            capital_flows.append({"type": "dragon_tiger_list", "status": "available" if lhb.status == "live" else "cached", **lhb.data})

        financial_item_scores = [self._financial_content_score(result) for result in financial_results]
        financial_score = self._coverage_average(financial_item_scores)
        from research_data_quality import build_quality, official_window_assessment
        official_window_scores = [
            result.score if official_window_assessment(result.data, cutoff, previous_day)["completeAsObserved"] else 0
            for result in official_results
        ]
        official_window_score = self._coverage_average(official_window_scores)
        # Availability/coverage are no longer penalized for media credibility;
        # the media-not-official restriction remains separate, explicit metadata.
        news_score = self._coverage_average([self._average(news_results), official_window_score])
        index_content_score = self._index_content_score(index_history)
        quote_content_scores = self._quote_content_scores(a_history, universe)
        aggregate_content_score = self._aggregate_content_score(market_turnover, market_breadth)
        # The report contract defines five required market items: the complete
        # three-index set, turnover plus breadth, and one quote per watchlist stock.
        market_score = self._coverage_average([
            index_content_score, aggregate_content_score, *quote_content_scores
        ])
        capital_score = lhb.score  # Publicly undisclosed realtime net flow is not a failed source.
        section_scores = {
            "tradingCalendar": calendar.score,
            "overnightGlobal": self._average(us_results),
            "previousAShareSession": self._coverage_average([index_content_score, aggregate_content_score]),
            "policyAndCalendar": global_news.score,
            "sectors": sectors.score,
            "capitalFlows": capital_score,
            "watchlist": self._coverage_average([self._coverage_average(quote_content_scores), financial_score]),
            "announcementsAndNews": news_score,
        }
        overall = self._coverage_average(list(section_scores.values()))
        # Rounding an almost-complete mean must not erase a real field gap.
        if overall == 100 and (any(value < 100 for value in section_scores.values())
                               or any(value < 100 for value in (market_score, capital_score, news_score, financial_score))
                               or state.cache_hits):
            overall = 99
        completeness = {"market": market_score, "capital": capital_score, "news": news_score, "financial": financial_score, "overall": overall}

        state.warnings.extend(
            [
                "北向资金实时数据明确不可用，未使用旧口径或估算值填充。",
                "stock_news_em 提供媒体新闻，不等同于交易所或上市公司官方公告。",
                (
                    "巨潮官方公告源不可用，未编造公司公告。"
                    if all(result.status == "unavailable" for result in official_results)
                    else "巨潮官方公告与媒体新闻分栏；未把媒体标题写成公司公告。"
                ),
                "上一交易日行情只使用带交易日期的收盘日线与按日统计；未使用无日期的盘前实时快照冒充历史行情。",
                "市场广度只有在上游统计日期等于上一交易日且家数非零时才采用；否则显式置空并降低完整度。",
                "新浪日线成交量在响应中保持 provider_native_unspecified；同花顺行业单位按 AkShare 文档标注为万手/亿元。",
            ]
        )
        if overall == 0:
            status = "unavailable"
        elif overall == 100 and not state.cache_hits:
            status = "live"
        else:
            status = "partial"

        data = {
            "tradingCalendar": calendar.data,
            "overnightGlobal": overnight,
            "previousAShareSession": previous_session,
            "policyAndCalendar": policy,
            "sectors": sectors.data or [],
            "capitalFlows": capital_flows,
            "watchlist": watchlist,
            "announcementsAndNews": [*official_items, *stock_news],
            "sectionCompleteness": section_scores,
        }

        referenced_source_ids: set[str] = set()
        _collect_source_ids(data, referenced_source_ids)
        all_results = [
            calendar,
            a_history,
            index_history,
            market_turnover,
            market_breadth,
            sectors,
            global_news,
            lhb,
            *us_results,
            *financial_results,
            *news_results,
            *official_results,
        ]
        modules = {
            "trading_calendar": calendar, "a_share_history": a_history,
            "a_share_indices_history": index_history, "a_share_market_turnover": market_turnover,
            "a_share_market_breadth": market_breadth, "sectors": sectors,
            "global_news": global_news, "capital_flows": lhb,
            **{f"us_index_{symbol}": result for symbol, result in zip(US_INDEXES, us_results)},
            **{f"financial_{stock['code']}": result for stock, result in zip(universe, financial_results)},
            **{f"stock_news_{stock['code']}": result for stock, result in zip(universe, news_results)},
            **{f"cninfo_{stock['code']}": result for stock, result in zip(universe, official_results)},
        }
        quality = build_quality(cutoff=cutoff, previous_day=previous_day, modules=modules,
                                indices=indices, official_results=official_results,
                                stock_codes=[stock["code"] for stock in universe],
                                market_score=market_score, financial_score=financial_score,
                                section_scores=section_scores)
        sources_by_id: dict[str, dict[str, Any]] = {}
        for result in all_results:
            for source in result.sources:
                if source.get("id") in referenced_source_ids:
                    sources_by_id[str(source["id"])] = source

        sources = list(sources_by_id.values())
        completed_at = self._now()
        latest_component = _latest_evidence_datetime({"data": data, "sources": sources})
        meta_as_of = max(completed_at, latest_component) if latest_component is not None else completed_at

        response = {
            "schemaVersion": SCHEMA_VERSION,
            "meta": {
                "status": status,
                "provider": "akshare",
                "sourceType": "akshare-http",
                "targetDate": target.isoformat(),
                "asOf": _china_iso(meta_as_of),
                "timezone": "Asia/Shanghai",
                "isLive": status == "live",
                "dataCompleteness": completeness,
                "dataQuality": quality,
                "cacheHits": state.cache_hits,
                "warnings": list(dict.fromkeys(state.warnings)),
            },
            "data": data,
            "sources": sources,
        }
        return sanitize_json(response)


def get_a_share_morning_context(
    request: Mapping[str, Any],
    *,
    provider: AkShareMorningProvider | None = None,
) -> dict[str, Any]:
    """Public shared core used by both the command bridge and MCP server."""
    return (provider or AkShareMorningProvider()).get_morning_context(request)


__all__ = [
    "AkShareMorningProvider",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "ModulePayload",
    "get_a_share_morning_context",
    "normalize_stock_code",
    "sanitize_json",
]
