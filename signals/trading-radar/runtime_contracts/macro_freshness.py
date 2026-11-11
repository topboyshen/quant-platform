"""Read-only financing freshness. Never fetch a calendar or rewrite observations."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from runtime_contracts.calendar import TZ, day, read_object, sessions, previous_session
from runtime_contracts.calendar_authority import verify_session_year

CALENDAR_PATH = Path(__file__).resolve().parents[1] / "data" / "trading_calendar.json"


def _calendar_path(explicit=None):
    """Match the scheduler's deployment override; explicit test input wins."""
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.getenv("RADAR_TRADING_CALENDAR_FILE", "").strip()
    return Path(configured).expanduser() if configured else CALENDAR_PATH


def financing_freshness(observed_date, slice_date, *, now: datetime, calendar_path=None):
    """Keep the existing T/T-1 contract, measured in verified exchange sessions.

    This validates only freshness; availability, numeric validity and anomalies
    remain independent gates in the collector. It is not a successful EOD result.
    """
    try:
        observed, target = day(observed_date), day(slice_date)
        if now.tzinfo is None or target > now.astimezone(TZ).date():
            raise ValueError("slice clock invalid")
        dates = sessions(read_object(_calendar_path(calendar_path)), now)
        authority = verify_session_year(dates, target.year)
        if target not in dates or observed not in dates:
            raise ValueError("observation or target not in verified calendar")
        previous = previous_session(dates, target)
        lag = dates.index(target) - dates.index(observed)
        return {"stale": observed not in {previous, target},
                "reason": "within_one_session" if observed in {previous, target} else "outside_one_session",
                "observed_date": observed.isoformat(), "slice_date": target.isoformat(),
                "previous_session": previous.isoformat(), "lag_sessions": lag,
                "calendar_authority": authority}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"stale": True, "reason": "calendar_or_date_unverified",
                "detail": str(exc), "lag_sessions": None}


def financing_growth_basis(observed_date, basis_date, *, now: datetime, calendar_path=None):
    """Daily growth needs a separately verified immediately preceding session.

    An up-to-date balance alone does not prove that a percentage is a daily
    change. This never infers a missing denominator date from a filename/mtime.
    Latest balance freshness is a separate collector gate.
    """
    try:
        observed, basis = day(observed_date), day(basis_date)
        if now.tzinfo is None or observed > now.astimezone(TZ).date():
            raise ValueError("observation clock invalid")
        dates = sessions(read_object(_calendar_path(calendar_path)), now)
        authority = verify_session_year(dates, observed.year)
        expected = previous_session(dates, observed)
        valid = basis == expected
        return {"verified": valid,
                "reason": "adjacent_sessions" if valid else "nonadjacent_growth_basis",
                "observed_date": observed.isoformat(), "basis_date": basis.isoformat(),
                "expected_basis_date": expected.isoformat(), "calendar_authority": authority}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"verified": False, "reason": "growth_basis_unverified", "detail": str(exc)}
