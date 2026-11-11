"""Bounded JSON-only EOD metrics. Validate completely before rendering any samples."""
from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path

from .calendar import TZ, day, expected_session, instant, read_object, sessions
from .eod import validate_watchdog
from .notification_health import read_live_health

BUSINESS_METRICS = {
    "ok": "1 if watchdog inspection passed; deferral is separately identified.",
    "deferred": "1 if waiting for same-day retry; not business success.",
    "recovery_status": "none=0 awaiting=1 recovered=2 failed_final=3 missing=4 non_trading=5 running=6 unknown=7.",
    "systemd_first_failed": "Legacy name: first AUDIT attempt failed; not a probe of systemd state.",
    "checked_unixtime": "Watchdog inspection timestamp, not textfile sampling time.",
    "attempts": "Same-day EOD audit attempt count.",
}
FLAGS = {
    "evidence_valid": "1 only for a fully validated watchdog artifact.",
    "calendar_valid": "1 if the shared calendar covers the current day with valid metadata/density.",
    "evidence_current": "1 if artifact trade_date matches the required session; no wall-clock expiry on weekends.",
    "terminal_due": "1 at/after 15:50 of the required session. Unknown calendar is fail-closed.",
    "required_date_unixtime": "Midnight Asia/Shanghai of required session; NaN if unknown.",
}
RUN_FILE = "swing_eod_watchdog_run.json"
RUN_METRICS = {
    "watchdog_run_failed": "1 if the watchdog/daily-report program itself crashed in its latest run; 0 if it completed; NaN if unknown.",
    "watchdog_run_valid": "1 only for a fully validated watchdog run-status artifact (written by the watchdog process itself).",
    "watchdog_run_current": "1 if the run-status artifact trade_date matches the required session.",
    "watchdog_run_checked_unixtime": "Time of the latest watchdog run attempt; NaN if unknown.",
}


NOTIFICATION_FILE = "swing_notification_health.json"
DISPATCH_DB_NAME = "swing_macro.db"
# The sender-written snapshot is only a SHORT-LIVED fallback for when the database cannot be read
# for a moment (lock / transient error).  The snapshot is written by the sender, which may have
# crashed, so an old one must never be shown as current: past this age it counts as UNKNOWN.
SNAPSHOT_MAX_AGE_SECONDS = 900
NOTIFICATION_METRICS = {
    "notification_health_valid": "1 only for fully validated notification-health numbers of a v6 dispatch DB: recomputed live (read-only) from the DB, or a snapshot younger than 15 minutes when the DB was unreadable (0 means UNKNOWN, not healthy).",
    "notification_health_live": "1 if the numbers were recomputed just now by a read-only SELECT on the dispatch table (independent of the sender process); 0 if a fallback snapshot or unknown.",
    "notification_health_checked_unixtime": "Time the notification-health numbers were computed (live) or written (snapshot); NaN if unknown.",
    "notification_review_needed": "Notifications needing human review (uncertain, stuck, exhausted, over-budget, quarantined, conflicting); NaN if unknown.",
    "notification_uncertain": "Unreviewed uncertain notifications (delivery cannot be confirmed; never auto-retried); NaN if unknown.",
    "notification_sending_expired": "Rows left in 'sending' past their lease (process died around the POST); NaN if unknown.",
    "notification_retry_wait_stuck": "retry_wait rows whose retry never ran; NaN if unknown.",
    "notification_failed_exhausted": "Unreviewed notifications that ran out of retry budget (not sent); NaN if unknown.",
    "notification_rate_limited_over_budget": "Unreviewed notifications stopped because the rate-limit wait exceeded the budget (not sent); NaN if unknown.",
    "notification_failed_permanent": "Unreviewed notifications permanently rejected (configuration); NaN if unknown.",
    "notification_quarantined_legacy": "Events quarantined because a legacy failed/sending record exists; NaN if unknown.",
    "notification_conflicts": "Unreviewed same-event/different-body or changed-target conflicts; NaN if unknown.",
    "notification_pending_stale": "Rows enqueued but never claimed for >5 min; NaN if unknown.",
}
_NOTIFICATION_FIELDS = {
    "notification_review_needed": "review_needed",
    "notification_uncertain": "uncertain",
    "notification_sending_expired": "sending_expired",
    "notification_retry_wait_stuck": "retry_wait_stuck",
    "notification_failed_exhausted": "failed_exhausted",
    "notification_rate_limited_over_budget": "rate_limited_over_budget",
    "notification_failed_permanent": "failed_permanent",
    "notification_quarantined_legacy": "quarantined_legacy",
    "notification_conflicts": "conflicts",
    "notification_pending_stale": "pending_stale",
}


def _snapshot_values(path: Path, now: datetime) -> dict:
    """Strict validation of the sender-written snapshot; any problem leaves everything unknown."""
    parsed = {}
    payload = read_object(path, 4096)
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported notification-health schema")
    checked = instant(payload.get("checked_at"))
    if checked.timestamp() > now.timestamp() + 300:
        raise ValueError("notification-health time in the future")
    if now.timestamp() - checked.timestamp() > SNAPSHOT_MAX_AGE_SECONDS:
        raise ValueError("notification-health snapshot is stale")
    if payload.get("available") is not True:
        raise ValueError("dispatch tables not available")
    for metric, field in _NOTIFICATION_FIELDS.items():
        value = payload.get(field)
        if type(value) is not int or value < 0:
            raise ValueError("invalid counter")
        parsed[metric] = value
    return {**parsed, "notification_health_valid": 1, "notification_health_live": 0,
            "notification_health_checked_unixtime": checked.timestamp()}


def _notification_values(path: Path, now: datetime, db_path: Path | None = None) -> dict:
    """Live read-only recomputation first; a young snapshot only if the DB was unreadable.

    Any problem leaves every value unknown (never a healthy-looking 0).  A readable database that
    is not (validly) schema v6 is unknown as well - a stale snapshot must not paper over it.
    """
    values = {key: float("nan") for key in NOTIFICATION_METRICS}
    values["notification_health_valid"] = 0
    values["notification_health_live"] = 0
    # Same directory as the watchdog artifact unless RADAR_SWING_DB points elsewhere.  Resolved here
    # (not in the host script) so the exporter's call signature and import set stay unchanged.
    override = os.environ.get("RADAR_SWING_DB", "").strip()
    db = db_path if db_path is not None else Path(override) if override else path.with_name(DISPATCH_DB_NAME)
    status, health = read_live_health(db, now)
    try:
        if status == "ok":
            parsed = {}
            for metric, field in _NOTIFICATION_FIELDS.items():
                value = health.get(field)
                if type(value) is not int or value < 0:
                    raise ValueError("invalid counter")
                parsed[metric] = value
            values.update(parsed)
            values.update(notification_health_valid=1, notification_health_live=1,
                          notification_health_checked_unixtime=now.timestamp())
        elif status == "error":
            values.update(_snapshot_values(path, now))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        pass
    return values


def _watchdog_run_values(path: Path, required, now: datetime) -> dict:
    """Strictly validate the run-status file; any problem leaves every value unknown."""
    values = {"watchdog_run_failed": float("nan"), "watchdog_run_valid": 0, "watchdog_run_current": 0,
              "watchdog_run_checked_unixtime": float("nan")}
    try:
        payload = read_object(path, 4096)
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError("unsupported run-status schema")
        status = payload.get("status")
        if status not in {"ok", "failed"}:
            raise ValueError("invalid run status")
        trade_date = day(payload.get("trade_date"))
        checked = instant(payload.get("checked_at"))
        if checked.timestamp() > now.timestamp() + 300 or checked.astimezone(TZ).date() != trade_date:
            raise ValueError("run-status time/date inconsistent")
        values.update(watchdog_run_failed=int(status == "failed"), watchdog_run_valid=1,
                      watchdog_run_current=int(required is not None and trade_date == required),
                      watchdog_run_checked_unixtime=checked.timestamp())
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        pass
    return values


def watchdog_metrics(path: Path, calendar_path: Path, now: datetime | None = None,
                     db_path: Path | None = None) -> str:
    now = now or datetime.now(TZ)
    values = {key: float("nan") for key in BUSINESS_METRICS}
    flags = {"evidence_valid": 0, "calendar_valid": 0, "evidence_current": 0,
             "terminal_due": 1, "required_date_unixtime": float("nan")}
    trade_date = None
    required = None
    try:
        validated = validate_watchdog(read_object(path, 65536), now)
        trade_date = validated.pop("trade_date")
        values = validated
        flags["evidence_valid"] = 1
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        pass  # All business values remain NaN; never emit a partially validated set.
    try:
        required = expected_session(sessions(read_object(calendar_path), now), now)
        flags.update(calendar_valid=1, evidence_current=int(trade_date == required),
                     terminal_due=int(now >= datetime.combine(required, time(15, 50), TZ)),
                     required_date_unixtime=datetime.combine(required, time(), TZ).timestamp())
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        pass
    run_values = _watchdog_run_values(path.with_name(RUN_FILE), required, now)
    notification_values = _notification_values(path.with_name(NOTIFICATION_FILE), now, db_path)
    all_values = {**values, **flags, **run_values, **notification_values}
    lines = []
    for key, help_text in {**BUSINESS_METRICS, **FLAGS, **RUN_METRICS, **NOTIFICATION_METRICS}.items():
        name = "radar_swing_eod_" + ("watchdog_ok" if key == "ok" else key)
        value = all_values[key]
        rendered = "NaN" if isinstance(value, float) and value != value else str(value)
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {rendered}"])
    return "\n".join(lines) + "\n"
