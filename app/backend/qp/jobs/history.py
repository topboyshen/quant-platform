"""One-off jobs for importing legacy history into qp signals."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext, job
from qp.signals.legacy_history import import_legacy_history

CST = ZoneInfo("Asia/Shanghai")
DEFAULT_SWING_DB = "/home/ubuntu/trading-radar/data/swing_macro.db"


@job(
    "signals.import_legacy_history",
    "Import legacy swing decisions for past dates (read-only SQLite; skips dates qp already has)",
    schedule_hint="manual one-off",
)
def import_legacy_history_job(ctx: JobContext) -> dict:
    path = os.getenv("QP_LEGACY_SWING_DB", DEFAULT_SWING_DB).strip()
    if not Path(path).is_file():
        return {"ok": True, "skipped": f"legacy swing db not found: {path}"}
    raw_before = os.getenv("QP_HISTORY_BEFORE", "").strip()
    before = dt.date.fromisoformat(raw_before) if raw_before else dt.datetime.now(CST).date()
    with Session(ctx.engine) as session:
        counts = import_legacy_history(session, path, before=before)
        session.commit()
        return {"ok": True, "before": before.isoformat(), **counts}
