"""ETF record chain (M10): decision identity, revisioned states, eligibility, snapshots.

Ported from the 2026-11-10 design freeze.  Facts are append-only; numbers are only
assembled in ``chain_snapshot``.  These tables coexist with ``target_decision`` and
``decision_outcome`` and never change them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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


def _json(default: str = "'{}'::jsonb"):
    return mapped_column(JSONB, nullable=False, server_default=text(default), default=dict)


def _list():
    return mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChainPolicy(Base):
    __tablename__ = "chain_policy"
    __table_args__ = (
        CheckConstraint("kind IN ('eligibility','stats','rule','universe','engine')", name="kind"),
        CheckConstraint("length(definition_sha256) = 64", name="sha"),
        S,
    )
    policy_version: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any]] = _json()
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frozen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()


class ChainUniverse(Base):
    __tablename__ = "chain_universe"
    __table_args__ = (
        CheckConstraint("code_count >= 0", name="count"),
        CheckConstraint("length(checksum) = 64", name="checksum"),
        S,
    )
    universe_version: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class ChainUniverseMember(Base):
    __tablename__ = "chain_universe_member"
    __table_args__ = S
    universe_version: Mapped[str] = mapped_column(
        Text, ForeignKey("signals.chain_universe.universe_version"), primary_key=True
    )
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)


class ChainBatch(Base):
    __tablename__ = "chain_batch"
    __table_args__ = (
        UniqueConstraint("trade_date", "source", "scheduled_at", name="uq_chain_batch_date_source_at"),
        CheckConstraint("source IN ('timer','manual','legacy')", name="source"),
        CheckConstraint("expected_target_count > 0", name="expected"),
        S,
    )
    batch_key: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    scheduled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    universe_version: Mapped[str] = mapped_column(
        Text, ForeignKey("signals.chain_universe.universe_version"), nullable=False
    )
    expected_target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class ChainDecision(Base):
    __tablename__ = "chain_decision"
    __table_args__ = (
        UniqueConstraint(
            "trade_date", "code", "batch_key", "rule_version", "universe_version", name="uq_chain_decision_identity"
        ),
        CheckConstraint("origin IN ('native','migrated_v1')", name="origin"),
        CheckConstraint("attribution_status IN ('confirmed','unknown')", name="attribution"),
        CheckConstraint("length(decision_uid) = 35 AND left(decision_uid, 3) = 'qd_'", name="uid"),
        CheckConstraint("origin = 'native' OR imported_at IS NOT NULL", name="imported"),
        Index("ix_chain_decision_date_code", "trade_date", "code"),
        Index("ix_chain_decision_batch", "batch_key"),
        Index("ix_chain_decision_origin", "origin", "trade_date"),
        S,
    )
    decision_uid: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    batch_key: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_batch.batch_key"), nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_policy.policy_version"), nullable=False)
    universe_version: Mapped[str] = mapped_column(
        Text, ForeignKey("signals.chain_universe.universe_version"), nullable=False
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    attribution_status: Mapped[str] = mapped_column(Text, nullable=False)
    input_evidence_ref: Mapped[str | None] = mapped_column(Text)
    input_evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = _now()
    imported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ChainAttempt(Base):
    __tablename__ = "chain_attempt"
    __table_args__ = (
        UniqueConstraint("run_id", "decision_uid", name="uq_chain_attempt_run_decision"),
        CheckConstraint("length(attempt_id) = 36 AND left(attempt_id, 4) = 'att_'", name="aid"),
        Index("ix_chain_attempt_decision", "decision_uid", "started_at"),
        Index(
            "uq_chain_attempt_source_decision",
            "source_decision_id",
            unique=True,
            postgresql_where=text("source_decision_id IS NOT NULL"),
        ),
        S,
    )
    attempt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_uid: Mapped[str] = mapped_column(
        Text, ForeignKey("signals.chain_decision.decision_uid", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    engine_version: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    source_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = _now()


class ChainState(Base):
    __tablename__ = "chain_state"
    __table_args__ = (
        UniqueConstraint("decision_uid", "aspect", "state_seq", name="uq_chain_state_revision"),
        CheckConstraint("state_seq >= 1", name="seq"),
        CheckConstraint(
            "(aspect = 'data' AND state IN ('complete','missing','stale','collection_failed'))"
            " OR (aspect = 'signal' AND state IN ('not_triggered','triggered'))"
            " OR (aspect = 'risk' AND state IN ('allowed','blocked'))",
            name="aspect",
        ),
        Index("ix_chain_state_aspect", "aspect", "state"),
        S,
    )
    state_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_uid: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_decision.decision_uid"), nullable=False)
    source_attempt_id: Mapped[str | None] = mapped_column(Text, ForeignKey("signals.chain_attempt.attempt_id"))
    aspect: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[dict[str, Any]] = _json()
    evidence_refs: Mapped[list[Any]] = _list()
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_state_id: Mapped[str | None] = mapped_column(Text, ForeignKey("signals.chain_state.state_id"))
    created_at: Mapped[dt.datetime] = _now()


class ChainOutcome(Base):
    __tablename__ = "chain_outcome"
    __table_args__ = (
        UniqueConstraint("decision_uid", "horizon_sessions", "revision", name="uq_chain_outcome_revision"),
        CheckConstraint("horizon_sessions IN (1,3,5,10,20)", name="horizon"),
        CheckConstraint("revision >= 1", name="revision_no"),
        CheckConstraint("execution_state IN ('unverified','filled','unfilled','expired')", name="execution"),
        CheckConstraint(
            "result_state IN ('pending_maturity','matured','expired_uncollected','invalid')", name="result"
        ),
        CheckConstraint(
            "(execution_state IN ('unfilled','expired')"
            " AND result_state IN ('invalid','expired_uncollected'))"
            " OR execution_state IN ('unverified','filled')",
            name="axes",
        ),
        CheckConstraint("source_kind IN ('native','migrated_v1')", name="source_kind"),
        Index("ix_chain_outcome_due", "expected_maturity_date", "execution_state"),
        Index("ix_chain_outcome_result", "result_state"),
        S,
    )
    outcome_uid: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_uid: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_decision.decision_uid"), nullable=False)
    source_attempt_id: Mapped[str | None] = mapped_column(Text, ForeignKey("signals.chain_attempt.attempt_id"))
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_state: Mapped[str] = mapped_column(Text, nullable=False)
    result_state: Mapped[str] = mapped_column(Text, nullable=False)
    expected_maturity_date: Mapped[dt.date | None] = mapped_column(Date)
    evaluation_as_of: Mapped[dt.date | None] = mapped_column(Date)
    entry_date: Mapped[dt.date | None] = mapped_column(Date)
    entry_open: Mapped[float | None] = mapped_column(Numeric(18, 4))
    entry_fill: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_close: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exit_fill: Mapped[float | None] = mapped_column(Numeric(18, 4))
    benchmark_code: Mapped[str | None] = mapped_column(Text)
    benchmark_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    gap_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    gross_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    net_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    excess_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    mfe: Mapped[float | None] = mapped_column(Numeric(18, 8))
    mae: Mapped[float | None] = mapped_column(Numeric(18, 8))
    fees_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    slippage_return: Mapped[float | None] = mapped_column(Numeric(18, 8))
    fill_reason: Mapped[str | None] = mapped_column(Text)
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    revision_reason: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    evidence_link_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()


class ChainEligibility(Base):
    __tablename__ = "chain_eligibility"
    __table_args__ = (
        UniqueConstraint("decision_uid", "policy_version", "evaluation_seq", name="uq_chain_eligibility_revision"),
        CheckConstraint("verdict IN ('eligible','excluded','unknown')", name="verdict"),
        CheckConstraint("evaluation_seq >= 1", name="seq"),
        Index("ix_chain_eligibility_verdict", "policy_version", "verdict", "reason_code"),
        S,
    )
    eligibility_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_uid: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_decision.decision_uid"), nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_policy.policy_version"), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    reason_detail: Mapped[dict[str, Any]] = _json()
    evidence_refs: Mapped[list[Any]] = _list()
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    universe_version: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_eligibility_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("signals.chain_eligibility.eligibility_id")
    )
    created_at: Mapped[dt.datetime] = _now()


class ChainSnapshot(Base):
    __tablename__ = "chain_snapshot"
    __table_args__ = (
        UniqueConstraint("business_date", "protocol_version", "cutoff_at", "digest", name="uq_chain_snapshot_identity"),
        CheckConstraint("length(digest) = 64", name="digest"),
        CheckConstraint("length(snapshot_id) = 36 AND left(snapshot_id, 4) = 'snp_'", name="id"),
        Index("ix_chain_snapshot_date", "business_date", "generated_at"),
        S,
    )
    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    cutoff_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    eligibility_policy_version: Mapped[str] = mapped_column(
        Text, ForeignKey("signals.chain_policy.policy_version"), nullable=False
    )
    universe_version: Mapped[str | None] = mapped_column(Text)
    batch_keys: Mapped[list[Any]] = _list()
    body: Mapped[dict[str, Any]] = _json()
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generator_version: Mapped[str] = mapped_column(Text, nullable=False)


class ChainSnapshotSupersede(Base):
    __tablename__ = "chain_snapshot_supersede"
    __table_args__ = (
        CheckConstraint("snapshot_id <> superseded_by", name="distinct"),
        S,
    )
    snapshot_id: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_snapshot.snapshot_id"), primary_key=True)
    superseded_by: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_snapshot.snapshot_id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChainSnapshotItem(Base):
    __tablename__ = "chain_snapshot_item"
    __table_args__ = (
        CheckConstraint("section IN ('funnel','sample','progress','loss')", name="section"),
        Index("ix_chain_snapshot_item_bucket", "snapshot_id", "section", "bucket"),
        S,
    )
    snapshot_id: Mapped[str] = mapped_column(Text, ForeignKey("signals.chain_snapshot.snapshot_id"), primary_key=True)
    section: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    item_key: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_uid: Mapped[str | None] = mapped_column(Text, ForeignKey("signals.chain_decision.decision_uid"))
    code: Mapped[str | None] = mapped_column(Text)
    batch_key: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    eligibility_id: Mapped[str | None] = mapped_column(Text)
    outcome_uid: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = _json()


class ChainEvidenceLink(Base):
    __tablename__ = "chain_evidence_link"
    __table_args__ = (
        UniqueConstraint(
            "owner_kind", "owner_id", "sidecar_kind", "sidecar_path", "checked_at", name="uq_chain_evidence_link_check"
        ),
        CheckConstraint("owner_kind IN ('decision','outcome')", name="owner"),
        CheckConstraint("db_state IN ('present','missing')", name="db"),
        CheckConstraint("sidecar_state IN ('pending','verified','failed')", name="sidecar"),
        Index("ix_chain_evidence_link_owner", "owner_kind", "owner_id"),
        S,
    )
    link_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    sidecar_kind: Mapped[str] = mapped_column(Text, nullable=False)
    sidecar_path: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    observed_sha256: Mapped[str | None] = mapped_column(String(64))
    db_state: Mapped[str] = mapped_column(Text, nullable=False)
    sidecar_state: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = _json()
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class ChainImportRegister(Base):
    __tablename__ = "chain_import_register"
    __table_args__ = (
        UniqueConstraint("source_table", "source_pk", name="uq_chain_import_register_source"),
        CheckConstraint("mapping_status IN ('mapped','unknown','conflict')", name="status"),
        Index("ix_chain_import_register_status", "mapping_status"),
        S,
    )
    import_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_pk: Mapped[str] = mapped_column(Text, nullable=False)
    decision_uid: Mapped[str | None] = mapped_column(Text, ForeignKey("signals.chain_decision.decision_uid"))
    mapping_status: Mapped[str] = mapped_column(Text, nullable=False)
    original_business_date: Mapped[dt.date | None] = mapped_column(Date)
    original_created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict[str, Any]] = _json()
