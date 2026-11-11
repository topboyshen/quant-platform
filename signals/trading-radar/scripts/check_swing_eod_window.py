#!/usr/bin/env python3
"""Allow scheduled EOD execution only in the same-day post-close window."""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
WINDOW_START = time(15, 25)


def within_eod_catchup_window(now: Optional[datetime] = None) -> bool:
    current = now or datetime.now(BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    current = current.astimezone(BEIJING)
    return current.weekday() < 5 and current.time() >= WINDOW_START


def main() -> None:
    if not within_eod_catchup_window():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
