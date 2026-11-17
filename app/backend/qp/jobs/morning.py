"""M8 morning-chain shadow job: read-only snapshot of the legacy chain.

ARCHIVED 2026-11-14: the report generation chain was deleted by request and is not
restored; this job is manual-only and has no timer.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext, job
from qp.morning.chain import import_morning_chain

CST = ZoneInfo("Asia/Shanghai")
DEFAULT_ASTOCK_ROOT = "/home/ubuntu/AStock_AI_System"
DEFAULT_BUS_ROOT = "/home/ubuntu/signal-bus"
DEFAULT_STATE_DIR = "/home/ubuntu/.local/state/signal-bus"


@job("reports.import_morning_chain", "Snapshot the legacy morning chain into reports.* "
                                     "(read-only; no relay, no alerts)",
     schedule_hint="manual only (M8 archived 2026-11-14; report timers removed 2026-11-13)")
def import_morning_chain_job(ctx: JobContext) -> dict:
    raw_day = os.getenv("QP_REPORT_DATE", "").strip()
    day = (dt.date.fromisoformat(raw_day) if raw_day else dt.datetime.now(CST).date())
    astock = os.getenv("QP_MORNING_ASTOCK_ROOT", DEFAULT_ASTOCK_ROOT)
    bus = os.getenv("QP_SIGNAL_BUS_ROOT", DEFAULT_BUS_ROOT)
    state = os.getenv("QP_SIGNAL_BUS_STATE", DEFAULT_STATE_DIR)
    try:
        with Session(ctx.engine) as session:
            report, created = import_morning_chain(session, day=day, astock_root=astock,
                                                   bus_root=bus, state_dir=state)
            session.commit()
            return {"ok": True, "date": day.isoformat(), "report_id": report.id,
                    "created": created, "kind": report.kind, "version": report.version}
    except ValueError as exc:
        return {"ok": True, "skipped": str(exc)}
