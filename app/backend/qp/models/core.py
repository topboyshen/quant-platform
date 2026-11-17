from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "user"
    __table_args__ = {"schema": "core"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash_scheme: Mapped[str] = mapped_column(String(32), nullable=False)  # argon2 | legacy_bcrypt | legacy_sha256
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str | None] = mapped_column(String(64))  # e.g. quantdinger:qd_users:1
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Session(TimestampMixin, Base):
    __tablename__ = "session"
    __table_args__ = {"schema": "core"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("core.user.id", ondelete="CASCADE"), nullable=False, index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))


class ServiceToken(TimestampMixin, Base):
    __tablename__ = "service_token"
    __table_args__ = {"schema": "core"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = {"schema": "core"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Setting(Base):
    __tablename__ = "setting"
    __table_args__ = {"schema": "core"}
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
