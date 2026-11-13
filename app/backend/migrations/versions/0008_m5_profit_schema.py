"""m5 profit research schema

Revision ID: 0008
Revises: 0007

Mirror of the QuantDinger profit-research history: runs, picks, daily observations and
run attempts.  Imported read-only; the legacy worker keeps running untouched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text("'{}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        'profit_run',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('run_key', sa.String(length=128), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('signal_date', sa.Date(), nullable=True),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('data_ready', sa.Boolean(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('rule_version', sa.Text(), nullable=True),
        sa.Column('release_sha256', sa.String(length=128), nullable=True),
        sa.Column('payload_sha256', sa.String(length=128), nullable=True),
        sa.Column('candidate_count', sa.Integer(), nullable=True),
        sa.Column('latest_market_date', sa.Date(), nullable=True),
        sa.Column('universe_count', sa.Integer(), nullable=True),
        sa.Column('quote_coverage', sa.Numeric(), nullable=True),
        sa.Column('preselected_count', sa.Integer(), nullable=True),
        sa.Column('history_coverage', sa.Numeric(), nullable=True),
        sa.Column('benchmark_symbol', sa.String(length=32), nullable=True),
        _jsonb('backtest_json'),
        _jsonb('paper_json'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_profit_run')),
        schema='research',
    )
    op.create_index(op.f('ix_profit_run_run_key'), 'profit_run', ['run_key'], schema='research')
    op.create_index(op.f('ix_profit_run_signal_date'), 'profit_run', ['signal_date'],
                    schema='research')

    op.create_table(
        'profit_pick',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('pick_key', sa.String(length=128), nullable=True),
        sa.Column('source_sha256', sa.String(length=128), nullable=True),
        sa.Column('run_id', sa.BigInteger(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('signal_date', sa.Date(), nullable=True),
        sa.Column('rule_version', sa.Text(), nullable=True),
        sa.Column('symbol', sa.String(length=32), nullable=True),
        sa.Column('code', sa.String(length=16), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('signal_price', sa.Numeric(), nullable=True),
        sa.Column('paper_weight', sa.Numeric(), nullable=True),
        sa.Column('signal_score', sa.Numeric(), nullable=True),
        sa.Column('m20', sa.Numeric(), nullable=True),
        sa.Column('m60', sa.Numeric(), nullable=True),
        sa.Column('m120', sa.Numeric(), nullable=True),
        sa.Column('atr_pct', sa.Numeric(), nullable=True),
        sa.Column('entry_ma20', sa.Numeric(), nullable=True),
        sa.Column('invalidation_ma60', sa.Numeric(), nullable=True),
        sa.Column('entry_max_gap_pct', sa.Numeric(), nullable=True),
        sa.Column('max_holding_sessions', sa.Integer(), nullable=True),
        sa.Column('single_side_cost', sa.Numeric(), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=True),
        sa.Column('adjustment', sa.String(length=32), nullable=True),
        sa.Column('benchmark_symbol', sa.String(length=32), nullable=True),
        sa.Column('state', sa.Text(), nullable=True),
        sa.Column('state_reason', sa.Text(), nullable=True),
        sa.Column('entry_date', sa.Date(), nullable=True),
        sa.Column('entry_price', sa.Numeric(), nullable=True),
        sa.Column('entry_benchmark_price', sa.Numeric(), nullable=True),
        sa.Column('exit_signal_date', sa.Date(), nullable=True),
        sa.Column('exit_date', sa.Date(), nullable=True),
        sa.Column('exit_price', sa.Numeric(), nullable=True),
        sa.Column('exit_benchmark_price', sa.Numeric(), nullable=True),
        sa.Column('exit_reason', sa.Text(), nullable=True),
        sa.Column('holding_sessions', sa.Integer(), nullable=True),
        sa.Column('realized_return', sa.Numeric(), nullable=True),
        sa.Column('benchmark_return', sa.Numeric(), nullable=True),
        sa.Column('excess_return', sa.Numeric(), nullable=True),
        sa.Column('latest_trade_date', sa.Date(), nullable=True),
        sa.Column('latest_price', sa.Numeric(), nullable=True),
        sa.Column('latest_unrealized_return', sa.Numeric(), nullable=True),
        sa.Column('latest_benchmark_return', sa.Numeric(), nullable=True),
        sa.Column('latest_excess_return', sa.Numeric(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_profit_pick')),
        schema='research',
    )
    op.create_index(op.f('ix_profit_pick_pick_key'), 'profit_pick', ['pick_key'], schema='research')
    op.create_index(op.f('ix_profit_pick_run_id'), 'profit_pick', ['run_id'], schema='research')
    op.create_index(op.f('ix_profit_pick_signal_date'), 'profit_pick', ['signal_date'],
                    schema='research')

    op.create_table(
        'profit_observation',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('pick_id', sa.BigInteger(), nullable=True),
        sa.Column('trade_date', sa.Date(), nullable=True),
        sa.Column('event', sa.Text(), nullable=True),
        sa.Column('state_before', sa.Text(), nullable=True),
        sa.Column('state_after', sa.Text(), nullable=True),
        sa.Column('open', sa.Numeric(), nullable=True),
        sa.Column('high', sa.Numeric(), nullable=True),
        sa.Column('low', sa.Numeric(), nullable=True),
        sa.Column('close', sa.Numeric(), nullable=True),
        sa.Column('benchmark_open', sa.Numeric(), nullable=True),
        sa.Column('benchmark_close', sa.Numeric(), nullable=True),
        sa.Column('return_basis_price', sa.Numeric(), nullable=True),
        sa.Column('benchmark_return_basis_price', sa.Numeric(), nullable=True),
        sa.Column('threshold_scale', sa.Numeric(), nullable=True),
        sa.Column('holding_sessions', sa.Integer(), nullable=True),
        sa.Column('gross_return', sa.Numeric(), nullable=True),
        sa.Column('net_return', sa.Numeric(), nullable=True),
        sa.Column('benchmark_return', sa.Numeric(), nullable=True),
        sa.Column('excess_return', sa.Numeric(), nullable=True),
        sa.Column('source_sha256', sa.String(length=128), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_profit_observation')),
        schema='research',
    )
    op.create_index(op.f('ix_profit_observation_pick_id'), 'profit_observation', ['pick_id'],
                    schema='research')

    op.create_table(
        'profit_run_attempt',
        sa.Column('execution_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('run_id', sa.BigInteger(), nullable=True),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('signal_date', sa.Date(), nullable=True),
        sa.Column('rule_version', sa.Text(), nullable=True),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('release_sha256', sa.Text(), nullable=True),
        sa.Column('request_sha256', sa.Text(), nullable=True),
        _jsonb('run_document'),
        _jsonb('result_document'),
        _jsonb('previous_projection'),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('execution_id', name=op.f('pk_profit_run_attempt')),
        schema='research',
    )
    op.create_index(op.f('ix_profit_run_attempt_run_id'), 'profit_run_attempt', ['run_id'],
                    schema='research')


def downgrade() -> None:
    for table in ('profit_run_attempt', 'profit_observation', 'profit_pick', 'profit_run'):
        op.drop_table(table, schema='research')
