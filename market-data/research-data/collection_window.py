"""Validate new market-stage collection windows without consulting wall time.

Only call this when collecting a new stage, not when loading historical sealed
stages. All date comparisons use China Standard Time (UTC+08:00). A window may
not cross a CST date: this prevents one immutable daily object mixing two days.
Older source trade dates remain permissible, with the actual input timestamps
unchanged; acceptance is not evidence of an observation on that earlier date.
"""

import datetime as dt
import re


CST = dt.timezone(dt.timedelta(hours=8))
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_SAME_DAY_EARLIEST = dt.time(15, 10)


def _timestamp(value, field):
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"invalid_{field}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(CST)
    except (ValueError, OverflowError):
        raise ValueError(f"invalid_{field}") from None


def validate_stage_collection_time(trade_date, started_at, completed_at):
    """Raise a fixed-reason ValueError for an unsafe new-stage time window.

    Inputs are a canonical YYYY-MM-DD and timezone-aware ISO timestamps with
    seconds (optional microseconds), using T and Z or a colon-separated offset.
    The start must be at least 15:10 CST when the source trade date is the same
    as the collection day. No timestamp is inferred, rewritten, or returned.
    """
    if not isinstance(trade_date, str) or _DATE.fullmatch(trade_date) is None:
        raise ValueError("invalid_trade_date")
    try:
        source_day = dt.date.fromisoformat(trade_date)
    except ValueError:
        raise ValueError("invalid_trade_date") from None
    start = _timestamp(started_at, "started_at")
    end = _timestamp(completed_at, "completed_at")
    if end < start:
        raise ValueError("collection_window_reversed")
    if start.date() != end.date():
        raise ValueError("collection_window_crosses_cst_date")
    if source_day > end.date():
        raise ValueError("source_trade_date_in_future")
    if source_day == end.date() and start.time() < _SAME_DAY_EARLIEST:
        raise ValueError("collection_before_cst_close")
