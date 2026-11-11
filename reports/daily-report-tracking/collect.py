"""Bounded, read-only collection on Quant-Server; never generates source reports.

No credentials/config/env inspection, market requests, business runs, DB copies,
or production writes. Each source fails independently without invented zeroes.
The two reused modules are byte-pinned. Only their inspected pure/read-only
functions are called; no generator, persistence, capture, or renderer is called.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import types
import uuid
from zoneinfo import ZoneInfo

from report_binding import validate as validate_report_binding
from summary_availability import MAINTENANCE_SUFFIX
from etf_identity import FIELDS as ETF_IDENTITY_FIELDS, coverage_identity, run_identity
import etf_terminal_evidence as terminal_evidence
from readable_report import PRESENTATION_SCHEMA, retained_source

BEIJING = ZoneInfo("Asia/Shanghai")
ACCOUNT_ID = 1
ROOT = Path("/home/ubuntu/trading-radar")
STATE = ROOT / "data/profit-execution-control"
CAPTURE = ROOT / "data/research-validation/1"
OUTPUTS = Path("/home/ubuntu/.hermes/cron/output")
ORIGINALS = ROOT / "data/daily-research-summary/1/originals"
MANAGED_CONTROL = Path("/home/ubuntu/releases/daily-research-summary-20261012.1/control.json")
ETF_DB = ROOT / "data/swing_macro.db"
ETF_REPORT = ROOT / "data/eod_daily_report.json"
ETF_TERMINAL_ROOT = Path("/var/lib/quant-eod-evidence")
ETF_TERMINAL_RECEIPTS = ETF_TERMINAL_ROOT / "receipts"
PROFIT_HELPER = Path("/home/ubuntu/.hermes/scripts/profit_execution_evidence.py")
ETF_HELPER = ROOT / "radar/swing/daily_report.py"
PROFIT_HELPER_SHA = "cf005051d3b37ae7641a87defba02f93a62ae3550b20eef78e7f84e8a27c251d"
ETF_HELPER_SHA = "274b58de7c968aaf96e3792d9776e087d443866f2a73638779de82dfbb351041"
PROTOCOL_SHA = "44139ddf6df80b974275cda9111b2407d651c809bb61b764aaeb49f56d72eabf"
MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 2000
MAX_CANDIDATES = 2000
MAX_OUTCOMES = MAX_CANDIDATES * 5
MAX_RUNS_PER_DAY = 64
RESEARCH_JOB = "ec6c86fd856c"
HEALTH_JOB = "026a1083d9e0"
PG_COMMAND = ["/usr/bin/docker", "exec", "radar-quantdinger-postgres", "psql",
              "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", "quantdinger", "-d", "quantdinger"]
CANDIDATE_SQL = """SELECT substr(d.decision_id,1,129) AS decision_id,
 substr(d.run_id,1,129) AS run_id, substr(r.run_id,1,129) AS joined_run_id,
 substr(d.code,1,129) AS code, substr(d.decision_date,1,129) AS decision_date,
 substr(d.decision_at,1,129) AS decision_at, substr(d.validation_split,1,129) AS validation_split,
 d.raw_trigger_count, substr(r.status,1,129) AS run_status,
 substr(r.started_at,1,129) AS run_started_at
 FROM eod_target_decision d LEFT JOIN swing_eod_run r ON r.run_id=d.run_id
 WHERE d.raw_trigger_count>0 AND d.decision_state='allowed'"""


class SourceError(ValueError):
    """Only fixed codes from this module may appear in the public error result."""


def _require(condition, code):
    if not condition:
        raise SourceError(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "timestamp_timezone_missing")
    return parsed


def _utc_now():
    return datetime.now(timezone.utc)


def _read_bytes(path, limit=MAX_BYTES):
    path = Path(path)
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "linked_source_path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "source_file_limit")
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, "source_file_limit")
    return raw


def _json(raw):
    def reject(_):
        raise SourceError("nonfinite_json")
    return json.loads(raw, parse_constant=reject)


def _read_json(path):
    raw = _read_bytes(path)
    return _json(raw), {"path": str(path), "sha256": _sha(raw), "bytes": len(raw)}


def _module(path, expected):
    raw = _read_bytes(path)
    _require(_sha(raw) == expected, "source_module_hash_changed")
    # Compile in memory avoids creating __pycache__ in the production source tree.
    module = types.ModuleType("_daily_summary_readonly_" + path.stem)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def _pg_json(select, *, timeout_seconds=5):
    _require(select.lstrip().upper().startswith("SELECT ") and ";" not in select,
             "readonly_query_required")
    _require(timeout_seconds in (3, 5), "readonly_timeout_invalid")
    sql = (f"BEGIN READ ONLY; SET LOCAL statement_timeout='{timeout_seconds}s'; SET LOCAL lock_timeout='1s'; "
           "SET LOCAL TIME ZONE 'Asia/Shanghai'; " + select + "; ROLLBACK;")
    result = subprocess.run(PG_COMMAND + ["-c", sql], capture_output=True, timeout=12, check=False)
    _require(result.returncode == 0 and len(result.stdout) <= MAX_BYTES, "postgres_read_unavailable")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    _require(len(lines) == 1, "postgres_result_shape")
    return _json(lines[0])


def _attempt(execution_id):
    _require(str(uuid.UUID(execution_id)) == execution_id, "execution_uuid_invalid")
    rows = _pg_json("SELECT COALESCE(json_agg(x),'[]'::json) FROM (SELECT execution_id, "
                   "run_id, user_id, generated_at, signal_date, status, release_sha256, "
                   "result_document AS result FROM qd_profit_research_run_attempts "
                   "WHERE user_id=1 AND execution_id='" + execution_id + "'::uuid "
                   "AND octet_length(result_document::text)<=1048576 LIMIT 2) x")
    _require(isinstance(rows, list) and len(rows) == 1 and rows[0].get("user_id") == ACCOUNT_ID,
             "scoped_attempt_missing_or_ambiguous")
    return rows[0]


def _managed_bytes(path, limit=MAX_BYTES, *, required_mode=None):
    """Private immutable metadata/body read; never imports or invokes a sender."""
    path = Path(path)
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "managed_linked_path")
    before = path.lstat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                           s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
             and before.st_uid == os.getuid() and not before.st_mode & 0o077
             and (required_mode is None or stat.S_IMODE(before.st_mode) == required_mode)
             and before.st_size <= limit, "managed_file_identity")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require(signature(before) == signature(os.fstat(fd)), "managed_source_race")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            _require(total <= limit, "managed_source_budget")
        _require(signature(before) == signature(os.fstat(fd)) == signature(path.lstat()),
                 "managed_source_race")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _managed_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, "managed_duplicate_json_key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(SourceError("managed_nonfinite_json")))


def _managed_report(job, day, cutoff, *, expected_dispatch=None):
    """Resolve exact scheduler silence to its separately archived business body.

    Receipt mtime is only a bounded enumeration hint, never a business date or
    authorization proof. A missing/misdated hint can only make this fail closed.
    Every accepted record independently binds businessDate, generatedAt, job,
    dispatch, configured target and all archived byte hashes.
    """
    _require(job in (RESEARCH_JOB, HEALTH_JOB), "managed_job_invalid")
    directory = ORIGINALS / job
    _require(not any(p.is_symlink() for p in (directory, *directory.parents)), "managed_linked_path")
    info = directory.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
             and not info.st_mode & 0o077, "managed_directory_identity")
    if expected_dispatch is not None:
        _require(isinstance(expected_dispatch, str) and re.fullmatch(r"[0-9a-f]{32}", expected_dispatch),
                 "managed_dispatch_invalid")
        paths = [directory / (expected_dispatch + ".managed-delivery.json")]
    else:
        paths, entries = [], 0
        floor = datetime.combine(date.fromisoformat(day), day_time.min, BEIJING).timestamp()
        with os.scandir(directory) as stream:
            for entry in stream:
                entries += 1
                _require(entries <= 8192, "managed_archive_entry_budget")
                if entry.name.endswith(".managed-delivery.json"):
                    _require(re.fullmatch(r"[0-9a-f]{32}\.managed-delivery\.json", entry.name),
                             "managed_archive_name_invalid")
                    if entry.stat(follow_symlinks=False).st_mtime >= floor:
                        paths.append(directory / entry.name)
                        _require(len(paths) <= 128, "managed_receipt_budget")
    candidates = []
    for path in paths:
        raw = _managed_bytes(path, 32768)
        value = _managed_json(raw)
        _require(isinstance(value, dict), "managed_receipt_shape")
        if value.get("businessDate") == day:
            candidates.append((path, raw, value))
    _require(len(candidates) == 1, "managed_receipt_missing_or_ambiguous")
    path, managed_raw, managed = candidates[0]
    dispatch = path.name.split(".", 1)[0]
    mode = "profit" if job == RESEARCH_JOB else "health"
    _require(managed.get("schema") == "quant.original-delivery.v1"
             and managed.get("jobId") == job and managed.get("reportRole") == mode
             and managed.get("dispatchId") == dispatch
             and managed.get("status") in ("sent", "unknown", "blocked", "duplicate")
             and managed.get("ownership") == "managed_original_sender_not_Hermes_delivery"
             and managed.get("scheduledOutputFile") == dispatch + ".managed.stdout.raw"
             and managed.get("scheduledOutputSha256") == _sha(b"[SILENT]\n"), "managed_receipt_identity")
    generated = _instant(managed.get("generatedAt"))
    _require(generated.astimezone(BEIJING).date().isoformat() == day and generated <= cutoff,
             "managed_receipt_time")
    _require(all(isinstance(managed.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", managed[k])
                 for k in ("sourceEnvelopeSha256", "sourceRawSha256", "originalReceiptSha256", "textSha256", "targetFingerprint")),
             "managed_receipt_hash_shape")
    control = _managed_json(_read_bytes(MANAGED_CONTROL, 32768))
    _require(isinstance(control, dict) and str(control.get("accountId")) == "1"
             and control.get("targetFingerprint") == managed["targetFingerprint"], "managed_target_binding")
    _require(_managed_bytes(directory / managed["scheduledOutputFile"], 64) == b"[SILENT]\n",
             "managed_scheduler_output_mismatch")
    if managed["status"] == "sent":
        _require(isinstance(managed.get("claimId"), str) and re.fullmatch(r"[0-9a-f]{32}", managed["claimId"])
                 and type(managed.get("messageId")) is int and managed["messageId"] > 0,
                 "managed_sent_proof_invalid")
    receipt_paths = [directory / (dispatch + ".json"),
                     directory / (day + "-" + dispatch + ".final-decision.json")]
    matches = []
    for receipt_path in receipt_paths:
        if receipt_path.exists() or receipt_path.is_symlink():
            receipt_raw = _managed_bytes(receipt_path)
            if _sha(receipt_raw) == managed["originalReceiptSha256"]:
                matches.append((receipt_path, receipt_raw, _managed_json(receipt_raw)))
    _require(len(matches) == 1, "managed_original_receipt_missing_or_ambiguous")
    receipt_path, receipt_raw, receipt = matches[0]
    final = receipt.get("schema") == "daily-summary-original-redecision.v1"
    envelope_name = (day + "-" + dispatch + ".final.envelope.raw" if final
                     else dispatch + ".envelope.raw")
    _require(receipt.get("schema") in ("daily-summary-original-envelope.v1", "daily-summary-original-envelope.v2",
                                         "daily-summary-original-redecision.v1")
             and receipt.get("mode") == mode and receipt.get("jobId") == job
             and receipt.get("businessDate") == day and receipt.get("dispatchId") == dispatch
             and type(receipt.get("returnCode")) is int and receipt["returnCode"] == 0
             and receipt.get("silent") is False
             and receipt.get("originalFile") == dispatch + ".raw"
             and receipt.get("envelopeFile") == envelope_name, "managed_original_receipt_identity")
    original = _managed_bytes(directory / receipt["originalFile"])
    envelope = _managed_bytes(directory / envelope_name)
    _require(_sha(original) == receipt.get("bodySha256") == managed["sourceRawSha256"]
             and len(original) == receipt.get("bodyBytes")
             and _sha(envelope) == receipt.get("envelopeSha256") == managed["sourceEnvelopeSha256"] == managed["textSha256"]
             and len(envelope) == receipt.get("envelopeBytes") == managed.get("textBytes"),
             "managed_original_content_mismatch")
    suffix = MAINTENANCE_SUFFIX if receipt.get("noticeKind") == "summary_maintenance_warning" else b""
    _require(envelope == original + suffix, "managed_original_envelope_invalid")
    return envelope.decode("utf-8"), {
        "path": str(path), "sha256": _sha(managed_raw), "bytes": len(managed_raw),
        "originalReceipt": {"path": str(receipt_path), "sha256": _sha(receipt_raw)},
        "sourceRawSha256": _sha(original), "sourceEnvelopeSha256": _sha(envelope),
        "dispatchId": dispatch, "status": managed["status"],
        "targetFingerprint": managed["targetFingerprint"], "generatedAt": managed["generatedAt"],
        "scope": "archived_business_envelope_not_Hermes_scheduling_body",
    }


def _report(job, day, cutoff, *, expected_dispatch=None):
    paths = sorted((OUTPUTS / job).glob(day + "_*.md"))
    _require(len(paths) == 1, "original_report_missing_or_ambiguous")
    raw = _read_bytes(paths[0])
    text = raw.decode("utf-8")
    runs = re.findall(r"^\*\*Run Time:\*\* (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$", text, re.M)
    _require(len(runs) == 1 and "**Job ID:** " + job in text, "original_report_identity")
    run_time = datetime.fromisoformat(runs[0]).replace(tzinfo=BEIJING)
    _require(run_time.date().isoformat() == day and run_time <= cutoff, "original_report_time")
    report = {"path": str(paths[0]), "sha256": _sha(raw), "bytes": len(raw),
              "reportRunTime": run_time.isoformat()}
    head, separator, body = text.partition("\n---\n")
    if separator and body.strip() == "[SILENT]":
        envelope, reference = _managed_report(job, day, cutoff, expected_dispatch=expected_dispatch)
        report.update(recognizedEnvelope="hermes_managed_silence+verified_archive",
                      schedulingBodySha256=_sha(body.encode("utf-8")), managedArchive=reference)
        text = head + separator + envelope
    return text, report


def resolve_attempt(day: str, now: datetime) -> dict:
    """Resolve the explicit day's committed attempt without requiring an archive.

    This primitive is safe immediately after the existing research command has
    completed. Its reservation/handoff and receipt must already exist; it never
    selects a latest row and never triggers a missing execution.
    """
    cutoff = now
    _require(isinstance(cutoff, datetime) and cutoff.tzinfo is not None
             and date.fromisoformat(day).isoformat() == day, "invalid_attempt_day_or_clock")
    helper = _module(PROFIT_HELPER, PROFIT_HELPER_SHA)
    handoff = helper.handoff_for_day(STATE, ACCOUNT_ID, day, now=cutoff)
    _require(handoff["market_date"] == day and handoff["exit_code"] == 0, "handoff_day_or_exit")
    value = helper.validate_attempt(_attempt(handoff["execution_id"]), handoff["execution_id"])
    helper.verify_handoff_attempt(handoff, value)
    _require(value["market_date"] == day and _instant(value["generated_at"]) <= cutoff
             and _instant(value["generated_at"]).astimezone(BEIJING).date().isoformat() == day,
             "profit_source_time")
    result = value["result"]
    candidates = result.get("candidates")
    _require(isinstance(candidates, list), "candidate_list_missing")
    status = result.get("status")
    _require(status in ("DATA_BLOCKED", "CASH", "PAPER_CANDIDATES")
             and result.get("data_ready") is (status != "DATA_BLOCKED")
             and (status == "PAPER_CANDIDATES" or not candidates), "profit_business_state_invalid")
    receipt, receipt_reference = _read_json(STATE / (value["execution_id"] + ".receipt.json"))
    for field in ("execution_id", "request_sha256", "core_sha256", "package_sha256", "market_date"):
        _require(receipt.get(field) == handoff.get(field), "profit_receipt_identity_mismatch")
    _require(receipt.get("user_id") == ACCOUNT_ID and receipt.get("state") == "COMPLETED"
             and type(receipt.get("exit_code")) is int and receipt["exit_code"] == 0
             and receipt.get("business_run_id") == value["business_run_id"]
             and _instant(receipt["generated_at"]) == _instant(value["generated_at"]),
             "profit_receipt_state_mismatch")
    source_report = receipt.get("report")
    # The immutable attempt establishes business status; human candidate cards
    # do not print its machine token. Bind the receipt text to the same execution
    # and generation time, then _collect_profit compares the archived body.
    _require(isinstance(source_report, str)
             and re.findall(r"^执行编号：([^\r\n]+)$", source_report, re.M) == [value["execution_id"]]
             and re.findall(r"^生成：([^\r\n]+)$", source_report, re.M) == [result["generated_at"]],
             "profit_receipt_report_mismatch")
    return {"verified": True, "accountId": ACCOUNT_ID, "executionId": value["execution_id"],
            "state": "COMMITTED", "dispatchId": handoff["dispatch_id"], "routine": True,
            "runId": value["business_run_id"], "marketDate": value["market_date"],
            "generatedAt": value["generated_at"], "releaseSha256": value["release_sha256"],
            "resultSha256": value["result_sha256"], "status": result["status"],
            "dataReady": result.get("data_ready"), "candidateCount": len(candidates),
            "candidateIdentities": [
                {"code": row.get("code"), "name": row.get("name"), "symbol": row.get("symbol"),
                 "signalDate": row.get("signal", {}).get("date") if isinstance(row.get("signal"), dict) else None}
                if isinstance(row, dict) else None for row in candidates],
            "reason": result.get("reason"), "diagnostics": result.get("diagnostics"),
            "sourceReport": source_report, "sourceReportSha256": _sha(source_report.encode("utf-8")),
            "evidence": {"receipt": receipt_reference, "handoffContentSha256": handoff["content_sha256"],
                         "helperSha256": PROFIT_HELPER_SHA, "kind": "immutable_execution_snapshot"}}


def collect_profit_evidence(*, account_id=1, business_date, as_of):
    """Wrapper-facing fail-closed evidence lookup; raw errors are never exposed."""
    try:
        _require(type(account_id) is int and account_id == ACCOUNT_ID, "explicit_account_one_required")
        now = _instant(as_of) if isinstance(as_of, str) else as_of
        return resolve_attempt(business_date, now)
    except Exception as exc:
        return {"verified": False, "state": "UNVERIFIED", "accountId": account_id,
                "sourceErrors": [{"source": "profit", "code": str(exc) if isinstance(exc, SourceError)
                                  else "source_read_or_integrity_failure", "errorType": type(exc).__name__}]}


def _final_transport_decision(job, day, body, *, expected_dispatch=None, expected_original=None):
    """Validate an optional append-only post-archive decision for this exact report."""
    directory = ORIGINALS / job
    paths = sorted(directory.glob(day + "-*.final-decision.json"))
    if not paths:
        return None
    _require(len(paths) == 1, "transport_final_decision_ambiguous")
    final, final_ref = _read_json(paths[0])
    dispatch = uuid.UUID(final["dispatchId"]).hex
    stem = day + "-" + dispatch
    _require(paths[0].name == stem + ".final-decision.json"
             and final.get("schema") == "daily-summary-original-redecision.v1"
             and final.get("jobId") == job and final.get("businessDate") == day
             and final.get("silent") is False and final.get("returnCode") == 0
             and final.get("noticeKind") == "summary_maintenance_warning"
             and final.get("handoffStatus") == "unavailable"
             and final.get("deliveryStatus") == "unconfirmed"
             and final.get("automaticResend") is False,
             "transport_final_decision_identity")
    if expected_dispatch is not None:
        _require(dispatch == uuid.UUID(expected_dispatch).hex, "transport_final_dispatch_mismatch")
    _require(final.get("priorReceiptFile") == dispatch + ".json"
             and final.get("originalFile") == dispatch + ".raw"
             and final.get("envelopeFile") == stem + ".final.envelope.raw",
             "transport_final_path_invalid")
    prior_raw = _read_bytes(directory / final["priorReceiptFile"])
    _require(_sha(prior_raw) == final.get("priorReceiptSha256"), "transport_prior_receipt_hash")
    prior = _json(prior_raw)
    _require(prior.get("schema") == "daily-summary-original-envelope.v1" and prior.get("silent") is True
             and prior.get("dispatchId") == dispatch and prior.get("businessDate") == day
             and prior.get("jobId") == job and prior.get("returnCode") == 0
             and prior.get("originalFile") == dispatch + ".raw"
             and prior.get("envelopeFile") == dispatch + ".envelope.raw",
             "transport_prior_receipt_identity")
    raw = _read_bytes(directory / final["originalFile"])
    old_envelope = _read_bytes(directory / prior["envelopeFile"])
    envelope = _read_bytes(directory / final["envelopeFile"])
    _require(_sha(raw) == prior.get("bodySha256") == final.get("bodySha256")
             and len(raw) == prior.get("bodyBytes") == final.get("bodyBytes")
             and old_envelope == raw + b"\n[SILENT]\n"
             and _sha(old_envelope) == prior.get("envelopeSha256") == final.get("priorEnvelopeSha256")
             and len(old_envelope) == prior.get("envelopeBytes")
             and envelope == raw + MAINTENANCE_SUFFIX
             and _sha(envelope) == final.get("envelopeSha256") and len(envelope) == final.get("envelopeBytes")
             and envelope.decode("utf-8").strip() == body.strip(),
             "transport_final_content_mismatch")
    if expected_original is not None:
        _require(raw.decode("utf-8").strip() == expected_original.strip(), "transport_original_binding_mismatch")
    return {"finalDecision": final_ref, "priorReceiptSha256": final["priorReceiptSha256"],
            "dispatchId": dispatch, "deliveryStatus": "unconfirmed", "automaticResend": False}


def _collect_profit(day, cutoff):
    value = resolve_attempt(day, cutoff)
    text, report = _report(RESEARCH_JOB, day, cutoff, expected_dispatch=value["dispatchId"])
    body = text.split("\n---\n", 1)[-1].strip()
    final_decision = _final_transport_decision(RESEARCH_JOB, day, body,
        expected_dispatch=value["dispatchId"], expected_original=value["sourceReport"])
    if final_decision:
        report["transportFinalDecision"] = final_decision
    report.setdefault("recognizedEnvelope", "hermes_markdown")
    if body.endswith("\n[SILENT]"):
        body = body[: -len("\n[SILENT]")].strip()
        report["recognizedEnvelope"] = "hermes_markdown+terminal_silent_line"
    elif body.endswith(MAINTENANCE_SUFFIX.decode("utf-8").rstrip("\n")):
        body = body[:-len(MAINTENANCE_SUFFIX.decode("utf-8").rstrip("\n"))].strip()
        report.update(recognizedEnvelope="hermes_markdown+fixed_maintenance_notice",
                      noticeKind="summary_maintenance_warning", handoffStatus="unavailable",
                      deliveryClass="operational_warning", deliveryStatus="unconfirmed",
                      automaticResend=False)
    _require(value["sourceReport"].strip() == body,
             "original_research_report_mismatch")
    value["evidence"]["originalReport"] = report
    # Preserve the exact, already matched original for the readable projection.
    # This is report line selection only: no candidate/research recomputation.
    value["readableReport"] = retained_source(value["sourceReport"], value)
    del value["sourceReport"]
    _attach_investigation(value)
    return value


def _effective_for_report(value, verified_on):
    """Only attach a dated investigation to a verified same-day execution."""
    try:
        local_day = _instant(value.get("generatedAt")).astimezone(BEIJING).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return False
    return value.get("marketDate") == local_day and local_day >= verified_on


def _attach_investigation(value):
    """Separate, dated diagnostic annotation; never amend immutable result data."""
    diagnostic = value.get("diagnostics") or {}
    if (diagnostic.get("kind") == "oos_holding_interval_gap"
            and diagnostic.get("symbol") == "sh688432"
            and diagnostic.get("interval_end") == "2026-08-31"
            and diagnostic.get("missing_end") is True):
        value["explanation"] = {
            "verified": True, "kind": "oos_holding_interval_gap", "symbol": "sh688432",
            "interval_end": "2026-08-31", "cause": "trading_suspension",
            "suspensionStart": "2026-08-31", "suspensionEnd": "2026-09-11", "resumedOn": "2026-09-14",
            "verifiedOn": "2026-10-12", "evidenceKind": "independent_company_announcement_diagnostic",
            "sourceReferences": [
                {"title": "有研硅停牌公告（2026-037），中国证券报信息披露版公司公告全文",
                 "url": "https://epaper.cs.com.cn/zgzqb/html/2026-08/29/nw.D110000zgzqb_20260829_1-B035.htm",
                 "publishedOn": "2026-08-29"},
                {"title": "有研硅复牌公告（2026-039），上海证券报信息披露版公司公告全文",
                 "url": "https://paper.cnstock.com/html/2026-09/12/content_2268083.htm",
                 "publishedOn": "2026-09-12"}],
            "limitation": "交易所索引已核对，PDF直链未读到；全文依据为证券报信息披露版公司正式公告。新调查仅作解释，不是原始行情输入。"}
    elif (value.get("verified") is True and value.get("status") == "DATA_BLOCKED"
          and value.get("dataReady") is False
          and diagnostic.get("kind") == "oos_holding_interval_gap"
          and diagnostic.get("symbol") == "sh600825"
          and diagnostic.get("interval_start") == "2026-09-29"
          and diagnostic.get("interval_end") == "2026-10-13"
          and diagnostic.get("missing_end") is True and diagnostic.get("is_last_period") is False
          and _effective_for_report(value, "2026-10-20")):
        value["explanation"] = {
            "verified": True, "kind": "oos_holding_interval_gap", "symbol": "sh600825",
            "interval_end": "2026-10-13", "cause": "trading_suspension",
            "suspensionStart": "2026-10-13", "suspensionEnd": "2026-10-15", "resumedOn": "2026-10-16",
            "verifiedOn": "2026-10-20", "evidenceKind": "independent_company_announcement_diagnostic",
            "sourceReferences": [
                {"title": "新华传媒股票交易风险提示暨停牌核查公告（2026-030）",
                 "url": "https://static.cninfo.com.cn/finalpage/2026-10-13/1225602155.PDF",
                 "publishedOn": "2026-10-13"},
                {"title": "新华传媒关于股票交易停牌核查结果、股票交易风险提示暨复牌的公告（2026-031）",
                 "url": "https://static.cninfo.com.cn/finalpage/2026-10-16/1225607741.PDF",
                 "publishedOn": "2026-10-16"}],
            "limitation": "公告核实该日停牌。供应商内部制表过程和策略的停牌成交、估值规则仍待另行核验。"}
    if (value.get("executionId") == "d301bde2-00a4-4efb-8d1c-f0e06c6e41b2"
            and value.get("resultSha256") == "2514fc04bec02b643b0701d80aba6a5c9c0b3d1b76c6aad2cbb63d54fc16b4a6"):
        value["sourceDiagnostics"] = {
            "verified": True, "executionId": value["executionId"], "observedOn": "2026-10-12",
            "items": [
                {"symbol": "sh688825", "kind": "adjustment_field_unverified", "dayRows": 50,
                 "listingDate": "2026-07-27", "minimumHistoryRows": 242,
                 "listingSource": "https://www.sse.com.cn/disclosure/announcement/listing/ipo/c/c_20260724_10826610.shtml",
                 "rawSha256": "bd66d4b1a30364af5fc02c81b8bdcec9e8e115da867c4d17fa022ca45f2b312e"},
                {"symbol": "sh688981", "kind": "adjustment_field_unverified", "dayRows": 642,
                 "rawSha256": "278b8c223a55e302732581c4c3692a91363f7e7a92199a4abb7e8336b1d273c1"},
                {"symbol": "sh688521", "kind": "adjustment_field_unverified", "dayRows": 642,
                 "rawSha256": "e8f133cc54183ab07cc1f71c92a67c2e1a7194b92a1db9e7acaf953a982ba1eb"}],
            "pending": ["核实长鑫科技、中芯国际、芯原股份缺失前复权字段的供应商口径；不能直接用原价替代",
                        "长鑫科技仅有上市后50根日线，仍不足既有研究历史门槛"],
            "policy": "这些是同一已留存执行的附加诊断，未改变其DATA_BLOCKED终态、resultSha256或解析结果。"}


def _collect_health(day, cutoff):
    helper = _module(PROFIT_HELPER, PROFIT_HELPER_SHA)
    text, report = _report(HEALTH_JOB, day, cutoff)
    final_decision = _final_transport_decision(HEALTH_JOB, day, text.split("\n---\n", 1)[-1])
    if final_decision:
        report["transportFinalDecision"] = final_decision
    if text.rstrip().endswith(MAINTENANCE_SUFFIX.decode("utf-8").rstrip("\n")):
        text = text.rstrip()[:-len(MAINTENANCE_SUFFIX.decode("utf-8").rstrip("\n"))]
        _require(MAINTENANCE_SUFFIX.decode("utf-8").strip() not in text
                 and not any(line.strip().upper() == "[SILENT]" for line in text.splitlines()),
                 "health_maintenance_envelope_invalid")
        report.update(recognizedEnvelope="hermes_markdown+fixed_maintenance_notice",
                      noticeKind="summary_maintenance_warning", handoffStatus="unavailable",
                      deliveryClass="operational_warning", deliveryStatus="unconfirmed",
                      automaticResend=False)
    reference = helper.parse_report_reference(text)
    _require(reference.get("state") == "REFERENCE_ONLY", "health_marker_invalid")
    value = helper.validate_attempt(_attempt(reference["execution_id"]), reference["execution_id"])
    for key in ("execution_id", "market_date", "release_sha256", "result_sha256"):
        _require(reference.get(key) == value.get(key), "health_marker_database_mismatch")
    _require(_instant(reference["generated_at"]) == _instant(value["generated_at"])
             and value["market_date"] == day and _instant(value["generated_at"]) <= cutoff,
             "health_source_time")
    status = value["result"]["status"]
    _require(re.findall(r"^状态：(\S+)$", text, re.M) == [status], "health_business_status_mismatch")
    return {"verified": True, "accountId": ACCOUNT_ID, "executionId": value["execution_id"],
            "marketDate": day, "resultSha256": value["result_sha256"], "status": status,
            "generatedAt": value["generated_at"], "releaseSha256": value["release_sha256"],
            "evidence": {"originalReport": report, "marker": reference, "databaseMatchVerified": True}}


@contextmanager
def _sqlite_readonly(path=ETF_DB):
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "linked_sqlite_path")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _etf_rows(connection, helper, day, cutoff):
    candidates = connection.execute(CANDIDATE_SQL + " LIMIT ?", (MAX_CANDIDATES + 1,)).fetchall()
    _require(len(candidates) <= MAX_CANDIDATES, "etf_candidate_limit")
    ids = [row["decision_id"] for row in candidates]
    _require(len(set(ids)) == len(ids), "etf_candidate_identity_duplicate")
    # Bound every returned collection used by the existing partition before calling it.
    outcome_count = connection.execute("SELECT count(*) FROM eod_decision_outcome o JOIN "
        "eod_target_decision d ON d.decision_id=o.decision_id "
        "WHERE d.raw_trigger_count>0 AND d.decision_state='allowed'").fetchone()[0]
    _require(outcome_count <= MAX_OUTCOMES, "etf_outcome_limit")
    eligible = [r for r in candidates if helper.classify_candidate(r, date.fromisoformat(day)) is None]
    for row in candidates:
        # A current DB cannot reconstruct a prior-day/as-of historical snapshot.
        _require(str(row["decision_date"]) <= day, "etf_future_candidate")
    eligible_ids = [r["decision_id"] for r in eligible]
    matured = {}
    outcomes = []
    if eligible_ids:
        placeholders = ",".join("?" * len(eligible_ids))
        outcomes = connection.execute("SELECT decision_id, horizon_sessions, evaluation_as_of, fill_status, "
            "net_return, excess_return, fees_return, slippage_return, mfe, mae FROM eod_decision_outcome "
            "WHERE decision_id IN (" + placeholders + ") LIMIT ?", (*eligible_ids, MAX_OUTCOMES + 1)).fetchall()
        _require(len(outcomes) <= MAX_OUTCOMES, "etf_outcome_limit")
        dates = {r["decision_id"]: date.fromisoformat(r["decision_date"]) for r in eligible}
        seen = set()
        for row in outcomes:
            identity = (row["decision_id"], row["horizon_sessions"])
            _require(identity not in seen, "etf_duplicate_horizon")
            seen.add(identity)
            if row["horizon_sessions"] in helper.HORIZONS and helper._mature_outcome(row, dates[row["decision_id"]], date.fromisoformat(day)):
                matured[row["decision_id"]] = max(matured.get(row["decision_id"], ""), str(row["evaluation_as_of"])[:10])
    partition = helper.research_partition(connection, now=cutoff)
    _require(partition["qualified"]["records"] == len(eligible)
             and partition["qualified"]["mature_records"] == len(matured)
             and sum(partition["not_counted"]["excluded_records"].values()) == len(candidates) - len(eligible),
             "etf_partition_identity_mismatch")
    runs = [dict(r) for r in connection.execute("SELECT run_id,decision_date,data_date,status,started_at,completed_at,"
        "target_count,evaluated_count,failed_count,signal_count,allowed_signal_count,blocked_signal_count,decision_outcome, "
        "substr(json_extract(details,'$.rule_attribution'),1,16385) AS rule_attribution_json "
        "FROM swing_eod_run WHERE decision_date=? ORDER BY started_at,run_id LIMIT ?",
        (day, MAX_RUNS_PER_DAY + 1)).fetchall()]
    _require(0 < len(runs) <= MAX_RUNS_PER_DAY, "etf_day_runs_missing_or_limit")
    for run in runs:
        _require(_instant(run["started_at"]) <= cutoff, "etf_run_after_cutoff")
        if run["completed_at"] is not None:
            _require(_instant(run["completed_at"]) <= cutoff, "etf_run_after_cutoff")
    return runs, {"candidateRecords": len(candidates), "eligibleMature": len(matured),
        "eligibleRecords": len(eligible), "excluded": len(candidates)-len(eligible),
        "todayNewEligible": sum(r["decision_date"] == day for r in eligible),
        "matureAsOf": max(matured.values()) if matured else None,
        "eligibleDecisionIds": eligible_ids, "matureDecisionIds": sorted(matured),
        "storedOutcomeRows": len(outcomes), "partition": partition,
        "helperSha256": ETF_HELPER_SHA}


def _saved_etf_runs(saved, report, runs, day):
    """Bind saved first/final projections to the same current stored runs.

    A report that predates the latest run cannot validate that newer run. Old
    report bytes remain untouched; a missing identity is not filled from DB.
    Mismatch diagnostics expose only fixed field names and content hashes.
    """
    saved_runs = [r for r in runs if r["completed_at"] and
                  _instant(r["completed_at"]) <= _instant(saved["generated_at"])]
    _require(saved_runs, "saved_etf_report_run_absent")
    _require(saved_runs[-1]["run_id"] == runs[-1]["run_id"],
             "saved_etf_report_stale_for_latest_run")
    attempts = saved.get("attempts")
    _require(isinstance(attempts, dict), "saved_etf_report_attempts_invalid")
    for key, actual in (("first", saved_runs[0]), ("latest", saved_runs[-1])):
        reported = attempts.get(key)
        _require(isinstance(reported, dict), "saved_etf_report_attempts_invalid")
        # Complete identity is mandatory for newly collected summaries. This
        # never retrospectively grants identity to old frozen summaries.
        try:
            expected = run_identity(actual, day)
            observed = run_identity(reported, day)
        except (ValueError, TypeError, OverflowError):
            raise SourceError("saved_etf_report_run_identity_invalid") from None
        for field in ETF_IDENTITY_FIELDS:
            if observed["result"][field] != expected["result"][field]:
                error = SourceError("saved_etf_report_run_mismatch")
                error.diagnostic = {
                    "schema": "quant.etf-report-mismatch.v1", "position": key,
                    "field": field, "savedReport": report,
                    "reportedIdentitySha256": observed["resultSha256"],
                    "storedIdentitySha256": expected["resultSha256"],
                }
                raise error
    counts = saved.get("counts")
    _require(isinstance(counts, dict) and type(counts.get("signals")) is int
             and counts["signals"] == saved_runs[-1]["signal_count"],
             "saved_etf_signal_count_mismatch")
    return coverage_identity(saved_runs[0], saved_runs[-1], day, ACCOUNT_ID)


def _stale_etf_report(saved, runs, day):
    """Only a proven older snapshot can use independent terminal corroboration.

    Completed contradictory results, absent run IDs and unknown schemas never
    qualify. No values are copied into the original saved report.
    """
    _require(type(saved.get("schema_version")) is int and saved["schema_version"] == 2,
             "saved_etf_report_stale_identity_unavailable")
    attempts = saved.get("attempts")
    _require(isinstance(attempts, dict), "saved_etf_report_attempts_invalid")
    generated = _instant(saved["generated_at"])
    by_id = {r["run_id"]: r for r in runs}
    _require(len(by_id) == len(runs), "etf_day_run_identity_duplicate")
    modes = []
    for key, expected in (("first", runs[0]), ("latest", None)):
        projected = attempts.get(key)
        _require(isinstance(projected, dict) and isinstance(projected.get("run_id"), str)
            and projected["run_id"] in by_id and projected.get("decision_date") == day,
            "saved_etf_report_stale_identity_unavailable")
        actual = by_id[projected["run_id"]]
        _require(expected is None or expected["run_id"] == actual["run_id"],
                 "saved_etf_report_stale_identity_conflict")
        _require(_instant(actual["started_at"]) <= generated, "saved_etf_report_stale_time_conflict")
        if projected.get("status") == "running" and projected.get("completed_at") is None:
            _require(projected.get("decision_outcome") == "not_evaluated"
                and _instant(actual["completed_at"]) > generated,
                "saved_etf_report_stale_time_conflict")
            modes.append("running_snapshot_before_terminal")
        else:
            try:
                _require(run_identity(projected, day) == run_identity(actual, day)
                    and _instant(actual["completed_at"]) <= generated,
                    "saved_etf_report_stale_identity_conflict")
            except (TypeError, ValueError):
                raise SourceError("saved_etf_report_stale_identity_conflict") from None
    _require(_instant(runs[-1]["completed_at"]) > generated,
             "saved_etf_report_not_proven_stale")
    if attempts["latest"]["run_id"] != runs[-1]["run_id"]:
        modes.append("older_snapshot_before_latest_terminal")
    _require(modes, "saved_etf_report_not_proven_stale")
    # Reject contradictory signal totals even when only the newer terminal is missing.
    if attempts["latest"].get("completed_at") is not None:
        _require(type(saved.get("counts", {}).get("signals")) is int
            and saved["counts"]["signals"] == attempts["latest"]["signal_count"],
            "saved_etf_signal_count_mismatch")
    return sorted(set(modes))


def _terminal_file(path, uid, gid, mode):
    """Read a bounded existing single-link regular file with stable identity."""
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "terminal_linked_path")
    before = path.lstat()
    signature = lambda m: (m.st_dev,m.st_ino,m.st_mode,m.st_uid,m.st_gid,m.st_nlink,
                            m.st_size,m.st_mtime_ns,m.st_ctime_ns)
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
        and (before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode)) == (uid,gid,mode)
        and before.st_size <= 16384, "terminal_file_identity")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require(signature(before) == signature(os.fstat(fd)), "terminal_file_race")
        raw = os.read(fd, 16385)
        _require(len(raw) == before.st_size and signature(before) == signature(os.fstat(fd))
            == signature(path.lstat()), "terminal_file_race")
    finally:
        os.close(fd)
    return raw, {"path": str(path), "sha256": _sha(raw), "bytes": len(raw)}


def _terminal_corrobation(runs, day, cutoff):
    """Read only the pinned post-activation immutable receipt chain; never run it."""
    try:
        deadline = time.monotonic() + 3.0
        for parent in (ETF_TERMINAL_ROOT, *ETF_TERMINAL_ROOT.parents):
            info = parent.lstat()
            _require(stat.S_ISDIR(info.st_mode) and not parent.is_symlink()
                and info.st_uid == 0 and not info.st_mode & 0o022, "terminal_parent_identity")
        directory = ETF_TERMINAL_RECEIPTS.lstat()
        _require(stat.S_ISDIR(directory.st_mode) and not ETF_TERMINAL_RECEIPTS.is_symlink()
            and (directory.st_uid,directory.st_gid,stat.S_IMODE(directory.st_mode)) == (1000,1001,0o700),
            "terminal_receipt_directory_identity")
        activation_raw, activation_ref = _terminal_file(ETF_TERMINAL_ROOT / "ACTIVATION.json",0,0,0o444)
        enabled = terminal_evidence.activation(activation_raw)
        unique_runs = {r["run_id"]: r for r in (runs[0],runs[-1])}
        matches = {key: [] for key in unique_runs}
        with os.scandir(ETF_TERMINAL_RECEIPTS) as entries:
            count = 0
            for entry in entries:
                count += 1
                _require(count <= 512 and time.monotonic() <= deadline, "terminal_directory_budget")
                for run_id in unique_runs:
                    if entry.name.startswith(day + "-" + run_id + "-") and entry.name.endswith(".json"):
                        matches[run_id].append(entry.name)
        receipts = []
        for run_id, run in unique_runs.items():
            _require(len(matches[run_id]) == 1, "terminal_receipt_missing_or_ambiguous")
            path = ETF_TERMINAL_RECEIPTS / matches[run_id][0]
            raw, ref = _terminal_file(path,1000,1001,0o444)
            proof = terminal_evidence.validate_receipt(terminal_evidence.strict_json(raw), path.name,
                                                       enabled,run,day,cutoff)
            receipts.append({**proof,"file":ref})
        after = ETF_TERMINAL_RECEIPTS.lstat()
        _require((directory.st_dev,directory.st_ino,directory.st_mtime_ns,directory.st_ctime_ns)
            == (after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns)
            and time.monotonic() <= deadline, "terminal_directory_changed_or_timeout")
        _require(_terminal_file(ETF_TERMINAL_ROOT / "ACTIVATION.json",0,0,0o444)[0] == activation_raw,
                 "terminal_activation_changed")
        return {"schema":"quant.etf-terminal-corroboration.v1","activation":activation_ref,
            "receipts":receipts,"countsAuthority":"current_same_run_database_projection",
            "limitation":"Journal receipt corroborates terminal identity, rule/source and recorded systemd invocation; scheduled versus manual trigger origin is not proved by this receipt. Its source-observation counts are not database result counts."}
    except SourceError:
        raise
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        raise SourceError("terminal_receipt_validation_unavailable") from None


def _resolve_etf_runs(saved, report, runs, day, cutoff):
    try:
        return _saved_etf_runs(saved,report,runs,day), {"savedReportStale":False}
    except SourceError as initial:
        # Contradictory terminal projections stay rejected. Only proven staleness
        # plus the separately installed immutable terminal chain may proceed.
        if str(initial) not in ("saved_etf_report_run_absent", "saved_etf_report_stale_for_latest_run",
                                "saved_etf_report_run_identity_invalid"):
            raise
        try:
            modes = _stale_etf_report(saved,runs,day)
            proof = _terminal_corrobation(runs,day,cutoff)
            identity = coverage_identity(runs[0],runs[-1],day,ACCOUNT_ID)
        except (SourceError,ValueError,TypeError,KeyError) as exc:
            error = SourceError(str(exc) if isinstance(exc,SourceError) else "terminal_receipt_validation_unavailable")
            error.diagnostic = {"schema":"quant.etf-report-mismatch.v1", "savedReport":report,
                "initialRejection":str(initial), "terminalCorroboration":"unavailable"}
            raise error from None
        return identity, {"savedReportStale":True,"staleness":modes,"originalRejection":str(initial),
                          "terminalCorroboration":proof}


def _collect_etf(day, cutoff):
    _require(cutoff.astimezone(BEIJING).time() >= day_time(16, 10), "before_etf_outcome_read_cutoff")
    helper = _module(ETF_HELPER, ETF_HELPER_SHA)
    saved, report = _read_json(ETF_REPORT)
    _require(saved.get("trade_date") == day and saved.get("report_type") == "eod_daily_report"
             and _instant(saved["generated_at"]) <= cutoff
             and _instant(saved["generated_at"]).astimezone(BEIJING).date().isoformat() == day,
             "saved_etf_report_wrong_day_or_time")
    with _sqlite_readonly(ETF_DB) as connection:
        runs, samples = _etf_rows(connection, helper, day, cutoff)
        queried = _utc_now().isoformat()
    coverage, reconciliation = _resolve_etf_runs(saved, report, runs, day, cutoff)
    return {"verified": True, "accountId": ACCOUNT_ID, "decisionDate": day,
            "firstRun": runs[0], "finalRun": runs[-1], "runCount": len(runs), "samples": samples,
            "coverageIdentity": coverage,
            "observedAt": queried, "evidence": {"savedReport": report, **reconciliation,
                "savedReportGeneratedAt": saved["generated_at"], "currentStoredPartitionReadAt": queried,
                "outcomePolicy": "Read existing outcome rows after 16:10; never invoke the evaluator or relabel evaluation_as_of."}}


def _verify_content(value):
    _require(isinstance(value, dict) and value.get("contentSha256") ==
             _sha(_canonical({k: v for k, v in value.items() if k != "contentSha256"}).encode()),
             "capture_content_hash_mismatch")


def _collect_capture(day, cutoff):
    manifest, reference = _read_json(CAPTURE / "manifest.json")
    _verify_content(manifest)
    _require(manifest.get("contractVersion") == "profit.forward-capture.v1"
             and manifest.get("scopeId") == ACCOUNT_ID and manifest.get("protocolSha256") == PROTOCOL_SHA
             and manifest.get("status") == "ok", "capture_manifest_identity_or_state")
    age = (_utc_now() - _instant(manifest["lastCheckedAt"])).total_seconds()
    _require(0 <= age <= 180, "capture_manifest_not_fresh")
    start, start_reference = _read_json(CAPTURE / "capture-start.json")
    _require(start.get("scopeId") == ACCOUNT_ID and start.get("protocolSha256") == PROTOCOL_SHA
             and start.get("captureStartedAt") == manifest.get("captureStartedAt"), "capture_start_mismatch")
    paths = sorted((CAPTURE / "events").glob("*.json"))
    _require(len(paths) <= MAX_EVENTS, "capture_event_limit")
    events = []
    for path in paths:
        event, _ = _read_json(path)
        _verify_content(event)
        source = event["source"]
        _require(event.get("scopeId") == ACCOUNT_ID and event.get("protocolSha256") == PROTOCOL_SHA
                 and event.get("contractVersion") == "profit.forward-observation.v1"
                 and event.get("classification") in ("development_only", "forward_observed")
                 and type(event.get("formalAdmission")) is bool, "capture_event_identity")
        execution = str(uuid.UUID(source["executionId"]))
        _require(path.name == execution + ".json" and source["executionId"] == execution, "capture_event_filename")
        _require(_instant(source["generatedAt"]) <= _instant(event["firstSeenAt"]) <= cutoff,
                 "capture_event_time")
        expected = "development_only" if _instant(source["generatedAt"]) <= _instant(start["captureStartedAt"]) else "forward_observed"
        _require(event["classification"] == expected, "capture_classification_mismatch")
        events.append(event)
    index = [{"executionId": e["source"]["executionId"], "contentSha256": e["contentSha256"]} for e in events]
    _require(_sha(_canonical(index).encode()) == manifest.get("eventIndexSha256"), "capture_index_hash_mismatch")
    development = sum(e["classification"] == "development_only" for e in events)
    forward = sum(e["classification"] == "forward_observed" for e in events)
    formal = sum(e["formalAdmission"] is True for e in events)
    _require((manifest["events"], manifest["developmentOnly"], manifest["forwardObserved"], manifest["formalAdmitted"])
             == (len(events), development, forward, formal), "capture_count_mismatch")
    # This deployed producer explicitly emits no formal admissions. Do not infer
    # future admission dates from firstSeenAt; a new contract requires review.
    _require(formal == 0, "formal_admission_contract_requires_review")
    _require(_sha(_read_bytes(CAPTURE / "manifest.json")) == reference["sha256"], "capture_snapshot_changed")
    return {"verified": True, "scopeId": ACCOUNT_ID, "observedAt": _utc_now().isoformat(),
            "developmentOnly": development, "forwardObserved": forward, "formalAdmitted": formal,
            "todayNewForward": sum(e["classification"] == "forward_observed" and
                _instant(e["firstSeenAt"]).astimezone(BEIJING).date().isoformat() == day for e in events),
            "todayNewFormal": 0, "protocolReason": manifest["reason"],
            "evidence": {"manifest": reference, "captureStart": start_reference,
                         "lastCheckedAt": manifest["lastCheckedAt"], "manifestAgeSeconds": age,
                         "eventIndexSha256": manifest["eventIndexSha256"], "eventCount": len(events),
                         "incrementClock": "firstSeenAt for forward observations only; never generatedAt or mtime"}}


# A report snapshot holds paper observations, never fills or calculated returns.
_TRACKING_SCHEMA = "quant.paper-tracking.v1"
_TRACKING_ROWS = 104  # At most 40 prior identities plus 64 pending records.
_TRADING_SYMBOL = re.compile(r"(?:sh|sz|bj)\d{6}")


def _prior_archive(day):
    root = ORIGINALS.parent
    _require(not any(p.is_symlink() for p in (root, *root.parents)), "tracking_linked_prior_path")
    prior_day, versions = None, []
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, 1):
            _require(count <= 4096, "tracking_archive_budget")
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-v([0-9]+)\.json", entry.name)
            if not match or match[1] >= day:
                continue
            if prior_day is None or match[1] > prior_day:
                prior_day, versions = match[1], []
            if match[1] == prior_day:
                versions.append(match[2])
    _require(prior_day is not None and versions == ["1"], "tracking_prior_missing_or_ambiguous")
    raw = _managed_bytes(root / (prior_day + "-v1.json"), required_mode=0o600)
    archived = _managed_json(raw)
    binding = validate_report_binding(archived, require_target=True)
    _require(archived["businessDate"] == prior_day and archived["accountId"] == "1"
             and _instant(archived["generatedAt"]).astimezone(BEIJING).date().isoformat() == prior_day,
             "tracking_prior_report_identity")
    return prior_day, archived, _sha(raw), binding["sha256"]


def _tracking_prior(day, cutoff):
    # Compare the latest *earlier* retained report with the latest earlier
    # recorded attempt. A failed/ambiguous newest report is never skipped in
    # favour of an older one with candidates.
    prior_day, archived, report_sha, binding_sha = _prior_archive(day)
    date_literal = date.fromisoformat(day).isoformat()
    cutoff_literal = cutoff.isoformat()
    attempt_query = f"""SELECT COALESCE(json_agg(x),'[]'::json) FROM (
        SELECT user_id, execution_id, run_id, signal_date, generated_at, release_sha256, status,
          CASE WHEN octet_length(result_document::text)<=1048576 THEN result_document END AS result
        FROM qd_profit_research_run_attempts
        WHERE user_id=1 AND recorded_at<='{cutoff_literal}'::timestamptz AND signal_date=(
          SELECT max(signal_date) FROM qd_profit_research_run_attempts
          WHERE user_id=1 AND signal_date<'{date_literal}'::date AND recorded_at<='{cutoff_literal}'::timestamptz)
        ORDER BY execution_id LIMIT 2) x"""
    attempts = _pg_json(attempt_query, timeout_seconds=3)
    _require(len(attempts) == 1 and type(attempts[0]["user_id"]) is int and attempts[0]["user_id"] == ACCOUNT_ID
             and attempts[0]["signal_date"] == prior_day, "tracking_prior_attempt_ambiguous")
    source = archived["details"]["sources"]["profit"]
    _require(source.get("verified") is True and archived["details"]["checks"]["profit"].get("verified") is True,
             "tracking_prior_source_unverified")
    helper = _module(PROFIT_HELPER, PROFIT_HELPER_SHA)
    attempt = helper.validate_attempt(attempts[0], source["executionId"])
    _require(type(source["accountId"]) is int and source["accountId"] == ACCOUNT_ID and source["marketDate"] == prior_day
             and source["resultSha256"] == attempt["result_sha256"] and source["runId"] == attempt["business_run_id"]
             and source["releaseSha256"] == attempt["release_sha256"]
             and _instant(source["generatedAt"]) == _instant(attempt["generated_at"])
             and _instant(source["generatedAt"]) <= _instant(archived["generatedAt"]) <= cutoff
             and source["status"] == attempt["result"]["status"], "tracking_prior_attempt_mismatch")
    status = source["status"]
    _require(status in ("DATA_BLOCKED", "CASH", "PAPER_CANDIDATES"), "tracking_prior_status_invalid")
    candidates = source["candidateIdentities"]
    _require(isinstance(candidates, list) and type(source["candidateCount"]) is int
             and len(candidates) == source["candidateCount"] <= 40
             and (status == "PAPER_CANDIDATES" or not candidates), "tracking_prior_count_invalid")
    actual = [{"symbol": r["symbol"], "name": r["name"], "code": r["code"], "signalDate": r["signal"]["date"]}
              for r in attempt["result"]["candidates"]]
    _require(actual == candidates and len({r["symbol"] for r in actual}) == len(actual),
             "tracking_prior_candidates_mismatch")
    for row in candidates:
        _require(_security_identity(row) and row["signalDate"] == prior_day, "tracking_prior_security_invalid")
    return {"verified": True, "day": prior_day, "status": status, "executionId": source["executionId"],
            "runId": source["runId"], "resultSha256": source["resultSha256"], "candidateCount": len(candidates),
            "reportSha256": report_sha, "bindingSha256": binding_sha, "candidateIdentities": candidates}


def _security_identity(row):
    symbol, name = row.get("symbol"), row.get("name")
    return (isinstance(symbol, str) and _TRADING_SYMBOL.fullmatch(symbol) and row.get("code") == symbol[2:]
            and isinstance(name, str) and re.fullmatch(r"[\w\u3400-\u9fff ·*Ａ-Ｚａ-ｚ-]{1,32}", name)
            and name.strip() == name)


def _tracking_query(day, cutoff, prior_run):
    date_literal = date.fromisoformat(day).isoformat()
    _require(isinstance(cutoff, datetime) and cutoff.tzinfo is not None
             and cutoff.astimezone(BEIJING).date().isoformat() == date_literal
             and (prior_run is None or type(prior_run) is int and prior_run > 0), "tracking_clock_or_run_invalid")
    as_of = cutoff.isoformat()
    run_id = prior_run or 0
    return f"""SELECT json_build_object('observedAt',CURRENT_TIMESTAMP, 'accountId',1,
      'pendingCount',(SELECT count(*) FROM qd_profit_picks WHERE user_id=1 AND created_at<='{as_of}'
                      AND state='EXIT_PENDING'),
      'lateChanges',(SELECT count(*) FROM qd_profit_picks WHERE user_id=1 AND created_at<='{as_of}'
                      AND updated_at>'{as_of}'),
      'rows',(SELECT COALESCE(json_agg(x),'[]'::json) FROM (
        SELECT p.id AS "pickId",p.run_id AS "runId",p.signal_date AS "signalDate",
          p.symbol,p.code,p.name,p.state,p.exit_signal_date AS "triggerDate",p.exit_date AS "exitDate",
          p.exit_reason AS "exitReason",p.max_holding_sessions AS "maxSessions",
          p.created_at AS "createdAt",p.updated_at AS "updatedAt",p.source_sha256 AS "pickSha256",
          o.id AS "observationId",o.trade_date AS "tradeDate",o.event,o.state_before AS "stateBefore",
          o.state_after AS "stateAfter",o.open,o.close,o.holding_sessions AS "holdingSessions",
          o.source_sha256 AS "sourceSha256",o.observed_at AS "observedAt"
        FROM qd_profit_picks p LEFT JOIN qd_profit_pick_observations o ON o.pick_id=p.id
          AND o.trade_date=(CASE WHEN p.state='EXIT_PENDING' THEN p.exit_signal_date ELSE '{date_literal}'::date END)
          AND o.observed_at<='{as_of}'::timestamptz
        WHERE p.user_id=1 AND p.created_at<='{as_of}'::timestamptz AND p.signal_date<='{date_literal}'::date
          AND (p.run_id={run_id} OR p.state='EXIT_PENDING')
        ORDER BY p.id,o.id LIMIT {_TRACKING_ROWS+1}) x))"""


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _observed_row(row, cutoff, expected_day):
    stamp = _instant(row["observedAt"])
    _require(_security_identity(row) and all(type(row[k]) is int and row[k]>0
             for k in ("pickId","runId","observationId"))
             and date.fromisoformat(row["signalDate"]).isoformat() == row["signalDate"] <= expected_day
             and row["tradeDate"] == expected_day == stamp.astimezone(BEIJING).date().isoformat()
             and _instant(row["createdAt"]) <= stamp <= cutoff and _instant(row["updatedAt"]) <= cutoff
             and re.fullmatch(r"[a-f0-9]{64}",row["sourceSha256"])
             and re.fullmatch(r"[a-f0-9]{64}",row["pickSha256"])
             and _positive(row["open"]) and _positive(row["close"]), "tracking_observation_unverified")
    return {k:row[k] for k in ("symbol","code","name","signalDate","runId","pickId","pickSha256",
                               "observationId","observedAt","tradeDate","sourceSha256","event",
                               "stateBefore","stateAfter","open","close","holdingSessions")}


def _candidate_tracking(identity, prior, rows, day, cutoff):
    result = {**identity,"runId":prior["runId"],"status":"unconfirmed","pickId":None,"observationId":None,
              "sourceSha256":None,"observedAt":None,"event":None,"stateBefore":None,"stateAfter":None,
              "open":None,"close":None}
    matches = [r for r in rows if r["symbol"] == identity["symbol"] and r["runId"] == prior["runId"]]
    if not matches:
        return {**result,"reason":"pick_missing"}
    if len(matches) != 1 or any(matches[0][k] != identity[k] for k in ("symbol","code","signalDate","name")):
        return {**result,"reason":"pick_or_observation_ambiguous"}
    row = matches[0]
    if row["observationId"] is None:
        return {**result,"reason":"observation_missing"}
    try:
        observation = _observed_row(row,cutoff,day)
    except (SourceError, TypeError, ValueError, KeyError):
        return {**result,"reason":"observation_unverified"}
    transition = (row["stateBefore"],row["event"],row["stateAfter"])
    if transition in (("PENDING_ENTRY","NEXT_OPEN_GATE_PASSED","OPEN"),
                      ("PENDING_ENTRY","CLOSE_BELOW_FROZEN_MA60","EXIT_PENDING")):
        return {**observation,"status":"entered"}
    if transition == ("PENDING_ENTRY","NEXT_OPEN_GATE_FAILED","EXPIRED"):
        return {**observation,"status":"expired"}
    return {**result,"reason":"entry_event_unconfirmed"}


def _pending_tracking(rows, snapshot, expected_pending, day, cutoff):
    selected = [r for r in rows if r["state"] == "EXIT_PENDING"]
    _require(len(selected) == snapshot["pendingCount"] == expected_pending <= 64
             and len({r["pickId"] for r in selected}) == len(selected)
             and snapshot["lateChanges"] == 0, "tracking_pending_population_unverified")
    pending = []
    for row in selected:
        _require(row["triggerDate"] <= day and row["exitDate"] is None
                 and row["exitReason"] == row["event"] in ("MAX_HOLDING_SESSIONS","CLOSE_BELOW_FROZEN_MA60")
                 and row["stateBefore"] in ("PENDING_ENTRY","OPEN") and row["stateAfter"] == "EXIT_PENDING"
                 and type(row["holdingSessions"]) is int and row["holdingSessions"] > 0
                 and (row["event"] != "MAX_HOLDING_SESSIONS" or row["stateBefore"] == "OPEN"
                      and type(row["maxSessions"]) is int and row["maxSessions"] > 0
                      and row["holdingSessions"] >= row["maxSessions"]), "tracking_pending_reason_unverified")
        pending.append({**_observed_row(row,cutoff,row["triggerDate"]), "triggerDate":row["triggerDate"]})
    return pending


def _tracking_from_rows(day, cutoff, prior, snapshot, expected_pending):
    rows = snapshot["rows"]
    _require(type(snapshot["accountId"]) is int and snapshot["accountId"] == ACCOUNT_ID
             and isinstance(rows,list) and len(rows) <= _TRACKING_ROWS
             and type(snapshot["pendingCount"]) is int and type(snapshot["lateChanges"]) is int
             and cutoff.astimezone(BEIJING).date().isoformat() == day
             and cutoff <= _instant(snapshot["observedAt"])
             and _instant(snapshot["observedAt"]).astimezone(BEIJING).date().isoformat() == day,
             "tracking_snapshot_invalid_or_oversize")
    candidates = [_candidate_tracking(x,prior,rows,day,cutoff) for x in prior.get("candidateIdentities",[])]
    detail = {"schema":_TRACKING_SCHEMA,"verified":True,"accountId":ACCOUNT_ID,"businessDate":day,
              "cutoffAt":cutoff.isoformat(),"observedAt":snapshot["observedAt"],"prior":prior,
              "candidates":candidates,"pendingVerified":False,"pendingExits":[]}
    try:
        detail["pendingExits"] = _pending_tracking(rows,snapshot,expected_pending,day,cutoff)
        detail["pendingVerified"] = True
    except (TypeError,ValueError,KeyError):
        detail["pendingError"] = "tracking_pending_unverified"
    detail["canonicalSha256"] = _sha(_canonical(detail).encode("utf-8"))
    return detail


def _collect_tracking(day, cutoff, expected_pending):
    try:
        prior = _tracking_prior(day,cutoff)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        prior = {"verified":False,"error":"tracking_prior_unverified"}
    try:
        query = _tracking_query(day,cutoff,prior.get("runId"))
        return _tracking_from_rows(day,cutoff,prior,_pg_json(query,timeout_seconds=3),expected_pending)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        return {"schema":_TRACKING_SCHEMA,"verified":False,"accountId":ACCOUNT_ID,
                "businessDate":day,"cutoffAt":cutoff.isoformat(),"error":"tracking_snapshot_unavailable"}


def _collect_portfolio(day, cutoff):
    row = _pg_json("SELECT json_build_object('accountId',1,'observedAt',CURRENT_TIMESTAMP,"
        "'openRecords',count(*) FILTER(WHERE state='OPEN'),"
        "'exitPendingRecords',count(*) FILTER(WHERE state='EXIT_PENDING'),"
        "'activeSecurities',count(DISTINCT symbol),"
        "'overlapRecords',count(*)-count(DISTINCT symbol)) "
        "FROM qd_profit_picks WHERE user_id=1 AND state IN ('OPEN','EXIT_PENDING')")
    _require(row.get("accountId") == ACCOUNT_ID, "portfolio_account_mismatch")
    for field in ("openRecords", "exitPendingRecords", "activeSecurities", "overlapRecords"):
        _require(type(row.get(field)) is int and row[field] >= 0, "portfolio_count_invalid")
    _instant(row["observedAt"])
    return {**row, "tracking": _collect_tracking(day, cutoff, row["exitPendingRecords"]),
            "verified": True, "actualCashUsed": None, "portfolioReturn": None,
            "evidence": {"grain": "independent paper records", "source": "qd_profit_picks user_id=1",
                         "timeMeaning": "PostgreSQL read-only transaction timestamp; not source generation time",
                         "cashNavBasis": "not provided; no sums of nominal strategy weights"}}


def collect(day: str, now: datetime) -> dict:
    """Collect today's evidence; now is cutoff, generatedAt is completion time.

    Source generated times, evaluation dates and firstSeenAt are never changed.
    Real-time portfolio queries retain their actual DB transaction timestamp.
    No historical current-DB replay is supported. Errors expose safe fixed codes
    only, never raw exception strings, SQL errors, credentials or source text.
    """
    _require(isinstance(now, datetime) and now.tzinfo is not None, "zoned_cutoff_required")
    _require(date.fromisoformat(day).isoformat() == day
             and now.astimezone(BEIJING).date().isoformat() == day
             and _utc_now().astimezone(BEIJING).date().isoformat() == day, "current_business_day_required")
    result = {"presentationSchema": PRESENTATION_SCHEMA, "accountId": ACCOUNT_ID, "businessDate": day, "sourceCutoffAt": now.isoformat(),
              "sourceErrors": []}
    for name, reader in (("profit", _collect_profit), ("health", _collect_health),
                         ("etf", _collect_etf), ("capture", _collect_capture), ("portfolio", _collect_portfolio)):
        started = _utc_now()
        try:
            result[name] = reader(day, now)
            result[name]["collectionStartedAt"] = started.isoformat()
            result[name]["collectionCompletedAt"] = _utc_now().isoformat()
        except Exception as exc:
            code = str(exc) if isinstance(exc, SourceError) else "source_read_or_integrity_failure"
            result[name] = {"verified": False, "accountId": ACCOUNT_ID}
            failure = {"source": name, "code": code, "errorType": type(exc).__name__}
            if isinstance(exc, SourceError) and hasattr(exc, "diagnostic"):
                failure["diagnostic"] = exc.diagnostic
            result["sourceErrors"].append(failure)
    result["generatedAt"] = _utc_now().isoformat()
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Read existing daily evidence; no sending or source generation")
    parser.add_argument("--day", required=True)
    args = parser.parse_args()
    print(_canonical(collect(args.day, _utc_now())))
