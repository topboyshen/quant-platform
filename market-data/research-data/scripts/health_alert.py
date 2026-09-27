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
DUE_RUN_CODES = ("scheduled_snapshot_missing",)
CST = dt.timezone(dt.timedelta(hours=8))
RECEIPT_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
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
    """Explicit "should have run but did not" events, keyed by their due date.

    These are deliberately not critical atoms: the audit severity is unchanged and
    ordinary warnings still never page on their own. Only a new code+dueDate is a
    new event; repeated audits for the same due date collapse into one.
    """
    atoms = set()
    if isinstance(data, dict):
        for issue in data.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("code") not in DUE_RUN_CODES:
                continue
            code = issue["code"]
            for date in _issue_dates(issue):
                atoms.add(f"{code}|{date}")
    return sorted(atoms)


def _critical_atoms(data):
    """Alert-worthy fault atoms "code|date" from critical-severity issues only.

    warning/information issues stay in the audit (and on the dashboard) but, as
    before this change, do not page on their own.
    """
    atoms = set()
    if isinstance(data, dict):
        for issue in data.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("severity") != "critical":
                continue
            code = issue.get("code")
            code = code if isinstance(code, str) and CODE_RE.fullmatch(code) else "unrecognized_issue_code"
            for date in _issue_dates(issue):
                atoms.add(f"{code}|{date}")
                if len(atoms) >= MAX_ATOMS:
                    return sorted(atoms)
    return sorted(atoms)


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
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict) or issue.get("code") not in RETAINED_CODES:
            continue
        code = issue["code"]
        dates = issue.get("dates")
        if not isinstance(dates, list):
            dates = issue.get("affectedDates")
        observed = {d for d in dates if isinstance(d, str) and DATE_RE.fullmatch(d)} if isinstance(dates, list) else set()
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
    status = data.get("status")
    if not isinstance(status, str) or status not in STATUS_VALUES:
        result.update(status="unknown", reason="health_status_invalid")
    else:
        result["status"] = status
    result["codes"] = _codes(data)
    result["criticalAtoms"] = _critical_atoms(data)
    result["dueRunAtoms"] = _due_run_atoms(data)
    result["retained"] = _retained_counts(data, load_retained(retained_path))
    generated = _instant(data.get("generatedAt"))
    if generated is None:
        result.update(stale=True, reason=result["reason"] or "health_generated_at_invalid")
        return result
    result["generatedAt"] = data["generatedAt"]
    age = (now.astimezone(CST) - generated.astimezone(CST)).total_seconds() / 3600
    result["ageHours"] = round(age, 3)
    # A stale artifact means this alert is not describing the run that just
    # finished; say so instead of presenting old numbers as current.
    result["stale"] = age > float(max_age_hours) or age < -0.5
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
    atoms = data.get("atoms") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("schemaVersion") != STATE_SCHEMA_VERSION
            or not isinstance(atoms, list) or not all(isinstance(a, str) and "|" in a for a in atoms)
            or data.get("fingerprint") != fingerprint(atoms)):
        return None
    return data


def _split_code(atom):
    return atom.split("|", 1)[0]


def _is_observability(atom):
    """Audit-level atoms describe the audit itself, not the business data."""
    return _split_code(atom).startswith(OBSERVABILITY_PREFIX)


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
    if prev_atoms is None:
        added, recovered = sorted(effective), []
        kind = "first_observation" if effective else "no_fault"
    else:
        added = sorted(effective - prev_atoms)
        recovered = sorted(prev_atoms - effective)
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
    notify = bool(added or recovered)
    business = sorted(a for a in current if not _is_observability(a))
    state = {"schemaVersion": STATE_SCHEMA_VERSION, "atoms": sorted(effective),
             "fingerprint": fingerprint(sorted(effective)),
             "businessAtoms": business if audit_visible else sorted(prev_business or []),
             "auditVisible": audit_visible,
             "auditStatus": info.get("status") if info.get("status") in STATUS_VALUES else "unknown",
             # Evidence reference only; never part of the fingerprint.
             "evidence": {"auditGeneratedAt": info.get("generatedAt") if _instant(info.get("generatedAt")) else None,
                          "unitState": unit_state if unit_state in UNIT_STATES else "unknown"},
             "decision": kind, "decidedAt": now.isoformat()}
    return {"notify": notify, "kind": kind, "new": new_failure, "worsened": worsened,
            "recovered": recovered, "current": list(current), "fingerprint": state["fingerprint"],
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
                 "should_run_but_did_not": "应运行但未产生当次结果", "unchanged": "无变化",
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
    return lines


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
    """Return {'delivered': bool, 'reason': code, 'receipt': allowlisted}."""
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
    receipt_parser = sub.add_parser("receipt")
    receipt_parser.add_argument("--json-file", required=True)
    receipt_parser.add_argument("--exit-code", type=int, required=True)
    receipt_parser.add_argument("--state-file", required=True)
    args = parser.parse_args(argv)

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
        except (OSError, ValueError):
            print("STATE_COMMITTED=no")
            return 1
        Path(args.pending_file).unlink(missing_ok=True)
        print("STATE_COMMITTED=yes")
        return 0
    try:
        raw = Path(args.json_file).read_bytes()
    except OSError:
        raw = b""
    result = parse_receipt(raw, args.exit_code)
    receipt = {**result["receipt"], "delivered": result["delivered"], "reason": result["reason"],
               "checkedAt": dt.datetime.now(CST).isoformat()}
    try:
        write_receipt(args.state_file, receipt)
        stored = "yes"
    except (OSError, ValueError):
        stored = "no"
    print(f"DELIVERED={'yes' if result['delivered'] else 'no'}")
    print(f"RECEIPT_REASON={result['reason']}")
    print(f"RECEIPT_STORED={stored}")
    return 0 if result["delivered"] and stored == "yes" else 1


if __name__ == "__main__":
    raise SystemExit(main())
