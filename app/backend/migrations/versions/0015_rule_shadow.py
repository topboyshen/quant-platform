"""M13 rule shadow: experimental rule signals and their outcomes

Revision ID: 0015
Revises: 0014

v2 research runs beside the live rule.  These tables never feed
target_decision, the record chain or notifications; they exist so a candidate
rule (for example qp-swing.v2 = daily volume breakout above MA60 at 2x volume)
can accumulate forward returns under the same execution model.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.create_table(
        'rule_shadow_signal',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('rule_version', sa.String(length=32), nullable=False),
        sa.Column('signal_type', sa.String(length=48), nullable=False),
        sa.Column('decision_date', sa.Date(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('sector', sa.String(length=32), nullable=False),
        sa.Column('reference_close', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('volume_ratio', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column('risk_level', sa.String(length=24), nullable=True),
        _jsonb('detail'),
        sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_rule_shadow_signal')),
        sa.UniqueConstraint('rule_version', 'decision_date', 'code', 'signal_type',
                            name=op.f('uq_rule_shadow_signal_identity')),
        schema='signals')
    op.create_index('ix_rule_shadow_signal_date', 'rule_shadow_signal', ['decision_date'],
                    unique=False, schema='signals')

    op.create_table(
        'rule_shadow_outcome',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('signal_id', sa.BigInteger(), nullable=False),
        sa.Column('horizon_sessions', sa.Integer(), nullable=False),
        sa.Column('entry_date', sa.Date(), nullable=True),
        sa.Column('exit_date', sa.Date(), nullable=True),
        sa.Column('entry_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('exit_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('gross_return', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('net_return', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('benchmark_return', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('excess_return', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('fill_status', sa.String(length=16), nullable=False),
        sa.Column('fill_reason', sa.String(length=64), nullable=True),
        _jsonb('details'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['signal_id'], ['signals.rule_shadow_signal.id'],
                                name=op.f('fk_rule_shadow_outcome_signal_id'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_rule_shadow_outcome')),
        sa.UniqueConstraint('signal_id', 'horizon_sessions',
                            name=op.f('uq_rule_shadow_outcome_signal_id_horizon')),
        schema='signals')
    op.create_index('ix_rule_shadow_outcome_signal_id', 'rule_shadow_outcome', ['signal_id'],
                    unique=False, schema='signals')


def downgrade() -> None:
    op.drop_index('ix_rule_shadow_outcome_signal_id', table_name='rule_shadow_outcome',
                  schema='signals')
    op.drop_table('rule_shadow_outcome', schema='signals')
    op.drop_index('ix_rule_shadow_signal_date', table_name='rule_shadow_signal', schema='signals')
    op.drop_table('rule_shadow_signal', schema='signals')
