"""Freeze one read-only summary before an independently receipted delivery.

Shadow mode never imports the sender or opens the notification outbox. Scheduled
mode cannot backfill a missed day; systemd's timer is explicitly non-persistent.
Existing daily records are reused without recollecting/changing their body.
"""
import argparse
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

from daily_summary import VERSION, build_summary
from etf_identity import coverage_marker, validate_coverage_identity
import report_binding

TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT = "1"
STATE = Path("/home/ubuntu/trading-radar/data/daily-research-summary/1")
CONTROL = Path("/home/ubuntu/releases/daily-research-summary-20261012.1/control.json")


def _response_time():
    # Read after the one sender call, not from the earlier scheduler/cutoff
    # clock. Tests patch this clock; production has no path/env/CLI override.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def business_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_business_date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid_business_date")
    return parsed


def validate_control(control, day):
    """Verify account, contract, canonical effective date and target binding.

    ``day`` is the caller's explicitly derived Asia/Shanghai business date.
    This shared authorization check does not impose the scheduled time window.
    """
    current = business_date(day)
    if (not isinstance(control, dict) or control.get("enabled") is not True
            or control.get("accountId") != ACCOUNT
            or control.get("contract") != VERSION):
        raise ValueError("daily_delivery_not_enabled")
    effective = business_date(control.get("effectiveDate"))
    if current < effective:
        raise ValueError("before_daily_delivery_effective_date")
    expected = control.get("targetFingerprint")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("missing_target_binding")
    return expected


def validate_frozen_summary(summary, day, *, expected_target=None):
    """Validate frozen metadata without rerendering or refreshing source data."""
    if not isinstance(summary, dict):
        raise ValueError("frozen_summary_identity_conflict")
    text = summary.get("text")
    details = summary.get("details")
    if (not isinstance(text, str) or not text.strip()
            or len(text.encode("utf-16-le")) // 2 >= 3500
            or summary.get("businessDate") != day
            or summary.get("accountId") != ACCOUNT
            or summary.get("revision") != "v1"
            or summary.get("version") != VERSION
            or not isinstance(details, dict)
            or str(details.get("accountId")) != ACCOUNT
            or not isinstance(summary.get("sourceReferences"), list)
            or summary.get("textSha256") != hashlib.sha256(text.encode()).hexdigest()):
        raise ValueError("frozen_summary_identity_conflict")
    generated = summary.get("generatedAt")
    if not isinstance(generated, str):
        raise ValueError("frozen_summary_generation_time_invalid")
    parsed = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.astimezone(TZ).date() < business_date(day):
        raise ValueError("frozen_summary_generation_time_invalid")
    presentation = summary.get("presentationSchema")
    if presentation is not None and presentation != report_binding.PRESENTATION_SCHEMA:
        raise ValueError("frozen_presentation_schema_unknown")
    readable = report_binding.is_readable(summary)
    if readable:
        report_binding.validate(summary, expected_target=expected_target,
                                require_target=expected_target is not None)
    elif "evidenceBinding" in summary:
        raise ValueError("frozen_presentation_binding_conflict")
    etf_coverage = details.get("etfCoverage")
    if etf_coverage is not None:
        validate_coverage_identity(etf_coverage, day, ACCOUNT)
        if not readable:
            markers = [line for line in text.splitlines() if line.startswith("[ETF-COVERAGE")]
            if markers != [coverage_marker(etf_coverage)]:
                raise ValueError("frozen_etf_coverage_body_binding")
    return summary


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def exclusive_json(path, value):
    """Publish a complete immutable file; a losing creator never overwrites."""
    raw = encoded(value)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(name)


def frozen_summary(root, day, collector, now, *, target_fingerprint=None):
    # A formatter/code change never rewrites the text of an existing v1 record.
    # Invalid existing records are rejected, not rebuilt from current sources.
    business_date(day)
    root = Path(root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError("unsafe_summary_directory")
    with (root / ".freeze.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = root / (day + "-v1.json")
        if target.is_symlink():
            raise ValueError("unsafe_summary_file")
        if target.exists():
            if target.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("oversized_summary")
            summary = json.loads(target.read_text())
            return validate_frozen_summary(summary, day, expected_target=target_fingerprint)
        data = collector(day, now)
        if (not isinstance(data, dict) or data.get("businessDate") != day
                or str(data.get("accountId")) != ACCOUNT):
            raise ValueError("collector_identity_conflict")
        summary = build_summary(data)
        if summary.get("businessDate") != day:
            raise ValueError("rendered_day_conflict")
        summary["text"] += "\n查看原始证据：QuantDinger 报告中心；样本分层：验证与复盘。仅研究/模拟，不下单。"
        if len(summary["text"].encode("utf-16-le")) // 2 >= 3500:
            raise ValueError("summary_exceeds_delivery_limit")
        summary.update(accountId=ACCOUNT, revision="v1",
                       textSha256=hashlib.sha256(summary["text"].encode()).hexdigest())
        summary["details"]["textUtf16Length"] = len(summary["text"].encode("utf-16-le")) // 2
        if report_binding.is_readable(summary):
            report_binding.seal(summary, target_fingerprint)
        validate_frozen_summary(summary, day, expected_target=target_fingerprint)
        exclusive_json(target, summary)
        return summary


def scheduled(root, control, collector, now, sender_factory, *, ledger=None):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("zoned_schedule_time_required")
    local = now.astimezone(TZ)
    # Scheduled timing, not a market-calendar change. Holiday results remain
    # missing/skipped rather than borrowing the last trading day's success.
    if local.weekday() >= 5 or not ((16, 20) <= (local.hour, local.minute) < (17, 0)):
        raise ValueError("outside_daily_delivery_window")
    expected = validate_control(control, local.date().isoformat())
    day = local.date().isoformat()
    from crosspath_delivery import Ledger
    ledger = ledger if ledger is not None else Ledger()
    identity = {"accountId": ACCOUNT, "businessDate": day, "channel": "telegram",
                "targetFingerprint": expected, "scope": "summary",
                "subject": "daily_research_summary"}
    with ledger.locked(business_date=day) as tx:
        plan = {}

        def owned_collector(requested_day, clock):
            data = collector(requested_day, clock)
            if not isinstance(data, dict):
                raise ValueError("collector_identity_conflict")
            data = dict(data)
            profit = data.get("profit", {})
            if not isinstance(profit, dict):
                profit = {}
            execution = profit.get("executionId")
            result_hash = profit.get("resultSha256")
            valid = False
            try:
                valid = (profit.get("verified") is True
                         and str(uuid.UUID(execution)) == execution
                         and isinstance(result_hash, str)
                         and re.fullmatch(r"[0-9a-f]{64}", result_hash) is not None)
            except (ValueError, TypeError, AttributeError):
                pass
            if valid:
                valid = build_summary(data)["details"]["checks"]["profit"]["verified"] is True
            keys = ["profit:" + execution] if valid else []
            prior = tx.coverage(keys, identity=identity) if keys else {}
            existing = prior.get(keys[0]) if keys else None
            reference_only = existing is not None and existing.get("status") != "absent"
            plan.update(keys=[] if reference_only else keys, execution=execution if valid else None,
                        resultSha256=result_hash if valid else None)
            data["notificationCoverage"] = {
                "schema": "quant.notification-coverage.v1", "mode": "reference_only" if reference_only else "full",
                "executionId": execution if valid else None,
                "resultSha256": result_hash if valid else None,
                "previousStatus": existing["status"] if reference_only else None,
                "priorClaimId": existing["claim_id"] if reference_only else None,
                "coveredKeys": plan["keys"],
            }
            return data

        summary = frozen_summary(root, day, owned_collector, now, target_fingerprint=expected)
        coverage = summary.get("details", {}).get("notificationCoverage", {})
        if coverage.get("schema") != "quant.notification-coverage.v1":
            # Existing historical frozen reports are not rewritten or given
            # retrospective cross-path delivery proof by this new release.
            raise ValueError("historical_frozen_summary_has_no_crosspath_binding")
        metadata = {"reportRole": "summary", "summaryRevision": "v1"}
        for field in ("executionId", "resultSha256"):
            if coverage.get(field) is not None:
                metadata[field] = coverage[field]
        readable = report_binding.is_readable(summary)
        if readable:
            # Keep the existing ledger schema readable by old entrypoints.
            # The source receipt is this canonical immutable frozen summary,
            # including its new evidenceBinding; it is not a business result.
            metadata["sourceReceiptSha256"] = report_binding.frozen_digest(summary, expected)
        claim = tx.begin(identity, summary["text"], coverage=coverage["coveredKeys"], metadata=metadata)
    if not claim["allowed"]:
        old = claim.get("duplicate_status")
        accepted_duplicate = claim.get("status") == "duplicate" and old == "sent"
        if readable and claim.get("metadata", {}).get("sourceReceiptSha256") != metadata["sourceReceiptSha256"]:
            accepted_duplicate = False
        elif not readable and "sourceReceiptSha256" in claim.get("metadata", {}):
            # A new-format immutable intent must not be downgraded by removing
            # the archive's schema/binding and retaining only the same body.
            accepted_duplicate = False
        receipt = {**claim, "status": "sent" if accepted_duplicate else "unknown",
                   "attempted": False, "requires_reconciliation": not accepted_duplicate,
                   "proofSource": "crosspath_ledger",
                   "historicalOutboxStateNotReconciled": True}
        return {"businessDate": day, "receipt": receipt,
                "textSha256": summary["textSha256"], "crosspath": claim}

    def provider_once(text):
        try:
            response = sender_factory(expected)(text)
        except Exception:
            response = {"ok": False, "reason": "sender_unavailable"}
        if readable and isinstance(response, dict):
            response = {**response, "senderReturnedAt": _response_time()}
        if (isinstance(response, dict) and response.get("ok") is True
                and (response.get("targetFingerprint") != expected
                     or type(response.get("message_id")) is not int
                     or response["message_id"] <= 0
                     or ("http_status" in response and
                         (type(response["http_status"]) is not int
                          or not 200 <= response["http_status"] < 300)))):
            response = {**response, "ok": False, "reason": "provider_binding_unconfirmed"}
        if readable and isinstance(response, dict) and response.get("ok") is True:
            try:
                report_binding.validate_provider_identity(summary, response, expected)
            except (ValueError, TypeError, OverflowError):
                response = {**response, "ok": False, "reason": "provider_sender_or_body_unconfirmed"}
        if readable and isinstance(response, dict):
            # The frozen-summary digest (including the evidence binding) is
            # already persisted in the immutable pre-send intent.
            # Keep it alongside the actual provider response, without claiming
            # Telegram itself signs the research metadata or user-read state.
            response = {**response, "reportBinding": report_binding.receipt_binding(summary, expected)}
        # If this commit fails, the pre-send intent remains UNKNOWN. The old
        # dedicated outbox still receives the actual provider proof, never a
        # fabricated success and never an instruction to try another sender.
        try:
            with ledger.locked(business_date=day) as tx:
                tx.finish(claim, response)
        except Exception:
            pass
        return response

    from delivery import deliver
    receipt = deliver(Path(root) / "notification-outbox.sqlite3", ACCOUNT,
                      "telegram:" + expected, summary["businessDate"], "v1",
                      summary["text"], provider_once)
    # The original outbox and new append-only ownership ledger are separate
    # evidence axes. Neither is rewritten to reconcile the other; disagreement
    # remains pending reconciliation, never permission to send again.
    return {"businessDate": summary["businessDate"], "receipt": receipt,
            "textSha256": summary["textSha256"], "crosspath": claim}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("shadow", "scheduled"))
    parser.add_argument("--day")
    parser.add_argument("--input-json", type=Path)
    args = parser.parse_args()
    now = datetime.now(TZ)
    if args.mode == "scheduled" and (args.day or args.input_json):
        raise ValueError("scheduled_overrides_refused")
    if args.input_json:
        data = json.loads(args.input_json.read_text())
        collector = lambda day, clock: data
    else:
        from collect import collect
        collector = collect
    if args.mode == "shadow":
        day = args.day or now.date().isoformat()
        result = build_summary(collector(day, now))
        result["delivery"] = "shadow_not_sent"
    else:
        from telegram_once import configured_sender
        control = json.loads(CONTROL.read_text())
        result = scheduled(STATE, control, collector, now, configured_sender)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.mode == "scheduled" and result["receipt"]["status"] != "sent":
        return 2
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "errorClass": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
