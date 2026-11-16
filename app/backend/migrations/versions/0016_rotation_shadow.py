"""M14 C13 cross-sectional ETF rotation shadow: bars, rebalance runs and daily NAV

Revision ID: 0016
Revises: 0015

The rotation shadow is validation-only: its portfolio, trades and NAV live in
dedicated ``signals.rotation_shadow_*`` tables and never feed target_decision,
the record chain or notifications.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.create_table(
        'rotation_shadow_bar',
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('trade_date', sa.Date(), nullable=False),
        sa.Column('open', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('high', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('low', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('close', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('volume', sa.Numeric(precision=24, scale=4), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('code', 'trade_date', name=op.f('pk_rotation_shadow_bar')),
        schema='signals')
    op.create_index('ix_rotation_shadow_bar_date', 'rotation_shadow_bar', ['trade_date'],
                    unique=False, schema='signals')

    op.create_table(
        'rotation_shadow_account',
        sa.Column('rule_version', sa.String(length=32), nullable=False),
        sa.Column('phase', sa.String(length=16), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('baseline_date', sa.Date(), nullable=False),
        _jsonb('parameters'),
        _jsonb('quality'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('rule_version', 'phase', name=op.f('pk_rotation_shadow_account')),
        schema='signals')

    op.create_table(
        'rotation_shadow_run',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('rule_version', sa.String(length=32), nullable=False),
        sa.Column('phase', sa.String(length=16), nullable=False),
        sa.Column('signal_date', sa.Date(), nullable=False),
        sa.Column('execute_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('universe', sa.Integer(), nullable=False),
        sa.Column('top_k', sa.Integer(), nullable=False),
        sa.Column('selected', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('nav_before', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('nav_after', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('turnover', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column('fills', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        _jsonb('detail'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_rotation_shadow_run')),
        sa.UniqueConstraint('rule_version', 'phase', 'signal_date', name=op.f('uq_rotation_shadow_run_signal')),
        schema='signals')
    op.create_index('ix_rotation_shadow_run_execute', 'rotation_shadow_run',
                    ['status', 'execute_date'], unique=False, schema='signals')

    op.create_table(
        'rotation_shadow_day',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('rule_version', sa.String(length=32), nullable=False),
        sa.Column('phase', sa.String(length=16), nullable=False),
        sa.Column('trade_date', sa.Date(), nullable=False),
        sa.Column('session_index', sa.Integer(), nullable=False),
        sa.Column('nav', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('cash', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('invested', sa.Numeric(precision=18, scale=4), nullable=False),
        _jsonb('positions'),
        sa.Column('benchmark_nav', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('ew_nav', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('exposure', sa.Numeric(precision=10, scale=4), nullable=False),
        _jsonb('detail'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_rotation_shadow_day')),
        sa.UniqueConstraint('rule_version', 'phase', 'trade_date', name=op.f('uq_rotation_shadow_day_date')),
        schema='signals')
    op.create_index('ix_rotation_shadow_day_date', 'rotation_shadow_day', ['trade_date'],
                    unique=False, schema='signals')


def downgrade() -> None:
    op.drop_index('ix_rotation_shadow_day_date', table_name='rotation_shadow_day', schema='signals')
    op.drop_table('rotation_shadow_day', schema='signals')
    op.drop_index('ix_rotation_shadow_run_execute', table_name='rotation_shadow_run', schema='signals')
    op.drop_table('rotation_shadow_run', schema='signals')
    op.drop_table('rotation_shadow_account', schema='signals')
    op.drop_index('ix_rotation_shadow_bar_date', table_name='rotation_shadow_bar', schema='signals')
    op.drop_table('rotation_shadow_bar', schema='signals')
