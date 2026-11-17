"""M6.2 daily research summary: validate and freeze the legacy report.

The legacy system already freezes one immutable JSON per business date.  qp
validates its internal hashes (text/details/references/binding) before archiving
it as a ``reports.report``; it never rewrites the legacy file.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from qp.models.reports import Report
from qp.reports.archive import SourceRef, freeze_report
from qp.reports.legacy import digest, file_sha256, validate_legacy_daily

KIND = "daily_research_summary"


class LegacyDailyInvalid(ValueError):
    """The legacy daily summary failed its own integrity checks."""


def import_daily_summary(
    session: Session,
    summary: dict[str, Any],
    *,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
) -> tuple[Report, bool]:
    errors = validate_legacy_daily(summary)
    if errors:
        raise LegacyDailyInvalid("invalid legacy daily summary: " + ", ".join(errors))
    day = dt.date.fromisoformat(summary["businessDate"])
    if source_path is not None and source_sha256 is None:
        source_sha256 = file_sha256(source_path)
    source = SourceRef(
        source="legacy_daily_summary",
        locator=str(source_path) if source_path is not None else None,
        sha256=source_sha256,
        detail={
            "textSha256": summary.get("textSha256"),
            "detailsSha256": digest(summary.get("details")),
            "version": summary.get("version"),
        },
    )
    return freeze_report(session, kind=KIND, business_date=day, payload=summary, sources=[source])
