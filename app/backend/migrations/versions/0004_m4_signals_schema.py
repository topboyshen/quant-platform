"""m4 signals schema

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eod_run",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision_outcome", sa.String(length=24), nullable=True),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("evaluated_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("signal_count", sa.Integer(), nullable=False),
        sa.Column("allowed_signal_count", sa.Integer(), nullable=False),
        sa.Column("blocked_signal_count", sa.Integer(), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("legacy_run_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_eod_run")),
        sa.UniqueConstraint("decision_date", "attempt_no", name=op.f("uq_eod_run_decision_date_attempt_no")),
        schema="signals",
    )
    op.create_table(
        "risk_snapshot",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column(
            "values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "sources", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "observed_at",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "missing_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["signals.eod_run.run_id"], name=op.f("fk_risk_snapshot_run_id_eod_run"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_snapshot")),
        sa.UniqueConstraint("run_id", name=op.f("uq_risk_snapshot_run_id")),
        schema="signals",
    )
    op.create_table(
        "target_decision",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("sector", sa.String(length=32), nullable=False),
        sa.Column("data_status", sa.String(length=16), nullable=False),
        sa.Column("decision_state", sa.String(length=16), nullable=False),
        sa.Column("risk_action", sa.String(length=24), nullable=False),
        sa.Column("risk_level", sa.String(length=24), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reference_close", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("raw_trigger_count", sa.Integer(), nullable=False),
        sa.Column(
            "raw_signals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("validation_split", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["signals.eod_run.run_id"], name=op.f("fk_target_decision_run_id_eod_run"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_decision")),
        sa.UniqueConstraint("run_id", "code", name=op.f("uq_target_decision_run_id_code")),
        schema="signals",
    )
    op.create_index(
        "ix_target_decision_decision_date_code",
        "target_decision",
        ["decision_date", "code"],
        unique=False,
        schema="signals",
    )
    op.create_index("ix_target_decision_run_id", "target_decision", ["run_id"], unique=False, schema="signals")
    op.create_table(
        "trend_signal",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("signal_type", sa.String(length=48), nullable=False),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=True),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("confidence", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trend_signal")),
        sa.UniqueConstraint(
            "decision_date", "code", "signal_type", name=op.f("uq_trend_signal_decision_date_code_signal_type")
        ),
        schema="signals",
    )
    op.create_table(
        "decision_outcome",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("decision_id", sa.BigInteger(), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("exit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("gross_return", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("net_return", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("benchmark_return", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("excess_return", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("fill_status", sa.String(length=16), nullable=False),
        sa.Column("fill_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["signals.target_decision.id"],
            name=op.f("fk_decision_outcome_decision_id_target_decision"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_outcome")),
        sa.UniqueConstraint(
            "decision_id", "horizon_sessions", name=op.f("uq_decision_outcome_decision_id_horizon_sessions")
        ),
        schema="signals",
    )
    op.create_index(
        "ix_decision_outcome_decision_id", "decision_outcome", ["decision_id"], unique=False, schema="signals"
    )
    op.create_table(
        "macro_slice",
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("margin_balance", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("margin_growth_pct", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("limit_up", sa.Integer(), nullable=True),
        sa.Column("limit_down", sa.Integer(), nullable=True),
        sa.Column("limit_ratio", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "collect_status",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("decision_date", name=op.f("pk_macro_slice")),
        schema="signals",
    )
    op.create_table(
        "risk_state",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column(
            "values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "sources", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("override", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "raw", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_state")),
        schema="signals",
    )
    op.create_index("ix_risk_state_observed_at", "risk_state", ["observed_at"], unique=False, schema="signals")
    op.create_table(
        "approved_intake",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "risk_check", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approved_intake")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_approved_intake_idempotency_key")),
        schema="signals",
    )
    op.create_index(
        "ix_approved_intake_received_at", "approved_intake", ["received_at"], unique=False, schema="signals"
    )
    op.create_table(
        "execution_digest",
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column(
            "body", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_state", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("digest_date", name=op.f("pk_execution_digest")),
        schema="signals",
    )


def downgrade() -> None:
    op.drop_table("execution_digest", schema="signals")
    op.drop_index("ix_approved_intake_received_at", table_name="approved_intake", schema="signals")
    op.drop_table("approved_intake", schema="signals")
    op.drop_index("ix_risk_state_observed_at", table_name="risk_state", schema="signals")
    op.drop_table("risk_state", schema="signals")
    op.drop_table("macro_slice", schema="signals")
    op.drop_index("ix_decision_outcome_decision_id", table_name="decision_outcome", schema="signals")
    op.drop_table("decision_outcome", schema="signals")
    op.drop_table("trend_signal", schema="signals")
    op.drop_index("ix_target_decision_run_id", table_name="target_decision", schema="signals")
    op.drop_index("ix_target_decision_decision_date_code", table_name="target_decision", schema="signals")
    op.drop_table("target_decision", schema="signals")
    op.drop_table("risk_snapshot", schema="signals")
    op.drop_table("eod_run", schema="signals")
