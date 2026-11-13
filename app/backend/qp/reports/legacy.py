"""Read-only adapter for the legacy frozen daily research summary.

Mirrors the hashes used by the legacy ``report_binding`` module so qp can prove a
legacy file is internally consistent before archiving it.  qp never writes the
legacy directory.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

PRESENTATION_SCHEMA = "quant.daily-readable.v1"
BINDING_SCHEMA = "quant.daily-report-evidence-binding.v1"
CONTRACT = "daily-research-summary.v1"
SOURCE_FIELDS = ("executionId", "runId", "marketDate", "generatedAt", "status",
                 "releaseSha256", "resultSha256")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_readable_daily(summary: Any) -> bool:
    """Operational-failure records share the directory but are not readable reports."""
    return (isinstance(summary, dict)
            and summary.get("presentationSchema") == PRESENTATION_SCHEMA
            and summary.get("version") == CONTRACT)


def legacy_daily_path(root: str | Path, day: dt.date) -> Path | None:
    """Return the frozen file for a business date, if present."""
    root = Path(root)
    for name in (f"{day.isoformat()}-v1.json", f"{day.isoformat()}-operational-failure.json"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def load_legacy_daily(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("legacy daily summary is not an object")
    return data


def _projection(summary: dict[str, Any], target_fingerprint: str | None) -> dict[str, Any]:
    details = summary.get("details") or {}
    sources = details.get("sources") or {}
    profit = sources.get("profit") or {}
    health = sources.get("health") or {}
    refs = summary.get("sourceReferences") or []
    return {
        "schema": BINDING_SCHEMA,
        "presentationSchema": PRESENTATION_SCHEMA,
        "contract": summary.get("version"),
        "businessDate": summary.get("businessDate"),
        "accountId": "1",
        "revision": "v1",
        "generatedAt": summary.get("generatedAt"),
        "channel": "telegram",
        "targetFingerprint": target_fingerprint,
        "destinationStatus": "target_bound" if target_fingerprint else "unbound_preview",
        "bodySha256": summary.get("textSha256"),
        "profit": {field: profit.get(field) for field in SOURCE_FIELDS},
        "health": {field: health.get(field) for field in SOURCE_FIELDS},
        "etfCoverage": json.loads(canonical_json(details.get("etfCoverage"))),
        "notificationCoverage": json.loads(canonical_json(details.get("notificationCoverage", {}))),
        "sourceReferencesSha256": digest(refs),
        "detailsSha256": digest(details),
    }


def validate_legacy_daily(summary: dict[str, Any]) -> list[str]:
    """Return a list of integrity errors; an empty list means the file is consistent."""
    errors: list[str] = []
    if not isinstance(summary, dict):
        return ["not_an_object"]
    if summary.get("presentationSchema") != PRESENTATION_SCHEMA:
        errors.append("presentation_schema")
    if summary.get("version") != CONTRACT:
        errors.append("contract_version")
    if str(summary.get("accountId")) != "1" or summary.get("revision") != "v1":
        errors.append("account_or_revision")

    text = summary.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append("text_missing")
    elif summary.get("textSha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        errors.append("text_sha256")

    day = summary.get("businessDate")
    try:
        parsed = dt.date.fromisoformat(str(day))
        if parsed.isoformat() != day:
            errors.append("business_date")
    except ValueError:
        errors.append("business_date")

    details = summary.get("details")
    if not isinstance(details, dict) or str(details.get("accountId")) != "1":
        errors.append("details_shape")
    if not isinstance(summary.get("sourceReferences"), list):
        errors.append("source_references")
    try:
        instant = dt.datetime.fromisoformat(str(summary.get("generatedAt", "")).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            errors.append("generated_at_tz")
    except ValueError:
        errors.append("generated_at")

    binding = summary.get("evidenceBinding")
    if not isinstance(binding, dict):
        return errors + ["binding_missing"]
    if binding.get("bodySha256") != summary.get("textSha256"):
        errors.append("binding_body_sha256")
    if binding.get("detailsSha256") != digest(details):
        errors.append("binding_details_sha256")
    if binding.get("sourceReferencesSha256") != digest(summary.get("sourceReferences")):
        errors.append("binding_refs_sha256")
    projection = _projection(summary, binding.get("targetFingerprint"))
    if binding != {**projection, "sha256": digest(projection)}:
        errors.append("binding_projection")
    return errors
