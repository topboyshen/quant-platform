"""M6.5 shadow delivery: record a report delivery and a shadow outbox row.

Nothing is sent.  ``notify.outbox`` keeps the cross-module view, while
``reports.report_delivery`` keeps the report-specific send result.  A recipient
receipt is recorded separately by :mod:`qp.reports.receipts`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.notify import Outbox
from qp.models.reports import Report, ReportDelivery
from qp.reports.archive import mark_delivery, record_delivery

SHADOW_NOTE = "shadow mode: not sent"


def _outbox_dedup_key(report: Report) -> str:
    return f"report:{report.kind}:{report.business_date.isoformat()}:v{report.version}"


def _body(report: Report) -> str:
    payload = report.payload if isinstance(report.payload, dict) else {}
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return f"{report.kind} {report.business_date.isoformat()} v{report.version}"


def shadow_deliver(
    session: Session, report: Report, *, channel: str = "telegram", target: str = "ops"
) -> tuple[ReportDelivery, Outbox | None]:
    """Create/refresh the shadow delivery for one report; idempotent."""
    delivery = record_delivery(session, report, channel=channel, target=target, mode="shadow")
    outbox = None
    if delivery.status == "pending":
        mark_delivery(session, delivery, status="shadowed", send_result={"note": SHADOW_NOTE})
        key = _outbox_dedup_key(report)
        outbox = session.scalar(select(Outbox).where(Outbox.dedup_key == key))
        if outbox is None:
            outbox = Outbox(
                channel=channel,
                recipient=target,
                dedup_key=key,
                subject=f"{report.kind} {report.business_date.isoformat()}",
                body=_body(report),
                mode="shadow",
                status="pending",
            )
            session.add(outbox)
            session.flush()
    return delivery, outbox
