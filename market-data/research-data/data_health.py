#!/usr/bin/env python3
"""Read-only data coverage audit. Never imports collectors, locks, or sends messages."""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys

CST = dt.timezone(dt.timedelta(hours=8))
REFERENCES = ("sh600000", "sz000001", "sh600519", "sz300750")
FIELDS = ("close", "high", "low", "amount")
FAILURES = {"rejected", "deferred", "failed", "temporary_failure", "usage_error"}


class EvidenceError(ValueError):
    pass


def strict_json(raw):
    def reject(_):
        raise EvidenceError("non_finite_json")
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise EvidenceError("non_finite_json")
        return result
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject, parse_float=finite, object_pairs_hook=unique)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def iso_date(value):
    if not isinstance(value, str):
        raise EvidenceError("invalid_date_type")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise EvidenceError("invalid_date") from None
    if parsed.isoformat() != value:
        raise EvidenceError("noncanonical_date")
    return parsed


def timestamp(value):
    if not isinstance(value, str):
        raise EvidenceError("invalid_timestamp_type")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvidenceError("timestamp_without_timezone")
    return parsed.astimezone(CST)


def read_file(path, limit=4 * 1024 * 1024, private=False):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise EvidenceError("invalid_file_type_or_size")
        if private and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
            raise EvidenceError("invalid_stage_permissions")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise EvidenceError("file_too_large")
        return raw


def generation(root):
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError("qlib_root_invalid")
    tag = read_file(root.parent / ".tag", 128).decode().strip()
    embedded = read_file(root / ".qlib-release", 128).decode().strip()
    iso_date(tag)
    if tag != embedded:
        raise EvidenceError("qlib_tags_disagree")
    return (root.stat().st_ino, tag)


def qlib_evidence(root):
    before = generation(root)
    calendar = read_file(root / "calendars/day.txt", 1024 * 1024).decode().splitlines()
    if not calendar or calendar != sorted(set(calendar)):
        raise EvidenceError("calendar_not_strictly_increasing")
    for day in calendar:
        iso_date(day)
    dates = {}
    for symbol in REFERENCES:
        starts, features = [], {}
        for field in FIELDS:
            raw = read_file(root / "features" / symbol / (field + ".day.bin"))
            if len(raw) < 8 or len(raw) % 4:
                raise EvidenceError("invalid_reference_binary")
            values = struct.unpack("<" + str(len(raw) // 4) + "f", raw)
            start = values[0]
            if not math.isfinite(start) or start < 0 or not start.is_integer():
                raise EvidenceError("invalid_reference_start")
            starts.append(int(start))
            features[field] = values[1:]
        if len(set(starts)) != 1 or len({len(v) for v in features.values()}) != 1:
            raise EvidenceError("reference_fields_disagree")
        for index in range(len(features["close"]) - 1, 0, -1):
            position = starts[0] + index
            values = [features["close"][index - 1]] + [features[f][index] for f in FIELDS]
            if position < len(calendar) and all(math.isfinite(v) for v in values):
                dates[symbol] = calendar[position]
                break
        if symbol not in dates:
            raise EvidenceError("reference_complete_date_missing")
    if len(set(dates.values())) != 1:
        raise EvidenceError("reference_dates_disagree")
    complete = next(iter(dates.values()))
    if complete > before[1] or generation(root) != before:
        raise EvidenceError("qlib_generation_changed_or_invalid")
    return calendar, complete, before


def has_source_error(value):
    if isinstance(value, dict):
        return any(((key == "error" or key.endswith("_error")) and bool(child))
                   or has_source_error(child) for key, child in value.items())
    return isinstance(value, list) and any(has_source_error(child) for child in value)


def snapshot_rows(path):
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("database_missing_or_symlinked")
    result = {}
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise EvidenceError("database_quick_check_failed")
        for day, raw in conn.execute("SELECT date, payload FROM snapshot ORDER BY date"):
            iso_date(day)
            if not isinstance(raw, str):
                raise EvidenceError("non_text_snapshot_payload")
            row = {"payloadSha256": hashlib.sha256(raw.encode()).hexdigest(), "state": "valid"}
            try:
                payload = strict_json(raw)
                activity = payload["akshare"]["activity"]["统计日期"]
                if (payload["date"] != day or payload["qlib"]["date"] != day
                        or not isinstance(activity, str) or activity[:10] != day):
                    raise EvidenceError("internal_dates_disagree")
                if has_source_error(payload):
                    raise EvidenceError("stored_source_errors")
            except (ValueError, KeyError, TypeError) as exc:
                row["state"] = "invalid"
                row["reason"] = str(exc) if isinstance(exc, EvidenceError) else "stored_payload_invalid_schema"
            result[day] = row
    return result


def stage_evidence(root, now):
    if not root.exists():
        return {}, "missing"
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        return {}, "invalid_permissions"
    result = {}
    for path in sorted(root.glob("*.json")):
        try:
            iso_date(path.stem)
            document = strict_json(read_file(path, private=True))
            unsigned = dict(document)
            record_sha = unsigned.pop("recordSha256")
            payload = document["akshare"]
            window = document["collectionWindow"]
            start, end = timestamp(window["startedAt"]), timestamp(window["completedAt"])
            sectors, activity = payload["sectors"], payload["activity"]
            if (set(document) != {"schemaVersion", "tradeDate", "collectionWindow", "akshare", "payloadSha256", "recordSha256"}
                    or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
                    or set(window) != {"startedAt", "completedAt"}
                    or document["tradeDate"] != path.stem or str(activity["统计日期"])[:10] != path.stem
                    or not start <= end <= now + dt.timedelta(minutes=5)
                    or iso_date(path.stem) > now.date()
                    or canonical_hash(payload) != document["payloadSha256"]
                    or canonical_hash(unsigned) != record_sha
                    or not isinstance(sectors, list) or len(sectors) < 50 or has_source_error(payload)):
                raise EvidenceError("invalid_stage")
            for sector in sectors:
                if (not isinstance(sector, dict) or not {"name", "chg", "netInflow", "upCount", "downCount", "leader"} <= set(sector)
                        or not isinstance(sector["name"], str) or not sector["name"].strip()):
                    raise EvidenceError("invalid_sector")
            for field in ("上涨", "下跌", "平盘", "涨停", "跌停"):
                value = activity[field]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise EvidenceError("invalid_activity")
            if sum(activity[f] for f in ("上涨", "下跌", "平盘")) < 1000:
                raise EvidenceError("invalid_breadth")
            result[path.stem] = {"state": "valid", "payloadSha256": document["payloadSha256"],
                                 "recordSha256": record_sha, "observedAt": end.isoformat()}
        except (OSError, ValueError, KeyError, TypeError):
            result[path.stem] = {"state": "invalid", "reason": "stage_validation_failed"}
    return result, "checked"


def recent_run(path, now):
    try:
        record = strict_json(read_file(path))
        when = timestamp(record["timestamp"])
        if (when > now + dt.timedelta(minutes=5)
                or record.get("status") not in FAILURES | {"inserted", "skipped_existing", "idempotent", "success"}
                or (record.get("akshareStage") is not None and not isinstance(record["akshareStage"], dict))):
            raise EvidenceError("invalid_run_record")
        if record.get("target") is not None and iso_date(record["target"]) > now.date():
            raise EvidenceError("invalid_run_target")
        summary = {"status": record["status"], "timestamp": when.isoformat(),
                   "target": record.get("target"), "qlibTag": record.get("qlibTag"),
                   "hasWarnings": bool(record.get("warnings")),
                   "stageUnavailable": (record.get("akshareStage") or {}).get("status") == "unavailable"}
        stage = record.get("akshareStage") or {}
        for date_key, hash_key in (("tradeDate", "payloadSha256"), ("usedTradeDate", "usedPayloadSha256")):
            if (stage.get(date_key) is None) != (stage.get(hash_key) is None):
                raise EvidenceError("incomplete_stage_claim")
        if stage.get("tradeDate") is not None:
            iso_date(stage["tradeDate"])
            summary["stageTradeDate"] = stage["tradeDate"]
        if stage.get("payloadSha256") is not None:
            validate_hash(stage["payloadSha256"])
            summary["stagePayloadSha256"] = stage["payloadSha256"]
        if stage.get("usedTradeDate") is not None:
            iso_date(stage["usedTradeDate"])
            summary["usedStageTradeDate"] = stage["usedTradeDate"]
        if stage.get("usedPayloadSha256") is not None:
            validate_hash(stage["usedPayloadSha256"])
            summary["usedStagePayloadSha256"] = stage["usedPayloadSha256"]
        if record.get("payloadSha256") is not None:
            iso_date(record.get("target"))
            validate_hash(record["payloadSha256"])
            summary["payloadSha256"] = record["payloadSha256"]
        return summary, when
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unknown", "reason": "missing_or_invalid_last_run"}, None


def validate_hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise EvidenceError("invalid_sha256")


def audit(db, qlib, stages, last_run, mode, since, monitor_since, now, baseline=None):
    since, monitor_since = iso_date(since), iso_date(monitor_since)
    if since > monitor_since or monitor_since > now.date():
        raise EvidenceError("invalid_audit_window")
    calendar, complete, gen = qlib_evidence(qlib)
    if calendar[0] > since.isoformat() or iso_date(complete) > now.date():
        raise EvidenceError("calendar_coverage_or_complete_date_invalid")
    rows = snapshot_rows(db)
    stage_rows, stage_state = stage_evidence(stages, now)
    last, last_time = recent_run(last_run, now)
    business_mode = read_file(mode, 32).decode().strip()
    issues = []

    def issue(code, severity="warning", **details):
        issues.append({"code": code, "severity": severity, **details})

    if business_mode != "FROZEN":
        issue("business_mode_not_frozen", "critical")
    if any(iso_date(d) > now.date() for d in rows):
        issue("future_snapshot_date", "critical")
    if any(d > complete for d in rows):
        issue("snapshot_ahead_of_qlib_reference")
    if stage_state != "checked" or any(v["state"] != "valid" for v in stage_rows.values()):
        issue("market_stage_missing_or_invalid")
    elif not stage_rows:
        issue("no_sealed_market_evidence")
    calendar_set = set(calendar)
    for kind, records in (("snapshot", rows), ("stage", stage_rows)):
        outside = [d for d in records if calendar[0] <= d <= calendar[-1] and d not in calendar_set]
        if outside:
            issue(kind + "_outside_known_trading_calendar", dates=sorted(outside))
            for day in outside:
                records[day]["state"] = "invalid"
                records[day]["reason"] = "outside_known_trading_calendar"
        uncovered = [d for d in records if d < calendar[0]]
        if uncovered:
            issue(kind + "_before_calendar_coverage", dates=sorted(uncovered))
    for date_key, hash_key in (("stageTradeDate", "stagePayloadSha256"),
                               ("usedStageTradeDate", "usedStagePayloadSha256")):
        if last.get(date_key):
            claimed = stage_rows.get(last[date_key], {})
            if (claimed.get("state") != "valid" or (last.get(hash_key)
                    and claimed.get("payloadSha256") != last[hash_key])):
                issue("last_run_claimed_stage_missing_or_mismatched", tradeDate=last[date_key])
    if last.get("payloadSha256") and last["status"] in {"inserted", "skipped_existing", "idempotent", "success"}:
        if rows.get(last["target"], {}).get("payloadSha256") != last["payloadSha256"]:
            issue("last_run_snapshot_missing_or_mismatched", "critical", tradeDate=last["target"])
    baseline_state = "not_provided"
    if baseline is not None:
        baseline_state = "matched"
        expected = strict_json(read_file(baseline))["rowHashes"]
        if not isinstance(expected, dict) or not expected:
            raise EvidenceError("invalid_row_baseline")
        for day, digest in expected.items():
            iso_date(day)
            validate_hash(digest)
        changed = [d for d, h in expected.items() if rows.get(d, {}).get("payloadSha256") != h]
        if changed:
            baseline_state = "mismatch"
            issue("historical_rows_changed_or_missing", "critical", dates=sorted(changed))
    deadline = dt.datetime.combine(now.date(), dt.time(22, 45), CST)
    due_date = now.date() if now >= deadline else now.date() - dt.timedelta(days=1)
    due_start = dt.datetime.combine(due_date, dt.time(22, 30), CST)
    if last_time is None or last_time < due_start:
        issue("scheduled_snapshot_missing", dueDate=due_date.isoformat())
    if last["status"] in FAILURES:
        issue("last_snapshot_not_successful", status=last["status"], target=last.get("target"))
    if last.get("hasWarnings") or last.get("stageUnavailable"):
        issue("last_snapshot_material_warning")
    ledger = []
    new_due_gaps = []
    for day in (d for d in calendar if since.isoformat() <= d <= complete):
        row = rows.get(day, {"state": "missing"})
        stage = stage_rows.get(day, {"state": "missing"})
        legacy = day < monitor_since.isoformat()
        pending = day == now.date().isoformat() and now < deadline
        item = {"tradeDate": day, "snapshotState": row["state"], "stageState": stage["state"],
                "classification": "legacy" if legacy else "monitored", "pendingWindow": pending,
                "qlibAvailability": "calendar_and_reference_evidence_only",
                "recoveryReadiness": "not_applicable" if row["state"] == "valid" else (
                    "partial_evidence_only" if stage["state"] == "valid" else "missing_or_invalid_market_evidence")}
        if row["state"] != "missing":
            item["payloadSha256"] = row["payloadSha256"]
        if row.get("reason"):
            item["reason"] = row["reason"]
        if row["state"] != "valid" and not legacy and not pending:
            new_due_gaps.append(day)
        ledger.append(item)
    legacy_bad = [row["tradeDate"] for row in ledger if row["classification"] == "legacy" and row["snapshotState"] != "valid"]
    if legacy_bad:
        issue("legacy_gaps_or_invalid_rows", dates=legacy_bad)
    if new_due_gaps:
        issue("unresolved_trading_day_gaps", "critical" if len(new_due_gaps) >= 2 else "warning", dates=new_due_gaps)
    latest_stage = max((d for d, v in stage_rows.items() if v["state"] == "valid"), default=None)
    pending_upstream = bool(latest_stage and latest_stage > complete
                            and latest_stage == now.date().isoformat() and now < deadline)
    overdue_stages = sorted(d for d, value in stage_rows.items() if value["state"] == "valid"
                            and d > complete and (d < now.date().isoformat() or now >= deadline))
    if overdue_stages:
        issue("qlib_behind_observed_market_stage", pendingWindow=False,
              overdueStageDates=overdue_stages, latestStageDate=latest_stage, qlibCompleteDate=complete)
    if generation(qlib) != gen:
        raise EvidenceError("qlib_generation_changed_during_audit")
    return {"schemaVersion": 1, "generatedAt": now.isoformat(), "readOnly": True,
            "status": "critical" if any(i["severity"] == "critical" for i in issues) else "warning" if issues else "ok",
            "scope": "local_evidence_only", "businessMode": business_mode,
            "auditSince": since.isoformat(), "monitorSince": monitor_since.isoformat(),
            "schedule": {"expectedLastRunDate": due_date.isoformat(), "todayDeadline": deadline.isoformat()},
            "qlib": {"releaseTag": gen[1], "calendarLastDate": calendar[-1], "referenceCompleteDate": complete,
                     "upstreamFreshness": "not_independently_verified", "pendingObservedStage": pending_upstream,
                     "fullMarketCoverage": "not_scanned"},
            "snapshot": {"rowCount": len(rows), "latestValidDate": max((d for d, v in rows.items() if v["state"] == "valid"), default=None),
                         "quickCheck": "ok", "baseline": baseline_state, "rowHashes": {d: v["payloadSha256"] for d, v in rows.items()}},
            "lastRun": last, "stages": stage_rows, "issues": issues, "ledger": ledger,
            "automaticRecoveryAuthorized": False,
            "limitations": ["No independent exchange calendar or release API lookup.",
                            "Valid market stage alone cannot reconstruct historical observed_at sources.",
                            "Stored rows receive strict JSON/date/error checks, not a full market revalidation."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--qlib", type=Path, required=True)
    parser.add_argument("--stages", type=Path, required=True)
    parser.add_argument("--last-run", type=Path, required=True)
    parser.add_argument("--mode", type=Path, required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--monitor-since", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(args.db, args.qlib, args.stages, args.last_run, args.mode,
                       args.since, args.monitor_since, dt.datetime.now(CST), args.baseline)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # No paths, raw database data, or arbitrary exception text in journal.
        result = {"schemaVersion": 1, "readOnly": True, "status": "unknown",
                  "reason": str(exc) if isinstance(exc, EvidenceError) else "evidence_read_or_validation_failed",
                  "errorType": type(exc).__name__}
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None))
    return 0  # Data warnings are payload state, not collector/process failure.


if __name__ == "__main__":
    raise SystemExit(main())
