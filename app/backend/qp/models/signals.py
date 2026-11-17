"""signals schema (M4): swing EOD runs, macro risk, target decisions, trend signals, outcomes,
macro slices, approved-signal intake and execution digests. SHADOW only: writes go to the qp
database; legacy SQLite/state files are read-only inputs for comparison."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    decision_outcome: Mapped[str | None] = mapped_column(
        String(24)
    )  # no_signal|emitted|blocked|data_unavailable|skipped
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allowed_signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_codes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
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
    missing_fields: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )


class TargetDecision(Base):
    __tablename__ = "target_decision"
    __table_args__ = (
        UniqueConstraint("run_id", "code", name="uq_target_decision_run_id_code"),
        Index("ix_target_decision_decision_date_code", "decision_date", "code"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("signals.eod_run.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    raw_signals: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
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
        ForeignKey("signals.target_decision.id", ondelete="CASCADE"), nullable=False, index=True
    )
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


class ShadowSignal(Base):
    """Counterfactual sample: a trend-triggered entry the risk gate blocked.

    Validation only: never notified, never part of target_decision/decision_outcome
    statistics, no effect on the production chain.
    """

    __tablename__ = "shadow_signal"
    __table_args__ = (
        UniqueConstraint("decision_date", "code", name="uq_shadow_signal_decision_date_code"),
        UniqueConstraint("source_decision_id", name="uq_shadow_signal_source_decision_id"),
        Index("ix_shadow_signal_decision_date", "decision_date"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    decision_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sector: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    source_decision_id: Mapped[int] = mapped_column(
        ForeignKey("signals.target_decision.id", ondelete="CASCADE"), nullable=False
    )
    source_run_id: Mapped[int | None] = mapped_column(BigInteger)
    risk_level: Mapped[str | None] = mapped_column(String(24))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    raw_trigger_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_signals: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    reference_close: Mapped[float | None] = mapped_column(Numeric(18, 4))
    recorded_at: Mapped[dt.datetime] = _now_col()


class ShadowOutcome(Base):
    __tablename__ = "shadow_outcome"
    __table_args__ = (
        UniqueConstraint("shadow_id", "horizon_sessions", name="uq_shadow_outcome_shadow_id_horizon_sessions"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_id: Mapped[int] = mapped_column(
        ForeignKey("signals.shadow_signal.id", ondelete="CASCADE"), nullable=False, index=True
    )
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    entry_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    gross_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    net_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    benchmark_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    excess_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    fill_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unfilled")
    fill_reason: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()


class RuleShadowSignal(Base):
    """A shadow signal produced by an experimental rule version (e.g. qp-swing.v2).

    Validation only: never written to target_decision, never notified, never part
    of the live chain.  ``detail`` keeps the rule diagnostics and the live risk
    level on that day so gated and ungated variants can be compared later.
    """

    __tablename__ = "rule_shadow_signal"
    __table_args__ = (
        UniqueConstraint("rule_version", "decision_date", "code", "signal_type", name="uq_rule_shadow_signal_identity"),
        Index("ix_rule_shadow_signal_date", "decision_date"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(48), nullable=False)
    decision_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sector: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reference_close: Mapped[float | None] = mapped_column(Numeric(18, 4))
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))
    risk_level: Mapped[str | None] = mapped_column(String(24))
    detail: Mapped[dict[str, Any]] = _json_col()
    recorded_at: Mapped[dt.datetime] = _now_col()


class RuleShadowOutcome(Base):
    __tablename__ = "rule_shadow_outcome"
    __table_args__ = (
        UniqueConstraint("signal_id", "horizon_sessions", name="uq_rule_shadow_outcome_signal_id_horizon"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.rule_shadow_signal.id", ondelete="CASCADE"), nullable=False, index=True
    )
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    entry_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    gross_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    net_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    benchmark_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    excess_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    fill_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unfilled")
    fill_reason: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()


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
    push_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending|shadowed|sent|failed


class RotationShadowBar(Base):
    """Daily ETF bars used only by the C13 rotation shadow (raw, unadjusted).

    Split normalization is only for ranking. Execution and valuation always use
    these raw prices; a new price discontinuity blocks the paper ledger.
    """

    __tablename__ = "rotation_shadow_bar"
    __table_args__ = (
        Index("ix_rotation_shadow_bar_date", "trade_date"),
        S,
    )
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[float] = mapped_column(Numeric(24, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="tencent_day")
    updated_at: Mapped[dt.datetime] = _now_col()


class RotationShadowAccount(Base):
    """Registered shadow contract and data quality, separate from immutable NAV."""

    __tablename__ = "rotation_shadow_account"
    __table_args__ = S
    rule_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    phase: Mapped[str] = mapped_column(String(16), primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    baseline_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    parameters: Mapped[dict[str, Any]] = _json_col()
    quality: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()


class RotationShadowRun(Base):
    """One C13 rebalance decision: signal close -> next-open execution."""

    __tablename__ = "rotation_shadow_run"
    __table_args__ = (
        UniqueConstraint("rule_version", "phase", "signal_date", name="uq_rotation_shadow_run_signal"),
        Index("ix_rotation_shadow_run_execute", "status", "execute_date"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="forward")
    signal_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    execute_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|executed
    universe: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    selected: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    nav_before: Mapped[float | None] = mapped_column(Numeric(18, 4))
    nav_after: Mapped[float | None] = mapped_column(Numeric(18, 4))
    turnover: Mapped[float | None] = mapped_column(Numeric(10, 4))
    fills: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    detail: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()


class RotationShadowDay(Base):
    """Daily mark-to-market of the rotation shadow and its two benchmarks."""

    __tablename__ = "rotation_shadow_day"
    __table_args__ = (
        UniqueConstraint("rule_version", "phase", "trade_date", name="uq_rotation_shadow_day_date"),
        Index("ix_rotation_shadow_day_date", "trade_date"),
        S,
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="forward")
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    session_index: Mapped[int] = mapped_column(Integer, nullable=False)
    nav: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    cash: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    invested: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    positions: Mapped[dict[str, Any]] = _json_col()
    benchmark_nav: Mapped[float | None] = mapped_column(Numeric(18, 4))
    ew_nav: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exposure: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    detail: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime] = _now_col()
