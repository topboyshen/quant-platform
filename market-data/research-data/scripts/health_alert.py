"""Bounded status/message/receipt handling for the research-data health alerter.

The shell wrapper must not treat a zero exit code as proof of delivery: the
hermes send command returns a JSON receipt, and only a reported success with a
message id and no skip counts as delivered. Keeping this parsing, the message
formatting and the receipt sanitizing here makes them testable without
subprocesses, and keeps every value the shell interpolates allowlisted.

Notify-on-change (batch 1, 2026-09-26): the audit's true severity is kept
unchanged (critical stays critical and stays queryable in last.json), but a
notification is sent only when the alert-worthy fault set changes against the
last committed state: a new failure, a worsening (new gap date / affected set
expanded), a recovery, or an audit that should have run but did not produce a
current result. The fault fingerprint is built from issue type + stage/code +
target dates / affected set only; runId, run/audit timestamps and other per-run
values are evidence references and never part of it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

STATUS_VALUES = ("ok", "warning", "critical", "missing", "unknown")
ALERT_STATUSES = frozenset({"critical", "missing", "unknown"})
UNIT_STATES = ("active", "inactive", "failed", "unknown")
CODE_RE = re.compile(r"[a-z0-9_]{1,64}\Z")
REASON_RE = re.compile(r"[a-z0-9_]{1,64}\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
MAX_RECEIPT_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 1200
MAX_ATOMS = 2000
OBSERVABILITY_PREFIX = "audit_"
DUE_RUN_CODES = ("scheduled_snapshot_missing", "scheduled_core_market_missing")
CORE_OPERATIONAL_CODES = ("last_core_market_not_successful", "last_snapshot_not_successful",
                          "independent_expected_day_unavailable", "qlib_not_current_for_expected_trading_day")
CST = dt.timezone(dt.timedelta(hours=8))
RECEIPT_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
RUN_DECISION_SCHEMA = "research.active-run-decision.v1"
RUN_VERDICTS = ("pass", "fail", "insufficient", "pending", "not_required")
RUN_STATUSES = ("inserted", "skipped_existing", "idempotent", "success", "skipped_non_trading",
                "rejected", "deferred", "failed", "temporary_failure", "usage_error", "unknown", "not_configured")
RUN_CONTRACTS = {"research.full-snapshot.v1": ("full_snapshot", True, "full_snapshot"),
                 "research.core-market.v1": ("core_market", False, "qlib_market_summary")}
RETAINED_CODES = ("unresolved_trading_day_gaps", "legacy_gaps_or_invalid_rows")
DEFAULT_RETAINED_PATH = Path(__file__).resolve().parents[1] / "config" / "health-alert-retained.json"
# Date-bearing issue fields that define WHICH dates/targets a fault affects.
# Deliberately excludes runId, runAt, timestamp, generatedAt and similar per-run
# evidence, so the same fault never looks new just because it was re-observed.
LIST_DATE_KEYS = ("dates", "affectedDates", "overdueStageDates")
SCALAR_DATE_KEYS = ("tradeDate", "target", "targetDate", "dueDate")


def _instant(value):
    if not isinstance(value, str) or not INSTANT_RE.fullmatch(value):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_day(value):
    try:
        return isinstance(value, str) and dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _codes(data):
    codes = []
    if isinstance(data, dict):
        for issue in data.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            code = issue.get("code")
            if isinstance(code, str) and CODE_RE.fullmatch(code) and code not in codes:
                codes.append(code)
            if len(codes) >= 8:
                break
    return codes


def _issue_dates(issue):
    """Sorted affected dates of one issue; '*' when the issue names no date."""
    found = set()
    for key in LIST_DATE_KEYS:
        values = issue.get(key)
        if isinstance(values, list):
            found.update(v for v in values if isinstance(v, str) and DATE_RE.fullmatch(v))
    for key in SCALAR_DATE_KEYS:
        value = issue.get(key)
        if isinstance(value, str) and DATE_RE.fullmatch(value):
            found.add(value)
    return sorted(found) or ["*"]


def _due_run_atoms(data):
    """Explicit missing-run/core-failed-run events, keyed only by due date.

    These are deliberately not critical atoms: the audit severity is unchanged and
    ordinary warnings still never page on their own. Only a new code+dueDate is a
    new event; repeated audits for the same due date collapse into one.
    """
    atoms = set()
    if isinstance(data, dict):
        for issue in data.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("acceptedFreeze") is True:
                continue
            if issue.get("code") not in DUE_RUN_CODES + CORE_OPERATIONAL_CODES:
                continue
            code = issue["code"]
            due = issue.get("dueDate")
            atoms.add(f"{code}|{due if isinstance(due, str) and DATE_RE.fullmatch(due) else '*'}")
    return sorted(atoms)


def _critical_atoms(data):
    """Alert-worthy fault atoms "code|date" from critical-severity issues only.

    warning/information issues stay in the audit (and on the dashboard) but, as
    before this change, do not page on their own.
    """
    atoms = set()
    if isinstance(data, dict):
        for issue in data.get("issues") or []:
            if (not isinstance(issue, dict) or issue.get("severity") != "critical"
                    or issue.get("acceptedFreeze") is True):
                continue
            code = issue.get("code")
            code = code if isinstance(code, str) and CODE_RE.fullmatch(code) else "unrecognized_issue_code"
            for date in _issue_dates(issue):
                atoms.add(f"{code}|{date}")
                if len(atoms) >= MAX_ATOMS:
                    return sorted(atoms)
    return sorted(atoms)


def _run_decision(data, generated):
    """Project the audit's verdict, checking identity only, never business gates.

    Absence is the legacy contract. Presence with a broken schema/binding is an
    observability failure, not permission to reuse a previous successful run.
    """
    if "activeRunDecision" not in data:
        return None
    value, last = data["activeRunDecision"], data.get("activeLastRun")
    fields = {"schema", "contract", "scope", "fullSnapshot", "artifactKind", "scheduledDate", "scheduledFor",
              "deadlineAt", "evaluatedAt", "expectedTargetDate", "run", "verification", "artifact", "calendar"}
    if not isinstance(value, dict) or not fields <= value.keys() or not isinstance(last, dict):
        raise ValueError("invalid_run_decision")
    contract = value.get("contract")
    if (type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1
            or value.get("schema") != RUN_DECISION_SCHEMA
            or contract not in RUN_CONTRACTS or data.get("activeContract") != contract
            or _instant(value.get("evaluatedAt")) != generated):
        raise ValueError("invalid_run_decision_identity")
    scope, full, kind = RUN_CONTRACTS[contract]
    if (value.get("scope") != scope or value.get("fullSnapshot") is not full
            or value.get("artifactKind") != kind):
        raise ValueError("invalid_run_decision_contract")
    day = value.get("scheduledDate")
    if not _iso_day(day) or day != generated.astimezone(CST).date().isoformat():
        raise ValueError("invalid_run_decision_day")
    scheduled, deadline = _instant(value.get("scheduledFor")), _instant(value.get("deadlineAt"))
    if (scheduled is None or deadline is None or scheduled > deadline
            or scheduled.astimezone(CST).date().isoformat() != day
            or deadline.astimezone(CST).date().isoformat() != day):
        raise ValueError("invalid_run_decision_schedule")
    for date in (value.get("expectedTargetDate"), last.get("target")):
        if date is not None and not _iso_day(date):
            raise ValueError("invalid_run_decision_target")
    run, artifact, check = value.get("run"), value.get("artifact"), value.get("verification")
    if (not isinstance(run, dict) or not {"runId", "status", "targetDate", "startedAt", "completedAt"} <= run.keys()
            or not isinstance(artifact, dict) or not {"payloadSha256", "rowState", "quality"} <= artifact.keys()
            or not isinstance(check, dict)):
        raise ValueError("invalid_run_decision_fields")
    if (run.get("status") not in RUN_STATUSES or run.get("status") != last.get("status")
            or run.get("targetDate") != last.get("target") or run.get("runId") != last.get("runId")):
        raise ValueError("invalid_run_decision_run_binding")
    if run.get("runId") is not None and (not isinstance(run["runId"], str) or not re.fullmatch(r"[0-9a-f]{32}", run["runId"])):
        raise ValueError("invalid_run_decision_run_id")
    for output, source in (("startedAt", "startedAt"), ("completedAt", "timestamp")):
        claimed, actual = run.get(output), last.get(source)
        if ((claimed is None) != (actual is None)
                or (claimed is not None and (_instant(claimed) is None or _instant(claimed) != _instant(actual)))):
            raise ValueError("invalid_run_decision_time_binding")
    payload = artifact.get("payloadSha256")
    if (payload != last.get("payloadSha256")
            or (payload is not None and (not isinstance(payload, str) or not re.fullmatch(r"[0-9a-f]{64}", payload)))):
        raise ValueError("invalid_run_decision_payload_binding")
    if artifact.get("rowState") not in ("valid", "invalid", "missing"):
        raise ValueError("invalid_run_decision_row_state")
    quality = artifact.get("quality")
    if quality is not None:
        if (not isinstance(quality, dict) or contract != "research.core-market.v1"
                or quality.get("scope") != "sealed_core_qlib_summary"
                or quality.get("targetDate") != run.get("targetDate") or not _iso_day(quality.get("targetDate"))
                or quality.get("payloadSha256") != payload or payload is None
                or quality.get("priceStatus") not in ("partial", "complete")
                or any(type(quality.get(k)) is not int or quality[k] < 0 for k in ("valid", "invalid", "eligible"))
                or type(quality.get("ratio")) not in (int, float) or not math.isfinite(quality["ratio"])
                or not isinstance(quality.get("generationTag"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", quality["generationTag"])):
            raise ValueError("invalid_run_decision_quality_identity")
    calendar = value["calendar"]
    if calendar is not None:
        if (not isinstance(calendar, dict) or calendar.get("status") not in ("open", "closed")
                or calendar.get("targetDate") != day
                or any(not _iso_day(calendar.get(k)) for k in ("targetDate", "previousTradingDate", "nextTradingDate", "sourceDate"))
                or any(not isinstance(calendar.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", calendar[k])
                       for k in ("evidenceSha256", "sourceEvidenceSha256"))
                or _instant(calendar.get("observedAt")) is None
                or calendar.get("proofKind") not in ("sealed_archive", "validated_terminal_context",
                                                    "validated_non_trading_skip", "validated_closed_interval")):
            raise ValueError("invalid_run_decision_calendar_identity")
    if (check.get("status") not in RUN_VERDICTS
            or check.get("scope") != "local_run_artifact_calendar_identity"):
        raise ValueError("invalid_run_decision_verdict")
    # Successful verdicts have a complete evidence identity in this schema.
    # Fail/insufficient/pending may intentionally retain missing identity fields.
    if check["status"] in ("pass", "not_required"):
        if (any(run[field] is None for field in ("runId", "targetDate", "startedAt", "completedAt"))
                or not isinstance(value["calendar"], dict)
                or (check["status"] == "pass" and payload is None)):
            raise ValueError("incomplete_run_decision_success_identity")
    for field in ("reasons", "currentIssueCodes", "historicalIssueCodes", "materialWarningCodes"):
        codes = check.get(field)
        if (not isinstance(codes, list) or len(codes) > 256
                or not all(isinstance(code, str) and CODE_RE.fullmatch(code) for code in codes)):
            raise ValueError("invalid_run_decision_codes")
    # Only the whitelist is exposed to messages; raw errors/paths never travel.
    return {"status": check["status"], "scheduledDate": day, "targetDate": run.get("targetDate"),
            "contract": contract, "materialWarningCodes": list(check["materialWarningCodes"])}


FRESHNESS_SCHEMA = "research.supply-freshness.v1"
FRESHNESS_STATUSES = ("current", "stale", "unknown", "not_evaluated")


def _supply_freshness(data):
    """Validated supply-chain freshness, independent of the snapshot verdict.

    Producers that predate the explicit field are read from their issue list.
    Raises ValueError for a present but malformed field.
    """
    value = data.get("supplyFreshness")
    if value is None:
        for issue in data.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if issue.get("code") == "qlib_not_current_for_expected_trading_day" and _iso_day(issue.get("expectedTarget")):
                return {"status": "stale", "expectedTargetDate": issue["expectedTarget"],
                        "actualTargetDate": issue.get("actualTarget") if _iso_day(issue.get("actualTarget")) else None,
                        "dueDate": issue.get("dueDate") if _iso_day(issue.get("dueDate")) else None,
                        "snapshotRequired": None, "derived": True}
            if issue.get("code") == "independent_expected_day_unavailable" and _iso_day(issue.get("dueDate")):
                return {"status": "unknown", "expectedTargetDate": None, "actualTargetDate": None,
                        "dueDate": issue["dueDate"], "snapshotRequired": None, "derived": True}
        return None
    if (not isinstance(value, dict) or value.get("schema") != FRESHNESS_SCHEMA
            or value.get("status") not in FRESHNESS_STATUSES or not _iso_day(value.get("dueDate"))
            or (value.get("status") in ("current", "stale")
                and not (_iso_day(value.get("expectedTargetDate")) and _iso_day(value.get("actualTargetDate"))))
            or (value.get("status") == "stale" and value["expectedTargetDate"] == value["actualTargetDate"])
            or (value.get("status") == "current" and value["expectedTargetDate"] != value["actualTargetDate"])
            or (value.get("snapshotRequired") is not None and type(value.get("snapshotRequired")) is not bool)):
        raise ValueError("invalid_supply_freshness")
    return {"status": value["status"], "expectedTargetDate": value.get("expectedTargetDate"),
            "actualTargetDate": value.get("actualTargetDate"), "dueDate": value["dueDate"],
            "snapshotRequired": value["snapshotRequired"], "derived": False}


def load_retained(path=None):
    """Return {'baseline': {code: set}, 'later': {code: set}} or None if unusable."""
    try:
        raw = Path(path or DEFAULT_RETAINED_PATH).read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        return None
    base = data.get("originalBaseline")
    later = data.get("laterRegistrations")
    if not isinstance(base, dict) or not isinstance(later, list):
        return None
    result = {"baseline": {}, "later": {code: set() for code in RETAINED_CODES}}
    for code in RETAINED_CODES:
        dates = base.get(code)
        if not isinstance(dates, list) or not all(isinstance(d, str) and DATE_RE.fullmatch(d) for d in dates):
            return None
        result["baseline"][code] = set(dates)
    for item in later:
        if (not isinstance(item, dict) or item.get("code") not in RETAINED_CODES
                or not isinstance(item.get("date"), str) or not DATE_RE.fullmatch(item["date"])):
            return None
        result["later"][item["code"]].add(item["date"])
    return result


def _retained_counts(data, retained=None):
    """Classify audit dates against the explicit confirmed-retained list.

    Only dates on the evidence-backed list count as retained: the original
    8 gap days + 10 legacy days, and the separately registered 2026-09-22.
    Every other date of those issue types is 'unconfirmed' and is never
    described as "not a new fault". An unreadable list confirms nothing.
    """
    counts = {"unresolvedTradingDayGaps": 0, "legacyGapDays": 0,
              "laterRegisteredGapDays": 0, "laterRegisteredDates": [],
              "unconfirmedDays": 0, "unconfirmedDates": [],
              "retainedListAvailable": retained is not None}
    if not isinstance(data, dict):
        return counts
    unconfirmed, later_dates = set(), set()
    observed_by_code = {code: set() for code in RETAINED_CODES}
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict) or issue.get("code") not in RETAINED_CODES:
            continue
        code = issue["code"]
        dates = issue.get("dates")
        if not isinstance(dates, list):
            dates = issue.get("affectedDates")
        if isinstance(dates, list):
            observed_by_code[code].update(
                d for d in dates if isinstance(d, str) and DATE_RE.fullmatch(d))
    # A freeze audit may split one code into expected absence and damaged rows.
    # Count the union once, regardless of issue order or repeated dates.
    for code, observed in observed_by_code.items():
        if retained is None:
            unconfirmed |= observed
            continue
        base = observed & retained["baseline"][code]
        later = (observed - base) & retained["later"][code]
        if code == "unresolved_trading_day_gaps":
            counts["unresolvedTradingDayGaps"] = len(base)
        else:
            counts["legacyGapDays"] = len(base)
        later_dates |= later
        unconfirmed |= observed - base - later
    counts["laterRegisteredDates"] = sorted(later_dates)
    counts["laterRegisteredGapDays"] = len(later_dates)
    counts["unconfirmedDates"] = sorted(unconfirmed)
    counts["unconfirmedDays"] = len(unconfirmed)
    return counts


def _recovery_evidence(data):
    """Validate optional ledgers before deriving any recovery/policy evidence.

    Older artifacts may omit the ledgers. A present malformed ledger is not an
    empty ledger: the caller must retain the last known business faults.
    """
    core = data.get("coreMarket", {})
    if not isinstance(core, dict):
        raise ValueError("health_ledger_invalid")
    valid = []
    for rows, state_key in ((data.get("ledger", []), "snapshotState"),
                            (core.get("ledger", []), "coreState")):
        if not isinstance(rows, list):
            raise ValueError("health_ledger_invalid")
        dates, seen = set(), set()
        for row in rows:
            if (not isinstance(row, dict) or not _iso_day(row.get("tradeDate"))
                    or row.get(state_key) not in ("valid", "missing", "invalid")
                    or row["tradeDate"] in seen):
                raise ValueError("health_ledger_invalid")
            seen.add(row["tradeDate"])
            if row[state_key] == "valid":
                dates.add(row["tradeDate"])
        valid.append(sorted(dates))
    issues = data.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
        raise ValueError("health_issues_invalid")
    freeze_issues = [issue for issue in issues if issue.get("acceptedFreeze") is True]
    absence_dates = set()
    for issue in freeze_issues:
        dates = issue.get("dates", [])
        if not isinstance(dates, list) or any(not _iso_day(day) for day in dates):
            raise ValueError("health_issues_invalid")
        absence_dates.update(dates)
    return {"validFullDates": valid[0], "validCoreDates": valid[1],
            "freezeAbsenceDates": sorted(absence_dates),
            "freezeLimitationCodes": sorted({issue["code"] for issue in freeze_issues
                                             if isinstance(issue.get("code"), str)}),
            "freezeStalenessAccepted": any(
                issue.get("code") == "qlib_not_current_for_expected_trading_day"
                for issue in freeze_issues)}


def describe(path, now=None, max_age_hours=6.0, retained_path=None):
    """Classify the audit artifact without ever raising on bad input."""
    now = now or dt.datetime.now(CST)
    result = {"status": "unknown", "codes": [], "generatedAt": None,
              "stale": True, "ageHours": None, "reason": None, "criticalAtoms": [],
              "dueRunAtoms": [],
              "retained": _retained_counts(None, load_retained(retained_path))}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        result.update(status="missing", reason="health_json_missing")
        return result
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError):
        result.update(status="unknown", reason="health_json_invalid")
        return result
    if not isinstance(data, dict):
        result.update(status="unknown", reason="health_json_not_object")
        return result
    try:
        evidence = _recovery_evidence(data)
    except ValueError as exc:
        result.update(status="unknown", reason=str(exc))
        return result
    status = data.get("status")
    if not isinstance(status, str) or status not in STATUS_VALUES:
        result.update(status="unknown", reason="health_status_invalid")
    else:
        result["status"] = status
    result["acceptedFreeze"] = data.get("acceptedFreeze") is True
    result.update(evidence)
    result["codes"] = _codes(data)
    result["criticalAtoms"] = _critical_atoms(data)
    result["dueRunAtoms"] = _due_run_atoms(data)
    result["retained"] = _retained_counts(data, load_retained(retained_path))
    generated = _instant(data.get("generatedAt"))
    if generated is None:
        result.update(stale=True, reason=result["reason"] or "health_generated_at_invalid")
        return result
    result["generatedAt"] = data["generatedAt"]
    try:
        result["supplyFreshness"] = _supply_freshness(data)
    except (ValueError, TypeError, KeyError):
        result["supplyFreshnessInvalid"] = True
    if "activeRunDecision" in data:
        # New producers own the current-run judgement. Do not independently
        # reinterpret their legacy run issues alongside the unique verdict.
        result["dueRunAtoms"] = []
        try:
            result["activeRunDecision"] = _run_decision(data, generated)
        except (ValueError, TypeError, KeyError):
            result["activeRunDecisionInvalid"] = True
    age = (now.astimezone(CST) - generated.astimezone(CST)).total_seconds() / 3600
    result["ageHours"] = round(age, 3)
    # A stale artifact means this alert is not describing the run that just
    # finished; say so instead of presenting old numbers as current.
    result["stale"] = age > float(max_age_hours) or age < -0.5
    # At/after the independent 23:00 check, a recent manual/pre-window audit
    # does not prove that today's scheduled 22:45 audit ran.
    if now.astimezone(CST).time() >= dt.time(23, 0):
        due = dt.datetime.combine(now.astimezone(CST).date(), dt.time(22, 45), CST)
        if generated.astimezone(CST) < due:
            result.update(stale=True, reason="scheduled_audit_missing")
    if result["stale"] and result["reason"] is None:
        result["reason"] = "health_json_stale"
    return result


# ------------------------------------------------------------ change decision

def alert_atoms(info, unit_state, now=None):
    """Current alert-worthy fault set.

    Audit-level problems (no current result, unreadable result, failed audit
    unit) are keyed by the scheduled day they concern, so each missed or broken
    scheduled run is its own event ("should run but did not"), while repeated
    attempts for the same day collapse into one.
    """
    now = (now or dt.datetime.now(CST)).astimezone(CST)
    day = now.date().isoformat()
    status = info.get("status")
    atoms = set()
    if status == "critical":
        # The audit derives "critical" from its critical issues; keep that
        # coupling so a warning-level artifact never pages on its own.
        atoms.update(info.get("criticalAtoms") or ["audit_status_critical|*"])
    if status in STATUS_VALUES and not info.get("stale"):
        # A fresh audit that ran but found a scheduled run missing is a real
        # "should run but did not" event even though it is only a warning.
        atoms.update(info.get("dueRunAtoms") or [])
        run = info.get("activeRunDecision")
        if isinstance(run, dict) and run.get("status") in ("fail", "insufficient"):
            atoms.add(f"active_run_{run['status']}|{run['scheduledDate']}")
        # Keyed by the expected target, not the audit day: one stale price
        # library over a multi-day holiday is one fault, not one per night.
        fresh = info.get("supplyFreshness")
        # Accepted freeze already explains a stale price library. A new expected
        # date must not become a new_failure every trading day.
        if (isinstance(fresh, dict) and fresh.get("status") == "stale"
                and not info.get("freezeStalenessAccepted")):
            atoms.add(f"qlib_supply_stale|{fresh['expectedTargetDate']}")
        elif isinstance(fresh, dict) and fresh.get("status") == "unknown":
            atoms.add(f"qlib_supply_freshness_unknown|{fresh.get('dueDate') or day}")
    if info.get("supplyFreshnessInvalid"):
        atoms.add(f"audit_supply_freshness_invalid|{day}")
    if info.get("activeRunDecisionInvalid"):
        atoms.add(f"audit_run_decision_invalid|{day}")
    if status in ("missing", "unknown"):
        atoms.add(f"audit_result_{status}|{day}")
    elif info.get("stale"):
        atoms.add(f"audit_not_current|{day}")
    if unit_state == "failed":
        atoms.add(f"audit_unit_failed|{day}")
    return sorted(atoms)


def fingerprint(atoms):
    body = json.dumps(sorted(atoms), separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(body).hexdigest()


def read_state(path):
    """Last committed state or None (missing/corrupt state => everything is new)."""
    if not path:
        return None
    try:
        p = Path(path)
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_STATE_BYTES:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return data if _valid_state(data) else None


def _valid_state(data):
    atoms = data.get("atoms") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("schemaVersion") != STATE_SCHEMA_VERSION
            or not isinstance(atoms, list) or not all(isinstance(a, str) and "|" in a for a in atoms)
            or data.get("fingerprint") != fingerprint(atoms)):
        return False
    return True


def _split_code(atom):
    return atom.split("|", 1)[0]


def _is_observability(atom):
    """Audit-level atoms describe the audit itself, not the business data."""
    return _split_code(atom).startswith(OBSERVABILITY_PREFIX)


def _removed_faults(info, previous, current):
    """Separate date/scope transitions from evidence-backed recovery.

    A successful *later* run does not repair an earlier missing snapshot. The
    current-run identity may advance, while historical gap atoms remain owned by
    the audit. Price freshness is different: a bound actual price date can prove
    that an older target was reached, even when the new target is still stale.
    No state/receipt normalization is done here; old pending receipts retain
    their exact state hash and are applied by the unchanged delivery protocol.
    """
    recovered, advanced, unconfirmed, reclassified = [], [], [], []
    for atom in sorted(previous - current):
        code, day = atom.split("|", 1)
        if code in ("active_run_fail", "active_run_insufficient"):
            run = info.get("activeRunDecision")
            scheduled = run.get("scheduledDate") if isinstance(run, dict) else None
            if (_iso_day(day) and _iso_day(scheduled)
                    and scheduled == day and run.get("status") == "pass"):
                recovered.append(atom)
            elif (_iso_day(day) and scheduled == day
                  and code == "active_run_insufficient" and run.get("status") == "fail"):
                # A confirmed failure supersedes uncertainty, not recovery.
                advanced.append(atom)
            elif (_iso_day(day) and _iso_day(scheduled) and scheduled > day
                  and run.get("status") in ("pass", "fail", "insufficient")):
                advanced.append(atom)
            else:
                # Missing/unknown/pending/holiday evidence is not recovery.
                unconfirmed.append(atom)
        elif code == "qlib_supply_stale" and info.get("freezeStalenessAccepted"):
            fresh = info.get("supplyFreshness") if isinstance(info.get("supplyFreshness"), dict) else {}
            actual = fresh.get("actualTargetDate")
            if _iso_day(day) and _iso_day(actual) and actual >= day:
                recovered.append(atom)
            else:
                # The price date was not reached. Dropping the atom is the freeze
                # policy, not evidence that the old target was filled.
                reclassified.append(atom)
        elif code == "qlib_supply_stale":
            fresh = info.get("supplyFreshness")
            expected = fresh.get("expectedTargetDate") if isinstance(fresh, dict) else None
            actual = fresh.get("actualTargetDate") if isinstance(fresh, dict) else None
            if (_iso_day(day) and _iso_day(expected) and _iso_day(actual)
                    and fresh.get("status") in ("current", "stale") and expected >= day):
                if actual >= day:
                    recovered.append(atom)
                elif expected > day:
                    advanced.append(atom)
                else:
                    unconfirmed.append(atom)
            else:
                unconfirmed.append(atom)
        elif code == "qlib_supply_freshness_unknown":
            fresh = info.get("supplyFreshness")
            due = fresh.get("dueDate") if isinstance(fresh, dict) else None
            if (_iso_day(day) and _iso_day(due) and due >= day
                    and fresh.get("status") in ("current", "stale")
                    and _iso_day(fresh.get("expectedTargetDate"))
                    and _iso_day(fresh.get("actualTargetDate"))):
                # Only the ability to determine freshness has recovered; a
                # known stale supply remains its own active business fault.
                recovered.append(atom)
            elif (_iso_day(day) and _iso_day(due) and due > day
                  and fresh.get("status") == "unknown"):
                advanced.append(atom)
            else:
                unconfirmed.append(atom)
        elif code in ("unresolved_trading_day_gaps", "unresolved_core_market_gaps"):
            evidence = info.get("validFullDates") if code == "unresolved_trading_day_gaps" else info.get("validCoreDates")
            if _iso_day(day) and day in set(evidence or []):
                recovered.append(atom)
            elif info.get("acceptedFreeze") and day in set(info.get("freezeAbsenceDates") or []):
                reclassified.append(atom)
            else:
                # A missing or unproven row is not recovery. A valid ledger row
                # above is the evidence. Policy absence is reclassified above.
                unconfirmed.append(atom)
        elif info.get("acceptedFreeze") and code in set(info.get("freezeLimitationCodes") or []):
            reclassified.append(atom)
        elif (_iso_day(day) and (_is_observability(atom)
                                or code in DUE_RUN_CODES + CORE_OPERATIONAL_CODES)
              and any(_split_code(other) == code and _iso_day(other.split("|", 1)[1])
                      and other.split("|", 1)[1] > day for other in current)):
            # Another missed scheduled date cannot repair the previous one.
            advanced.append(atom)
        else:
            recovered.append(atom)
    return recovered, advanced, unconfirmed, reclassified


def decide(info, unit_state, previous=None, now=None):
    """Return the notification decision and the state to commit afterwards."""
    now = (now or dt.datetime.now(CST)).astimezone(CST)
    current = alert_atoms(info, unit_state, now)
    prev_atoms = set(previous["atoms"]) if isinstance(previous, dict) else None
    if isinstance(previous, dict):
        if "businessAtoms" in previous:
            prev_business = set(previous.get("businessAtoms") or [])
        else:
            # Legacy state written before R03 has no businessAtoms field. Inherit
            # conservatively from its atoms (business atoms only) instead of
            # treating the missing field as "no business faults", which would
            # report every known fault as recovered on the first blind audit.
            prev_business = {a for a in (previous.get("atoms") or []) if not _is_observability(a)}
    else:
        prev_business = None
    audit_visible = not any(_is_observability(atom) for atom in current)
    # A missing, unreadable or stale audit is not evidence of recovery. The last
    # valid business fault set is carried forward so business faults are neither
    # reported as recovered while the audit is blind, nor re-reported as new once
    # a fresh audit can read them again.
    effective = set(current)
    if not audit_visible and prev_business is not None:
        effective |= prev_business
    advanced, unconfirmed = [], []
    if prev_atoms is None:
        added, recovered, reclassified = sorted(effective), [], []
        kind = "first_observation" if effective else "no_fault"
    else:
        added = sorted(effective - prev_atoms)
        recovered, advanced, unconfirmed, reclassified = _removed_faults(info, prev_atoms, effective)
        effective.update(unconfirmed)
        kind = "unchanged"
    prev_codes = {_split_code(a) for a in (prev_atoms or set())}
    new_failure = [a for a in added if _split_code(a) not in prev_codes]
    worsened = [a for a in added if _split_code(a) in prev_codes]
    not_run = [a for a in added
               if _is_observability(a) or _split_code(a) in DUE_RUN_CODES]
    if prev_atoms is not None:
        if not_run:
            kind = "should_run_but_did_not"
        elif new_failure:
            kind = "new_failure"
        elif worsened:
            kind = "worsened"
        elif recovered:
            kind = "recovered"
        elif advanced:
            kind = "scope_advanced"
        elif reclassified:
            kind = "policy_reclassified"
    notify = bool(added or recovered or advanced)
    business = sorted(a for a in effective if not _is_observability(a))
    state = {"schemaVersion": STATE_SCHEMA_VERSION, "atoms": sorted(effective),
             "fingerprint": fingerprint(sorted(effective)),
             "businessAtoms": business if audit_visible else sorted(prev_business or []),
             "auditVisible": audit_visible,
             "auditStatus": info.get("status") if info.get("status") in STATUS_VALUES else "unknown",
             # Evidence reference only; never part of the fingerprint.
             "evidence": {"auditGeneratedAt": info.get("generatedAt") if _instant(info.get("generatedAt")) else None,
                          "unitState": unit_state if unit_state in UNIT_STATES else "unknown",
                          "policyReclassified": reclassified},
             "decision": kind, "decidedAt": now.isoformat()}
    return {"notify": notify, "kind": kind, "new": new_failure, "worsened": worsened,
            "recovered": recovered, "reclassified": reclassified,
            "rolledForward": advanced, "recoveryUnconfirmed": unconfirmed,
            "current": list(current), "fingerprint": state["fingerprint"],
            "previousFingerprint": previous.get("fingerprint") if isinstance(previous, dict) else None,
            "state": state}


def write_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("state_path_not_regular_file")
    if state.get("fingerprint") != fingerprint(state.get("atoms") or []):
        raise ValueError("state_fingerprint_mismatch")
    body = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


# ------------------------------------------------------------------ messages

def _retained_line(info):
    """保留项单独一行；只有全部日期都在已确认保留清单内，才可写“均非新增故障”。"""
    retained = (info or {}).get("retained") or {}
    gaps = int(retained.get("unresolvedTradingDayGaps") or 0)
    legacy = int(retained.get("legacyGapDays") or 0)
    later = [d for d in (retained.get("laterRegisteredDates") or []) if isinstance(d, str) and DATE_RE.fullmatch(d)]
    unconfirmed = [d for d in (retained.get("unconfirmedDates") or []) if isinstance(d, str) and DATE_RE.fullmatch(d)]
    if gaps == 0 and legacy == 0 and not later and not unconfirmed:
        return "保留项：无"
    parts = [f"保留项（历史，不补造）：原确认缺口日 {gaps} 个 + 遗留问题日 {legacy} 个"]
    if later:
        parts.append(f"；后续单列登记缺口日 {'、'.join(later)}")
    if unconfirmed:
        shown = "、".join(unconfirmed[:8]) + ("…" if len(unconfirmed) > 8 else "")
        parts.append(f"；另有 {len(unconfirmed)} 个日期不在已确认保留清单：{shown}（可能为新增，待核）")
    elif retained.get("retainedListAvailable"):
        parts.append("（均在已确认保留清单内，非新增故障）")
    return "".join(parts)


CHANGE_LABELS = {"first_observation": "首次记录（无已提交基线）", "new_failure": "新增故障",
                 "worsened": "恶化（受影响集合扩大）", "recovered": "恢复",
                 "scope_advanced": "运行/目标日期推进（不代表旧故障恢复）",
                 "should_run_but_did_not": "应运行但未产生当次结果", "unchanged": "无变化",
                 "policy_reclassified": "政策重分类为已知限制（不是数据恢复）",
                 "no_fault": "无告警级问题"}


def _atom_text(atoms, limit=8):
    shown = [a.replace("|", "@") for a in atoms[:limit] if isinstance(a, str)]
    return "、".join(shown) + ("…" if len(atoms) > limit else "") if shown else "无"


def _change_lines(decision):
    if not isinstance(decision, dict):
        return []
    kind = decision.get("kind") if decision.get("kind") in CHANGE_LABELS else "unchanged"
    lines = [f"变化：{CHANGE_LABELS[kind]}"]
    if decision.get("new"):
        lines.append(f"新增：{_atom_text(decision['new'])}")
    if decision.get("worsened"):
        lines.append(f"恶化：{_atom_text(decision['worsened'])}")
    if decision.get("recovered"):
        lines.append(f"恢复：{_atom_text(decision['recovered'])}")
        if any(_split_code(atom) == "qlib_supply_freshness_unknown" for atom in decision["recovered"]):
            lines.append("新鲜度判定恢复仅表示已能判定，不代表价格追平或历史缺口修复。")
    if decision.get("rolledForward"):
        lines.append(f"日期/运行范围推进（非恢复）：{_atom_text(decision['rolledForward'])}")
    if decision.get("recoveryUnconfirmed"):
        lines.append(f"恢复未证实（保留）：{_atom_text(decision['recoveryUnconfirmed'])}")
    return lines


def _run_line(info):
    if info.get("activeRunDecisionInvalid"):
        return ["当前运行核验：证据身份无效，保持待核；不据此确认业务恢复。"]
    run = info.get("activeRunDecision")
    if not isinstance(run, dict):
        return []
    warning = "；保留警告 " + ",".join(run["materialWarningCodes"][:8]) if run["materialWarningCodes"] else ""
    return [f"当前运行核验：{run['status']}；计划日期 {run['scheduledDate']}；目标 {run['targetDate'] or '未记录'}{warning}"]


def _freshness_line(info):
    if info.get("supplyFreshnessInvalid"):
        return ["供应链新鲜度：字段无效，待核；不据此确认价格库已更新。"]
    fresh = info.get("supplyFreshness")
    if not isinstance(fresh, dict) or fresh.get("status") == "not_evaluated":
        return []
    labels = {"current": "当前", "stale": "滞后", "unknown": "无法判定"}
    detail = ""
    if fresh.get("status") in ("current", "stale"):
        detail = f"（应到 {fresh.get('expectedTargetDate')}，实际 {fresh.get('actualTargetDate') or '未知'}）"
    snapshot = {True: "；快照：需运行", False: "；快照：休市无需运行"}.get(fresh.get("snapshotRequired"), "")
    return [f"供应链新鲜度：{labels.get(fresh.get('status'), '无法判定')}{detail}{snapshot}"]


def message(info, hostname="unknown", unit_state="unknown", now=None, entry_reason="scheduled_audit",
            decision=None):
    now = now or dt.datetime.now(CST)
    status = info.get("status") if info.get("status") in STATUS_VALUES else "unknown"
    state = unit_state if unit_state in UNIT_STATES else "unknown"
    reason = info.get("reason") if isinstance(info.get("reason"), str) and REASON_RE.fullmatch(info["reason"]) else "none"
    codes = ",".join(info.get("codes") or []) or "none"
    generated = info.get("generatedAt") if _instant(info.get("generatedAt")) else "unknown"
    entry = entry_reason if isinstance(entry_reason, str) and REASON_RE.fullmatch(entry_reason) else "scheduled_audit"
    safe_host = hostname if isinstance(hostname, str) and re.fullmatch(r"[\w.-]{1,64}", hostname) else "unknown"
    text = "\n".join([
        f"research-data 健康告警｜{now.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"状态：{status}",
        *_run_line(info),
        *_freshness_line(info),
        *_change_lines(decision),
        f"审计单元：{state}",
        f"触发：{entry}",
        f"问题：{codes}",
        _retained_line(info),
        f"审计时间：{generated}",
        f"数据新鲜度：{'陈旧' if info.get('stale') else '当次'}（{reason}）",
        f"主机：{safe_host}",
        "说明：只读审计，不自动恢复；仅在故障集合变化时通知，审计原始级别不变；本消息由发送回执确认。",
    ])
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS - 1] + "…"
    return text


def should_alert(info, unit_state, previous=None, now=None):
    """Notify only when the alert-worthy fault set changed.

    Without a committed previous state every current fault counts as new, so
    stale/unreadable output, a failed audit unit or critical issues still alert
    (fail toward alerting). With a state, an unchanged critical set is silent;
    new/worsened/recovered/should-run-but-did-not are notified.
    """
    return decide(info, unit_state, previous, now)["notify"]


def parse_receipt(raw, exit_code):
    """Return {'delivered': bool, 'reason': code, 'receipt': allowlisted}.

    delivered here means this Hermes JSON says success, is not skipped, and has a
    non-empty message_id. It does not prove the platform, the expected chat, or
    that the recipient binding matches the prepared body.
    """
    receipt = {"schemaVersion": RECEIPT_SCHEMA_VERSION, "hermesExitCode": int(exit_code)}
    if not isinstance(raw, (bytes, bytearray)):
        return {"delivered": False, "reason": "receipt_unreadable", "receipt": receipt}
    if len(raw) > MAX_RECEIPT_BYTES:
        return {"delivered": False, "reason": "receipt_too_large", "receipt": receipt}
    receipt["receiptSha256"] = hashlib.sha256(bytes(raw)).hexdigest()
    if int(exit_code) != 0:
        return {"delivered": False, "reason": "hermes_exit_nonzero", "receipt": receipt}
    try:
        payload = json.loads(bytes(raw).decode("utf-8"),
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError):
        return {"delivered": False, "reason": "receipt_invalid_json", "receipt": receipt}
    if not isinstance(payload, dict):
        return {"delivered": False, "reason": "receipt_not_object", "receipt": receipt}
    receipt["success"] = payload.get("success") is True
    receipt["skipped"] = payload.get("skipped") is True
    if payload.get("success") is not True:
        return {"delivered": False, "reason": "receipt_not_success", "receipt": receipt}
    if payload.get("skipped") is True:
        # A skip is not a delivery.
        return {"delivered": False, "reason": "receipt_skipped", "receipt": receipt}
    message_id = payload.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)) or not str(message_id).strip():
        return {"delivered": False, "reason": "receipt_missing_message_id", "receipt": receipt}
    text = str(message_id)
    receipt["messageId"] = text[:128]
    target = payload.get("target")
    if isinstance(target, str) and 0 < len(target) <= 128 and re.fullmatch(r"[\w:.#@+-]{1,128}", target):
        receipt["target"] = target
    return {"delivered": True, "reason": "delivered", "receipt": receipt}


def write_receipt(path, receipt):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("receipt_path_not_regular_file")
    body = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _shell_lines(info, unit_state, decision=None):
    status = info.get("status") if info.get("status") in STATUS_VALUES else "unknown"
    codes = ",".join(code for code in (info.get("codes") or []) if CODE_RE.fullmatch(code))
    generated = info.get("generatedAt") if _instant(info.get("generatedAt")) else "unknown"
    reason = info.get("reason") if isinstance(info.get("reason"), str) and REASON_RE.fullmatch(info["reason"]) else "none"
    state = unit_state if unit_state in UNIT_STATES else "unknown"
    retained = (info or {}).get("retained") or {}
    if decision is None:
        decision = decide(info, unit_state, None)
    lines = [f"STATUS={status}", f"CODES={codes}", f"GENERATED_AT={generated}",
             f"RETAINED_GAP_DAYS={int(retained.get('unresolvedTradingDayGaps') or 0)}",
             f"RETAINED_LEGACY_DAYS={int(retained.get('legacyGapDays') or 0)}",
             f"RETAINED_LATER_DAYS={int(retained.get('laterRegisteredGapDays') or 0)}",
             f"UNCONFIRMED_DAYS={int(retained.get('unconfirmedDays') or 0)}",
             f"STALE={'yes' if info.get('stale') else 'no'}", f"REASON={reason}",
             f"UNIT_STATE={state}",
             f"DECISION={decision['kind']}",
             f"NEW_COUNT={len(decision['new'])}", f"WORSENED_COUNT={len(decision['worsened'])}",
             f"RECOVERED_COUNT={len(decision['recovered'])}",
             f"FINGERPRINT={decision['fingerprint']}",
             f"ALERT={'yes' if decision['notify'] else 'no'}"]
    return lines


def prepare(path, state_file, pending_file, message_file, *, max_age_hours=6.0,
            unit_state="unknown", hostname="unknown", entry_reason="scheduled_audit",
            retained_path=None, now=None):
    """Bind the decision, pending state and message to one observed snapshot.

    The caller holds the notification-state lock through confirmed delivery and
    commit. Preparation never commits the notification state. Any preparation
    failure must abort delivery; the wrapper owns and removes the unique staging
    directory, including any partially prepared files.
    """
    paths = [Path(value).absolute() for value in (state_file, pending_file, message_file)]
    if len(set(paths)) != len(paths):
        raise ValueError("prepare_paths_not_distinct")
    now = now or dt.datetime.now(CST)
    info = describe(path, now=now, max_age_hours=max_age_hours, retained_path=retained_path)
    previous = read_state(state_file)
    decision = decide(info, unit_state, previous, now=now)
    text = message(info, hostname=hostname, unit_state=unit_state, now=now,
                   entry_reason=entry_reason, decision=decision)
    write_state(pending_file, decision["state"])
    # Exclusive creation is safe inside the wrapper's unique private directory;
    # it also refuses an unexpected pre-existing file or symlink.
    with Path(message_file).open("x", encoding="utf-8") as stream:
        stream.write(text + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return _shell_lines(info, unit_state, decision)


def _state_sha(state):
    if state is None:
        return None
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _bound_delivery(path):
    """Read only a confirmed receipt bound to the exact prepared state."""
    if not path or not Path(path).exists():
        return None
    file = Path(path)
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_STATE_BYTES + MAX_RECEIPT_BYTES:
        raise ValueError("delivery_receipt_not_regular_or_bounded")
    data = json.loads(file.read_text(encoding="utf-8"))
    # Legacy and explicitly failed receipts have no recovery authority.
    if not isinstance(data, dict) or "deliveryState" not in data or data.get("delivered") is not True:
        return None
    if (data.get("hermesExitCode") != 0 or data.get("success") is not True
            or data.get("skipped") is not False or data.get("reason") != "delivered"
            or not isinstance(data.get("messageId"), str) or not data["messageId"].strip()
            or type(data.get("stateApplied")) is not bool or not _valid_state(data["deliveryState"])
            or data.get("deliveryStateSha256") != _state_sha(data["deliveryState"])
            or "previousStateSha256" not in data):
        raise ValueError("delivery_receipt_binding_invalid")
    return data


def _inflight_delivery(path):
    if not path:
        return None
    file = Path(path)
    if not file.exists() and not file.is_symlink():
        return None
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_STATE_BYTES + MAX_RECEIPT_BYTES:
        raise ValueError("inflight_not_regular_or_bounded")
    data = json.loads(file.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("kind") != "health-alert.delivery-inflight.v1"
            or not _valid_state(data.get("deliveryState"))
            or data.get("deliveryStateSha256") != _state_sha(data["deliveryState"])
            or "previousStateSha256" not in data):
        raise ValueError("inflight_binding_invalid")
    return data


def _sync_directory(path):
    descriptor = os.open(str(Path(path).parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def begin_delivery(pending_file, state_file, inflight_file):
    """Persist local intent before starting a possibly side-effecting sender."""
    if Path(inflight_file).exists() or Path(inflight_file).is_symlink():
        raise ValueError("inflight_already_exists")
    state = read_state(pending_file)
    if state is None:
        raise ValueError("inflight_pending_state_invalid")
    marker = {"schemaVersion": 1, "kind": "health-alert.delivery-inflight.v1",
              "deliveryState": state, "deliveryStateSha256": _state_sha(state),
              "previousStateSha256": _state_sha(read_state(state_file)),
              "startedAt": dt.datetime.now(CST).isoformat()}
    write_receipt(inflight_file, marker)
    _sync_directory(inflight_file)


def _clear_inflight(path, target_sha):
    marker = _inflight_delivery(path)
    if marker is None:
        return
    if marker["deliveryStateSha256"] != target_sha:
        raise ValueError("inflight_delivery_mismatch")
    Path(path).unlink()
    _sync_directory(path)


def _mark_delivery_applied(receipt_file, state):
    receipt = _bound_delivery(receipt_file)
    if receipt is None or receipt["stateApplied"]:
        return
    if receipt["deliveryStateSha256"] != _state_sha(state):
        raise ValueError("delivery_receipt_state_mismatch")
    receipt["stateApplied"] = True
    write_receipt(receipt_file, receipt)


def recover_delivery(receipt_file, state_file, inflight_file=None):
    """Apply an already confirmed local delivery before any later send decision.

    This recovers only receipts successfully written locally. Remote success
    followed by a missing local receipt remains unknown, not server idempotency.
    """
    marker = _inflight_delivery(inflight_file)
    receipt = _bound_delivery(receipt_file)
    if marker and (receipt is None or marker["deliveryStateSha256"] != receipt["deliveryStateSha256"]
                   or marker["previousStateSha256"] != receipt["previousStateSha256"]):
        raise ValueError("delivery_outcome_unknown")
    if receipt is None:
        return False
    current_sha = _state_sha(read_state(state_file))
    target_sha = receipt["deliveryStateSha256"]
    if receipt["stateApplied"]:
        if marker:
            if current_sha != target_sha:
                raise ValueError("delivery_recovery_state_changed")
            _clear_inflight(inflight_file, target_sha)
            return True
        return False
    if current_sha not in (receipt["previousStateSha256"], target_sha):
        raise ValueError("delivery_recovery_state_changed")
    if current_sha != target_sha:
        write_state(state_file, receipt["deliveryState"])
    _mark_delivery_applied(receipt_file, receipt["deliveryState"])
    _clear_inflight(inflight_file, target_sha)
    return True


def _record_receipt(args):
    try:
        raw = Path(args.json_file).read_bytes()
    except OSError:
        raw = b""
    result = parse_receipt(raw, args.exit_code)
    receipt = {**result["receipt"], "delivered": result["delivered"], "reason": result["reason"],
               "checkedAt": dt.datetime.now(CST).isoformat()}
    marker = None
    try:
        if args.inflight_file:
            marker = _inflight_delivery(args.inflight_file)
            if marker is None:
                raise ValueError("delivery_inflight_missing")
        if result["delivered"] and args.pending_file:
            state = read_state(args.pending_file)
            if state is None or not args.previous_state_file:
                raise ValueError("delivery_pending_state_invalid")
            receipt.update(deliveryState=state, deliveryStateSha256=_state_sha(state),
                           previousStateSha256=_state_sha(read_state(args.previous_state_file)), stateApplied=False)
            if marker and (marker["deliveryStateSha256"] != receipt["deliveryStateSha256"]
                           or marker["previousStateSha256"] != receipt["previousStateSha256"]):
                raise ValueError("delivery_receipt_inflight_mismatch")
        write_receipt(args.state_file, receipt)
        if args.inflight_file:
            _sync_directory(args.state_file)
            # Explicit skip proves this invocation did not send. All other
            # unconfirmed outcomes retain the marker for reconciliation.
            if result["reason"] == "receipt_skipped":
                _clear_inflight(args.inflight_file, marker["deliveryStateSha256"])
        stored = "yes"
    except (OSError, ValueError):
        stored = "no"
    print(f"DELIVERED={'yes' if result['delivered'] else 'no'}")
    print(f"RECEIPT_REASON={result['reason']}")
    print(f"RECEIPT_STORED={stored}")
    if args.inflight_file and not (result["delivered"] and stored == "yes"):
        print("DELIVERY_OUTCOME=" + ("not_sent" if result["reason"] == "receipt_skipped" and stored == "yes" else "unknown"))
    return 0 if result["delivered"] and stored == "yes" else 1



def scheduled_audit_status(path, now=None):
    """Whether today's 22:45 audit artifact exists. Does not classify faults."""
    now = (now or dt.datetime.now(CST)).astimezone(CST)
    due = dt.datetime.combine(now.date(), dt.time(22, 45), tzinfo=CST)
    if now < due:
        return "not_due"
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return "missing"
    generated = _instant(data.get("generatedAt")) if isinstance(data, dict) else None
    if generated is None or not due <= generated.astimezone(CST) <= now:
        return "missing"
    return "fresh"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("describe", "message"):
        p = sub.add_parser(name)
        p.add_argument("--json", dest="path", required=True)
        p.add_argument("--max-age-hours", type=float, default=6.0)
        p.add_argument("--unit-state", default="unknown")
        p.add_argument("--state-file", default=None)
        p.add_argument("--retained", default=None)
        if name == "describe":
            p.add_argument("--pending-file", default=None)
        else:
            p.add_argument("--host", default="unknown")
            p.add_argument("--entry-reason", default="scheduled_audit")
    commit_parser = sub.add_parser("commit-state")
    commit_parser.add_argument("--pending-file", required=True)
    commit_parser.add_argument("--state-file", required=True)
    commit_parser.add_argument("--receipt-file", default=None)
    commit_parser.add_argument("--inflight-file", default=None)
    receipt_parser = sub.add_parser("receipt")
    receipt_parser.add_argument("--json-file", required=True)
    receipt_parser.add_argument("--exit-code", type=int, required=True)
    receipt_parser.add_argument("--state-file", required=True)
    receipt_parser.add_argument("--pending-file", default=None)
    receipt_parser.add_argument("--previous-state-file", default=None)
    receipt_parser.add_argument("--inflight-file", default=None)
    begin_parser = sub.add_parser("begin-delivery")
    begin_parser.add_argument("--pending-file", required=True)
    begin_parser.add_argument("--state-file", required=True)
    begin_parser.add_argument("--inflight-file", required=True)
    recover_parser = sub.add_parser("recover-delivery")
    recover_parser.add_argument("--receipt-file", required=True)
    recover_parser.add_argument("--state-file", required=True)
    recover_parser.add_argument("--inflight-file", default=None)
    prepare_parser = sub.add_parser("prepare")
    audit_parser = sub.add_parser("scheduled-audit")
    audit_parser.add_argument("--json", dest="path", required=True)
    prepare_parser.add_argument("--json", dest="path", required=True)
    prepare_parser.add_argument("--state-file", required=True)
    prepare_parser.add_argument("--pending-file", required=True)
    prepare_parser.add_argument("--message-file", required=True)
    prepare_parser.add_argument("--max-age-hours", type=float, default=6.0)
    prepare_parser.add_argument("--unit-state", default="unknown")
    prepare_parser.add_argument("--retained", default=None)
    prepare_parser.add_argument("--host", default="unknown")
    prepare_parser.add_argument("--entry-reason", default="scheduled_audit")
    args = parser.parse_args(argv)

    if args.command == "scheduled-audit":
        print(scheduled_audit_status(args.path))
        return 0
    if args.command == "begin-delivery":
        try:
            begin_delivery(args.pending_file, args.state_file, args.inflight_file)
        except (OSError, ValueError):
            print("DELIVERY_STARTED=no")
            return 1
        print("DELIVERY_STARTED=prepared")
        return 0
    if args.command == "recover-delivery":
        try:
            recovered = recover_delivery(args.receipt_file, args.state_file, args.inflight_file)
        except (OSError, ValueError, UnicodeError):
            print("DELIVERY_RECOVERED=no (failed)")
            return 1
        print(f"DELIVERY_RECOVERED={'yes' if recovered else 'not_needed'}")
        return 0
    if args.command == "prepare":
        try:
            lines = prepare(args.path, args.state_file, args.pending_file, args.message_file,
                            max_age_hours=args.max_age_hours, unit_state=args.unit_state,
                            hostname=args.host, entry_reason=args.entry_reason, retained_path=args.retained)
        except (OSError, ValueError):
            print("PREPARED=no")
            return 1
        print("PREPARED=yes\n" + "\n".join(lines))
        return 0
    if args.command in ("describe", "message"):
        info = describe(args.path, max_age_hours=args.max_age_hours, retained_path=args.retained)
        decision = decide(info, args.unit_state, read_state(args.state_file))
        if args.command == "describe":
            if args.pending_file:
                try:
                    write_state(args.pending_file, decision["state"])
                except (OSError, ValueError):
                    pass  # no pending state => nothing is committed; next run re-decides
            print("\n".join(_shell_lines(info, args.unit_state, decision)))
            return 0
        print(message(info, hostname=args.host, unit_state=args.unit_state,
                      entry_reason=args.entry_reason, decision=decision))
        return 0
    if args.command == "commit-state":
        state = read_state(args.pending_file)
        if state is None:
            print("STATE_COMMITTED=no")
            return 1
        try:
            write_state(args.state_file, state)
            _mark_delivery_applied(args.receipt_file, state)
            _clear_inflight(args.inflight_file, _state_sha(state))
        except (OSError, ValueError):
            print("STATE_COMMITTED=no")
            return 1
        Path(args.pending_file).unlink(missing_ok=True)
        print("STATE_COMMITTED=yes")
        return 0
    return _record_receipt(args)


if __name__ == "__main__":
    raise SystemExit(main())
