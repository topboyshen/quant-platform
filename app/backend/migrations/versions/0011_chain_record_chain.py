"""M10 ETF record chain: identity, six-state split, eligibility, snapshot

Revision ID: 0011
Revises: 0010

Ports the frozen record-chain semantics (design freeze 2026-11-10) onto the
qp signals schema.  Facts stay append-only: state/outcome/eligibility rows are
revisions, a snapshot is the only place numbers are assembled.  The new tables
coexist with target_decision/decision_outcome; nothing here changes them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SHA = 64


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(default), nullable=False)


def _ts(name: str, nullable: bool = False, default: bool = True) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()") if default else None, nullable=nullable
    )


def upgrade() -> None:
    op.create_table(
        "chain_policy",
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        _jsonb("definition"),
        sa.Column("definition_sha256", sa.String(SHA), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("policy_version", name=op.f("pk_chain_policy")),
        sa.CheckConstraint(
            "kind IN ('eligibility','stats','rule','universe','engine')", name=op.f("ck_chain_policy_kind")
        ),
        sa.CheckConstraint("length(definition_sha256) = 64", name=op.f("ck_chain_policy_sha")),
        schema="signals",
    )

    op.create_table(
        "chain_universe",
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("code_count", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(SHA), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("universe_version", name=op.f("pk_chain_universe")),
        sa.CheckConstraint("code_count >= 0", name=op.f("ck_chain_universe_count")),
        sa.CheckConstraint("length(checksum) = 64", name=op.f("ck_chain_universe_checksum")),
        schema="signals",
    )

    op.create_table(
        "chain_universe_member",
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("sector", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["universe_version"],
            ["signals.chain_universe.universe_version"],
            name=op.f("fk_chain_universe_member_universe_version_chain_universe"),
        ),
        sa.PrimaryKeyConstraint("universe_version", "code", name=op.f("pk_chain_universe_member")),
        schema="signals",
    )

    op.create_table(
        "chain_batch",
        sa.Column("batch_key", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("expected_target_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["universe_version"],
            ["signals.chain_universe.universe_version"],
            name=op.f("fk_chain_batch_universe_version_chain_universe"),
        ),
        sa.PrimaryKeyConstraint("batch_key", name=op.f("pk_chain_batch")),
        sa.UniqueConstraint("trade_date", "source", "scheduled_at", name=op.f("uq_chain_batch_date_source_at")),
        sa.CheckConstraint("source IN ('timer','manual','legacy')", name=op.f("ck_chain_batch_source")),
        sa.CheckConstraint("expected_target_count > 0", name=op.f("ck_chain_batch_expected")),
        schema="signals",
    )

    op.create_table(
        "chain_decision",
        sa.Column("decision_uid", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("batch_key", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("attribution_status", sa.Text(), nullable=False),
        sa.Column("input_evidence_ref", sa.Text(), nullable=True),
        sa.Column("input_evidence_sha256", sa.String(SHA), nullable=True),
        _ts("created_at"),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_key"], ["signals.chain_batch.batch_key"], name=op.f("fk_chain_decision_batch_key_chain_batch")
        ),
        sa.ForeignKeyConstraint(
            ["rule_version"],
            ["signals.chain_policy.policy_version"],
            name=op.f("fk_chain_decision_rule_version_chain_policy"),
        ),
        sa.ForeignKeyConstraint(
            ["universe_version"],
            ["signals.chain_universe.universe_version"],
            name=op.f("fk_chain_decision_universe_version_chain_universe"),
        ),
        sa.PrimaryKeyConstraint("decision_uid", name=op.f("pk_chain_decision")),
        sa.UniqueConstraint(
            "trade_date",
            "code",
            "batch_key",
            "rule_version",
            "universe_version",
            name=op.f("uq_chain_decision_identity"),
        ),
        sa.CheckConstraint("origin IN ('native','migrated_v1')", name=op.f("ck_chain_decision_origin")),
        sa.CheckConstraint("attribution_status IN ('confirmed','unknown')", name=op.f("ck_chain_decision_attribution")),
        sa.CheckConstraint(
            "length(decision_uid) = 35 AND left(decision_uid, 3) = 'qd_'", name=op.f("ck_chain_decision_uid")
        ),
        sa.CheckConstraint("origin = 'native' OR imported_at IS NOT NULL", name=op.f("ck_chain_decision_imported")),
        schema="signals",
    )

    op.create_table(
        "chain_attempt",
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("decision_uid", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engine_version", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("source_decision_id", sa.BigInteger(), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["decision_uid"],
            ["signals.chain_decision.decision_uid"],
            name=op.f("fk_chain_attempt_decision_uid_chain_decision"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_chain_attempt")),
        sa.UniqueConstraint("run_id", "decision_uid", name=op.f("uq_chain_attempt_run_decision")),
        sa.CheckConstraint(
            "length(attempt_id) = 36 AND left(attempt_id, 4) = 'att_'", name=op.f("ck_chain_attempt_aid")
        ),
        schema="signals",
    )
    op.create_index(
        "uq_chain_attempt_source_decision",
        "chain_attempt",
        ["source_decision_id"],
        unique=True,
        schema="signals",
        postgresql_where=sa.text("source_decision_id IS NOT NULL"),
    )
    op.create_index("ix_chain_attempt_decision", "chain_attempt", ["decision_uid", "started_at"], schema="signals")
    op.create_index("ix_chain_decision_date_code", "chain_decision", ["trade_date", "code"], schema="signals")
    op.create_index("ix_chain_decision_batch", "chain_decision", ["batch_key"], schema="signals")
    op.create_index("ix_chain_decision_origin", "chain_decision", ["origin", "trade_date"], schema="signals")

    op.create_table(
        "chain_state",
        sa.Column("state_id", sa.Text(), nullable=False),
        sa.Column("decision_uid", sa.Text(), nullable=False),
        sa.Column("source_attempt_id", sa.Text(), nullable=True),
        sa.Column("aspect", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        _jsonb("basis"),
        _jsonb("evidence_refs", "'[]'::jsonb"),
        sa.Column("protocol_version", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_seq", sa.Integer(), nullable=False),
        sa.Column("supersedes_state_id", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["decision_uid"],
            ["signals.chain_decision.decision_uid"],
            name=op.f("fk_chain_state_decision_uid_chain_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["source_attempt_id"],
            ["signals.chain_attempt.attempt_id"],
            name=op.f("fk_chain_state_source_attempt_id_chain_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_state_id"],
            ["signals.chain_state.state_id"],
            name=op.f("fk_chain_state_supersedes_state_id_chain_state"),
        ),
        sa.PrimaryKeyConstraint("state_id", name=op.f("pk_chain_state")),
        sa.UniqueConstraint("decision_uid", "aspect", "state_seq", name=op.f("uq_chain_state_revision")),
        sa.CheckConstraint("state_seq >= 1", name=op.f("ck_chain_state_seq")),
        sa.CheckConstraint(
            "(aspect = 'data' AND state IN ('complete','missing','stale','collection_failed'))"
            " OR (aspect = 'signal' AND state IN ('not_triggered','triggered'))"
            " OR (aspect = 'risk' AND state IN ('allowed','blocked'))",
            name=op.f("ck_chain_state_aspect"),
        ),
        schema="signals",
    )
    op.create_index("ix_chain_state_aspect", "chain_state", ["aspect", "state"], schema="signals")

    op.create_table(
        "chain_outcome",
        sa.Column("outcome_uid", sa.Text(), nullable=False),
        sa.Column("decision_uid", sa.Text(), nullable=False),
        sa.Column("source_attempt_id", sa.Text(), nullable=True),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("execution_state", sa.Text(), nullable=False),
        sa.Column("result_state", sa.Text(), nullable=False),
        sa.Column("expected_maturity_date", sa.Date(), nullable=True),
        sa.Column("evaluation_as_of", sa.Date(), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("entry_open", sa.Numeric(18, 4), nullable=True),
        sa.Column("entry_fill", sa.Numeric(18, 4), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_close", sa.Numeric(18, 4), nullable=True),
        sa.Column("exit_fill", sa.Numeric(18, 4), nullable=True),
        sa.Column("benchmark_code", sa.Text(), nullable=True),
        sa.Column("benchmark_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("gap_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("gross_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("net_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("excess_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("mfe", sa.Numeric(18, 8), nullable=True),
        sa.Column("mae", sa.Numeric(18, 8), nullable=True),
        sa.Column("fees_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("slippage_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("fill_reason", sa.Text(), nullable=True),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("revision_reason", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("evidence_link_id", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["decision_uid"],
            ["signals.chain_decision.decision_uid"],
            name=op.f("fk_chain_outcome_decision_uid_chain_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["source_attempt_id"],
            ["signals.chain_attempt.attempt_id"],
            name=op.f("fk_chain_outcome_source_attempt_id_chain_attempt"),
        ),
        sa.PrimaryKeyConstraint("outcome_uid", name=op.f("pk_chain_outcome")),
        sa.UniqueConstraint("decision_uid", "horizon_sessions", "revision", name=op.f("uq_chain_outcome_revision")),
        sa.CheckConstraint("horizon_sessions IN (1,3,5,10,20)", name=op.f("ck_chain_outcome_horizon")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_chain_outcome_revision_no")),
        sa.CheckConstraint(
            "execution_state IN ('unverified','filled','unfilled','expired')", name=op.f("ck_chain_outcome_execution")
        ),
        sa.CheckConstraint(
            "result_state IN ('pending_maturity','matured','expired_uncollected','invalid')",
            name=op.f("ck_chain_outcome_result"),
        ),
        sa.CheckConstraint(
            "(execution_state IN ('unfilled','expired')"
            " AND result_state IN ('invalid','expired_uncollected'))"
            " OR execution_state IN ('unverified','filled')",
            name=op.f("ck_chain_outcome_axes"),
        ),
        sa.CheckConstraint("source_kind IN ('native','migrated_v1')", name=op.f("ck_chain_outcome_source_kind")),
        schema="signals",
    )
    op.create_index(
        "ix_chain_outcome_due", "chain_outcome", ["expected_maturity_date", "execution_state"], schema="signals"
    )
    op.create_index("ix_chain_outcome_result", "chain_outcome", ["result_state"], schema="signals")

    op.create_table(
        "chain_eligibility",
        sa.Column("eligibility_id", sa.Text(), nullable=False),
        sa.Column("decision_uid", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        _jsonb("reason_detail"),
        _jsonb("evidence_refs", "'[]'::jsonb"),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_seq", sa.Integer(), nullable=False),
        sa.Column("supersedes_eligibility_id", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["decision_uid"],
            ["signals.chain_decision.decision_uid"],
            name=op.f("fk_chain_eligibility_decision_uid_chain_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["signals.chain_policy.policy_version"],
            name=op.f("fk_chain_eligibility_policy_version_chain_policy"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_eligibility_id"],
            ["signals.chain_eligibility.eligibility_id"],
            name=op.f("fk_chain_eligibility_supersedes_chain_eligibility"),
        ),
        sa.PrimaryKeyConstraint("eligibility_id", name=op.f("pk_chain_eligibility")),
        sa.UniqueConstraint(
            "decision_uid", "policy_version", "evaluation_seq", name=op.f("uq_chain_eligibility_revision")
        ),
        sa.CheckConstraint("verdict IN ('eligible','excluded','unknown')", name=op.f("ck_chain_eligibility_verdict")),
        sa.CheckConstraint("evaluation_seq >= 1", name=op.f("ck_chain_eligibility_seq")),
        schema="signals",
    )
    op.create_index(
        "ix_chain_eligibility_verdict",
        "chain_eligibility",
        ["policy_version", "verdict", "reason_code"],
        schema="signals",
    )

    op.create_table(
        "chain_snapshot",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("protocol_version", sa.Text(), nullable=False),
        sa.Column("eligibility_policy_version", sa.Text(), nullable=False),
        sa.Column("universe_version", sa.Text(), nullable=True),
        _jsonb("batch_keys", "'[]'::jsonb"),
        _jsonb("body"),
        sa.Column("digest", sa.String(SHA), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["eligibility_policy_version"],
            ["signals.chain_policy.policy_version"],
            name=op.f("fk_chain_snapshot_policy_chain_policy"),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_chain_snapshot")),
        sa.UniqueConstraint(
            "business_date", "protocol_version", "cutoff_at", "digest", name=op.f("uq_chain_snapshot_identity")
        ),
        sa.CheckConstraint("length(digest) = 64", name=op.f("ck_chain_snapshot_digest")),
        sa.CheckConstraint(
            "length(snapshot_id) = 36 AND left(snapshot_id, 4) = 'snp_'", name=op.f("ck_chain_snapshot_id")
        ),
        schema="signals",
    )
    op.create_index("ix_chain_snapshot_date", "chain_snapshot", ["business_date", "generated_at"], schema="signals")

    op.create_table(
        "chain_snapshot_supersede",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("superseded_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["signals.chain_snapshot.snapshot_id"],
            name=op.f("fk_chain_snapshot_supersede_snapshot_id_chain_snapshot"),
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["signals.chain_snapshot.snapshot_id"],
            name=op.f("fk_chain_snapshot_supersede_superseded_by_chain_snapshot"),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_chain_snapshot_supersede")),
        sa.CheckConstraint("snapshot_id <> superseded_by", name=op.f("ck_chain_snapshot_supersede_distinct")),
        schema="signals",
    )

    op.create_table(
        "chain_snapshot_item",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("decision_uid", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("batch_key", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("eligibility_id", sa.Text(), nullable=True),
        sa.Column("outcome_uid", sa.Text(), nullable=True),
        _jsonb("detail"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["signals.chain_snapshot.snapshot_id"],
            name=op.f("fk_chain_snapshot_item_snapshot_id_chain_snapshot"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_uid"],
            ["signals.chain_decision.decision_uid"],
            name=op.f("fk_chain_snapshot_item_decision_uid_chain_decision"),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "section", "bucket", "item_key", name=op.f("pk_chain_snapshot_item")),
        sa.CheckConstraint(
            "section IN ('funnel','sample','progress','loss')", name=op.f("ck_chain_snapshot_item_section")
        ),
        schema="signals",
    )
    op.create_index(
        "ix_chain_snapshot_item_bucket", "chain_snapshot_item", ["snapshot_id", "section", "bucket"], schema="signals"
    )

    op.create_table(
        "chain_evidence_link",
        sa.Column("link_id", sa.Text(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("sidecar_kind", sa.Text(), nullable=False),
        sa.Column("sidecar_path", sa.Text(), nullable=False),
        sa.Column("expected_sha256", sa.String(SHA), nullable=True),
        sa.Column("observed_sha256", sa.String(SHA), nullable=True),
        sa.Column("db_state", sa.Text(), nullable=False),
        sa.Column("sidecar_state", sa.Text(), nullable=False),
        _jsonb("detail"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("link_id", name=op.f("pk_chain_evidence_link")),
        sa.UniqueConstraint(
            "owner_kind",
            "owner_id",
            "sidecar_kind",
            "sidecar_path",
            "checked_at",
            name=op.f("uq_chain_evidence_link_check"),
        ),
        sa.CheckConstraint("owner_kind IN ('decision','outcome')", name=op.f("ck_chain_evidence_link_owner")),
        sa.CheckConstraint("db_state IN ('present','missing')", name=op.f("ck_chain_evidence_link_db")),
        sa.CheckConstraint(
            "sidecar_state IN ('pending','verified','failed')", name=op.f("ck_chain_evidence_link_sidecar")
        ),
        schema="signals",
    )
    op.create_index("ix_chain_evidence_link_owner", "chain_evidence_link", ["owner_kind", "owner_id"], schema="signals")

    op.create_table(
        "chain_import_register",
        sa.Column("import_id", sa.Text(), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_pk", sa.Text(), nullable=False),
        sa.Column("decision_uid", sa.Text(), nullable=True),
        sa.Column("mapping_status", sa.Text(), nullable=False),
        sa.Column("original_business_date", sa.Date(), nullable=True),
        sa.Column("original_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb("detail"),
        sa.ForeignKeyConstraint(
            ["decision_uid"],
            ["signals.chain_decision.decision_uid"],
            name=op.f("fk_chain_import_register_decision_uid_chain_decision"),
        ),
        sa.PrimaryKeyConstraint("import_id", name=op.f("pk_chain_import_register")),
        sa.UniqueConstraint("source_table", "source_pk", name=op.f("uq_chain_import_register_source")),
        sa.CheckConstraint(
            "mapping_status IN ('mapped','unknown','conflict')", name=op.f("ck_chain_import_register_status")
        ),
        schema="signals",
    )
    op.create_index("ix_chain_import_register_status", "chain_import_register", ["mapping_status"], schema="signals")

    op.execute("""
        CREATE VIEW signals.chain_decision_canonical_attempt AS
        SELECT decision_uid, attempt_id, run_id, run_status
        FROM (
            SELECT a.decision_uid, a.attempt_id, a.run_id, r.status AS run_status,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.decision_uid
                       ORDER BY CASE WHEN r.status = 'success' THEN 0 ELSE 1 END,
                                COALESCE(a.completed_at, a.started_at) DESC NULLS LAST,
                                a.run_id DESC
                   ) AS rn
            FROM signals.chain_attempt a
            LEFT JOIN signals.eod_run r ON r.run_id = a.run_id
        ) ranked
        WHERE rn = 1
    """)
    op.execute("""
        CREATE VIEW signals.chain_decision_current_state AS
        SELECT s.*
        FROM signals.chain_state s
        JOIN signals.chain_decision_canonical_attempt c
          ON c.decision_uid = s.decision_uid AND c.attempt_id = s.source_attempt_id
        JOIN (
            SELECT decision_uid, source_attempt_id, aspect, MAX(state_seq) AS max_seq
            FROM signals.chain_state
            GROUP BY decision_uid, source_attempt_id, aspect
        ) m ON m.decision_uid = s.decision_uid
           AND m.source_attempt_id = s.source_attempt_id
           AND m.aspect = s.aspect AND m.max_seq = s.state_seq
    """)
    op.execute("""
        CREATE VIEW signals.chain_decision_current_outcome AS
        SELECT o.*
        FROM signals.chain_outcome o
        JOIN signals.chain_decision_canonical_attempt c
          ON c.decision_uid = o.decision_uid
        JOIN (
            SELECT o2.decision_uid, o2.horizon_sessions, MAX(o2.revision) AS max_revision
            FROM signals.chain_outcome o2
            JOIN signals.chain_decision_canonical_attempt c2
              ON c2.decision_uid = o2.decision_uid
            WHERE o2.source_attempt_id IS NULL OR o2.source_attempt_id = c2.attempt_id
            GROUP BY o2.decision_uid, o2.horizon_sessions
        ) m ON m.decision_uid = o.decision_uid
           AND m.horizon_sessions = o.horizon_sessions
           AND m.max_revision = o.revision
        WHERE o.source_attempt_id IS NULL OR o.source_attempt_id = c.attempt_id
    """)
    op.execute("""
        CREATE VIEW signals.chain_decision_current_eligibility AS
        SELECT e.*
        FROM signals.chain_eligibility e
        JOIN (
            SELECT decision_uid, policy_version, MAX(evaluation_seq) AS max_seq
            FROM signals.chain_eligibility
            GROUP BY decision_uid, policy_version
        ) m ON m.decision_uid = e.decision_uid
           AND m.policy_version = e.policy_version
           AND m.max_seq = e.evaluation_seq
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION signals.chain_forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in (
        "chain_decision",
        "chain_attempt",
        "chain_state",
        "chain_outcome",
        "chain_eligibility",
        "chain_snapshot",
        "chain_snapshot_supersede",
        "chain_snapshot_item",
        "chain_evidence_link",
        "chain_import_register",
    ):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON signals.{table}
            FOR EACH ROW EXECUTE FUNCTION signals.chain_forbid_mutation()
        """)


def downgrade() -> None:
    for view in (
        "chain_decision_current_eligibility",
        "chain_decision_current_outcome",
        "chain_decision_current_state",
        "chain_decision_canonical_attempt",
    ):
        op.execute(f"DROP VIEW IF EXISTS signals.{view}")
    # dropping the tables drops their triggers; the shared function goes last
    for table in (
        "chain_import_register",
        "chain_evidence_link",
        "chain_snapshot_item",
        "chain_snapshot_supersede",
        "chain_snapshot",
        "chain_eligibility",
        "chain_outcome",
        "chain_state",
        "chain_attempt",
        "chain_decision",
        "chain_batch",
        "chain_universe_member",
        "chain_universe",
        "chain_policy",
    ):
        op.drop_table(table, schema="signals")
    op.execute("DROP FUNCTION IF EXISTS signals.chain_forbid_mutation()")
