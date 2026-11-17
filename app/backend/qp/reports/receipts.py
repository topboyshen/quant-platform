"""M6.5 recipient receipts from the Hermes delivery ledger (read-only).

Hermes records deliveries and executions in SQLite.  A delivered row is evidence
that the provider accepted the message; it is not proof the recipient read it.
qp stores it as ``reports.report_receipt`` separate from ``send_result``.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.reports import Report, ReportDelivery, ReportReceipt

CST = dt.timezone(dt.timedelta(hours=8))
JOB_KINDS = {"ec6c86fd856c": "profit_summary", "026a1083d9e0": "profit_radar_health"}


def load_delivered(deliveries_db: str | Path, executions_db: str | Path) -> list[dict[str, Any]]:
    deliveries = sqlite3.connect(f"file:{deliveries_db}?mode=ro", uri=True)
    deliveries.row_factory = sqlite3.Row
    executions = sqlite3.connect(f"file:{executions_db}?mode=ro", uri=True)
    executions.row_factory = sqlite3.Row
    try:
        jobs = {row["id"]: row["job_id"] for row in executions.execute("SELECT id, job_id FROM executions")}
        out: list[dict[str, Any]] = []
        for row in deliveries.execute(
            "SELECT execution_id, status, created_at, finished_at FROM deliveries "
            "WHERE status = 'delivered' ORDER BY created_at"
        ):
            job_id = jobs.get(row["execution_id"])
            kind = JOB_KINDS.get(str(job_id))
            if kind is None:
                continue
            out.append(
                {
                    "execution_id": row["execution_id"],
                    "job_id": job_id,
                    "kind": kind,
                    "created_at": row["created_at"],
                    "finished_at": row["finished_at"],
                }
            )
        return out
    finally:
        deliveries.close()
        executions.close()


def _day(value: str) -> dt.date | None:
    try:
        instant = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=CST)
    return instant.astimezone(CST).date()


def attach_receipts(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    attached = skipped = 0
    for row in rows:
        day = _day(row.get("created_at"))
        if day is None:
            skipped += 1
            continue
        report = session.scalar(
            select(Report)
            .where(Report.kind == row["kind"], Report.business_date == day)
            .order_by(Report.version.desc())
            .limit(1)
        )
        if report is None:
            skipped += 1
            continue
        delivery = session.scalar(
            select(ReportDelivery)
            .where(ReportDelivery.report_id == report.id)
            .order_by(ReportDelivery.id.desc())
            .limit(1)
        )
        if delivery is None:
            skipped += 1
            continue
        exists = session.scalar(
            select(ReportReceipt).where(
                ReportReceipt.delivery_id == delivery.id,
                ReportReceipt.evidence["execution_id"].astext == row["execution_id"],
            )
        )
        if exists is not None:
            skipped += 1
            continue
        received_at = None
        if row.get("finished_at"):
            try:
                received_at = dt.datetime.fromisoformat(str(row["finished_at"]))
            except ValueError:
                received_at = None
        session.add(
            ReportReceipt(
                delivery_id=delivery.id,
                status="received",
                received_at=received_at,
                evidence={
                    "source": "hermes_deliveries",
                    "execution_id": row["execution_id"],
                    "job_id": row["job_id"],
                    "delivery_status": "delivered",
                },
            )
        )
        attached += 1
    session.flush()
    return {"attached": attached, "skipped": skipped}
