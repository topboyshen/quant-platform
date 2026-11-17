from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base, TimestampMixin


class JobDef(TimestampMixin, Base):
    __tablename__ = "job_def"
    __table_args__ = {"schema": "ops"}
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    schedule_hint: Mapped[str | None] = mapped_column(String(128))  # informational; systemd timer owns the schedule
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class JobRun(Base):
    __tablename__ = "job_run"
    __table_args__ = (
        # at most one successful run per (job, dedup_key)
        Index(
            "uq_job_run_success_dedup",
            "job_name",
            "dedup_key",
            unique=True,
            postgresql_where=text("status = 'succeeded' AND dedup_key IS NOT NULL"),
        ),
        Index("ix_job_run_job_started", "job_name", "started_at"),
        {"schema": "ops"},
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # running|succeeded|failed|skipped_locked|skipped_dup
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")  # timer|manual|api
    host: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float | None] = mapped_column(Float)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class HealthCheck(Base):
    __tablename__ = "health_check"
    __table_args__ = {"schema": "ops"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    check: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)


class Alert(TimestampMixin, Base):
    __tablename__ = "alert"
    __table_args__ = {"schema": "ops"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="firing")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshot"
    __table_args__ = {"schema": "ops"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class LegacyUnit(Base):
    """Read-only register of legacy systemd units/timers (task center). qp never controls these."""

    __tablename__ = "legacy_unit"
    __table_args__ = {"schema": "ops"}
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)  # system|user
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # service|timer
    description: Mapped[str | None] = mapped_column(Text)
    load_state: Mapped[str | None] = mapped_column(String(32))
    active_state: Mapped[str | None] = mapped_column(String(32))
    sub_state: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[str | None] = mapped_column(String(32))
    exit_status: Mapped[str | None] = mapped_column(String(16))
    run_user: Mapped[str | None] = mapped_column(String(64))
    fragment: Mapped[str | None] = mapped_column(Text)
    activates: Mapped[str | None] = mapped_column(String(256))
    triggered_by: Mapped[str | None] = mapped_column(String(256))
    last_trigger: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_elapse: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_exit: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Inventory(Base):
    """Latest snapshot per kind (containers, ports, certs, egress, logs, host, pg, coverage, services, projects)."""

    __tablename__ = "inventory"
    __table_args__ = {"schema": "ops"}
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data: Mapped[Any] = mapped_column(JSONB, nullable=False)
