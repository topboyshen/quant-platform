"""M6 report archive: freeze immutable reports, deliveries and independent receipts.

A frozen report cannot be replaced: the same (kind, business_date, version) with a
different canonical payload raises :class:`ReportConflict`.  Delivery status and
recipient receipt are deliberately separate; a sent/shadowed delivery never implies
the recipient received it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource

DELIVERY_STATUSES = {"pending", "shadowed", "sent", "failed", "unknown"}
RECEIPT_STATUSES = {"pending", "received", "failed", "unknown"}


class ReportConflict(RuntimeError):
    """The same report key was frozen with different content."""


@dataclass(frozen=True)
class SourceRef:
    source: str
    locator: str | None = None
    sha256: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def freeze_report(
    session: Session,
    *,
    kind: str,
    business_date: dt.date,
    payload: dict[str, Any],
    sources: Iterable[SourceRef] = (),
    version: int = 1,
) -> tuple[Report, bool]:
    """Freeze a report; returns (report, created).  Idempotent on identical content."""
    digest = payload_sha256(payload)
    existing = session.scalar(
        select(Report).where(Report.kind == kind, Report.business_date == business_date, Report.version == version)
    )
    if existing is not None:
        if existing.content_sha256 != digest:
            raise ReportConflict(
                f"{kind} {business_date.isoformat()} v{version} is already frozen with a different payload"
            )
        return existing, False
    report = Report(
        kind=kind,
        business_date=business_date,
        version=version,
        status="frozen",
        content_sha256=digest,
        payload=payload,
        frozen_at=utcnow(),
    )
    session.add(report)
    session.flush()
    for ref in sources:
        session.add(
            ReportSource(
                report_id=report.id, source=ref.source, locator=ref.locator, sha256=ref.sha256, detail=ref.detail
            )
        )
    session.flush()
    return report, True


def record_delivery(
    session: Session, report: Report, *, channel: str, target: str, mode: str = "shadow", dedup_key: str | None = None
) -> ReportDelivery:
    if mode not in ("shadow", "live"):
        raise ValueError(f"bad delivery mode: {mode}")
    key = dedup_key or (f"report:{report.kind}:{report.business_date.isoformat()}:{report.version}:{channel}:{target}")
    existing = session.scalar(select(ReportDelivery).where(ReportDelivery.dedup_key == key))
    if existing is not None:
        return existing
    delivery = ReportDelivery(
        report_id=report.id, channel=channel, target=target, mode=mode, status="pending", dedup_key=key
    )
    session.add(delivery)
    session.flush()
    return delivery


def mark_delivery(
    session: Session,
    delivery: ReportDelivery,
    *,
    status: str,
    send_result: dict[str, Any] | None = None,
    sent_at: dt.datetime | None = None,
) -> ReportDelivery:
    if status not in DELIVERY_STATUSES:
        raise ValueError(f"bad delivery status: {status}")
    delivery.status = status
    if status != "pending":
        delivery.sent_at = sent_at or utcnow()
    if send_result is not None:
        delivery.send_result = send_result
    session.flush()
    return delivery


def record_receipt(
    session: Session,
    delivery: ReportDelivery,
    *,
    status: str,
    evidence: dict[str, Any] | None = None,
    received_at: dt.datetime | None = None,
) -> ReportReceipt:
    if status not in RECEIPT_STATUSES:
        raise ValueError(f"bad receipt status: {status}")
    receipt = ReportReceipt(
        delivery_id=delivery.id,
        status=status,
        evidence=evidence or {},
        received_at=received_at or (utcnow() if status == "received" else None),
    )
    session.add(receipt)
    session.flush()
    return receipt
