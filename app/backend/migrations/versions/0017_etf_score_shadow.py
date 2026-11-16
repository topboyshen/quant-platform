"""M15 ETF research score shadow: imported score snapshots (read-only, no orders)

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text("'{}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        'etf_score_run',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('model_version', sa.String(length=64), nullable=False),
        sa.Column('model_sha256', sa.String(length=64), nullable=False),
        sa.Column('data_cutoff', sa.Date(), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('label_horizon', sa.Integer(), nullable=False),
        sa.Column('train_start', sa.Date(), nullable=True),
        sa.Column('train_end', sa.Date(), nullable=True),
        sa.Column('data_master_snapshot', sa.String(length=64), nullable=True),
        _jsonb('metrics'),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_etf_score_run')),
        sa.UniqueConstraint('model_version', 'data_cutoff',
                            name=op.f('uq_etf_score_run_version_cutoff')),
        schema='research')
    op.create_index('ix_etf_score_run_cutoff', 'etf_score_run', ['data_cutoff'],
                    unique=False, schema='research')

    op.create_table(
        'etf_score',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('run_id', sa.BigInteger(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('score', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('eligible', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['research.etf_score_run.id'],
                                name=op.f('fk_etf_score_run_id'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_etf_score')),
        sa.UniqueConstraint('run_id', 'code', name=op.f('uq_etf_score_run_code')),
        schema='research')
    op.create_index('ix_etf_score_run_id', 'etf_score', ['run_id'], unique=False, schema='research')
    op.create_index('ix_etf_score_run_rank', 'etf_score', ['run_id', 'rank'], unique=False,
                    schema='research')


def downgrade() -> None:
    op.drop_index('ix_etf_score_run_rank', table_name='etf_score', schema='research')
    op.drop_index('ix_etf_score_run_id', table_name='etf_score', schema='research')
    op.drop_table('etf_score', schema='research')
    op.drop_index('ix_etf_score_run_cutoff', table_name='etf_score_run', schema='research')
    op.drop_table('etf_score_run', schema='research')
