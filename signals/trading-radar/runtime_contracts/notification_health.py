"""Independent, read-only health computation for the notification dispatch queue (schema v6).

Stdlib only, so the host textfile exporter can import it without pulling in the sender.

* ``compute_dispatch_health`` holds the ONE definition of the health SQL.  The sender's
  ``SwingStore.dispatch_health`` and the exporter both call it, so the two can never drift apart.
* ``open_readonly`` opens the database with ``mode=ro`` (+ ``PRAGMA query_only``): the check can
  never write, never create a journal and never block the sender for long.
* ``read_live_health`` is what the exporter runs on EVERY scrape: it recomputes the numbers
  from the dispatch table itself, so a crashed sender cannot leave a stale "0 to review" behind.

Only counts and ages are produced - never payloads, chat ids, tokens or error text.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

DISPATCH_SCHEMA_VERSION = 6
PENDING_STALE_SECONDS = 300.0
READ_TIMEOUT_SECONDS = 2.0

TERMINAL_UNCONFIRMED = (
    "failed_permanent",
    "failed_exhausted",
    "rate_limited_over_budget",
    "uncertain",
    "quarantined_legacy",
)
ALL_STATES = (
    "pending",
    "sending",
    "retry_wait",
    "confirmed",
    *TERMINAL_UNCONFIRMED,
)
# Columns the health SQL reads; checked before it runs so a damaged table is "unknown", not 0.
REQUIRED_COLUMNS = frozenset({
    "state", "reviewed_at", "lease_expires_at", "budget_deadline_at", "next_attempt_at",
    "created_at", "updated_at", "conflict_count",
})


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_instant(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def open_readonly(db_path: Any, timeout: float = READ_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Read-only connection: ``mode=ro`` URI plus ``query_only`` (defence in depth)."""
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=ON")
    return conn


def compute_dispatch_health(conn: sqlite3.Connection, now: datetime) -> Dict[str, Any]:
    """Health summary of ``notification_dispatch`` (SELECT only).

    Every non-confirmed, un-reviewed row is ``review_needed``.  A row that carries a recorded
    conflict is never sendable; once a human reviewed it, it stops raising the stale-pending /
    stuck-retry alarms (the conflict itself was the alarm) but it is still never sent.
    """
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    stamp = iso(now)
    stale_before = iso(now - timedelta(seconds=PENDING_STALE_SECONDS))

    def scalar(sql: str, params: tuple = ()) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    states = {state: 0 for state in ALL_STATES}
    for state, count in conn.execute(
        "SELECT state,COUNT(*) FROM notification_dispatch GROUP BY state"
    ).fetchall():
        states[str(state)] = int(count)
    unreviewed = {
        state: scalar(
            "SELECT COUNT(*) FROM notification_dispatch WHERE state=? AND reviewed_at IS NULL",
            (state,),
        )
        for state in TERMINAL_UNCONFIRMED
    }
    open_condition = "(conflict_count=0 OR reviewed_at IS NULL)"
    sending_expired = scalar(
        "SELECT COUNT(*) FROM notification_dispatch WHERE state='sending' AND "
        "(lease_expires_at IS NULL OR lease_expires_at<?)", (stamp,))
    retry_wait_stuck = scalar(
        "SELECT COUNT(*) FROM notification_dispatch WHERE state='retry_wait' AND "
        f"{open_condition} AND (budget_deadline_at<? OR next_attempt_at<?)",
        (stamp, stale_before),
    )
    pending_stale = scalar(
        "SELECT COUNT(*) FROM notification_dispatch WHERE state='pending' AND "
        f"{open_condition} AND created_at<?",
        (stale_before,),
    )
    conflicts = scalar(
        "SELECT COUNT(*) FROM notification_dispatch WHERE conflict_count>0 AND reviewed_at IS NULL"
    )
    oldest = conn.execute(
        "SELECT MIN(updated_at) FROM notification_dispatch WHERE "
        "(state IN ('failed_permanent','failed_exhausted','rate_limited_over_budget',"
        "'uncertain','quarantined_legacy') AND reviewed_at IS NULL)"
    ).fetchone()[0]
    oldest_dt = parse_instant(oldest)
    review_needed = (
        sum(unreviewed.values()) + sending_expired + retry_wait_stuck + pending_stale + conflicts
    )
    return {
        "schema_version": 1,
        "states": states,
        "in_flight": max(0, states["sending"] - sending_expired),
        "uncertain": unreviewed["uncertain"],
        "failed_permanent": unreviewed["failed_permanent"],
        "failed_exhausted": unreviewed["failed_exhausted"],
        "rate_limited_over_budget": unreviewed["rate_limited_over_budget"],
        "quarantined_legacy": unreviewed["quarantined_legacy"],
        "sending_expired": sending_expired,
        "retry_wait_stuck": retry_wait_stuck,
        "pending_stale": pending_stale,
        "conflicts": conflicts,
        "review_needed": review_needed,
        "oldest_review_age_seconds": (
            max(0, int((now - oldest_dt).total_seconds())) if oldest_dt else 0
        ),
    }


def read_live_health(db_path: Any, now: datetime) -> Tuple[str, Dict[str, Any]]:
    """Recompute the health straight from the database, read-only.

    Returns ``(status, payload)``:

    * ``("ok", {"available": True, ...})``   - fresh numbers, computed just now;
    * ``("not_v6", {"available": False})``  - readable, but not (validly) schema v6: UNKNOWN;
    * ``("error", {"available": False})``   - could not read (missing file, lock, damage): UNKNOWN.
    """
    conn = None
    try:
        conn = open_readonly(db_path)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != DISPATCH_SCHEMA_VERSION:
            return "not_v6", {"available": False, "reason": f"schema_version_{version}"}
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(notification_dispatch)")}
        if not REQUIRED_COLUMNS <= columns:
            return "not_v6", {"available": False, "reason": "dispatch_table_damaged"}
        return "ok", {"available": True, **compute_dispatch_health(conn, now)}
    except (sqlite3.Error, OSError, ValueError, OverflowError):
        return "error", {"available": False, "reason": "unreadable"}
    finally:
        if conn is not None:
            conn.close()
