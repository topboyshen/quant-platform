"""research schema (M5): ledger mirror, phase2 lineage and importer bookkeeping.

SHADOW only: rows are imported read-only from `research_ledger` and the phase2 SQLite
database; qp never writes back to either source.  UUID columns are stored as text and
timestamps are kept as the source values (no local re-interpretation).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Double, Identity, Index, Integer,
                        Numeric, String, Text, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from qp.models.base import Base

S = {"schema": "research"}


def _json_col(default: str = "'{}'::jsonb"):
    return mapped_column(JSONB, nullable=False, server_default=text(default), default=dict)


class ForecastProtocol(Base):
    __tablename__ = "forecast_protocol"
    __table_args__ = (S,)
    protocol_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    protocol_version: Mapped[str | None] = mapped_column(Text)
    protocol_hash: Mapped[str | None] = mapped_column(Text)
    universe_rule_hash: Mapped[str | None] = mapped_column(Text)
    target_definition_hash: Mapped[str | None] = mapped_column(Text)
    source_policy_hash: Mapped[str | None] = mapped_column(Text)
    effective_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)


class ForecastBatch(Base):
    __tablename__ = "forecast_batch"
    __table_args__ = (Index("ix_forecast_batch_target_session", "target_session"), S)
    batch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    protocol_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    external_batch_key: Mapped[str | None] = mapped_column(Text)
    target_session: Mapped[dt.date | None] = mapped_column(Date)
    correlation_group: Mapped[str | None] = mapped_column(Text)
    expected_universe_size: Mapped[int | None] = mapped_column(Integer)
    qualified_coverage: Mapped[int | None] = mapped_column(Integer)
    batch_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(Text)
    imported_from: Mapped[str | None] = mapped_column(Text)


class Forecast(Base):
    __tablename__ = "forecast"
    __table_args__ = (Index("ix_forecast_batch_id", "batch_id"),
                      Index("ix_forecast_target_session", "target_session"), S)
    forecast_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    batch_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    protocol_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    parent_forecast_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    forecast_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    target_key: Mapped[str | None] = mapped_column(Text)
    metric_definition_hash: Mapped[str | None] = mapped_column(Text)
    target_session: Mapped[dt.date | None] = mapped_column(Date)
    window_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    probability: Mapped[float | None] = mapped_column(Numeric)
    confidence_level: Mapped[float | None] = mapped_column(Numeric)
    lower_bound: Mapped[float | None] = mapped_column(Numeric)
    upper_bound: Mapped[float | None] = mapped_column(Numeric)
    baseline_hash: Mapped[str | None] = mapped_column(Text)
    invalidation_hash: Mapped[str | None] = mapped_column(Text)
    settlement_rule_hash: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    feature_snapshot_hash: Mapped[str | None] = mapped_column(Text)
    source_hashes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    data_cutoff: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    prospective_eligible: Mapped[bool | None] = mapped_column(Boolean)
    retrospective_reason: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)


class ForecastOutcome(Base):
    __tablename__ = "forecast_outcome"
    __table_args__ = (Index("ix_forecast_outcome_forecast_id", "forecast_id"), S)
    outcome_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    forecast_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    revision_no: Mapped[int | None] = mapped_column(Integer)
    outcome_status: Mapped[str | None] = mapped_column(Text)
    observed_value: Mapped[float | None] = mapped_column(Numeric)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    input_hash: Mapped[str | None] = mapped_column(Text)
    rule_version: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[str | None] = mapped_column(Text)
    prior_outcome_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (S,)
    source_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    source_type: Mapped[str | None] = mapped_column(Text)
    source_locator: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    data_cutoff: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    license_class: Mapped[str | None] = mapped_column(Text)
    reliability_tier: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    captured_by: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False,
                                                 server_default=text("'{}'::jsonb"), default=dict)


class LedgerAudit(Base):
    __tablename__ = "ledger_audit"
    __table_args__ = (S,)
    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    input_hash: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False,
                                                 server_default=text("'{}'::jsonb"), default=dict)


class CalibrationSnapshot(Base):
    __tablename__ = "calibration_snapshot"
    __table_args__ = (S,)
    calibration_snapshot_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    metric_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    eligible_forecast_count: Mapped[int | None] = mapped_column(Integer)
    independent_batch_count: Mapped[int | None] = mapped_column(Integer)
    excluded_count: Mapped[int | None] = mapped_column(Integer)
    input_filter_hash: Mapped[str | None] = mapped_column(Text)
    metric_payload: Mapped[dict[str, Any]] = _json_col()
    computed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    computed_by: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    raw_due_forecast_count: Mapped[int | None] = mapped_column(Integer)
    raw_due_independent_batch_count: Mapped[int | None] = mapped_column(Integer)


class ModelArtifact(Base):
    __tablename__ = "model_artifact"
    __table_args__ = (S,)
    model_artifact_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    source_system: Mapped[str | None] = mapped_column(Text)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    artifact_hash: Mapped[str | None] = mapped_column(Text)
    artifact_role: Mapped[str | None] = mapped_column(Text)
    data_cutoff: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    universe_hash: Mapped[str | None] = mapped_column(Text)
    feature_hash: Mapped[str | None] = mapped_column(Text)
    label_hash: Mapped[str | None] = mapped_column(Text)
    code_hash: Mapped[str | None] = mapped_column(Text)
    config_hash: Mapped[str | None] = mapped_column(Text)
    random_seed: Mapped[str | None] = mapped_column(Text)
    split_hash: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    registered_by: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)


class ImportLineage(Base):
    __tablename__ = "import_lineage"
    __table_args__ = (Index("ix_import_lineage_source_record_id", "source_record_id"),
                      Index("ix_import_lineage_target_object_id", "target_object_id"), S)
    import_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    source_system: Mapped[str | None] = mapped_column(Text)
    source_database_hash: Mapped[str | None] = mapped_column(Text)
    source_table: Mapped[str | None] = mapped_column(Text)
    source_record_id: Mapped[str | None] = mapped_column(Text)
    source_record_hash: Mapped[str | None] = mapped_column(Text)
    original_recorded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    original_sealed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    import_batch_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    import_status: Mapped[str | None] = mapped_column(Text)
    target_object_type: Mapped[str | None] = mapped_column(Text)
    target_object_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    imported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    imported_by: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)


class Phase2Batch(Base):
    __tablename__ = "phase2_batch"
    __table_args__ = (S,)
    batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    cohort_id: Mapped[str | None] = mapped_column(Text)
    protocol_hash: Mapped[str | None] = mapped_column(Text)
    data_cutoff: Mapped[str | None] = mapped_column(Text)
    target_session: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    expected_n: Mapped[int | None] = mapped_column(Integer)
    nominal_n: Mapped[int | None] = mapped_column(Integer)
    qualified_n: Mapped[int | None] = mapped_column(Integer)
    independent_batch_count: Mapped[int | None] = mapped_column(Integer)
    block_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)
    sealed_at: Mapped[str | None] = mapped_column(Text)


class Phase2SourceSnapshot(Base):
    __tablename__ = "phase2_source_snapshot"
    __table_args__ = (Index("ix_phase2_source_snapshot_batch_id", "batch_id"), S)
    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    normalized_hash: Mapped[str | None] = mapped_column(Text)
    latest_session: Mapped[str | None] = mapped_column(Text)


class Phase2Exclusion(Base):
    __tablename__ = "phase2_exclusion"
    __table_args__ = (S,)
    exclusion_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)


class Phase2Protocol(Base):
    __tablename__ = "phase2_protocol"
    __table_args__ = (S,)
    protocol_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    protocol_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    protocol_json: Mapped[dict[str, Any]] = _json_col()
    frozen_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)


class Phase2RunState(Base):
    __tablename__ = "phase2_run_state"
    __table_args__ = (S,)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(Text)


class Phase2Audit(Base):
    __tablename__ = "phase2_audit"
    __table_args__ = (S,)
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    action: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = _json_col()


class ImportRun(Base):
    __tablename__ = "import_run"
    __table_args__ = (Index("ix_import_run_source_started_at", "source", "started_at"), S)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                                    server_default=func.now(), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    counts: Mapped[dict[str, Any]] = _json_col()
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- backtests
class BacktestRun(Base):
    __tablename__ = "backtest_run"
    __table_args__ = (Index("ix_backtest_run_status", "status"), S)
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    indicator_id: Mapped[int | None] = mapped_column(Integer)
    strategy_id: Mapped[int | None] = mapped_column(Integer)
    strategy_name: Mapped[str | None] = mapped_column(String(255))
    run_type: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(32))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    initial_capital: Mapped[float | None] = mapped_column(Numeric)
    commission: Mapped[float | None] = mapped_column(Numeric)
    slippage: Mapped[float | None] = mapped_column(Numeric)
    leverage: Mapped[int | None] = mapped_column(Integer)
    trade_direction: Mapped[str | None] = mapped_column(String(32))
    strategy_config: Mapped[dict[str, Any]] = _json_col()
    config_snapshot: Mapped[dict[str, Any]] = _json_col()
    engine_version: Mapped[str | None] = mapped_column(String(64))
    code_hash: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class BacktestEquity(Base):
    __tablename__ = "backtest_equity"
    __table_args__ = (S,)
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    point_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    point_time: Mapped[str | None] = mapped_column(String(64))
    point_value: Mapped[float | None] = mapped_column(Double)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class BacktestTrade(Base):
    __tablename__ = "backtest_trade"
    __table_args__ = (S,)
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    strategy_id: Mapped[int | None] = mapped_column(Integer)
    trade_time: Mapped[str | None] = mapped_column(String(64))
    trade_type: Mapped[str | None] = mapped_column(String(32))
    side: Mapped[str | None] = mapped_column(String(16))
    price: Mapped[float | None] = mapped_column(Double)
    amount: Mapped[float | None] = mapped_column(Double)
    profit: Mapped[float | None] = mapped_column(Double)
    balance: Mapped[float | None] = mapped_column(Double)
    reason: Mapped[str | None] = mapped_column(String(255))
    payload_json: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class BacktestOrder(Base):
    __tablename__ = "backtest_order"
    __table_args__ = (S,)
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    order_id: Mapped[str | None] = mapped_column(String(128))
    signal_index: Mapped[int | None] = mapped_column(Integer)
    signal_time: Mapped[str | None] = mapped_column(String(64))
    active_index: Mapped[int | None] = mapped_column(Integer)
    active_time: Mapped[str | None] = mapped_column(String(64))
    intent: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str | None] = mapped_column(String(16))
    order_type: Mapped[str | None] = mapped_column(String(32))
    time_in_force: Mapped[str | None] = mapped_column(String(32))
    requested_qty: Mapped[str | None] = mapped_column(Text)
    allocation_pct: Mapped[str | None] = mapped_column(Text)
    lot_size: Mapped[str | None] = mapped_column(Text)
    limit_price: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    terminal_index: Mapped[int | None] = mapped_column(Integer)
    terminal_time: Mapped[str | None] = mapped_column(String(64))
    terminal_code: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    message: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class BacktestFill(Base):
    __tablename__ = "backtest_fill"
    __table_args__ = (S,)
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fill_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    fill_id: Mapped[str | None] = mapped_column(String(128))
    order_seq: Mapped[int | None] = mapped_column(Integer)
    order_id: Mapped[str | None] = mapped_column(String(128))
    bar_index: Mapped[int | None] = mapped_column(Integer)
    fill_time: Mapped[str | None] = mapped_column(String(64))
    intent: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str | None] = mapped_column(String(16))
    qty: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(Text)
    notional: Mapped[str | None] = mapped_column(Text)
    commission: Mapped[str | None] = mapped_column(Text)
    realized_pnl: Mapped[str | None] = mapped_column(Text)
    cash_after: Mapped[str | None] = mapped_column(Text)
    equity_after: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class BacktestAccountPoint(Base):
    __tablename__ = "backtest_account_point"
    __table_args__ = (S,)
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    point_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    bar_index: Mapped[int | None] = mapped_column(Integer)
    point_time: Mapped[str | None] = mapped_column(String(64))
    mark_price: Mapped[str | None] = mapped_column(Text)
    cash: Mapped[str | None] = mapped_column(Text)
    equity: Mapped[str | None] = mapped_column(Text)
    position_qty: Mapped[str | None] = mapped_column(Text)
    average_entry: Mapped[str | None] = mapped_column(Text)
    opened_bar_index: Mapped[int | None] = mapped_column(Integer)
    realized_pnl: Mapped[str | None] = mapped_column(Text)
    unrealized_pnl: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


# --------------------------------------------------------------------------- AI records
class AiTask(Base):
    __tablename__ = "ai_task"
    __table_args__ = (S,)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    market: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str | None] = mapped_column(String(32))
    result_json: Mapped[dict[str, Any]] = _json_col()
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class AiMemory(Base):
    __tablename__ = "ai_memory"
    __table_args__ = (S,)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    market: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[int | None] = mapped_column(Integer)
    price_at_analysis: Mapped[float | None] = mapped_column(Numeric)
    summary: Mapped[str | None] = mapped_column(Text)
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False,
                                               server_default=text("'[]'::jsonb"), default=list)
    scores: Mapped[dict[str, Any]] = _json_col()
    indicators_snapshot: Mapped[dict[str, Any]] = _json_col()
    raw_result: Mapped[dict[str, Any]] = _json_col()
    consensus_score: Mapped[float | None] = mapped_column(Numeric)
    consensus_abs: Mapped[float | None] = mapped_column(Numeric)
    agreement_ratio: Mapped[float | None] = mapped_column(Numeric)
    quality_multiplier: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    validated_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    actual_outcome: Mapped[str | None] = mapped_column(String(32))
    actual_return_pct: Mapped[float | None] = mapped_column(Numeric)
    was_correct: Mapped[bool | None] = mapped_column(Boolean)
    user_feedback: Mapped[str | None] = mapped_column(String(32))
    feedback_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    task_status: Mapped[str | None] = mapped_column(String(32))
    task_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class AiSession(Base):
    __tablename__ = "ai_session"
    __table_args__ = (S,)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    market: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class AiMessage(Base):
    __tablename__ = "ai_message"
    __table_args__ = (Index("ix_ai_message_session_id", "session_id"), S)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class LlmCall(Base):
    __tablename__ = "llm_call"
    __table_args__ = (Index("ix_llm_call_request_sha256", "request_sha256"),
                      Index("ix_llm_call_created_at", "created_at"), S)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    shadow: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict[str, Any]] = _json_col()
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                                    server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- profit research
class ProfitRun(Base):
    __tablename__ = "profit_run"
    __table_args__ = (Index("ix_profit_run_run_key", "run_key"),
                      Index("ix_profit_run_signal_date", "signal_date"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_key: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[int | None] = mapped_column(Integer)
    signal_date: Mapped[dt.date | None] = mapped_column(Date)
    generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    data_ready: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    rule_version: Mapped[str | None] = mapped_column(Text)
    release_sha256: Mapped[str | None] = mapped_column(String(128))
    payload_sha256: Mapped[str | None] = mapped_column(String(128))
    candidate_count: Mapped[int | None] = mapped_column(Integer)
    latest_market_date: Mapped[dt.date | None] = mapped_column(Date)
    universe_count: Mapped[int | None] = mapped_column(Integer)
    quote_coverage: Mapped[float | None] = mapped_column(Numeric)
    preselected_count: Mapped[int | None] = mapped_column(Integer)
    history_coverage: Mapped[float | None] = mapped_column(Numeric)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32))
    backtest_json: Mapped[dict[str, Any]] = _json_col()
    paper_json: Mapped[dict[str, Any]] = _json_col()
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ProfitPick(Base):
    __tablename__ = "profit_pick"
    __table_args__ = (Index("ix_profit_pick_pick_key", "pick_key"),
                      Index("ix_profit_pick_run_id", "run_id"),
                      Index("ix_profit_pick_signal_date", "signal_date"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pick_key: Mapped[str | None] = mapped_column(String(128))
    source_sha256: Mapped[str | None] = mapped_column(String(128))
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(Integer)
    signal_date: Mapped[dt.date | None] = mapped_column(Date)
    rule_version: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(String(32))
    code: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(128))
    rank: Mapped[int | None] = mapped_column(Integer)
    signal_price: Mapped[float | None] = mapped_column(Numeric)
    paper_weight: Mapped[float | None] = mapped_column(Numeric)
    signal_score: Mapped[float | None] = mapped_column(Numeric)
    m20: Mapped[float | None] = mapped_column(Numeric)
    m60: Mapped[float | None] = mapped_column(Numeric)
    m120: Mapped[float | None] = mapped_column(Numeric)
    atr_pct: Mapped[float | None] = mapped_column(Numeric)
    entry_ma20: Mapped[float | None] = mapped_column(Numeric)
    invalidation_ma60: Mapped[float | None] = mapped_column(Numeric)
    entry_max_gap_pct: Mapped[float | None] = mapped_column(Numeric)
    max_holding_sessions: Mapped[int | None] = mapped_column(Integer)
    single_side_cost: Mapped[float | None] = mapped_column(Numeric)
    provider: Mapped[str | None] = mapped_column(String(32))
    adjustment: Mapped[str | None] = mapped_column(String(32))
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str | None] = mapped_column(Text)
    state_reason: Mapped[str | None] = mapped_column(Text)
    entry_date: Mapped[dt.date | None] = mapped_column(Date)
    entry_price: Mapped[float | None] = mapped_column(Numeric)
    entry_benchmark_price: Mapped[float | None] = mapped_column(Numeric)
    exit_signal_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_price: Mapped[float | None] = mapped_column(Numeric)
    exit_benchmark_price: Mapped[float | None] = mapped_column(Numeric)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    holding_sessions: Mapped[int | None] = mapped_column(Integer)
    realized_return: Mapped[float | None] = mapped_column(Numeric)
    benchmark_return: Mapped[float | None] = mapped_column(Numeric)
    excess_return: Mapped[float | None] = mapped_column(Numeric)
    latest_trade_date: Mapped[dt.date | None] = mapped_column(Date)
    latest_price: Mapped[float | None] = mapped_column(Numeric)
    latest_unrealized_return: Mapped[float | None] = mapped_column(Numeric)
    latest_benchmark_return: Mapped[float | None] = mapped_column(Numeric)
    latest_excess_return: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ProfitObservation(Base):
    __tablename__ = "profit_observation"
    __table_args__ = (Index("ix_profit_observation_pick_id", "pick_id"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pick_id: Mapped[int | None] = mapped_column(BigInteger)
    trade_date: Mapped[dt.date | None] = mapped_column(Date)
    event: Mapped[str | None] = mapped_column(Text)
    state_before: Mapped[str | None] = mapped_column(Text)
    state_after: Mapped[str | None] = mapped_column(Text)
    open: Mapped[float | None] = mapped_column(Numeric)
    high: Mapped[float | None] = mapped_column(Numeric)
    low: Mapped[float | None] = mapped_column(Numeric)
    close: Mapped[float | None] = mapped_column(Numeric)
    benchmark_open: Mapped[float | None] = mapped_column(Numeric)
    benchmark_close: Mapped[float | None] = mapped_column(Numeric)
    return_basis_price: Mapped[float | None] = mapped_column(Numeric)
    benchmark_return_basis_price: Mapped[float | None] = mapped_column(Numeric)
    threshold_scale: Mapped[float | None] = mapped_column(Numeric)
    holding_sessions: Mapped[int | None] = mapped_column(Integer)
    gross_return: Mapped[float | None] = mapped_column(Numeric)
    net_return: Mapped[float | None] = mapped_column(Numeric)
    benchmark_return: Mapped[float | None] = mapped_column(Numeric)
    excess_return: Mapped[float | None] = mapped_column(Numeric)
    source_sha256: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ProfitRunAttempt(Base):
    __tablename__ = "profit_run_attempt"
    __table_args__ = (Index("ix_profit_run_attempt_run_id", "run_id"), S)
    execution_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    signal_date: Mapped[dt.date | None] = mapped_column(Date)
    rule_version: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    release_sha256: Mapped[str | None] = mapped_column(Text)
    request_sha256: Mapped[str | None] = mapped_column(Text)
    run_document: Mapped[dict[str, Any]] = _json_col()
    result_document: Mapped[dict[str, Any]] = _json_col()
    previous_projection: Mapped[dict[str, Any]] = _json_col()
    recorded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- research projects
class Project(Base):
    __tablename__ = "project"
    __table_args__ = (Index("ix_project_public_id", "public_id"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[dict[str, Any]] = _json_col()
    goal: Mapped[str | None] = mapped_column(Text)
    default_branch_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int | None] = mapped_column(Integer)
    last_opened_branch_id: Mapped[int | None] = mapped_column(BigInteger)
    last_opened_module: Mapped[str | None] = mapped_column(Text)
    client_request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Branch(Base):
    __tablename__ = "branch"
    __table_args__ = (Index("ix_branch_project_id", "project_id"),
                      Index("ix_branch_public_id", "public_id"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(255))
    parent_branch_id: Mapped[int | None] = mapped_column(BigInteger)
    forked_from_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    head_hypothesis_id: Mapped[int | None] = mapped_column(BigInteger)
    head_indicator_id: Mapped[int | None] = mapped_column(BigInteger)
    head_strategy_id: Mapped[int | None] = mapped_column(BigInteger)
    head_review_id: Mapped[int | None] = mapped_column(BigInteger)
    head_run_request_id: Mapped[int | None] = mapped_column(BigInteger)
    head_evidence_id: Mapped[int | None] = mapped_column(BigInteger)
    revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    client_request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Admission(Base):
    __tablename__ = "admission"
    __table_args__ = (S,)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    writes_enabled: Mapped[bool | None] = mapped_column(Boolean)
    runs_enabled: Mapped[bool | None] = mapped_column(Boolean)
    revision: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[int | None] = mapped_column(Integer)


class AdmissionAudit(Base):
    __tablename__ = "admission_audit"
    __table_args__ = (S,)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    before: Mapped[dict[str, Any]] = _json_col()
    after: Mapped[dict[str, Any]] = _json_col()
    env_ceiling: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)


class Draft(Base):
    __tablename__ = "draft"
    __table_args__ = (Index("ix_draft_branch_id", "branch_id"), S)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    branch_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(Integer)
    module: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = _json_col()
    draft_revision: Mapped[int | None] = mapped_column(Integer)
    base_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_as_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at_draft_revision: Mapped[int | None] = mapped_column(Integer)
    saved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    saved_from: Mapped[str | None] = mapped_column(Text)
