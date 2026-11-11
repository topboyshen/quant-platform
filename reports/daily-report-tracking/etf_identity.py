"""Pure ETF batch-result identity; no I/O, clocks, imports of business code.

The digest deliberately covers this versioned, explicit result projection,
not the whole strategy, original supplier input, or profitability. Equivalent
zoned completion times normalize to UTC; missing values are never invented.
"""
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import re

SCHEMA = "quant.etf-run-result-identity.v1"
COVERAGE_SCHEMA = "quant.etf-summary-coverage.v1"
MARKER_PREFIX = "[ETF-COVERAGE sha256="
COUNT_FIELDS = ("evaluated_count", "failed_count", "signal_count",
                "allowed_signal_count", "blocked_signal_count")
FIELDS = ("run_id", "decision_date", "status", "decision_outcome", "completed_at") + COUNT_FIELDS
OUTCOMES = {"success": ("emitted", "no_signal", "blocked"),
            "partial": ("emitted", "blocked", "data_unavailable"),
            "failed": ("data_unavailable",), "skipped": ("skipped",)}
TZ = timezone(timedelta(hours=8))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def run_identity(run, business_date):
    if (not isinstance(business_date, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", business_date)
            or date.fromisoformat(business_date).isoformat() != business_date
            or not isinstance(run, dict)):
        raise ValueError("etf_identity_shape")
    if (not isinstance(run.get("run_id"), str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run["run_id"]) is None
            or run.get("decision_date") != business_date):
        raise ValueError("etf_identity_run_or_day")
    if (not isinstance(run.get("status"), str)
            or run["status"] not in OUTCOMES
            or run.get("decision_outcome") not in OUTCOMES[run["status"]]):
        raise ValueError("etf_identity_terminal_state")
    if any(type(run.get(k)) is not int or not 0 <= run[k] <= 2**63 - 1 for k in COUNT_FIELDS):
        raise ValueError("etf_identity_count")
    raw_time = run.get("completed_at")
    if not isinstance(raw_time, str):
        raise ValueError("etf_identity_completion")
    completed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    if completed.tzinfo is None or completed.astimezone(TZ).date().isoformat() != business_date:
        raise ValueError("etf_identity_completion")
    normalized = {k: run[k] for k in FIELDS}
    normalized["completed_at"] = completed.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha256(_canonical({"schema": SCHEMA, "run": normalized})).hexdigest()
    return {"schema": SCHEMA, "businessDate": business_date, "runId": run["run_id"],
            "result": normalized, "resultSha256": digest}


def validate_run_identity(identity, business_date):
    """Reject malformed, noncanonical, unbound or altered identity records."""
    if not isinstance(identity, dict) or set(identity) != {
            "schema", "businessDate", "runId", "result", "resultSha256"}:
        raise ValueError("etf_identity_record_shape")
    result = identity.get("result")
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise ValueError("etf_identity_projection_shape")
    expected = run_identity(result, business_date)
    if identity != expected:
        raise ValueError("etf_identity_digest_or_binding")
    return identity


def coverage_identity(first, final, business_date, account_id="1"):
    """A future summary's explicit ETF coverage; never inferred for old files."""
    if str(account_id) != "1" or isinstance(account_id, bool):
        raise ValueError("etf_coverage_account")
    first_identity = run_identity(first, business_date)
    final_identity = run_identity(final, business_date)
    if datetime.fromisoformat(first_identity["result"]["completed_at"]) > datetime.fromisoformat(
            final_identity["result"]["completed_at"]):
        raise ValueError("etf_coverage_order")
    return {"schema": COVERAGE_SCHEMA, "accountId": "1", "businessDate": business_date,
            "firstRun": first_identity, "finalRun": final_identity}


def validate_coverage_identity(value, business_date, account_id="1"):
    if not isinstance(value, dict) or set(value) != {
            "schema", "accountId", "businessDate", "firstRun", "finalRun"}:
        raise ValueError("etf_coverage_shape")
    first = validate_run_identity(value["firstRun"], business_date)
    final = validate_run_identity(value["finalRun"], business_date)
    expected = coverage_identity(first["result"], final["result"], business_date, account_id)
    if value != expected:
        raise ValueError("etf_coverage_binding")
    return value


def coverage_digest(value):
    validate_coverage_identity(value, value.get("businessDate"), value.get("accountId"))
    return hashlib.sha256(_canonical(value)).hexdigest()


def coverage_marker(value):
    return MARKER_PREFIX + coverage_digest(value) + "]"
