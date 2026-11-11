"""Best-effort JSON snapshot of the notification dispatch health.

Written next to the swing DB (``swing_notification_health.json``) so the existing
Prometheus textfile exporter can publish it without a DB connection.  It carries counts
and ages only - never payloads, tokens, chat ids or error text.  Grafana visibility of this
snapshot is "independently observable", NOT proof that an independent alert reached anyone.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

HEALTH_FILE = "swing_notification_health.json"
INT_FIELDS = (
    "uncertain",
    "failed_permanent",
    "failed_exhausted",
    "rate_limited_over_budget",
    "quarantined_legacy",
    "sending_expired",
    "retry_wait_stuck",
    "pending_stale",
    "conflicts",
    "review_needed",
    "in_flight",
    "oldest_review_age_seconds",
)


def build_snapshot(store: Any, now: datetime | None = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    health = store.notification_health()
    snapshot: Dict[str, Any] = {
        "schema_version": 1,
        "checked_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "available": bool(health.get("available")),
    }
    if snapshot["available"]:
        for field in INT_FIELDS:
            snapshot[field] = int(health.get(field, 0))
    return snapshot


def write_notification_health(store: Any, now: datetime | None = None) -> Path | None:
    if not hasattr(store, "db_path") or not hasattr(store, "notification_health"):
        return None  # not a SwingStore (e.g. a test double): nothing to observe
    path = Path(store.db_path).expanduser().resolve().parent / HEALTH_FILE
    snapshot = build_snapshot(store, now)
    fd, temporary = tempfile.mkstemp(prefix=".swing_notification_health.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
