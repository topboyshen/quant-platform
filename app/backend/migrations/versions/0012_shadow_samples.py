"""M12 shadow allowed samples: counterfactual entries blocked by the risk gate

Revision ID: 0012
Revises: 0011

The strategy can go months without an allowed entry (Sep–Oct: no trigger; Nov:
VIX > 17.5 blocks every trigger).  ``decision_outcome`` only materialises allowed
signals, so there is no forward return to validate the trend rule in such a
regime.  These two tables record the blocked-but-triggered entries and their
forward returns.  Validation only: no notification, no effect on the production
decision, chain or statistics.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.create_table(
        "shadow_signal",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("sector", sa.String(length=32), nullable=False),
        sa.Column("source_decision_id", sa.BigInteger(), nullable=False),
        sa.Column("source_run_id", sa.BigInteger(), nullable=True),
        sa.Column("risk_level", sa.String(length=24), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("raw_trigger_count", sa.Integer(), nullable=False),
        _jsonb("raw_signals", "'[]'::jsonb"),
        sa.Column("reference_close", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_decision_id"],
            ["signals.target_decision.id"],
            name=op.f("fk_shadow_signal_source_decision_id_target_decision"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_signal")),
        sa.UniqueConstraint("decision_date", "code", name=op.f("uq_shadow_signal_decision_date_code")),
        sa.UniqueConstraint("source_decision_id", name=op.f("uq_shadow_signal_source_decision_id")),
        sa.CheckConstraint("raw_trigger_count > 0", name=op.f("ck_shadow_signal_triggered")),
        schema="signals",
    )
    op.create_index(
        "ix_shadow_signal_decision_date", "shadow_signal", ["decision_date"], unique=False, schema="signals"
    )

    op.create_table(
        "shadow_outcome",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shadow_id", sa.BigInteger(), nullable=False),
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
        _jsonb("details"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["shadow_id"],
            ["signals.shadow_signal.id"],
            name=op.f("fk_shadow_outcome_shadow_id_shadow_signal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_outcome")),
        sa.UniqueConstraint("shadow_id", "horizon_sessions", name=op.f("uq_shadow_outcome_shadow_id_horizon_sessions")),
        sa.CheckConstraint("fill_status IN ('filled','unfilled')", name=op.f("ck_shadow_outcome_fill_status")),
        schema="signals",
    )
    op.create_index("ix_shadow_outcome_shadow_id", "shadow_outcome", ["shadow_id"], unique=False, schema="signals")


def downgrade() -> None:
    op.drop_index("ix_shadow_outcome_shadow_id", table_name="shadow_outcome", schema="signals")
    op.drop_table("shadow_outcome", schema="signals")
    op.drop_index("ix_shadow_signal_decision_date", table_name="shadow_signal", schema="signals")
    op.drop_table("shadow_signal", schema="signals")
