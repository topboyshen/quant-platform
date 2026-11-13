"""M7 API: read-only overview for the new frontend home page."""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp import __version__
from qp.api.main import Principal, get_db, require_auth
from qp.models.base import utcnow
from qp.models.market import HealthResult
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import JobRun
from qp.models.reports import Report
from qp.models.signals import EodRun

router = APIRouter(prefix="/api/overview")


@router.get("")
def overview(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    health_at = db.scalar(select(func.max(HealthResult.run_at)))
    health: dict[str, Any] = {"run_at": health_at, "checks": {}}
    if health_at is not None:
        rows = db.execute(select(HealthResult.check, HealthResult.status)
                          .where(HealthResult.run_at == health_at)).all()
        health["checks"] = {check: status for check, status in rows}
        health["ok"] = sum(1 for _, status in rows if status == "ok")
        health["total"] = len(rows)

    run = db.scalar(select(EodRun).order_by(EodRun.decision_date.desc(),
                                            EodRun.attempt_no.desc()).limit(1))
    signals = None
    if run is not None:
        signals = {"decision_date": run.decision_date, "attempt_no": run.attempt_no,
                   "status": run.status, "outcome": run.decision_outcome,
                   "signal_count": run.signal_count,
                   "allowed": run.allowed_signal_count,
                   "blocked": run.blocked_signal_count}

    by_kind = dict(db.execute(select(Report.kind, func.count()).group_by(Report.kind)).all())
    latest_report = db.scalar(select(func.max(Report.business_date)))

    since = utcnow() - dt.timedelta(hours=24)
    failed = db.scalar(select(func.count()).select_from(JobRun)
                       .where(JobRun.status == "failed", JobRun.started_at >= since)) or 0
    recent = db.execute(select(JobRun.id, JobRun.job_name, JobRun.status, JobRun.started_at)
                        .order_by(JobRun.id.desc()).limit(5)).all()

    outbox = dict(db.execute(select(Outbox.mode, func.count()).group_by(Outbox.mode)).all())
    attempts = db.scalar(select(func.count()).select_from(DeliveryAttempt)) or 0
    return {
        "version": __version__,
        "health": health,
        "signals": signals,
        "reports": {"by_kind": by_kind, "latest_date": latest_report},
        "jobs": {"failed_24h": failed,
                 "recent": [{"id": r[0], "job_name": r[1], "status": r[2],
                             "started_at": r[3]} for r in recent]},
        "notify": {"outbox": outbox, "delivery_attempt": attempts},
    }
