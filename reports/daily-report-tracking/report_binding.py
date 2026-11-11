"""Pure versioned binding for readable summaries; no I/O or business execution.

The legacy visible ETF marker is retained for legacy frozen files. New readable
reports bind their exact body, source metadata and delivery target in an
immutable pre-send intent, then copy that digest into the sender receipt. This
is a local evidence association with an accepted body, not a provider signature
over research metadata and not proof that the recipient read it.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

PRESENTATION_SCHEMA = "quant.daily-readable.v1"
SCHEMA = "quant.daily-report-evidence-binding.v1"
RECEIPT_SCHEMA = "quant.daily-report-receipt-binding.v1"
BOT_ID = 8658511979
BOT_USERNAME = "CmdrQuant_bot"
PROVIDER_PROOF_SCHEMA = "telegram-public-identity.v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _copy(value):
    return json.loads(_encoded(value))


def is_readable(summary):
    return isinstance(summary, dict) and summary.get("presentationSchema") == PRESENTATION_SCHEMA


def _projection(summary, target_fingerprint):
    if not is_readable(summary):
        raise ValueError("readable_presentation_schema_required")
    if target_fingerprint is not None and (
            not isinstance(target_fingerprint, str) or not _SHA.fullmatch(target_fingerprint)):
        raise ValueError("report_binding_target_invalid")
    text = summary.get("text")
    day = summary.get("businessDate")
    details = summary.get("details")
    refs = summary.get("sourceReferences")
    if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
            or summary.get("accountId") != "1" or summary.get("revision") != "v1"
            or summary.get("version") != "daily-research-summary.v1"
            or not isinstance(text, str) or not text.strip()
            or len(text.encode("utf-16-le")) // 2 >= 3500
            or not isinstance(details, dict) or str(details.get("accountId")) != "1"
            or not isinstance(refs, list)
            or summary.get("textSha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
        raise ValueError("report_binding_identity_invalid")
    instant = datetime.fromisoformat(summary.get("generatedAt", "").replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("report_binding_clock_invalid")
    # A legacy marker cannot accidentally act as a second, conflicting contract.
    if any(line.startswith("[ETF-COVERAGE") for line in text.splitlines()):
        raise ValueError("readable_legacy_marker_conflict")
    sources = details.get("sources", {})
    checks = details.get("checks", {})
    if not isinstance(sources, dict) or not isinstance(checks, dict):
        raise ValueError("report_binding_source_shape")
    profit, health = sources.get("profit", {}), sources.get("health", {})
    if not isinstance(profit, dict) or not isinstance(health, dict):
        raise ValueError("report_binding_source_shape")
    fields = ("executionId", "runId", "marketDate", "generatedAt", "status",
              "releaseSha256", "resultSha256")
    return {
        "schema": SCHEMA, "presentationSchema": PRESENTATION_SCHEMA,
        "contract": summary["version"], "businessDate": day,
        "accountId": "1", "revision": "v1", "generatedAt": summary["generatedAt"],
        "channel": "telegram", "targetFingerprint": target_fingerprint,
        "destinationStatus": "target_bound" if target_fingerprint else "unbound_preview",
        "bodySha256": summary["textSha256"],
        "profit": {field: profit.get(field) for field in fields},
        "health": {field: health.get(field) for field in fields},
        "etfCoverage": _copy(details.get("etfCoverage")),
        "notificationCoverage": _copy(details.get("notificationCoverage", {})),
        "sourceReferencesSha256": _digest(refs),
        # This includes the presentation inputs and verification outcomes. A
        # changed result/card/status/check cannot be resealed against an old
        # receipt's reportBinding digest, even if its human text stayed equal.
        "detailsSha256": _digest(details),
    }


def seal(summary, target_fingerprint=None):
    """Add a new local binding after the final text and details are complete."""
    projection = _projection(summary, target_fingerprint)
    summary["evidenceBinding"] = {**projection, "sha256": _digest(projection)}
    return summary


def validate(summary, *, expected_target=None, require_target=False):
    binding = summary.get("evidenceBinding") if isinstance(summary, dict) else None
    if not isinstance(binding, dict):
        raise ValueError("report_evidence_binding_missing")
    target = binding.get("targetFingerprint")
    if (require_target and target is None) or (expected_target is not None and target != expected_target):
        raise ValueError("report_evidence_target_mismatch")
    projection = _projection(summary, target)
    expected = {**projection, "sha256": _digest(projection)}
    if binding != expected:
        raise ValueError("report_evidence_binding_mismatch")
    return binding


def receipt_binding(summary, expected_target):
    binding = validate(summary, expected_target=expected_target, require_target=True)
    return {"schema": RECEIPT_SCHEMA, "sha256": binding["sha256"],
            "frozenSummarySha256": frozen_digest(summary, expected_target),
            "presentationSchema": PRESENTATION_SCHEMA,
            "accountId": "1", "businessDate": summary["businessDate"],
            "revision": "v1", "channel": "telegram",
            "targetFingerprint": expected_target, "bodySha256": summary["textSha256"],
            "meaning": "local_pre_send_evidence_binding_not_provider_metadata_signature"}


def frozen_digest(summary, expected_target):
    """Canonical frozen JSON bytes, exactly as the new freezer persists them.

    This uses the existing ledger's sourceReceiptSha256 field: the source
    receipt is the immutable frozen report. No ledger schema expansion is
    needed, and legacy ledger readers still understand every future intent.
    """
    validate(summary, expected_target=expected_target, require_target=True)
    return _digest(summary)


def validate_receipt(summary, receipt, expected_target):
    expected = receipt_binding(summary, expected_target)
    if not isinstance(receipt, dict) or receipt.get("reportBinding") != expected:
        raise ValueError("report_receipt_binding_mismatch")
    return expected


def validate_provider_identity(summary, receipt, expected_target):
    """New-format delivery requires the frozen public bot/body/time contract.

    No token is accepted or inspected here. These fields are retained by the
    sender after checking the platform's actual sendMessage result. Historical
    legacy receipts are deliberately not retrofitted with this proof.
    """
    if (not isinstance(receipt, dict) or receipt.get("ok") is not True
            or receipt.get("providerProofSchema") != PROVIDER_PROOF_SCHEMA
            or type(receipt.get("botId")) is not int or receipt["botId"] != BOT_ID
            or receipt.get("botUsername") != BOT_USERNAME
            or receipt.get("returnedSenderMatches") is not True
            or receipt.get("returnedTextMatches") is not True
            or receipt.get("bodySha256") != summary.get("textSha256")
            or receipt.get("targetFingerprint") != expected_target
            or type(receipt.get("message_id")) is not int or receipt["message_id"] <= 0
            or type(receipt.get("http_status")) is not int or receipt["http_status"] != 200
            or type(receipt.get("platformMessageDate")) is not int
            or receipt["platformMessageDate"] <= 0):
        raise ValueError("report_public_provider_identity_unconfirmed")
    try:
        platform_day = datetime.fromtimestamp(receipt["platformMessageDate"],
                                             timezone(timedelta(hours=8))).date().isoformat()
        generated = datetime.fromisoformat(summary["generatedAt"].replace("Z", "+00:00"))
        returned = datetime.fromisoformat(receipt.get("senderReturnedAt", "").replace("Z", "+00:00"))
        if (generated.tzinfo is None or returned.tzinfo is None or returned < generated
                or not int(generated.timestamp()) <= receipt["platformMessageDate"] <= int(returned.timestamp())):
            raise ValueError("report_public_provider_time_unconfirmed")
    except (OSError, OverflowError, ValueError, AttributeError, TypeError, KeyError):
        raise ValueError("report_public_provider_time_unconfirmed") from None
    if platform_day != summary.get("businessDate"):
        raise ValueError("report_public_provider_time_unconfirmed")
    return True
