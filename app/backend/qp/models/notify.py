from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base, TimestampMixin


class Outbox(TimestampMixin, Base):
    __tablename__ = "outbox"
    __table_args__ = {"schema": "notify"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # telegram|...
    recipient: Mapped[str] = mapped_column(String(128), nullable=False)  # logical name, never a secret
    dedup_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="shadow")  # shadow|live
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|shadowed|sent|failed
    send_result: Mapped[str | None] = mapped_column(String(32))  # sender-side result
    receipt_status: Mapped[str | None] = mapped_column(String(32))  # recipient-side receipt (sent != received)
    source_job_run_id: Mapped[int | None] = mapped_column(BigInteger)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempt"
    __table_args__ = {"schema": "notify"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    outbox_id: Mapped[int] = mapped_column(ForeignKey("notify.outbox.id", ondelete="CASCADE"), nullable=False, index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    ok: Mapped[bool | None] = mapped_column()
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
