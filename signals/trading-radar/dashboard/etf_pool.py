"""Bounded, read-only ETF universe joined to ONE immutable EOD attempt.

Membership comes from the configured universe, never from historical triggers.
No provider requests, SwingStore initialization, prices cache, or DB writes.
"""
from __future__ import annotations

import math
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Any

from runtime_contracts.calendar import TZ, day, expected_session, instant, read_object, sessions

MAX_TARGETS = 200
STATES = ("no_trigger", "allowed", "blocked", "data_failed", "missing", "unknown", "stale", "running")
COLUMNS = ("code", "decision_date", "data_date", "data_status", "decision_state", "raw_trigger_count",
           "risk_action", "risk_level", "reason_code", "reference_close", "snapshot_id")
MAX_DIAGNOSTIC_BYTES = 512_000
HISTORY_RUN_LIMIT = 400
HISTORY_ROW_LIMIT = 2000
SIGNAL_PERIODS = {"weekly_macd_golden_cross": "W", "volume_breakout_ma60": "D"}
DIAGNOSTIC_REASONS = {
    "data": {"data_unavailable", "insufficient_daily_rows", "data_date_mismatch", "fetch_failed",
             "empty_daily_data", "missing_date_or_close", "no_valid_daily_rows", "diagnostic_error"},
    "daily": {"data_not_ready", "ma_unavailable", "volume_missing", "volume_unavailable",
              "previous_close_above_ma", "close_not_above_ma", "volume_below_threshold", "diagnostic_error"},
    "weekly": {"data_not_ready", "insufficient_completed_weeks", "no_weekly_golden_cross",
               "weekly_values_unavailable", "diagnostic_error"},
}


def _diagnostic_empty(status="not_recorded", run_id=None, decision_date=None):
    return {"status": status, "run_id": run_id, "decision_date": decision_date,
            "data": None, "daily": None, "weekly": None}


def _history_empty(status="unavailable", scanned=0):
    return {"status": status, "lookback_runs": HISTORY_RUN_LIMIT, "scanned_runs": scanned,
            "excluded_stale_count": 0, "latest_raw_trigger": None, "latest_allowed_target": None}


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _bounded_json(raw, limit):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limit:
        raise ValueError("bounded JSON missing")
    return json.loads(raw, object_pairs_hook=_json_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _number(value, *, nullable=True, minimum=-1e15, maximum=1e15):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("invalid diagnostic number")
    return value


def _integer(value, maximum=1_000_000, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid diagnostic count")
    return value


def _boolean(value, *, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not bool:
        raise ValueError("invalid diagnostic boolean")
    return value


def _date_or_none(value):
    return None if value is None else day(value).isoformat()


def _reason_codes(raw, section):
    if (not isinstance(raw, list) or len(raw) > 8 or len(set(raw)) != len(raw)
            or any(not isinstance(x, str) or x not in DIAGNOSTIC_REASONS[section] for x in raw)):
        raise ValueError("invalid diagnostic reasons")
    return list(raw)


def _comparison(value, left, right, comparison, *, allow_unknown=False):
    if value is None and allow_unknown:
        return
    expected = None if left is None or right is None else comparison(left, right)
    if value is not expected:
        raise ValueError("diagnostic comparison contradicts operands")


def _daily_diagnostic(raw):
    result = {"evaluated": _boolean(raw["evaluated"], nullable=False),
              "reason_codes": _reason_codes(raw["reason_codes"], "daily"),
              "ma_period": _integer(raw["ma_period"], 1000),
              "volume_threshold": _number(raw["volume_threshold"], nullable=False, minimum=0, maximum=1000)}
    for key in ("previous_close", "close", "previous_ma", "ma", "volume", "average_volume20",
                "volume_ratio_raw", "volume_ratio"):
        result[key] = _number(raw[key], minimum=0)
    for key in ("previous_at_or_below_ma", "close_above_ma", "price_cross", "volume_ok", "triggered"):
        result[key] = _boolean(raw[key])
    if result["ma_period"] == 0:
        raise ValueError("invalid MA period")
    _comparison(result["previous_at_or_below_ma"], result["previous_close"], result["previous_ma"], lambda a, b: a <= b)
    _comparison(result["close_above_ma"], result["close"], result["ma"], lambda a, b: a > b)
    previous, current = result["previous_at_or_below_ma"], result["close_above_ma"]
    if result["price_cross"] is not (None if previous is None or current is None else previous and current):
        raise ValueError("invalid price cross")
    ratio, ratio_raw = result["volume_ratio"], result["volume_ratio_raw"]
    if ratio_raw is not None:
        if (result["volume"] is None or not result["average_volume20"]
                or not math.isclose(ratio_raw, result["volume"] / result["average_volume20"], rel_tol=1e-9, abs_tol=1e-9)
                or ratio != round(ratio_raw, 2)):
            raise ValueError("invalid volume ratio")
    _comparison(result["volume_ok"], ratio, result["volume_threshold"], lambda a, b: a >= b)
    if result["evaluated"]:
        if ratio_raw is None or any(result[k] is None for k in ("price_cross", "volume_ok")):
            raise ValueError("incomplete evaluated daily diagnostic")
        if result["triggered"] is not (result["price_cross"] and result["volume_ok"]):
            raise ValueError("invalid daily trigger")
        reasons = {reason for flag, reason in (
            (result["previous_at_or_below_ma"], "previous_close_above_ma"),
            (result["close_above_ma"], "close_not_above_ma"),
            (result["volume_ok"], "volume_below_threshold"),
        ) if not flag}
        if set(result["reason_codes"]) != reasons:
            raise ValueError("daily reasons contradict evaluated conditions")
    elif result["triggered"] is not None:
        raise ValueError("unevaluated daily trigger must be unknown")
    return result


def _weekly_diagnostic(raw, data_date):
    result = {"evaluated": _boolean(raw["evaluated"], nullable=False),
              "reason_codes": _reason_codes(raw["reason_codes"], "weekly"),
              "completed_bars": _integer(raw["completed_bars"], nullable=True),
              "minimum_bars": _integer(raw["minimum_bars"]),
              "previous_week_date": _date_or_none(raw["previous_week_date"]),
              "week_date": _date_or_none(raw["week_date"])}
    if result["minimum_bars"] != 35:
        raise ValueError("invalid weekly minimum")
    previous_date, current_date = result["previous_week_date"], result["week_date"]
    for value in (previous_date, current_date):
        if value and (not data_date or value > data_date or day(value).weekday() != 4):
            raise ValueError("invalid completed week date")
    if previous_date and (not current_date or previous_date >= current_date):
        raise ValueError("invalid weekly date order")
    for key in ("previous_dif", "previous_dea", "dif", "dea"):
        result[key] = _number(raw[key])
    for key in ("previous_at_or_below", "now_above", "golden_cross", "triggered"):
        result[key] = _boolean(raw[key])
    _comparison(result["previous_at_or_below"], result["previous_dif"], result["previous_dea"], lambda a, b: a <= b,
                allow_unknown=not result["evaluated"])
    _comparison(result["now_above"], result["dif"], result["dea"], lambda a, b: a > b,
                allow_unknown=not result["evaluated"])
    previous, current = result["previous_at_or_below"], result["now_above"]
    cross = None if previous is None or current is None else previous and current
    if result["golden_cross"] is not cross:
        raise ValueError("invalid weekly cross")
    if result["evaluated"]:
        if not previous_date or not current_date or (result["completed_bars"] or 0) < 35 or cross is None:
            raise ValueError("incomplete evaluated weekly diagnostic")
        if result["triggered"] is not cross:
            raise ValueError("invalid weekly trigger")
        if result["reason_codes"] != ([] if cross else ["no_weekly_golden_cross"]):
            raise ValueError("weekly reasons contradict evaluated conditions")
    elif result["triggered"] is not None:
        raise ValueError("unevaluated weekly trigger must be unknown")
    return result


def _clean_diagnostic(raw, item, run_id, decision_date):
    source = raw["data"]
    data = {"ready": _boolean(source["ready"], nullable=False),
            "rows": _integer(source["rows"]), "minimum_rows": _integer(source["minimum_rows"]),
            "data_date": _date_or_none(source["data_date"]),
            "required_date": _date_or_none(source["required_date"]),
            "reason_codes": _reason_codes(source["reason_codes"], "data")}
    if (data["required_date"] != decision_date or data["minimum_rows"] < 1
            or (data["data_date"] and data["data_date"] > decision_date)):
        raise ValueError("diagnostic date mismatch")
    daily, weekly = _daily_diagnostic(raw["daily"]), _weekly_diagnostic(raw["weekly"], data["data_date"])
    if data["minimum_rows"] != daily["ma_period"] + 5:
        raise ValueError("diagnostic warmup mismatch")
    if data["ready"]:
        if (data["reason_codes"] or data["rows"] < data["minimum_rows"] or data["data_date"] != decision_date
                or item["data_date"] != data["data_date"] or item["data_status"] != "ok"):
            raise ValueError("diagnostic readiness contradicts decision")
        if daily["close"] is not None and not math.isclose(daily["close"], item["reference_close"], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("diagnostic price contradicts decision")
    elif daily["evaluated"] or weekly["evaluated"] or item["data_status"] != "failed":
        raise ValueError("unready diagnostic contradicts decision")
    triggers = (daily["triggered"], weekly["triggered"])
    count = item["raw_trigger_count"]
    if type(count) is not int or not sum(x is True for x in triggers) <= count <= sum(x is not False for x in triggers):
        raise ValueError("diagnostic trigger count contradicts decision")
    return {"status": "recorded", "run_id": run_id, "decision_date": decision_date,
            "data": data, "daily": daily, "weekly": weekly}


def _load_diagnostics(pool, connection):
    run_id, decision_date = pool["run_id"], pool["decision_date"]
    if not run_id or pool["run_status"] == "running":
        return
    try:
        rows = connection.execute(
            "SELECT run_id,decision_date,status,completed_at,CASE WHEN length(details)<=? THEN details END "
            "FROM swing_eod_run WHERE run_id=? LIMIT 2", (MAX_DIAGNOSTIC_BYTES, run_id),
        ).fetchall()
    except sqlite3.Error:
        return
    status, by_code = "not_recorded", {}
    try:
        if (len(rows) != 1 or tuple(rows[0][:3]) != (run_id, decision_date, pool["run_status"])
                or instant(rows[0][3]) != instant(pool["observed_at"])):
            raise ValueError("unbound diagnostic run")
        details = _bounded_json(rows[0][4], MAX_DIAGNOSTIC_BYTES)
        if not isinstance(details, dict):
            raise ValueError("invalid details")
        raw = details.get("trigger_diagnostics")
        if raw is not None:
            if (not isinstance(raw, dict) or raw.get("schema_version") != "quant.swing-trigger-diagnostics.v1"
                    or raw.get("run_id") != run_id or raw.get("decision_date") != decision_date):
                raise ValueError("invalid diagnostic identity")
            targets = raw.get("targets")
            codes = {item["code"] for item in pool["items"]}
            if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
                raise ValueError("unbounded diagnostics")
            for entry in targets:
                code = entry.get("code") if isinstance(entry, dict) else None
                if code not in codes or code in by_code:
                    raise ValueError("unexpected/duplicate diagnostic target")
                by_code[code] = entry
            status = "recorded"
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        status = "invalid"
    for item in pool["items"]:
        item["trigger_diagnostics"] = _diagnostic_empty(status, run_id, decision_date)
        if status == "recorded":
            try:
                item["trigger_diagnostics"] = (_clean_diagnostic(by_code[item["code"]], item, run_id, decision_date)
                                               if item["code"] in by_code else _diagnostic_empty("not_recorded", run_id, decision_date))
            except (ValueError, TypeError, KeyError, OverflowError):
                item["trigger_diagnostics"] = _diagnostic_empty("invalid", run_id, decision_date)


def _eligible_history_run(row, now, decision_time):
    (run_id, run_date, status, started_at, completed_at, target_count, evaluated, failed,
     signal_count, allowed, blocked) = row
    try:
        run_date = day(run_date).isoformat()
        started, completed = instant(started_at).astimezone(TZ), instant(completed_at).astimezone(TZ)
        for value in (target_count, evaluated, failed, signal_count, allowed, blocked):
            _integer(value, 1000)
        scheduled = datetime.combine(day(run_date), decision_time, tzinfo=TZ)
        if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", run_id)
                or target_count < 1 or evaluated + failed != target_count
                or status not in {"success", "partial"} or (status == "success" and failed)
                or allowed + blocked != signal_count
                or started.date().isoformat() != run_date or completed.date().isoformat() != run_date
                or started < scheduled - timedelta(minutes=5) or completed < started
                or completed.timestamp() > now.timestamp() + 300):
            return None
        return {"run_id": run_id, "date": run_date, "started": started, "completed": completed}
    except (ValueError, TypeError, OverflowError):
        return None


def _history_signal(raw, row, run):
    kind = raw.get("signal_type") if isinstance(raw, dict) else None
    if kind not in SIGNAL_PERIODS or raw.get("period") != SIGNAL_PERIODS[kind]:
        raise ValueError("unknown history signal")
    detail = raw.get("detail")
    if not isinstance(detail, dict):
        raise ValueError("missing signal bar")
    bar = day(detail.get("data_date")).isoformat()
    if bar > row["data_date"] or (raw["period"] == "D" and bar != row["data_date"]):
        raise ValueError("signal bar date mismatch")
    if raw["period"] == "W" and (day(bar).weekday() != 4 or (day(row["data_date"]) - day(bar)).days > 6):
        raise ValueError("incomplete weekly bar")
    return {"signal_type": kind, "period": raw["period"], "signal_data_date": bar,
            "first_observed_date": run["date"], "first_observation_scope": "searched_window",
            "first_observed_at": row["observed_at"],
            "data_date": row["data_date"], "run_id": run["run_id"],
            "target_risk_action": row["risk_action"], "risk_level": row["risk_level"],
            "risk_scope": "target", "mixed_signal_gating_possible": row["risk_action"] == "allowed" and row["raw_trigger_count"] > 1}


def _load_history(pool, connection, now, config):
    # One bounded run query, one bounded joined decision query, never one query
    # per ETF. A capped window is explicit; absence does not mean "never".
    runs = connection.execute(
        "SELECT run_id,decision_date,status,started_at,completed_at,target_count,evaluated_count,failed_count,"
        "signal_count,allowed_signal_count,blocked_signal_count FROM swing_eod_run ORDER BY id DESC LIMIT ?",
        (HISTORY_RUN_LIMIT + 1,),
    ).fetchall()
    truncated = len(runs) > HISTORY_RUN_LIMIT
    runs = runs[:HISTORY_RUN_LIMIT]
    decision_time = time(15, 30)
    value = config.get("swing", {}).get("decision_time") if isinstance(config.get("swing", {}), dict) else None
    if isinstance(value, str) and re.fullmatch(r"[0-2][0-9]:[0-5][0-9] Asia/Shanghai", value):
        decision_time = time.fromisoformat(value.split()[0])
    eligible = {}
    for raw in runs:
        run = _eligible_history_run(raw, now, decision_time)
        if run:
            if run["run_id"] in eligible:
                raise ValueError("duplicate history run")
            eligible[run["run_id"]] = run
    histories = {item["code"]: _history_empty("none_in_window", len(runs)) for item in pool["items"]}
    events = {code: {} for code in histories}
    allowed_events = {code: {} for code in histories}
    rows = []
    if eligible:
        placeholders = ",".join("?" for _ in eligible)
        rows = connection.execute(
            "SELECT d.run_id,d.decision_id," + ",".join("d." + c for c in COLUMNS) +
            ",CASE WHEN length(d.raw_signals_json)<=16384 THEN d.raw_signals_json END,"
            "s.snapshot_id,s.level,s.decision_at,d.decision_at,d.validation_split "
            "FROM eod_target_decision d LEFT JOIN eod_risk_snapshot s ON s.run_id=d.run_id "
            "WHERE d.run_id IN (" + placeholders + ") AND d.raw_trigger_count>0 "
            "ORDER BY d.decision_date DESC,d.run_id,d.code LIMIT ?",
            (*eligible, HISTORY_ROW_LIMIT + 1),
        ).fetchall()
    truncated = truncated or len(rows) > HISTORY_ROW_LIMIT
    seen = set()
    for raw in rows[:HISTORY_ROW_LIMIT]:
        run_id, decision_id = raw[:2]
        row = dict(zip(COLUMNS, raw[2:2 + len(COLUMNS)]))
        if row["code"] not in histories:
            continue
        if (run_id, row["code"]) in seen:
            raise ValueError("ambiguous historical decision/risk")
        seen.add((run_id, row["code"]))
        run = eligible[run_id]
        if row["data_date"] != row["decision_date"]:
            histories[row["code"]]["excluded_stale_count"] += 1
            continue
        try:
            encoded, snapshot_id, level, risk_at, decision_at, split = raw[2 + len(COLUMNS):]
            if (not decision_id or row["snapshot_id"] != snapshot_id or row["risk_level"] != level
                    or level not in {"ok", "warning", "critical", "growth_blocked", "unavailable"}
                    or instant(risk_at) != instant(decision_at) or split != "live_oos"
                    or not run["started"] <= instant(decision_at) <= run["completed"]):
                raise ValueError("unbound historical risk")
            _decision(row, {"code": row["code"]}, run["date"], run["date"])
            row["observed_at"] = instant(decision_at).astimezone(TZ).isoformat()
            signals = _bounded_json(encoded, 16384)
            if not isinstance(signals, list) or len(signals) != row["raw_trigger_count"] or not 1 <= len(signals) <= 2:
                raise ValueError("invalid history signals")
            entries = [_history_signal(signal, row, run) for signal in signals]
            keys = [(e["signal_type"], e["period"], e["signal_data_date"]) for e in entries]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate raw signals")
            for key, entry in zip(keys, entries):
                code = row["code"]
                if key not in events[code] or entry["first_observed_at"] < events[code][key]["first_observed_at"]:
                    events[code][key] = entry
                if row["risk_action"] == "allowed":
                    if key not in allowed_events[code] or entry["first_observed_at"] < allowed_events[code][key]["first_observed_at"]:
                        allowed_events[code][key] = entry
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            continue
    for item in pool["items"]:
        history = histories[item["code"]]
        if events[item["code"]]:
            history["latest_raw_trigger"] = max(events[item["code"]].values(), key=lambda e: (e["signal_data_date"], e["signal_type"]))
            history["status"] = "available"
        if allowed_events[item["code"]]:
            history["latest_allowed_target"] = max(allowed_events[item["code"]].values(), key=lambda e: (e["signal_data_date"], e["signal_type"]))
        if truncated:
            history["status"] = "truncated"
        item["history"] = history


def pool_expected_date(path: Path, now: datetime) -> str | None:
    try:
        return expected_session(sessions(read_object(path), now), now).isoformat()
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def _text(value: Any, limit: int = 80) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _targets(config: Any) -> list[dict[str, str]]:
    raw = config.get("targets") if isinstance(config, dict) else None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TARGETS:
        raise ValueError("invalid universe")
    result = []
    codes = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid target")
        code = item.get("code")
        if (not isinstance(code, str) or not re.fullmatch(r"(?:15|16|18|50|51|52|56|58)[0-9]{4}", code)
                or code in codes):
            raise ValueError("invalid/duplicate ETF code")
        codes.add(code)
        result.append({"code": code, "name": _text(item.get("name")) or code,
                       "sector": _text(item.get("sector"), 40), "market": "CNStock"})
    return result


def _empty_item(target: dict[str, str], state: str = "missing", reason: str = "eod_evidence_missing") -> dict:
    return {**target, "state": state, "reason_code": reason, "decision_state": None,
            "decision_date": None, "data_date": None, "data_status": "unknown", "is_current": False,
            "reference_close": None, "raw_trigger_count": None, "risk_action": None, "risk_level": None}


def _decision(row: dict, target: dict, audit_date: str, expected: str | None) -> dict:
    """Reject contradictory rows instead of laundering them into 'no trigger'."""
    state = row["decision_state"]
    count = row["raw_trigger_count"]
    data_date = day(row["data_date"]).isoformat() if row["data_date"] else None
    if (row["decision_date"] != audit_date or state not in STATES[:4]
            or type(count) is not int or not 0 <= count <= 1000
            or (data_date and data_date > audit_date)):
        raise ValueError("invalid decision")
    price = row["reference_close"]
    if state == "data_failed":
        if row["data_status"] != "failed" or count != 0 or row["risk_action"] != "not_applicable" or price is not None:
            raise ValueError("invalid data failure")
    else:
        expected_action = {"no_trigger": "not_applicable", "allowed": "allowed", "blocked": "blocked"}[state]
        if (row["data_status"] != "ok" or data_date != audit_date
                or type(price) not in (int, float) or not math.isfinite(price) or not 0 < price < 1e9
                or row["risk_action"] != expected_action
                or (state == "no_trigger" and count != 0) or (state != "no_trigger" and count < 1)):
            raise ValueError("invalid evaluated row")
    display_state = state
    if expected is None:
        display_state = "unknown"
    elif audit_date != expected:
        display_state = "stale"
    return {**target, "state": display_state, "decision_state": state,
            "decision_date": audit_date, "data_date": data_date, "data_status": row["data_status"],
            "is_current": expected == audit_date and state != "data_failed",
            "reference_close": price, "raw_trigger_count": count,
            "risk_action": row["risk_action"], "risk_level": _text(row["risk_level"], 32),
            "reason_code": row["reason_code"] if isinstance(row["reason_code"], str)
            and re.fullmatch(r"[a-z0-9_]{1,80}", row["reason_code"]) else "eod_evidence_invalid"}


def _rows(connection: sqlite3.Connection, run_id: str, codes: set[str], audit_date: str) -> dict[str, dict]:
    # Two bounded reads in the caller's snapshot, never N per-ETF queries.
    snapshots = connection.execute(
        "SELECT snapshot_id,level,decision_at FROM eod_risk_snapshot WHERE run_id=? LIMIT 2", (run_id,)
    ).fetchall()
    if (len(snapshots) != 1 or not snapshots[0][0]
            or instant(snapshots[0][2]).astimezone(TZ).date().isoformat() != audit_date):
        raise ValueError("risk snapshot missing/ambiguous")
    snapshot_id, risk_level, _ = snapshots[0]
    rows = connection.execute(
        "SELECT " + ",".join(COLUMNS) + " FROM eod_target_decision WHERE run_id=? LIMIT ?",
        (run_id, MAX_TARGETS + 1),
    ).fetchall()
    result = {}
    for raw in rows:
        row = dict(zip(COLUMNS, raw))
        if (row["code"] not in codes or row["code"] in result
                or row["snapshot_id"] != snapshot_id or row["risk_level"] != risk_level):
            raise ValueError("unexpected/duplicate decision or unbound risk")
        result[row["code"]] = row
    return result


def _apply_audit(pool: dict, connection: sqlite3.Connection, audit: dict, now: datetime) -> None:
    run = audit["run"]
    coverage = audit["coverage"]
    audit_date = day(audit["decision_date"]).isoformat()
    if (not isinstance(run["run_id"], str) or not run["run_id"]
            or type(coverage["target_count"]) is not int or coverage["target_count"] != pool["configured_count"]
            or run.get("decision_eligible") is not True
            or audit_date > now.astimezone(TZ).date().isoformat()):
        raise ValueError("invalid audit/universe drift")
    pool.update({"run_id": run["run_id"], "decision_date": audit_date,
                 "run_status": run["status"], "observed_at": run.get("completed_at")})
    if run["status"] == "running":
        pool["items"] = [_empty_item(item, "running", "eod_running") for item in pool["items"]]
        pool["reason_code"] = "eod_running"
        return
    completed = instant(run["completed_at"])
    if (run["status"] not in {"success", "partial", "failed"}
            or completed.timestamp() > now.timestamp() + 300
            or completed.astimezone(TZ).date().isoformat() != audit_date):
        raise ValueError("invalid terminal timestamp/status")
    by_code = _rows(connection, run["run_id"], {t["code"] for t in pool["items"]}, audit_date)
    items = []
    for target in pool["items"]:
        try:
            item = (_decision(by_code[target["code"]], target, audit_date, pool["expected_date"])
                    if target["code"] in by_code else _empty_item(target))
        except (KeyError, ValueError, TypeError, OverflowError):
            item = _empty_item(target, "unknown", "eod_evidence_invalid")
        items.append(item)
    pool["items"] = items
    counts = Counter(item["decision_state"] for item in items)
    evaluated = sum(counts[state] for state in ("no_trigger", "allowed", "blocked"))
    if (len(by_code) == pool["configured_count"] and None not in counts
            and (evaluated != coverage["evaluated_count"] or counts["data_failed"] != coverage["failed_count"])):
        raise ValueError("audit coverage contradicts decisions")
    decision = audit.get("decision")
    if isinstance(decision, dict) and len(by_code) == pool["configured_count"] and None not in counts:
        # "allowed" means AT LEAST one signal survived, not that every raw
        # signal for this ETF passed. Mixed per-signal gating is legitimate.
        potential_allowed = sum(item["raw_trigger_count"] for item in items if item["decision_state"] == "allowed")
        fully_blocked = sum(item["raw_trigger_count"] for item in items if item["decision_state"] == "blocked")
        allowed = decision.get("allowed_signal_count")
        blocked = decision.get("blocked_signal_count")
        if (type(allowed) is not int or type(blocked) is not int
                or decision.get("signal_count") != potential_allowed + fully_blocked
                or allowed + blocked != potential_allowed + fully_blocked
                or not counts["allowed"] <= allowed <= potential_allowed or blocked < fully_blocked):
            raise ValueError("audit signal counts contradict decisions")
    pool["reason_code"] = "" if all(item["is_current"] for item in items) else "etf_evidence_incomplete"
    if run["status"] == "success" and not pool["reason_code"]:
        pool["status"] = "ok"


def load_etf_pool(config: Any, connection: sqlite3.Connection | None, audit: dict | None,
                  *, expected_date: str | None, now: datetime) -> dict:
    pool = {"schema_version": "quant.etf-pool.v1", "status": "degraded",
            "source": "configured_etf_universe+eod_target_decision", "configured_count": None,
            "expected_date": expected_date, "calendar_available": expected_date is not None,
            "decision_date": None, "observed_at": None, "run_id": None, "run_status": "missing",
            "reason_code": "eod_evidence_missing", "items": [], "counts": {state: 0 for state in STATES}}
    try:
        targets = _targets(config)
    except (ValueError, TypeError):
        pool.update(status="unavailable", reason_code="etf_universe_invalid")
        return pool
    pool["configured_count"] = len(targets)
    pool["items"] = [_empty_item(target) for target in targets]
    if connection is not None and isinstance(audit, dict):
        try:
            _apply_audit(pool, connection, audit, now)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            pool["reason_code"] = "eod_evidence_invalid"
            pool["items"] = [_empty_item(target, "unknown", "eod_evidence_invalid") for target in targets]
    if expected_date is None:
        pool["reason_code"] = "market_calendar_unavailable"
    # Extra evidence is orthogonal: it never upgrades today's currentness,
    # decision state, risk gate, or overall health based on a historical trigger.
    for item in pool["items"]:
        item["trigger_diagnostics"] = _diagnostic_empty()
        item["history"] = _history_empty()
    if connection is not None:
        _load_diagnostics(pool, connection)
        try:
            _load_history(pool, connection, now, config)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            pass
    counts = Counter(item["state"] for item in pool["items"])
    pool["counts"] = {state: counts[state] for state in STATES}
    return pool
