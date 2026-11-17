"""Shared calendar, source and market-job helpers (M3 shadow)."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.market import sources as src
from qp.models.base import utcnow
from qp.models.market import TradingDay

__all__ = ["src", "utcnow"]

CST = ZoneInfo("Asia/Shanghai")
MARKET_INBOX = Path(os.getenv("QP_MARKET_INBOX_FILE", "/var/lib/qp-inbox/market-legacy.json"))
MIN_MEM_MB = int(os.getenv("QP_MIN_MEM_MB", "600"))
RETAIN_DAYS = 90
SOURCE_MAX_AGE_H = 26
DOLT_CAL_SOURCE = "dolt.ts_trade_day_calendar"
CAL_FALLBACK_ORIGINS = ("qlib", "legacy")


class LowMemory(RuntimeError):
    pass


def mem_guard() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemAvailable:"):
                    mb = int(ln.split()[1]) // 1024
                    if mb < MIN_MEM_MB:
                        raise LowMemory(f"MemAvailable {mb}MB < {MIN_MEM_MB}MB")
                    return mb
    except OSError:
        return None
    return None


def canon(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def d(v: str) -> dt.date:
    return dt.date.fromisoformat(str(v)[:10])


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def open_days(s: Session, origin: str = "qp") -> list[dt.date]:
    return list(
        s.execute(
            select(TradingDay.date).where(TradingDay.origin == origin, TradingDay.is_open).order_by(TradingDay.date)
        ).scalars()
    )


def calendar_span(s: Session, origin: str) -> tuple[list[dt.date], dt.date | None]:
    rows = s.execute(select(TradingDay.date, TradingDay.is_open).where(TradingDay.origin == origin)).all()
    if not rows:
        return [], None
    opens = sorted(d for d, o in rows if o)
    return opens, max(d for d, _ in rows)


def _gap_only_weekends(max_date: dt.date, today: dt.date) -> bool:
    d = max_date + dt.timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:  # Mon-Fri; A-share never trades Sat/Sun
            return False
        d += dt.timedelta(days=1)
    return True


def calendar_covers(opens: list[dt.date], max_date: dt.date | None, today: dt.date) -> bool:
    """True only when session status through today can be determined.

    A calendar that stores days through today is sufficient. An open-days-only calendar
    that ends on Friday is accepted on Sat/Sun. Any weekday after the last stored date
    means coverage is insufficient (the calendar may be stale).
    """
    if not opens or max_date is None:
        return False
    return max_date >= today or _gap_only_weekends(max_date, today)


def calendar_for_expectation(s: Session, today: dt.date | None = None) -> tuple[list[dt.date] | None, dict]:
    """Prefer the qp calendar when it covers today; otherwise a covering legacy calendar.

    A non-empty but stale calendar is not used. If no calendar covers today, return
    (None, meta) so callers keep the expected date unknown.
    """
    today = today or now_cst().date()
    meta: dict[str, Any] = {"today": today.isoformat()}
    for origin in ("qp", "legacy"):
        days, max_date = calendar_span(s, origin)
        usable = calendar_covers(days, max_date, today)
        meta[origin] = {
            "open_days": len(days),
            "max_date": max_date.isoformat() if max_date else None,
            "covers_today": usable,
        }
        if usable:
            meta["origin"] = origin
            return days, meta
    meta["origin"] = None
    meta["reason"] = "no calendar covers today"
    return None, meta


def expected_trade_date(days: list[dt.date], now: dt.datetime, cutoff: dt.time) -> dt.date | None:
    """Latest open day whose cutoff has passed (today if open and now >= cutoff)."""
    today = now.date()
    cands = [x for x in days if x < today or (x == today and now.time() >= cutoff)]
    return cands[-1] if cands else None


def trading_lag(days: list[dt.date], have: dt.date | None, want: dt.date | None) -> int | None:
    if want is None:
        return None
    if have is None:
        return 999
    if have >= want:
        return 0
    return sum(1 for x in days if have < x <= want)
