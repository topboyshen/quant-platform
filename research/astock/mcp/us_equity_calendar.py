"""Reviewed regular-session US equity calendar shared with the renderer.

No remote queries or weekday guesses outside the published calendar scope.
An unexpected future market closure must be added as reviewed calendar data.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

US_EQUITY_CALENDAR_ID = "us_equity_regular_v1"
US_EQUITY_SYMBOLS = frozenset((".DJI", ".INX", ".IXIC"))
NEW_YORK_TZ = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _calendar() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "config" / "us-equity-calendar.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if (document.get("schemaVersion") != "us-equity-calendar.v1"
            or document.get("calendarId") != US_EQUITY_CALENDAR_ID
            or document.get("timezone") != "America/New_York"):
        raise ValueError("US equity calendar contract invalid")
    return document


def us_equity_session_close(session_day: date) -> datetime | None:
    calendar = _calendar()
    if session_day.year not in calendar["supportedYears"]:
        raise ValueError(f"US equity calendar year unsupported: {session_day.year}")
    if session_day.weekday() >= 5 or session_day.isoformat() in calendar["closedDates"]:
        return None
    close_at = calendar["earlyCloses"].get(session_day.isoformat(), calendar["regularClose"])
    return datetime.combine(session_day, time.fromisoformat(close_at), NEW_YORK_TZ)


def latest_completed_us_session(cutoff: datetime) -> date:
    if cutoff.tzinfo is None:
        raise ValueError("US equity cutoff must include timezone")
    eastern = cutoff.astimezone(NEW_YORK_TZ)
    candidate = eastern.date()
    for _ in range(15):
        close_at = us_equity_session_close(candidate)
        if close_at is not None and close_at <= eastern:
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError("US equity calendar has no completed session within search window")


def us_index_freshness(record: Mapping[str, Any], cutoff: datetime) -> str:
    """Recheck cached evidence against this request, not its saved green flag."""
    if record.get("marketCalendar") != US_EQUITY_CALENDAR_ID:
        raise ValueError("US equity evidence calendar missing or unsupported")
    if record.get("marketSymbol", record.get("symbol")) not in US_EQUITY_SYMBOLS:
        raise ValueError("US equity evidence symbol unsupported")
    session_day = date.fromisoformat(record.get("sessionDate", ""))
    close_at = us_equity_session_close(session_day)
    data_as_of = datetime.fromisoformat(str(record.get("dataAsOf", "")))
    if (close_at is None or data_as_of.tzinfo is None or data_as_of != close_at
            or close_at > cutoff):
        raise ValueError("US equity evidence session timestamp invalid")
    expected = latest_completed_us_session(cutoff)
    if session_day > expected:
        raise ValueError("US equity evidence is newer than the completed session")
    return "current" if session_day == expected else "stale"


def validate_us_index_return(record: Mapping[str, Any]) -> None:
    """Require full same-session price and adjacent-session return proof.

    A correct close timestamp alone cannot make a partially populated cached
    row a valid daily return.  Saved freshness flags are not numeric evidence.
    """

    def number(field: str, *, positive: bool = False) -> float:
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"US equity cached {field} is not a number")
        try:
            parsed = float(value)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(f"US equity cached {field} is not finite") from error
        if not math.isfinite(parsed) or (positive and parsed <= 0):
            raise ValueError(f"US equity cached {field} is not valid")
        return parsed

    open_price, high, low, close = (
        number(field, positive=True) for field in ("open", "high", "low", "close")
    )
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise ValueError("US equity cached OHLC bounds are inconsistent")
    session_text = record.get("sessionDate")
    if not isinstance(session_text, str):
        raise ValueError("US equity cached session date missing")
    session_day = date.fromisoformat(session_text)
    if session_day.isoformat() != session_text:
        raise ValueError("US equity cached session date format invalid")
    close_at = us_equity_session_close(session_day)
    if close_at is None:
        raise ValueError("US equity cached session is closed")
    previous_day = latest_completed_us_session(close_at - timedelta(microseconds=1))
    if record.get("previousCloseDate") != previous_day.isoformat():
        raise ValueError("US equity cached previous close is not the adjacent session")
    previous_close = number("previousClose", positive=True)
    change = number("change")
    change_percent = number("changePercent")
    expected_change = close - previous_close
    expected_percent = expected_change / previous_close * 100
    if not math.isclose(change, expected_change, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("US equity cached daily change does not match the closes")
    if not math.isclose(change_percent, expected_percent, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("US equity cached daily percent does not match the closes")
