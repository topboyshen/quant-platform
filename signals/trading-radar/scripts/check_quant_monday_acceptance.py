#!/usr/bin/env python3
"""Generate a read-only post-close acceptance report for Quant production.

The checker never sends Telegram messages, mutates SQLite, or changes runtime
services.  Its only writes are the JSON and Markdown reports under the selected
output directory.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUIRED_RISK_FIELDS = ("vix", "us_10y", "crude_oil")
VALID_RISK_LEVELS = {"ok", "warning", "critical", "growth_blocked"}
VALID_OUTCOMES = {"emitted", "no_signal", "blocked"}
VALID_DECISION_STATES = {"no_trigger", "allowed", "blocked", "data_failed"}
VALID_TOTAL_AMOUNT_SOURCES = {"tencent", "sina", "akshare", "eastmoney"}
TELEGRAM_PROBES = {
    "telegram_http": "302",
    "telegram_socks": "302",
    "gstatic_http": "204",
    "gstatic_socks": "204",
}
TELEGRAM_ENV_KEYS = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


def _check(
    name: str,
    passed: bool,
    summary: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "summary": summary,
        "evidence": evidence or {},
    }


def _not_ready(name: str, summary: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "status": "not_ready",
        "summary": summary,
        "evidence": evidence,
    }


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_iso_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # SQLite datetime('now') and all application-created audit rows are UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    allowed = {
        "macro_slice",
        "swing_eod_run",
        "eod_target_decision",
        "eod_risk_snapshot",
        "notification_outbox",
        "notification_dispatch",
    }
    if table not in allowed:
        return frozenset()
    return frozenset(str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall())


def _open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def inspect_eod(
    connection: sqlite3.Connection,
    *,
    decision_date: str,
    decision_time: clock_time,
) -> tuple[dict[str, Any], sqlite3.Row | None]:
    required = {
        "id",
        "run_id",
        "decision_date",
        "data_date",
        "status",
        "started_at",
        "completed_at",
        "target_count",
        "evaluated_count",
        "failed_count",
        "signal_count",
        "allowed_signal_count",
        "blocked_signal_count",
        "decision_outcome",
        "reason_codes",
    }
    columns = _table_columns(connection, "swing_eod_run")
    if not required.issubset(columns):
        return _check(
            "eod",
            False,
            "swing_eod_run schema is incomplete",
            {"missing_columns": sorted(required - columns)},
        ), None

    rows = connection.execute(
        "SELECT id,run_id,decision_date,data_date,status,started_at,completed_at,"
        "target_count,evaluated_count,failed_count,signal_count,"
        "allowed_signal_count,blocked_signal_count,decision_outcome,reason_codes "
        "FROM swing_eod_run WHERE decision_date=? ORDER BY id DESC LIMIT 1",
        (decision_date,),
    ).fetchall()
    if not rows:
        return _check(
            "eod",
            False,
            f"no EOD run exists for {decision_date}",
            {"decision_date": decision_date},
        ), None
    row = rows[0]

    started_at = _parse_iso_timestamp(row["started_at"])
    completed_at = _parse_iso_timestamp(row["completed_at"])
    expected_start = datetime.combine(date.fromisoformat(decision_date), decision_time, tzinfo=SHANGHAI).astimezone(
        timezone.utc
    )
    target_count = int(row["target_count"] or 0)
    evaluated_count = int(row["evaluated_count"] or 0)
    failed_count = int(row["failed_count"] or 0)
    signal_count = int(row["signal_count"] or 0)
    allowed_count = int(row["allowed_signal_count"] or 0)
    blocked_count = int(row["blocked_signal_count"] or 0)
    outcome = str(row["decision_outcome"] or "")
    reasons: list[str]
    reasons_valid = True
    try:
        decoded = json.loads(row["reason_codes"] or "[]")
        reasons_valid = isinstance(decoded, list)
        reasons = decoded if reasons_valid else []
    except (TypeError, ValueError):
        reasons_valid = False
        reasons = []

    problems = []
    run_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM swing_eod_run WHERE decision_date=?",
            (decision_date,),
        ).fetchone()[0]
    )
    if run_count != 1:
        problems.append(f"run_count={run_count}, expected=1")
    if str(row["status"]) != "success":
        problems.append(f"status={row['status']}")
    if str(row["data_date"] or "") != decision_date:
        problems.append(f"data_date={row['data_date'] or 'missing'}")
    if target_count <= 0:
        problems.append("target_count is not positive")
    if evaluated_count != target_count or failed_count != 0:
        problems.append(f"coverage={evaluated_count}/{target_count}, failed={failed_count}")
    if outcome not in VALID_OUTCOMES:
        problems.append(f"decision_outcome={outcome or 'missing'}")
    if reasons:
        problems.append(f"reason_codes={','.join(map(str, reasons))}")
    if not reasons_valid:
        problems.append("reason_codes is not a JSON list")
    if signal_count != allowed_count + blocked_count:
        problems.append("signal counters are inconsistent")
    if outcome == "emitted" and (signal_count <= 0 or allowed_count <= 0):
        problems.append("emitted outcome has no allowed signal")
    if outcome == "no_signal" and signal_count != 0:
        problems.append("no_signal outcome contains signals")
    if outcome == "blocked" and (signal_count <= 0 or allowed_count != 0):
        problems.append("blocked outcome is inconsistent")
    started_local = started_at.astimezone(SHANGHAI) if started_at is not None else None
    if (
        started_at is None
        or started_at < expected_start
        or started_local is None
        or started_local.date().isoformat() != decision_date
    ):
        problems.append("run did not start in the formal 15:30 window")
    if completed_at is None or (started_at is not None and completed_at < started_at):
        problems.append("completion timestamp is missing or invalid")

    evidence = {
        "run_id": str(row["run_id"]),
        "run_count": run_count,
        "decision_date": str(row["decision_date"]),
        "data_date": row["data_date"],
        "status": str(row["status"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "target_count": target_count,
        "evaluated_count": evaluated_count,
        "failed_count": failed_count,
        "signal_count": signal_count,
        "allowed_signal_count": allowed_count,
        "blocked_signal_count": blocked_count,
        "decision_outcome": outcome,
        "reason_codes": reasons,
        "reason_codes_valid": reasons_valid,
    }
    return _check(
        "eod",
        not problems,
        "15:30 EOD reached a complete success terminal state" if not problems else "; ".join(problems),
        evidence,
    ), row


def inspect_audit(connection: sqlite3.Connection, *, run: sqlite3.Row | None) -> dict[str, Any]:
    if run is None:
        return _check("audit", False, "EOD run is unavailable", {})
    required_tables = {
        "eod_target_decision": {
            "run_id",
            "snapshot_id",
            "data_status",
            "decision_state",
            "risk_action",
            "risk_level",
        },
        "eod_risk_snapshot": {
            "snapshot_id",
            "run_id",
            "decision_at",
            "level",
            "values_json",
            "source_timestamps_json",
            "available_at_json",
            "sources_json",
            "missing_fields_json",
        },
    }
    missing: dict[str, list[str]] = {}
    for table, required in required_tables.items():
        absent = required - _table_columns(connection, table)
        if absent:
            missing[table] = sorted(absent)
    if missing:
        return _check("audit", False, "immutable audit schema is incomplete", {"missing": missing})

    run_id = str(run["run_id"])
    target_count = int(run["target_count"] or 0)
    rows = connection.execute(
        "SELECT decision_state,COUNT(*) AS count FROM eod_target_decision WHERE run_id=? GROUP BY decision_state",
        (run_id,),
    ).fetchall()
    states = {str(row["decision_state"]): int(row["count"]) for row in rows}
    decision_count = sum(states.values())
    target_summary = connection.execute(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN data_status='ok' THEN 1 ELSE 0 END) AS data_ok,"
        "COUNT(DISTINCT snapshot_id) AS snapshot_ids,"
        "SUM(CASE WHEN snapshot_id IS NULL OR snapshot_id='' THEN 1 ELSE 0 END) "
        "AS missing_snapshot FROM eod_target_decision WHERE run_id=?",
        (run_id,),
    ).fetchone()
    target_snapshot_row = connection.execute(
        "SELECT snapshot_id FROM eod_target_decision WHERE run_id=? "
        "AND snapshot_id IS NOT NULL AND snapshot_id<>'' LIMIT 1",
        (run_id,),
    ).fetchone()
    target_snapshot_id = str(target_snapshot_row["snapshot_id"]) if target_snapshot_row is not None else ""
    risk_rows = connection.execute(
        "SELECT snapshot_id,decision_at,level,values_json,source_timestamps_json,"
        "available_at_json,sources_json,missing_fields_json FROM eod_risk_snapshot "
        "WHERE run_id=?",
        (run_id,),
    ).fetchall()
    risk_count = len(risk_rows)
    invalid_states = sorted(set(states) - VALID_DECISION_STATES)
    data_failed = states.get("data_failed", 0)
    problems = []
    if decision_count != target_count:
        problems.append(f"target evidence={decision_count}/{target_count}")
    if risk_count != 1:
        problems.append(f"risk snapshots={risk_count}/1")
    if invalid_states:
        problems.append(f"invalid decision states={','.join(invalid_states)}")
    if data_failed:
        problems.append(f"data_failed={data_failed}")
    data_ok = int(target_summary["data_ok"] or 0)
    snapshot_ids = int(target_summary["snapshot_ids"] or 0)
    missing_snapshot = int(target_summary["missing_snapshot"] or 0)
    if data_ok != decision_count:
        problems.append(f"target data_status ok={data_ok}/{decision_count}")
    if snapshot_ids != 1 or missing_snapshot:
        problems.append(f"target snapshot links distinct={snapshot_ids}, missing={missing_snapshot}")
    snapshot_contract: dict[str, Any] = {}
    if risk_count == 1:
        risk_row = risk_rows[0]
        decoded: dict[str, Any] = {}
        for key in (
            "values_json",
            "source_timestamps_json",
            "available_at_json",
            "sources_json",
            "missing_fields_json",
        ):
            try:
                decoded[key] = json.loads(risk_row[key])
            except (TypeError, ValueError):
                decoded[key] = None
        values = decoded["values_json"] if isinstance(decoded["values_json"], dict) else {}
        source_timestamps = (
            decoded["source_timestamps_json"] if isinstance(decoded["source_timestamps_json"], dict) else {}
        )
        available_at = decoded["available_at_json"] if isinstance(decoded["available_at_json"], dict) else {}
        sources = decoded["sources_json"] if isinstance(decoded["sources_json"], dict) else {}
        missing_fields = decoded["missing_fields_json"]
        snapshot_contract = {
            "snapshot_id_present": bool(str(risk_row["snapshot_id"] or "")),
            "decision_at_present": _parse_iso_timestamp(risk_row["decision_at"]) is not None,
            "level": risk_row["level"],
            "missing_fields": missing_fields,
            "values_complete": all(_finite_positive(values.get(field)) is not None for field in REQUIRED_RISK_FIELDS),
            "source_timestamps_complete": all(
                _finite_positive(source_timestamps.get(field)) is not None for field in REQUIRED_RISK_FIELDS
            ),
            "available_at_complete": all(bool(available_at.get(field)) for field in REQUIRED_RISK_FIELDS),
            "sources_complete": all(bool(sources.get(field)) for field in REQUIRED_RISK_FIELDS),
        }
        if not all(
            (
                snapshot_contract["snapshot_id_present"],
                snapshot_contract["decision_at_present"],
                snapshot_contract["values_complete"],
                snapshot_contract["source_timestamps_complete"],
                snapshot_contract["available_at_complete"],
                snapshot_contract["sources_complete"],
            )
        ):
            problems.append("risk snapshot structure is incomplete")
        if missing_fields != []:
            problems.append("risk snapshot missing_fields is not empty")
        if str(risk_row["level"] or "") not in VALID_RISK_LEVELS:
            problems.append("risk snapshot level is invalid")
        if target_snapshot_id != str(risk_row["snapshot_id"] or ""):
            problems.append("target decisions reference a different risk snapshot")
    foreign_key_issues = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if foreign_key_issues:
        problems.append(f"foreign_key_check={len(foreign_key_issues)}")
    return _check(
        "audit",
        not problems,
        "immutable target and risk evidence is complete" if not problems else "; ".join(problems),
        {
            "run_id": run_id,
            "target_decisions": decision_count,
            "target_count": target_count,
            "decision_states": states,
            "target_data_ok": data_ok,
            "target_snapshot_ids": snapshot_ids,
            "target_missing_snapshot_links": missing_snapshot,
            "target_snapshot_matches_run_snapshot": risk_count == 1
            and target_snapshot_id == str(risk_rows[0]["snapshot_id"] or ""),
            "risk_snapshots": risk_count,
            "risk_snapshot_contract": snapshot_contract,
            "foreign_key_issues": len(foreign_key_issues),
        },
    )


def inspect_total_amount(connection: sqlite3.Connection, *, decision_date: str) -> dict[str, Any]:
    required = {"date", "total_amount", "source", "extra"}
    columns = _table_columns(connection, "macro_slice")
    if not required.issubset(columns):
        return _check(
            "total_amount",
            False,
            "macro_slice schema is incomplete",
            {"missing_columns": sorted(required - columns)},
        )
    row = connection.execute(
        "SELECT date,total_amount,source,extra FROM macro_slice WHERE date=? LIMIT 1",
        (decision_date,),
    ).fetchone()
    if row is None:
        return _check(
            "total_amount",
            False,
            f"no macro slice exists for {decision_date}",
            {"decision_date": decision_date},
        )
    try:
        extra = json.loads(row["extra"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    extra = extra if isinstance(extra, dict) else {}
    collect = extra.get("collect_status", {})
    collect = collect if isinstance(collect, dict) else {}
    status = collect.get("total_amount", {})
    status = status if isinstance(status, dict) else {}
    value = _finite_positive(row["total_amount"])
    source = str(status.get("source") or row["source"] or "").strip()
    observed_date = str(status.get("observed_date") or "")
    problems = []
    if value is None or not 1_000 <= value <= 80_000:
        problems.append("value is missing or invalid")
    if status.get("available") is not True:
        problems.append("collector did not mark total_amount available")
    if observed_date != decision_date:
        problems.append(f"observed_date={observed_date or 'missing'}")
    if status.get("stale") is not False:
        problems.append("collector marked value stale or omitted stale=false")
    if status.get("anomalous") is not False:
        problems.append("collector marked value anomalous or omitted anomalous=false")
    if not source or source.lower() == "none":
        problems.append("source is missing")
    elif source not in VALID_TOTAL_AMOUNT_SOURCES:
        problems.append(f"source={source} is not an approved collector")
    row_source_tokens = {item for item in str(row["source"] or "").split("+") if item and item != "none"}
    if source and source not in row_source_tokens:
        problems.append("collector source is absent from macro_slice.source")
    return _check(
        "total_amount",
        not problems,
        f"成交额 {value:.2f} 亿元，日期和来源可信" if not problems and value is not None else "; ".join(problems),
        {
            "slice_date": str(row["date"]),
            "observed_date": observed_date or None,
            "value_yi": value,
            "source": source or None,
            "slice_source": row["source"],
            "available": status.get("available"),
            "stale": status.get("stale"),
            "anomalous": status.get("anomalous"),
        },
    )


_DISPATCH_PROBLEM_STATES = (
    "uncertain",
    "failed_exhausted",
    "failed_permanent",
    "rate_limited_over_budget",
    "quarantined_legacy",
)


def _inspect_dispatch(connection: sqlite3.Connection) -> tuple[list[str], dict[str, Any]]:
    """v6 dispatch queue: anything not confirmed (and not human-reviewed) is a problem.

    ``uncertain`` / exhausted / over-budget / quarantined / conflicting rows, expired
    ``sending`` leases and stuck ``retry_wait`` rows can never read as healthy here.
    """
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    columns = _table_columns(connection, "notification_dispatch")
    if not columns:
        if version >= 6:
            return ["dispatch_tables_missing_at_v6"], {"schema_version": version, "status": "missing"}
        return [], {"schema_version": version, "status": "not_migrated"}
    if version != 6:
        return [f"dispatch_tables_present_at_v{version}"], {"schema_version": version, "status": "inconsistent"}
    needed = {"state", "reviewed_at", "conflict_count", "lease_expires_at", "next_attempt_at",
              "budget_deadline_at", "attempts_used"}
    if not needed.issubset(columns):
        return ["dispatch_schema_incomplete"], {"schema_version": version, "missing_columns": sorted(needed - columns)}
    states: dict[str, int] = {}
    for row in connection.execute(
        "SELECT state,COUNT(*) AS count FROM notification_dispatch GROUP BY state"
    ).fetchall():
        states[str(row["state"])] = int(row["count"])
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    problems: list[str] = []

    def scalar(sql: str, params: tuple = ()) -> int:
        return int(connection.execute(sql, params).fetchone()[0])

    for state in _DISPATCH_PROBLEM_STATES:
        n = scalar(
            "SELECT COUNT(*) FROM notification_dispatch WHERE state=? AND reviewed_at IS NULL", (state,)
        )
        if n:
            problems.append(f"dispatch_{state}={n}")
    in_flight = states.get("sending", 0) - scalar(
        "SELECT COUNT(*) FROM notification_dispatch WHERE state='sending' AND "
        "(lease_expires_at IS NULL OR lease_expires_at<?)", (now,)
    )
    expired = states.get("sending", 0) - in_flight
    if expired:
        problems.append(f"dispatch_sending_lease_expired={expired}")
    if in_flight:
        problems.append(f"dispatch_sending_in_flight={in_flight}")
    for state in ("pending", "retry_wait"):
        if states.get(state, 0):
            problems.append(f"dispatch_{state}={states[state]}")
    conflicts = scalar(
        "SELECT COUNT(*) FROM notification_dispatch WHERE conflict_count>0 AND reviewed_at IS NULL"
    )
    if conflicts:
        problems.append(f"dispatch_conflicts={conflicts}")
    return problems, {
        "schema_version": version,
        "status": "ok" if not problems else "needs_review",
        "states": states,
        "conflicts_unreviewed": conflicts,
        "sending_expired": expired,
    }


def inspect_outbox(connection: sqlite3.Connection, *, decision_date: str) -> dict[str, Any]:
    required = {
        "status",
        "attempts",
        "created_at",
        "attempted_at",
        "sent_at",
        "last_error",
    }
    columns = _table_columns(connection, "notification_outbox")
    if not required.issubset(columns):
        return _check(
            "outbox",
            False,
            "notification_outbox schema is incomplete",
            {"missing_columns": sorted(required - columns)},
        )
    counts = {status: 0 for status in ("pending", "sending", "sent", "failed")}
    for row in connection.execute(
        "SELECT status,COUNT(*) AS count FROM notification_outbox GROUP BY status"
    ).fetchall():
        counts[str(row["status"])] = int(row["count"])

    cutoff = datetime.combine(date.fromisoformat(decision_date), clock_time(0, 0), tzinfo=SHANGHAI).astimezone(
        timezone.utc
    )
    recent_failed = []
    unknown_failed = []
    for row in connection.execute(
        "SELECT id,created_at,attempted_at,last_error FROM notification_outbox WHERE status='failed' ORDER BY id"
    ).fetchall():
        attempted = _parse_iso_timestamp(row["attempted_at"])
        created = _parse_iso_timestamp(row["created_at"])
        observed = attempted or created
        if observed is None:
            unknown_failed.append(int(row["id"]))
        elif observed >= cutoff:
            recent_failed.append(
                {
                    "id": int(row["id"]),
                    "created_at": row["created_at"],
                    "attempted_at": row["attempted_at"],
                    "error_present": bool(str(row["last_error"] or "").strip()),
                }
            )
    invalid_sent = int(
        connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status='sent' "
            "AND (sent_at IS NULL OR sent_at='' OR attempts < 1)"
        ).fetchone()[0]
    )
    dispatch_problems, dispatch_details = _inspect_dispatch(connection)
    problems = list(dispatch_problems)
    if counts["pending"]:
        problems.append(f"pending={counts['pending']}")
    if counts["sending"]:
        problems.append(f"sending={counts['sending']}")
    if recent_failed:
        problems.append(f"new_failed={len(recent_failed)}")
    if unknown_failed:
        problems.append(f"failed_with_invalid_timestamp={len(unknown_failed)}")
    if invalid_sent:
        problems.append(f"invalid_sent={invalid_sent}")
    return _check(
        "outbox",
        not problems,
        "no pending, sending, or new failed notifications" if not problems else "; ".join(problems),
        {
            "counts": counts,
            "new_failed_since_local_midnight": recent_failed,
            "failed_with_invalid_timestamp_ids": unknown_failed,
            "invalid_sent": invalid_sent,
            "historical_failed_allowed": counts["failed"] - len(recent_failed) - len(unknown_failed),
            "dispatch": dispatch_details,
        },
    )


def inspect_macro_risk(path: Path, *, now: datetime, max_age_seconds: int) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        return _check(
            "macro_risk",
            False,
            f"risk state cannot be read: {type(exc).__name__}",
            {"path": str(path)},
        )
    if not isinstance(payload, dict):
        return _check("macro_risk", False, "risk state is not a JSON object", {"path": str(path)})
    try:
        timestamp = float(payload.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0.0
    age = now.astimezone(timezone.utc).timestamp() - timestamp
    missing = payload.get("missing_fields")
    cached = payload.get("cached_fields")
    sources = payload.get("sources")
    missing = missing if isinstance(missing, list) else None
    cached = cached if isinstance(cached, list) else None
    sources = sources if isinstance(sources, dict) else {}
    values = {field: _finite_positive(payload.get(field)) for field in REQUIRED_RISK_FIELDS}
    problems = []
    if str(payload.get("level") or "") not in VALID_RISK_LEVELS:
        problems.append("level is invalid")
    if timestamp <= 0 or not math.isfinite(timestamp):
        problems.append("timestamp is invalid")
    elif age < -5:
        problems.append(f"timestamp is {abs(age):.1f}s in the future")
    elif age > max_age_seconds:
        problems.append(f"state age={age:.1f}s exceeds {max_age_seconds}s")
    if missing is None:
        problems.append("missing_fields is not a list")
    elif missing:
        problems.append(f"missing_fields={','.join(map(str, missing))}")
    if cached is None:
        problems.append("cached_fields is not a list")
    invalid_values = [field for field, value in values.items() if value is None]
    if invalid_values:
        problems.append(f"invalid values={','.join(invalid_values)}")
    missing_sources = [field for field in REQUIRED_RISK_FIELDS if not sources.get(field)]
    if missing_sources:
        problems.append(f"missing sources={','.join(missing_sources)}")
    return _check(
        "macro_risk",
        not problems,
        (
            "macro-risk state is fresh and complete"
            + (f"; cached within contract: {','.join(map(str, cached))}" if cached else "")
        )
        if not problems
        else "; ".join(problems),
        {
            "path": str(path),
            "level": payload.get("level"),
            "timestamp": timestamp or None,
            "age_seconds": round(max(0.0, age), 1) if timestamp > 0 else None,
            "max_age_seconds": max_age_seconds,
            "missing_fields": missing,
            "cached_fields": cached,
            "values": values,
            "sources": {field: sources.get(field) for field in REQUIRED_RISK_FIELDS},
        },
    )


def inspect_macro_risk_runtime(container: str, *, timeout_seconds: int = 10) -> dict[str, Any]:
    template = (
        '{"status":{{json .State.Status}},'
        '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
        '"restart_count":{{.RestartCount}},"oom_killed":{{.State.OOMKilled}}}'
    )
    try:
        completed = subprocess.run(
            ["docker", "inspect", "--format", template, container],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(
            "macro_risk_runtime",
            False,
            f"container inspection failed: {type(exc).__name__}",
            {"container": container},
        )
    try:
        state = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except ValueError:
        state = {}
    state = state if isinstance(state, dict) else {}
    problems = []
    if completed.returncode != 0:
        problems.append(f"docker inspect exit={completed.returncode}")
    if state.get("status") != "running":
        problems.append(f"status={state.get('status') or 'unknown'}")
    if state.get("health") != "healthy":
        problems.append(f"health={state.get('health') or 'unknown'}")
    if state.get("restart_count") != 0:
        problems.append(f"restart_count={state.get('restart_count')}")
    if state.get("oom_killed") is not False:
        problems.append(f"oom_killed={state.get('oom_killed')}")
    return _check(
        "macro_risk_runtime",
        not problems,
        "macro-risk producer is healthy with no restart or OOM" if not problems else "; ".join(problems),
        {
            "container": container,
            "status": state.get("status"),
            "health": state.get("health"),
            "restart_count": state.get("restart_count"),
            "oom_killed": state.get("oom_killed"),
        },
    )


def _load_telegram_credentials(path: Path) -> tuple[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Telegram environment is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise ValueError("Telegram environment owner does not match the current user")
        if metadata.st_mode & 0o077:
            raise ValueError("Telegram environment permissions are broader than 0600")
        if metadata.st_size > 4096:
            raise ValueError("Telegram environment exceeds 4096 bytes")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            content = handle.read(4097)
    finally:
        os.close(descriptor)
    if len(content) > 4096:
        raise ValueError("Telegram environment exceeds 4096 bytes")

    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError("Telegram environment contains a malformed line")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in TELEGRAM_ENV_KEYS:
            continue
        if key in values:
            raise ValueError(f"Telegram environment repeats {key}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value.strip()

    token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = values.get("TELEGRAM_CHAT_ID", "")
    if not token or any(character.isspace() for character in token) or ":" not in token:
        raise ValueError("Telegram bot token is missing or malformed")
    if not chat_id or not chat_id.lstrip("-").isdigit():
        raise ValueError("Telegram chat id is missing or malformed")
    return token, chat_id


def _telegram_api_get(
    *,
    token: str,
    method: str,
    chat_id: str,
    proxy: str,
    timeout_seconds: int,
) -> tuple[bool, int | None, float]:
    query = urlencode({"chat_id": chat_id}) if method == "getChat" else ""
    suffix = f"?{query}" if query else ""
    url = f"https://api.telegram.org/bot{token}/{method}{suffix}"
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    request = Request(url, headers={"User-Agent": "quant-readonly-acceptance/1"}, method="GET")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw = response.read(65_537)
        if len(raw) > 65_536:
            return False, status, (time.monotonic() - started) * 1000
        payload = json.loads(raw.decode("utf-8"))
        result = payload.get("result") if isinstance(payload, dict) else None
        passed = status == 200 and isinstance(payload, dict) and payload.get("ok") is True and isinstance(result, dict)
        if method == "getChat":
            passed = passed and str(result.get("id")) == chat_id
        elif method == "getMe":
            passed = passed and _finite_positive(result.get("id")) is not None
        return passed, status, (time.monotonic() - started) * 1000
    except HTTPError as exc:
        return False, int(exc.code), (time.monotonic() - started) * 1000
    except (OSError, URLError, UnicodeError, ValueError, json.JSONDecodeError):
        return False, None, (time.monotonic() - started) * 1000


def inspect_telegram(
    checker: Path,
    *,
    environment_file: Path,
    proxy: str,
    timeout_seconds: int,
    api_timeout_seconds: int,
) -> dict[str, Any]:
    if not checker.is_file():
        return _check(
            "telegram",
            False,
            "anonymous Telegram egress checker is missing",
            {"path": str(checker)},
        )
    environment = os.environ.copy()
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_BOT_TOKEN_FILE",
        "RADAR_TELEGRAM_BOT_TOKEN",
        "RADAR_TELEGRAM_CHAT_ID",
    ):
        environment.pop(key, None)
    try:
        completed = subprocess.run(
            [str(checker)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(
            "telegram",
            False,
            f"anonymous egress checker failed: {type(exc).__name__}",
            {"path": str(checker)},
        )
    probes: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if not fields or fields[0] not in TELEGRAM_PROBES:
            continue
        parsed: dict[str, str] = {}
        for item in fields[1:]:
            if "=" in item:
                key, value = item.split("=", 1)
                parsed[key] = value
        probes[fields[0]] = parsed
    problems = []
    for label, expected in TELEGRAM_PROBES.items():
        probe = probes.get(label, {})
        if probe.get("code") != expected or probe.get("expected") != expected or probe.get("curl_status") != "0":
            problems.append(label)
    if completed.returncode != 0:
        problems.append(f"exit={completed.returncode}")
    if "mihomo_egress_status=ok" not in completed.stdout.splitlines():
        problems.append("missing ok marker")
    try:
        token, chat_id = _load_telegram_credentials(environment_file)
    except (OSError, UnicodeError, ValueError) as exc:
        problems.append(f"credential contract={type(exc).__name__}")
        token = ""
        chat_id = ""

    api_probes: dict[str, dict[str, Any]] = {}
    if token and chat_id:
        for method in ("getMe", "getChat"):
            attempts = [
                _telegram_api_get(
                    token=token,
                    method=method,
                    chat_id=chat_id,
                    proxy=proxy,
                    timeout_seconds=api_timeout_seconds,
                )
                for _attempt in range(3)
            ]
            successes = sum(1 for passed, _status, _elapsed in attempts if passed)
            api_probes[method] = {
                "successes": successes,
                "attempts": 3,
                "http_statuses": [status for _passed, status, _elapsed in attempts],
                "latency_ms": [round(elapsed, 1) for _passed, _status, elapsed in attempts],
            }
            if successes < 2:
                problems.append(f"{method} successes={successes}/3")

    warning = any(item.get("successes") == 2 for item in api_probes.values())
    return _check(
        "telegram",
        not problems,
        (
            "anonymous egress and read-only Bot API identity checks passed"
            + (" with an isolated retry" if warning else "")
        )
        if not problems
        else f"failed probes: {', '.join(problems)}",
        {
            "checker": str(checker),
            "exit_code": completed.returncode,
            "probes": probes,
            "bot_api": api_probes,
            "stderr_present": bool(completed.stderr.strip()),
            "credential_source": str(environment_file),
            "credentials_redacted": True,
            "http_method": "GET",
            "message_sent": False,
        },
    )


def _render_markdown(report: dict[str, Any]) -> str:
    status_label = {
        "pass": "PASS",
        "fail": "FAIL",
        "not_ready": "NOT READY",
    }[report["status"]]
    lines = [
        f"# Quant post-close acceptance — {report['decision_date']}",
        "",
        f"- Result: **{status_label}**",
        f"- Generated: `{report['generated_at']}`",
        f"- Database: `{report['inputs']['database']}`",
        f"- Risk state: `{report['inputs']['risk_state']}`",
        "- Contract: read-only; no Telegram message, database write, restart, or deployment",
        "",
        "## Checks",
        "",
        "| Check | Result | Summary |",
        "|---|---:|---|",
    ]
    for item in report["checks"]:
        summary = str(item["summary"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{item['name']}` | **{item['status'].upper()}** | {summary} |")
    lines.extend(["", "## Reproducible evidence", "", "```json"])
    lines.append(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    lines.extend(["```", ""])
    return "\n".join(lines)


def write_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace("-", "").replace(":", "")[:15]
    base = output_dir / f"quant-acceptance-{report['decision_date'].replace('-', '')}-{stamp}"
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def run_acceptance(
    *,
    database: Path,
    risk_state: Path,
    telegram_checker: Path,
    telegram_environment: Path,
    telegram_proxy: str,
    macro_risk_container: str,
    decision_date: str,
    now: datetime,
    decision_time: clock_time = clock_time(15, 30),
    grace_minutes: int = 22,
    risk_max_age_seconds: int = 900,
    telegram_timeout_seconds: int = 45,
    telegram_api_timeout_seconds: int = 8,
    macro_runtime_check: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = now.astimezone(SHANGHAI)
    expected_date = date.fromisoformat(decision_date)
    ready_at = datetime.combine(expected_date, decision_time, tzinfo=SHANGHAI)
    ready_at += timedelta(minutes=grace_minutes)
    generated_at = now.isoformat(timespec="seconds")
    inputs = {
        "database": str(database),
        "risk_state": str(risk_state),
        "telegram_checker": str(telegram_checker),
        "telegram_environment": str(telegram_environment),
        "telegram_proxy": telegram_proxy,
        "macro_risk_container": macro_risk_container,
    }
    if now < ready_at:
        timing = _not_ready(
            "timing",
            f"acceptance window opens at {ready_at.isoformat(timespec='minutes')}",
            {"now": generated_at, "ready_at": ready_at.isoformat()},
        )
        return {
            "schema_version": "quant.post-close-acceptance.v1",
            "status": "not_ready",
            "decision_date": decision_date,
            "generated_at": generated_at,
            "inputs": inputs,
            "checks": [timing],
        }

    checks: list[dict[str, Any]] = [
        _check(
            "timing",
            True,
            "formal post-close acceptance window is open",
            {"now": generated_at, "ready_at": ready_at.isoformat()},
        )
    ]
    try:
        with closing(_open_readonly_sqlite(database)) as connection:
            connection.execute("BEGIN")
            eod, run = inspect_eod(
                connection,
                decision_date=decision_date,
                decision_time=decision_time,
            )
            checks.extend(
                [
                    eod,
                    inspect_total_amount(connection, decision_date=decision_date),
                    inspect_audit(connection, run=run),
                    inspect_outbox(connection, decision_date=decision_date),
                ]
            )
    except (OSError, sqlite3.Error) as exc:
        checks.append(
            _check(
                "database",
                False,
                f"database cannot be opened read-only: {type(exc).__name__}",
                {"path": str(database)},
            )
        )
    checks.append(inspect_macro_risk(risk_state, now=now, max_age_seconds=risk_max_age_seconds))
    runtime_check = macro_runtime_check or inspect_macro_risk_runtime
    checks.append(runtime_check(macro_risk_container))
    checks.append(
        inspect_telegram(
            telegram_checker,
            environment_file=telegram_environment,
            proxy=telegram_proxy,
            timeout_seconds=telegram_timeout_seconds,
            api_timeout_seconds=telegram_api_timeout_seconds,
        )
    )
    overall = "pass" if checks and all(item["status"] == "pass" for item in checks) else "fail"
    return {
        "schema_version": "quant.post-close-acceptance.v1",
        "status": overall,
        "decision_date": decision_date,
        "generated_at": generated_at,
        "inputs": inputs,
        "checks": checks,
    }


def _parse_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the read-only Quant post-close acceptance gate")
    parser.add_argument(
        "--decision-date",
        type=_parse_date,
        default=datetime.now(SHANGHAI).date().isoformat(),
    )
    parser.add_argument("--database", type=Path, default=BASE_DIR / "data/swing_macro.db")
    parser.add_argument("--risk-state", type=Path, default=BASE_DIR / "data/radar_risk_state.json")
    parser.add_argument(
        "--telegram-checker",
        type=Path,
        default=BASE_DIR / "scripts/check_mihomo_egress.sh",
    )
    parser.add_argument(
        "--telegram-environment",
        type=Path,
        default=Path("/etc/systemd/system/stock-radar.env"),
    )
    parser.add_argument("--telegram-proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--grace-minutes", type=int, default=22)
    parser.add_argument("--risk-max-age-seconds", type=int, default=900)
    parser.add_argument("--telegram-timeout-seconds", type=int, default=45)
    parser.add_argument("--telegram-api-timeout-seconds", type=int, default=8)
    parser.add_argument("--macro-risk-container", default="radar-macro-risk")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "quant-acceptance-reports",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0 <= args.grace_minutes <= 60:
        raise SystemExit("--grace-minutes must be between 0 and 60")
    if not 60 <= args.risk_max_age_seconds <= 3600:
        raise SystemExit("--risk-max-age-seconds must be between 60 and 3600")
    if not 5 <= args.telegram_timeout_seconds <= 120:
        raise SystemExit("--telegram-timeout-seconds must be between 5 and 120")
    if not 2 <= args.telegram_api_timeout_seconds <= 30:
        raise SystemExit("--telegram-api-timeout-seconds must be between 2 and 30")
    if args.telegram_proxy not in {
        "http://127.0.0.1:7890",
        "http://localhost:7890",
    }:
        raise SystemExit("--telegram-proxy must use the local Mihomo HTTP listener")

    report = run_acceptance(
        database=args.database,
        risk_state=args.risk_state,
        telegram_checker=args.telegram_checker,
        telegram_environment=args.telegram_environment,
        telegram_proxy=args.telegram_proxy,
        macro_risk_container=args.macro_risk_container,
        decision_date=args.decision_date,
        now=datetime.now(SHANGHAI),
        grace_minutes=args.grace_minutes,
        risk_max_age_seconds=args.risk_max_age_seconds,
        telegram_timeout_seconds=args.telegram_timeout_seconds,
        telegram_api_timeout_seconds=args.telegram_api_timeout_seconds,
    )
    json_path, markdown_path = write_reports(report, args.output_dir)
    print(f"result={report['status']}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    return {"pass": 0, "fail": 1, "not_ready": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
