"""Offline artifact tests; no database, notification, network or production files."""
import copy
import json
from datetime import date, datetime, timedelta

import pytest

from runtime_contracts.calendar import TZ, expected_session, previous_session, sessions
from runtime_contracts.eod import classify_day_status, validate_watchdog
from runtime_contracts.watchdog_metrics import watchdog_metrics

NOW = datetime(2026, 9, 11, 16, tzinfo=TZ)


def calendar_payload():
    start = date(2026, 1, 1)
    days = [start + timedelta(days=i) for i in range(365)]
    holidays = {date(2026, 9, 25), *(date(2026, 10, i) for i in range(1, 8))}
    dates = [d.isoformat() for d in days if d.weekday() < 5 and d not in holidays]
    return {"schema_version": 1, "source": "akshare.tool_trade_date_hist_sina",
            "generated_at": "2026-09-01T06:20:00+08:00", "count": len(dates),
            "min_date": dates[0], "max_date": dates[-1], "trading_dates": dates}


def payload(reason="success", recovered=True):
    first = {"status": "failed", "decision_outcome": "data_unavailable",
             "completed_at": "2026-09-11T15:30:20+08:00", "evaluated_count": 0, "failed_count": 10}
    latest = {"status": "success", "decision_outcome": "no_signal",
              "completed_at": "2026-09-11T15:48:20+08:00", "evaluated_count": 10, "failed_count": 0}
    if reason in {"failed", "awaiting_same_day_retry"}:
        latest = dict(first)
    if reason in {"running", "running_timeout"}:
        latest.update(status="running", completed_at=None)
    if reason == "evidence_incomplete":
        recovered = False
    if reason != "success":
        recovered = False
    if not recovered:
        first = dict(latest)
    summary = {"attempts": 2 if recovered else 1, "first": first, "latest": latest,
               "recovered_after_data_gap": recovered}
    if reason == "missing":
        summary.update(attempts=0, first=None, latest=None)
    result = {"ok": reason in {"success", "awaiting_same_day_retry"}, "reason": reason,
              "deferred": reason == "awaiting_same_day_retry", "checked_at": NOW.isoformat(),
              "trade_date": "2026-09-11", "attempts": summary}
    return {**result, **classify_day_status(result)}


def values(text):
    rows = [line.split() for line in text.splitlines() if line and not line.startswith("#")]
    assert len(rows) == len({row[0] for row in rows}), "duplicate metric samples"
    return {name.removeprefix("radar_swing_eod_"): value for name, value in rows}


def render(tmp_path, data, now=NOW, calendar=None):
    path, cal = tmp_path / "status.json", tmp_path / "calendar.json"
    path.write_text(json.dumps(data))
    cal.write_text(json.dumps(calendar if calendar is not None else calendar_payload()))
    return values(watchdog_metrics(path, cal, now))


def test_good_recovery_and_old_friday_remains_valid_on_weekend(tmp_path):
    got = render(tmp_path, payload(), datetime(2026, 9, 12, 23, tzinfo=TZ))
    assert got["evidence_valid"] == got["evidence_current"] == got["watchdog_ok"] == "1"
    assert got["recovery_status"] == "2"


@pytest.mark.parametrize("field,value", [
    ("ok", "false"), ("ok", 1), ("deferred", "false"), ("systemd_first_failed", 1),
    ("schema_version", True), ("schema_version", 99), ("reason", []),
    ("checked_at", "2026-09-11T16:00:00"), ("checked_at", "2026-09-12T16:00:00+08:00"),
    ("trade_date", "2026-09-10"), ("recovery_status", "failed_final"),
    ("business_terminal_status", "failed"), ("attempts", []),
])
def test_invalid_root_fails_closed_without_partial_samples(tmp_path, field, value):
    data = payload(); data[field] = value
    got = render(tmp_path, data)
    assert got["evidence_valid"] == "0" and got["watchdog_ok"] == "NaN"


@pytest.mark.parametrize("field,value", [("attempts", "bad"), ("attempts", True), ("attempts", -1),
                                         ("first", "bad"), ("latest", []), ("recovered_after_data_gap", "true")])
def test_malformed_attempt_summary_no_attribute_error_or_duplicate(tmp_path, field, value):
    data = payload(); data["attempts"][field] = value
    assert render(tmp_path, data)["evidence_valid"] == "0"


@pytest.mark.parametrize("field,value", [("status", []), ("completed_at", "bad"),
                                         ("completed_at", "2026-09-10T15:30:00+08:00"),
                                         ("failed_count", "0"), ("evaluated_count", -1)])
def test_invalid_nested_attempt(tmp_path, field, value):
    data = payload(); data["attempts"]["latest"][field] = value
    assert render(tmp_path, data)["evidence_valid"] == "0"


def test_missing_corrupt_deep_and_oversize_artifacts(tmp_path):
    path = tmp_path / "missing.json"
    cal = tmp_path / "cal.json"; cal.write_text(json.dumps(calendar_payload()))
    for content in (None, "{", "[]", "[" * 2000 + "0" + "]" * 2000, " " * 65537):
        if content is not None:
            path.write_text(content)
        got = values(watchdog_metrics(path, cal, NOW))
        assert got["evidence_valid"] == "0" and got["watchdog_ok"] == "NaN"


@pytest.mark.parametrize("reason,business,state", [("running", "running", 6), ("running_timeout", "failed", 3),
    ("evidence_incomplete", "failed", 3), ("missing", "missing", 4),
    ("awaiting_same_day_retry", "awaiting_retry", 1), ("failed", "failed", 3)])
def test_day_states_are_distinct(reason, business, state):
    data = payload(reason)
    assert data["business_terminal_status"] == business
    assert validate_watchdog(data, NOW)["recovery_status"] == state


def test_monday_1550_old_success_is_stale_not_recovery(tmp_path):
    got = render(tmp_path, payload(), datetime(2026, 9, 14, 15, 50, tzinfo=TZ))
    assert got["evidence_current"] == "0" and got["terminal_due"] == "1"


def test_pending_deadline_changes_without_rewriting_json(tmp_path):
    data = payload("awaiting_same_day_retry"); data["checked_at"] = "2026-09-11T15:40:00+08:00"
    before = render(tmp_path, data, datetime(2026, 9, 11, 15, 45, tzinfo=TZ))
    after = render(tmp_path, data, NOW)
    assert before["evidence_valid"] == after["evidence_valid"] == "1"
    assert before["terminal_due"] == "0" and after["terminal_due"] == "1"


def test_shared_calendar_holidays_weekends_and_previous_session():
    dates = sessions(calendar_payload(), NOW)
    assert previous_session(dates, date(2026, 10, 8)) == date(2026, 9, 30)
    assert previous_session(dates, date(2026, 9, 14)) == date(2026, 9, 11)
    assert expected_session(dates, datetime(2026, 10, 5, 18, tzinfo=TZ)) == date(2026, 9, 30)
    with pytest.raises(ValueError):
        previous_session(dates, date(2026, 10, 1))


@pytest.mark.parametrize("mutation", ["missing", "future", "duplicate", "gap", "weekend", "uncovered"])
def test_bad_calendar_is_unknown_never_assumed_holiday(tmp_path, mutation):
    cal = copy.deepcopy(calendar_payload())
    if mutation == "missing": cal = {}
    elif mutation == "future": cal["generated_at"] = "2099-01-01T00:00:00+08:00"
    elif mutation == "duplicate": cal["trading_dates"].append(cal["trading_dates"][-1])
    elif mutation == "gap": cal["trading_dates"] = [d for d in cal["trading_dates"] if "2026-08" not in d]
    elif mutation == "weekend": cal["trading_dates"].append("2026-09-12"); cal["trading_dates"].sort()
    elif mutation == "uncovered": cal["trading_dates"] = [d for d in cal["trading_dates"] if d < "2026-09-11"]
    if "trading_dates" in cal:
        cal.update(count=len(cal["trading_dates"]), max_date=cal["trading_dates"][-1])
    got = render(tmp_path, payload(), calendar=cal)
    assert got["calendar_valid"] == "0"
