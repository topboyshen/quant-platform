"""reports schema (M6): frozen reports, evidence bindings, deliveries and receipts.

A report is immutable once frozen (same kind/date/version cannot be replaced).
Delivery and receipt are separate rows so "sent" never implies "received".
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base

S = {"schema": "reports"}


def _json_col(default: str = "'{}'::jsonb"):
    return mapped_column(JSONB, nullable=False, server_default=text(default), default=dict)


class Report(Base):
    __tablename__ = "report"
    __table_args__ = (
        UniqueConstraint("kind", "business_date", "version", name="uq_report_kind_date_version"),
        Index("ix_report_kind_business_date", "kind", "business_date"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="frozen")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    frozen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ReportSource(Base):
    __tablename__ = "report_source"
    __table_args__ = (Index("ix_report_source_report_id", "report_id"), S)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.report.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    locator: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReportDelivery(Base):
    __tablename__ = "report_delivery"
    __table_args__ = (
        Index("ix_report_delivery_report_id", "report_id"),
        UniqueConstraint("dedup_key", name="uq_report_delivery_dedup_key"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.report.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="shadow")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    dedup_key: Mapped[str] = mapped_column(String(256), nullable=False)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    send_result: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReportReceipt(Base):
    __tablename__ = "report_receipt"
    __table_args__ = (Index("ix_report_receipt_delivery_id", "delivery_id"), S)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("reports.report_delivery.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    received_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
