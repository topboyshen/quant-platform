"""M6.3 research digest and execution digest archiving.

The research digest is the legacy hourly local JSON (read-only).  The execution
digest is not recomputed: qp references the M4 ``signals.execution_digest`` row
and archives its body plus the digest sha256.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from qp.models.reports import Report
from qp.models.signals import ExecutionDigest
from qp.reports.archive import SourceRef, freeze_report

RESEARCH_KIND = "research_digest"
EXECUTION_KIND = "execution_digest"


def _date(value: Any) -> dt.date | None:
    try:
        parsed = dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.isoformat() == value else None


def research_digest_date(payload: dict[str, Any]) -> dt.date:
    """Business date: index-close trade date, else max close row, else generated_at date."""
    cache = payload.get("index_close_cache") if isinstance(payload.get("index_close_cache"), dict) else {}
    day = _date(cache.get("trade_date"))
    if day is not None:
        return day
    rows = payload.get("index_closes") if isinstance(payload.get("index_closes"), list) else []
    days = [d for d in (_date(row.get("trade_date")) for row in rows if isinstance(row, dict)) if d]
    if days:
        return max(days)
    generated = str(payload.get("generated_at") or "")
    try:
        return dt.datetime.fromisoformat(generated.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError("research digest has no business date") from exc


def import_research_digest(session: Session, payload: dict[str, Any], *,
                           source_path: str | Path | None = None,
                           source_sha256: str | None = None) -> tuple[Report, bool]:
    if not isinstance(payload, dict) or "generated_at" not in payload:
        raise ValueError("not a research digest payload")
    day = research_digest_date(payload)
    source = SourceRef(
        source="legacy_research_digest",
        locator=str(source_path) if source_path is not None else None,
        sha256=source_sha256,
        detail={"generated_at": payload.get("generated_at")},
    )
    return freeze_report(session, kind=RESEARCH_KIND, business_date=day, payload=payload,
                         sources=[source])


def execution_digest_payload(row: ExecutionDigest) -> dict[str, Any]:
    return {
        "digest_date": row.digest_date.isoformat(),
        "item_count": row.item_count,
        "accepted": row.accepted,
        "rejected": row.rejected,
        "sha256": row.sha256,
        "body": row.body,
    }


def import_execution_digest(session: Session, row: ExecutionDigest, *,
                            source_locator: str | None = None) -> tuple[Report, bool]:
    payload = execution_digest_payload(row)
    source = SourceRef(source="signals.execution_digest", locator=source_locator,
                       sha256=row.sha256,
                       detail={"item_count": row.item_count, "accepted": row.accepted,
                               "rejected": row.rejected})
    return freeze_report(session, kind=EXECUTION_KIND, business_date=row.digest_date,
                         payload=payload, sources=[source])


def load_research_digest(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("research digest is not an object")
    return data
