"""Validate the existing A-share calendar artifact; never fetch or infer holidays."""
from __future__ import annotations

import bisect
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "akshare.tool_trade_date_hist_sina"


def read_object(path: Path, limit: int = 1024 * 1024) -> dict[str, Any]:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversize JSON")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("canonical date required")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("canonical date required")
    return parsed


def instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.timestamp() <= 0:
        raise ValueError("positive zoned timestamp required")
    return parsed


def _dense(dates: list[date]) -> bool:
    return (len(dates) >= 20 and not any(d.weekday() >= 5 for d in dates)
            and max((b - a).days for a, b in zip(dates, dates[1:])) <= 14)


def sessions(payload: dict[str, Any], now: datetime) -> list[date]:
    """Same density/coverage policy as runtime-observer, using the same JSON."""
    if now.tzinfo is None:
        raise ValueError("zoned clock required")
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("source") != SOURCE
            or instant(payload.get("generated_at")).timestamp() > now.timestamp() + 300):
        raise ValueError("calendar metadata invalid")
    raw = payload.get("trading_dates")
    if not isinstance(raw, list) or not 100 <= len(raw) <= 100000:
        raise ValueError("calendar samples invalid")
    dates = [day(value) for value in raw]
    if (dates != sorted(set(dates)) or type(payload.get("count")) is not int
            or payload["count"] != len(dates)
            or day(payload.get("min_date")) != dates[0]
            or day(payload.get("max_date")) != dates[-1]):
        raise ValueError("calendar dates invalid")
    today = now.astimezone(TZ).date()
    recent = [d for d in dates if d >= dates[-1] - timedelta(days=60)]
    relevant = [d for d in dates if abs((d - today).days) <= 60]
    if dates[-1] > today + timedelta(days=550) or not _dense(recent) or not _dense(relevant):
        raise ValueError("calendar density/boundary invalid")
    covered = dates[-1]
    while covered < today and (covered + timedelta(days=1)).weekday() >= 5:
        covered += timedelta(days=1)
    if not dates[0] <= today <= covered:
        raise ValueError("calendar coverage unknown")
    return dates


def expected_session(dates: list[date], now: datetime) -> date:
    local = now.astimezone(TZ)
    today = local.date()
    index = (bisect.bisect_right(dates, today) if local.time() >= time(15, 30)
             else bisect.bisect_left(dates, today)) - 1
    if index < 0:
        raise ValueError("previous session unavailable")
    return dates[index]


def previous_session(dates: list[date], required: date) -> date:
    index = bisect.bisect_left(dates, required)
    if index <= 0 or index >= len(dates) or dates[index] != required:
        raise ValueError("required session not covered")
    return dates[index - 1]
