"""Run-level audit: mirrors the legacy ``_audit_result`` bounded, conservative outcome."""

from __future__ import annotations

from typing import Any

STATUSES = {"success", "partial", "failed", "skipped"}
OUTCOMES = {"no_signal", "emitted", "blocked", "data_unavailable", "skipped"}


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def audit_result(result: dict[str, Any]) -> dict[str, Any]:
    target_count = _int(result.get("trend_targets"))
    if result.get("skipped"):
        return {
            "status": "skipped",
            "outcome": "skipped",
            "target_count": target_count,
            "evaluated_count": 0,
            "failed_count": 0,
            "signal_count": 0,
            "allowed_signal_count": 0,
            "blocked_signal_count": 0,
            "reason_codes": ["non_trading_day"],
        }

    evaluated_count = _int(result.get("trend_data_ok"))
    failed_targets = result.get("trend_data_failed", [])
    failed_count = len(failed_targets) if isinstance(failed_targets, list) else 0
    decision_count = _int(result.get("decision_signals"))
    blocked_count = min(_int(result.get("decision_risk_blocked_signals")), decision_count)
    allowed_count = decision_count - blocked_count

    raw_reason_codes = result.get("audit_reason_codes", [])
    reason_codes = (
        [str(value)[:64] for value in raw_reason_codes if isinstance(value, str) and value]
        if isinstance(raw_reason_codes, list)
        else []
    )

    collect_status = result.get("collect_status", {})
    if isinstance(collect_status, dict) and any(
        isinstance(item, dict) and (item.get("available") is not True or item.get("stale") or item.get("anomalous"))
        for item in collect_status.values()
    ):
        reason_codes.append("macro_data_partial")

    if target_count > 0 and evaluated_count == 0:
        status = "failed"
        outcome = "data_unavailable"
        reason_codes.append("trend_data_unavailable")
    else:
        status = "partial" if failed_count > 0 or reason_codes else "success"
        if decision_count > 0:
            outcome = "blocked" if allowed_count == 0 else "emitted"
        else:
            outcome = "no_signal" if status == "success" else "data_unavailable"
        if failed_count:
            reason_codes.append("trend_data_partial")

    return {
        "status": status,
        "outcome": outcome,
        "target_count": target_count,
        "evaluated_count": evaluated_count,
        "failed_count": failed_count,
        "signal_count": decision_count,
        "allowed_signal_count": allowed_count,
        "blocked_signal_count": blocked_count,
        "reason_codes": list(dict.fromkeys(reason_codes))[:8],
    }
