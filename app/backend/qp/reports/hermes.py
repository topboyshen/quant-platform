"""M6.4 Hermes cron summaries: read the legacy archived originals.

The two Hermes cron jobs (`profit` 15:10, `health` 15:35) archive each run under
``daily-research-summary/1/originals/<jobId>/`` as an envelope JSON plus the
original text body.  qp verifies the body sha256 and freezes the text as a report;
it never runs the legacy job or sends anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from qp.models.reports import Report
from qp.reports.archive import ReportConflict, SourceRef, freeze_report

ENVELOPE_SCHEMA = "daily-summary-original-envelope.v1"
KIND_BY_MODE = {"profit": "profit_summary", "health": "profit_radar_health"}


def _date(value: Any) -> dt.date | None:
    try:
        parsed = dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.isoformat() == value else None


def load_envelopes(root: str | Path, job_id: str) -> list[dict[str, Any]]:
    """Return envelope records (metadata JSON only) sorted by business date."""
    job_dir = Path(root) / job_id
    if not job_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(job_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != ENVELOPE_SCHEMA:
            continue
        if _date(data.get("businessDate")) is None:
            continue
        out.append({**data, "_path": str(path)})
    out.sort(key=lambda item: (item["businessDate"], os.path.getmtime(item["_path"])))
    return out


def find_envelope(root: str | Path, job_id: str, day: dt.date) -> dict[str, Any] | None:
    for item in reversed(load_envelopes(root, job_id)):
        if item["businessDate"] == day.isoformat():
            return item
    return None


def read_body(envelope: dict[str, Any]) -> str:
    path = Path(envelope["_path"]).parent / str(envelope.get("originalFile") or "")
    if not path.is_file():
        raise ValueError(f"hermes original body missing: {path.name}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != envelope.get("bodySha256"):
        raise ValueError("hermes body sha256 mismatch")
    return raw.decode("utf-8")


def import_hermes_summary(
    session: Session, envelope: dict[str, Any], *, body: str | None = None
) -> tuple[Report, bool]:
    mode = str(envelope.get("mode") or "")
    kind = KIND_BY_MODE.get(mode)
    if kind is None:
        raise ValueError(f"unknown hermes mode: {mode!r}")
    day = _date(envelope.get("businessDate"))
    if day is None:
        raise ValueError("hermes envelope has no business date")
    text = read_body(envelope) if body is None else body
    payload = {
        "jobId": envelope.get("jobId"),
        "mode": mode,
        "businessDate": day.isoformat(),
        "returnCode": envelope.get("returnCode"),
        "silent": envelope.get("silent"),
        "bodySha256": envelope.get("bodySha256"),
        "bodyBytes": envelope.get("bodyBytes"),
        "text": text,
    }
    envelope_sha = hashlib.sha256(Path(envelope["_path"]).read_bytes()).hexdigest()
    source = SourceRef(
        source="hermes_cron",
        locator=envelope.get("_path"),
        sha256=envelope_sha,
        detail={"jobId": envelope.get("jobId"), "mode": mode, "businessDate": day.isoformat()},
    )
    # A legacy retry can produce a new body for the same day: bump the version.
    version = 1
    while True:
        try:
            return freeze_report(
                session, kind=kind, business_date=day, payload=payload, sources=[source], version=version
            )
        except ReportConflict:
            version += 1
            if version > 10:
                raise
