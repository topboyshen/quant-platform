"""signals schema (M4): swing EOD runs, macro risk, target decisions, trend signals, outcomes,
macro slices, approved-signal intake and execution digests. SHADOW only: writes go to the qp
database; legacy SQLite/state files are read-only inputs for comparison."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String,
                        Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base

S = {"schema": "signals"}


def _now_col():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _json_col(default: str = "'{}'::jsonb"):
    return mapped_column(JSONB, nullable=False, server_default=text(default), default=dict)


class EodRun(Base):
    __tablename__ = "eod_run"
    __table_args__ = (
        UniqueConstraint("decision_date", "attempt_no", name="uq_eod_run_decision_date_attempt_no"),
        S,
    )
    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    decision_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success|partial|failed|skipped|running
    decision_outcome: Mapped[str | None] = mapped_column(String(24))  # no_signal|emitted|blocked|data_unavailable|skipped
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allowed_signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_codes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"),
                                                    default=list)
    details: Mapped[dict[str, Any]] = _json_col()
    legacy_run_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[dt.datetime] = _now_col()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class RiskSnapshot(Base):
    __tablename__ = "risk_snapshot"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_risk_snapshot_run_id"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("signals.eod_run.run_id", ondelete="CASCADE"), nullable=False)
    decision_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[str] = mapped_column(String(24), nullable=False)  # ok|warning|critical|growth_blocked|unavailable
    values: Mapped[dict[str, Any]] = _json_col()
    sources: Mapped[dict[str, Any]] = _json_col()
    observed_at: Mapped[dict[str, Any]] = _json_col()
    available_at: Mapped[dict[str, Any]] = _json_col()
    missing_fields: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"),
                                                      default=list)


class TargetDecision(Base):
    __tablename__ = "target_decision"
    __table_args__ = (
        UniqueConstraint("run_id", "code", name="uq_target_decision_run_id_code"),
        Index("ix_target_decision_decision_date_code", "decision_date", "code"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("signals.eod_run.run_id", ondelete="CASCADE"), nullable=False,
                                        index=True)
    decision_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sector: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    data_status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok|failed
    decision_state: Mapped[str] = mapped_column(String(16), nullable=False)  # no_trigger|data_failed|blocked|allowed
    risk_action: Mapped[str] = mapped_column(String(24), nullable=False)  # not_applicable|blocked|allowed
    risk_level: Mapped[str | None] = mapped_column(String(24))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_close: Mapped[float | None] = mapped_column(Numeric(18, 4))
    raw_trigger_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_signals: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"),
                                                   default=list)
    validation_split: Mapped[str] = mapped_column(String(16), nullable=False, default="live_oos")


class TrendSignal(Base):
    __tablename__ = "trend_signal"
    __table_args__ = (
        UniqueConstraint("decision_date", "code", "signal_type", name="uq_trend_signal_decision_date_code_signal_type"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    decision_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    signal_type: Mapped[str] = mapped_column(String(48), nullable=False)
    period: Mapped[str] = mapped_column(String(4), nullable=False)
    data_date: Mapped[dt.date | None] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detail: Mapped[dict[str, Any]] = _json_col()
    confidence: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionOutcome(Base):
    __tablename__ = "decision_outcome"
    __table_args__ = (
        UniqueConstraint("decision_id", "horizon_sessions", name="uq_decision_outcome_decision_id_horizon_sessions"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    decision_id: Mapped[int] = mapped_column(
        ForeignKey("signals.target_decision.id", ondelete="CASCADE"), nullable=False, index=True)
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    entry_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    gross_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    net_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    benchmark_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    excess_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    fill_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unfilled")  # filled|unfilled
    fill_reason: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = _json_col()


class MacroSlice(Base):
    __tablename__ = "macro_slice"
    __table_args__ = S
    decision_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    total_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    margin_balance: Mapped[float | None] = mapped_column(Numeric(18, 2))
    margin_growth_pct: Mapped[float | None] = mapped_column(Numeric(12, 3))
    limit_up: Mapped[int | None] = mapped_column(Integer)
    limit_down: Mapped[int | None] = mapped_column(Integer)
    limit_ratio: Mapped[float | None] = mapped_column(Numeric(12, 3))
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    collect_status: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()


class RiskState(Base):
    __tablename__ = "risk_state"
    __table_args__ = (
        Index("ix_risk_state_observed_at", "observed_at"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    values: Mapped[dict[str, Any]] = _json_col()
    sources: Mapped[dict[str, Any]] = _json_col()
    override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()


class ApprovedIntake(Base):
    __tablename__ = "approved_intake"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_approved_intake_idempotency_key"),
        Index("ix_approved_intake_received_at", "received_at"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # accepted|rejected
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_check: Mapped[dict[str, Any]] = _json_col()
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = _json_col()


class ExecutionDigest(Base):
    __tablename__ = "execution_digest"
    __table_args__ = S
    digest_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    body: Mapped[dict[str, Any]] = _json_col()
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    pushed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    push_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|shadowed|sent|failed
