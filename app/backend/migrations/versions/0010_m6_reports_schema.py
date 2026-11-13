"""m6 reports schema

Revision ID: 0010
Revises: 0009

Frozen reports, their evidence bindings, delivery attempts and independent
recipient receipts.  Reports are immutable once frozen; a changed body needs a
new version.  Shadow delivery never sends.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text("'{}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        'report',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('content_sha256', sa.String(length=64), nullable=False),
        _jsonb('payload'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_report')),
        sa.UniqueConstraint('kind', 'business_date', 'version', name='uq_report_kind_date_version'),
        schema='reports',
    )
    op.create_index(op.f('ix_report_kind_business_date'), 'report', ['kind', 'business_date'],
                    schema='reports')
    op.create_table(
        'report_source',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('report_id', sa.BigInteger(), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('locator', sa.Text(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        _jsonb('detail'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['report_id'], ['reports.report.id'],
                                name=op.f('fk_report_source_report_id_report'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_report_source')),
        schema='reports',
    )
    op.create_index(op.f('ix_report_source_report_id'), 'report_source', ['report_id'],
                    schema='reports')
    op.create_table(
        'report_delivery',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('report_id', sa.BigInteger(), nullable=False),
        sa.Column('channel', sa.String(length=32), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('dedup_key', sa.String(length=256), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        _jsonb('send_result'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['report_id'], ['reports.report.id'],
                                name=op.f('fk_report_delivery_report_id_report'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_report_delivery')),
        sa.UniqueConstraint('dedup_key', name='uq_report_delivery_dedup_key'),
        schema='reports',
    )
    op.create_index(op.f('ix_report_delivery_report_id'), 'report_delivery', ['report_id'],
                    schema='reports')
    op.create_table(
        'report_receipt',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('delivery_id', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        _jsonb('evidence'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['delivery_id'], ['reports.report_delivery.id'],
                                name=op.f('fk_report_receipt_delivery_id_report_delivery'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_report_receipt')),
        schema='reports',
    )
    op.create_index(op.f('ix_report_receipt_delivery_id'), 'report_receipt', ['delivery_id'],
                    schema='reports')


def downgrade() -> None:
    for table in ('report_receipt', 'report_delivery', 'report_source', 'report'):
        op.drop_table(table, schema='reports')
