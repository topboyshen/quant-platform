"""M7 API: read-only report center, deliveries and receipts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth, require_scope
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource

router = APIRouter(prefix="/api/reports", dependencies=[Depends(require_scope("reports:read"))])


def _report(r: Report) -> dict[str, Any]:
    return {"id": r.id, "kind": r.kind, "business_date": r.business_date,
            "version": r.version, "status": r.status,
            "content_sha256": r.content_sha256, "created_at": r.created_at,
            "frozen_at": r.frozen_at}


@router.get("")
def reports(kind: str | None = None, limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0), p: Principal = Depends(require_auth),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Report)
    if kind:
        stmt = stmt.where(Report.kind == kind)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    counts = dict(db.execute(select(Report.kind, func.count()).group_by(Report.kind)).all())
    rows = db.execute(stmt.order_by(Report.business_date.desc(), Report.kind,
                                    Report.version.desc()).limit(limit).offset(offset)).scalars()
    return {"total": total, "counts": counts, "rows": [_report(r) for r in rows]}


@router.get("/deliveries")
def deliveries(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
               p: Principal = Depends(require_auth),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(ReportDelivery)) or 0
    rows = db.execute(
        select(ReportDelivery, Report)
        .join(Report, ReportDelivery.report_id == Report.id)
        .order_by(ReportDelivery.id.desc()).limit(limit).offset(offset)).all()
    out = []
    for delivery, report in rows:
        receipt = db.scalar(select(ReportReceipt)
                            .where(ReportReceipt.delivery_id == delivery.id)
                            .order_by(ReportReceipt.id.desc()).limit(1))
        out.append({"id": delivery.id, "report_id": report.id, "kind": report.kind,
                    "business_date": report.business_date, "version": report.version,
                    "channel": delivery.channel, "target": delivery.target,
                    "mode": delivery.mode, "status": delivery.status,
                    "sent_at": delivery.sent_at,
                    "receipt_status": receipt.status if receipt else None,
                    "received_at": receipt.received_at if receipt else None})
    return {"total": total, "rows": out}


@router.get("/{report_id}")
def report_detail(report_id: int, p: Principal = Depends(require_auth),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    sources = db.execute(select(ReportSource).where(ReportSource.report_id == report_id)
                         .order_by(ReportSource.id)).scalars()
    deliveries = db.execute(select(ReportDelivery).where(ReportDelivery.report_id == report_id)
                            .order_by(ReportDelivery.id)).scalars()
    out_deliveries = []
    for delivery in deliveries:
        receipts = db.execute(select(ReportReceipt)
                              .where(ReportReceipt.delivery_id == delivery.id)
                              .order_by(ReportReceipt.id)).scalars()
        out_deliveries.append({
            "id": delivery.id, "channel": delivery.channel, "target": delivery.target,
            "mode": delivery.mode, "status": delivery.status, "sent_at": delivery.sent_at,
            "send_result": delivery.send_result,
            "receipts": [{"id": r.id, "status": r.status, "received_at": r.received_at,
                          "evidence": r.evidence} for r in receipts]})
    return {"report": {**_report(report), "payload": report.payload},
            "sources": [{"id": s.id, "source": s.source, "locator": s.locator,
                         "sha256": s.sha256, "detail": s.detail} for s in sources],
            "deliveries": out_deliveries}
