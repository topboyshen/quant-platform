"""market schema (M3): trading calendar, index closes, instrument master, Qlib catalog/status,
market summaries, data-source status, CNINFO counts, legacy-import reconciliation, compare and health
results. Rows carry ``origin``: 'qp' (fetched/computed by the new platform), 'legacy' (read-only
import of legacy outputs) or 'qlib' (read-only catalog of the legacy Qlib directory)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base

S = {"schema": "market"}


def _now_col():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _json_col():
    return mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)


class TradingDay(Base):
    __tablename__ = "trading_day"
    __table_args__ = S
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[dt.datetime] = _now_col()


class IndexClose(Base):
    __tablename__ = "index_close"
    __table_args__ = S
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = _now_col()


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = S
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)  # e.g. SH600000 (qlib style)
    name: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(8))
    board: Mapped[str | None] = mapped_column(String(16))
    qlib_start: Mapped[dt.date | None] = mapped_column(Date)
    qlib_end: Mapped[dt.date | None] = mapped_column(Date)
    in_qlib: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    name_source: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[dt.datetime] = _now_col()


class IndexMember(Base):
    __tablename__ = "index_member"
    __table_args__ = S
    index_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    end_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)


class MarketSummary(Base):
    __tablename__ = "market_summary"
    __table_args__ = S
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    data: Mapped[dict[str, Any]] = _json_col()
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = _now_col()


class LegacySnapshot(Base):
    """Index of legacy research-data snapshot.db rows (metadata + sha256 only, never payload)."""
    __tablename__ = "legacy_snapshot"
    __table_args__ = S
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    generated_at: Mapped[str | None] = mapped_column(String(40))
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)


class QlibStatus(Base):
    __tablename__ = "qlib_status"
    __table_args__ = S
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = _now_col()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # catalog | source
    data: Mapped[dict[str, Any]] = _json_col()


class SourceStatus(Base):
    __tablename__ = "source_status"
    __table_args__ = (Index("ix_source_status_source_at", "source", "at"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = _now_col()
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    via_proxy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[dict[str, Any]] = _json_col()


class AnnouncementCount(Base):
    __tablename__ = "announcement_count"
    __table_args__ = S
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[dt.datetime] = _now_col()
    detail: Mapped[dict[str, Any]] = _json_col()


class ImportLog(Base):
    __tablename__ = "import_log"
    __table_args__ = S
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = _now_col()
    item: Mapped[str] = mapped_column(String(64), nullable=False)
    legacy_path: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_file_sha256: Mapped[str | None] = mapped_column(String(64))
    legacy_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    legacy_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    db_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    db_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # match | mismatch


class CompareResult(Base):
    __tablename__ = "compare_result"
    __table_args__ = (Index("ix_compare_result_run_at", "run_at"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    item: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # match | mismatch | skipped | insufficient
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict[str, Any]] = _json_col()


class HealthResult(Base):
    __tablename__ = "health_result"
    __table_args__ = (Index("ix_health_result_run_at", "run_at"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | warning | critical | unknown
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict[str, Any]] = _json_col()
