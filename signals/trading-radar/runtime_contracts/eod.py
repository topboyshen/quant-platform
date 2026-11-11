"""Day-level EOD classification and strict artifact validation (no database I/O)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .calendar import TZ, day, instant

RECOVERY_ENUM = {
    "none": 0, "awaiting_retry": 1, "recovered_after_data_gap": 2,
    "failed_final": 3, "missing": 4, "non_trading_day": 5,
    "running": 6, "unknown": 7,
}
RUN_STATUSES = {"running", "success", "failed", "partial", "skipped"}


def retryable_data_failure(run: Any, *, decision_date: str | None = None) -> bool:
    """Classify an existing data-failure run; never start or mutate an attempt.

    The established failed/data_unavailable case is preserved. Newly eligible
    partial runs require a same-day identity and explicit zero signal counts,
    so partial emitted/blocked decisions cannot be retried as data gaps.
    Schedule, holiday, maintenance and idempotency gates remain with callers.
    """
    if not isinstance(run, Mapping):
        return False
    status = str(run.get("status") or "")
    if str(run.get("decision_outcome") or "") != "data_unavailable":
        return False
    if status == "failed":
        return True
    if decision_date is not None and run.get("decision_date") != decision_date:
        return False
    if status != "partial" or decision_date is None:
        return False
    try:
        day(decision_date)
    except (TypeError, ValueError):
        return False
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        return False
    return all(type(run.get(key)) is int and run[key] == 0
               for key in ("signal_count", "allowed_signal_count", "blocked_signal_count"))


def classify_day_status(result: dict[str, Any]) -> dict[str, Any]:
    attempts = result.get("attempts") or {}
    latest = attempts.get("latest") or {}
    first = attempts.get("first") or {}
    reason = result.get("reason")
    if reason == "non_trading_day":
        recovery, business = "non_trading_day", "skipped"
    elif result.get("deferred") is True:
        recovery, business = "awaiting_retry", "awaiting_retry"
    elif reason == "running":
        recovery, business = "running", "running"
    elif reason == "success" and result.get("ok") is True and latest.get("status") == "success":
        recovery = "recovered_after_data_gap" if attempts.get("recovered_after_data_gap") is True else "none"
        business = "success"
    elif reason == "missing" and attempts.get("attempts", 0) == 0:
        recovery, business = "missing", "missing"
    elif reason in {"failed", "partial", "running_timeout", "evidence_incomplete"}:
        recovery, business = "failed_final", "failed"
    else:
        recovery, business = "unknown", "unknown"
    return {"schema_version": 1, "business_terminal_status": business,
            "recovery_status": recovery, "systemd_first_failed": first.get("status") == "failed"}


def _count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 1000000:
        raise ValueError("nonnegative integer count required")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("boolean required")
    return value


def _attempt(value: Any, trade_date, checked: datetime) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in RUN_STATUSES:
        raise ValueError("invalid attempt")
    for key in ("evaluated_count", "failed_count"):
        _count(value.get(key))
    if not isinstance(value.get("decision_outcome"), (str, type(None))):
        raise ValueError("invalid outcome")
    completed = value.get("completed_at")
    if completed is None and value["status"] != "running":
        raise ValueError("terminal attempt needs completion timestamp")
    if completed is not None:
        ended = instant(completed)
        if ended.astimezone(TZ).date() != trade_date or ended.timestamp() > checked.timestamp():
            raise ValueError("attempt outside inspection/date")
    return value


def _validate_attempts(payload: dict[str, Any], checked: datetime) -> dict[str, Any]:
    attempts = payload.get("attempts")
    if not isinstance(attempts, dict):
        raise ValueError("attempts object required")
    count = _count(attempts.get("attempts"))
    recovered = _boolean(attempts.get("recovered_after_data_gap"))
    if count == 0:
        if attempts.get("first") is not None or attempts.get("latest") is not None or recovered:
            raise ValueError("nonempty zero-attempt summary")
        return attempts
    trade_date = day(payload.get("trade_date"))
    first = _attempt(attempts.get("first"), trade_date, checked)
    latest = _attempt(attempts.get("latest"), trade_date, checked)
    if count == 1 and first != latest:
        raise ValueError("single attempt summary inconsistent")
    if first.get("completed_at") and latest.get("completed_at"):
        if instant(first["completed_at"]) > instant(latest["completed_at"]):
            raise ValueError("attempt order inconsistent")
    partial_recovered = (first["status"] == "partial"
                         and retryable_data_failure(first, decision_date=trade_date.isoformat())
                         and latest.get("decision_date") == trade_date.isoformat()
                         and isinstance(latest.get("run_id"), str)
                         and bool(latest.get("run_id"))
                         and latest.get("run_id") != first.get("run_id"))
    expected_recovered = (count > 1 and (first["status"] == "failed" or partial_recovered)
                          and first.get("decision_outcome") == "data_unavailable"
                          and latest["status"] == "success")
    if recovered != expected_recovered:
        raise ValueError("recovery evidence inconsistent")
    return attempts


def validate_watchdog(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported watchdog schema")
    ok = _boolean(payload.get("ok"))
    deferred = _boolean(payload.get("deferred"))
    _boolean(payload.get("systemd_first_failed"))
    checked = instant(payload.get("checked_at"))
    trade_date = day(payload.get("trade_date"))
    if checked.timestamp() > now.timestamp() + 300 or checked.astimezone(TZ).date() != trade_date:
        raise ValueError("inspection time/date inconsistent")
    if not isinstance(payload.get("reason"), str):
        raise ValueError("reason required")
    attempts = _validate_attempts(payload, checked)
    latest = attempts.get("latest") or {}
    reason = payload["reason"]
    if ok != (reason in {"success", "awaiting_same_day_retry", "non_trading_day"}):
        raise ValueError("ok/reason inconsistent")
    if deferred != (reason == "awaiting_same_day_retry"):
        raise ValueError("deferral/reason inconsistent")
    if deferred and (latest.get("status") not in {"failed", "partial"} or latest.get("decision_outcome") != "data_unavailable"):
        raise ValueError("deferral evidence inconsistent")
    if deferred and latest.get("status") == "partial" and not retryable_data_failure(
            latest, decision_date=trade_date.isoformat()):
        raise ValueError("partial deferral identity or signal evidence inconsistent")
    if reason == "success" and (latest.get("status") != "success" or latest.get("failed_count") != 0):
        raise ValueError("success evidence inconsistent")
    if reason in {"missing", "non_trading_day"} and attempts["attempts"] != 0:
        raise ValueError("unexpected attempts")
    if reason == "running" and latest.get("status") != "running":
        raise ValueError("running evidence inconsistent")
    derived = classify_day_status(payload)
    for key in ("business_terminal_status", "recovery_status", "systemd_first_failed"):
        if payload.get(key) != derived[key]:
            raise ValueError("day classification inconsistent: " + key)
    return {"ok": int(ok), "deferred": int(deferred),
            "recovery_status": RECOVERY_ENUM[derived["recovery_status"]],
            "systemd_first_failed": int(derived["systemd_first_failed"]),
            "checked_unixtime": checked.timestamp(), "attempts": attempts["attempts"],
            "trade_date": trade_date}
