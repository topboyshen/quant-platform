"""m5 research projects schema

Revision ID: 0009
Revises: 0008

Mirror of the QuantDinger research-lab structures: projects, branches, admission
switches, admission audit and drafts.  Read-only import; qp does not take over
writes during the shadow window.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text("'{}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        'project',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('public_id', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        _jsonb('subject'),
        sa.Column('goal', sa.Text(), nullable=True),
        sa.Column('default_branch_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=True),
        sa.Column('last_opened_branch_id', sa.BigInteger(), nullable=True),
        sa.Column('last_opened_module', sa.Text(), nullable=True),
        sa.Column('client_request_id', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_project')),
        schema='research',
    )
    op.create_index(op.f('ix_project_public_id'), 'project', ['public_id'], schema='research')
    op.create_table(
        'branch',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('public_id', sa.Text(), nullable=True),
        sa.Column('project_id', sa.BigInteger(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('parent_branch_id', sa.BigInteger(), nullable=True),
        sa.Column('forked_from_artifact_id', sa.BigInteger(), nullable=True),
        sa.Column('head_hypothesis_id', sa.BigInteger(), nullable=True),
        sa.Column('head_indicator_id', sa.BigInteger(), nullable=True),
        sa.Column('head_strategy_id', sa.BigInteger(), nullable=True),
        sa.Column('head_review_id', sa.BigInteger(), nullable=True),
        sa.Column('head_run_request_id', sa.BigInteger(), nullable=True),
        sa.Column('head_evidence_id', sa.BigInteger(), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('client_request_id', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_branch')),
        schema='research',
    )
    op.create_index(op.f('ix_branch_project_id'), 'branch', ['project_id'], schema='research')
    op.create_index(op.f('ix_branch_public_id'), 'branch', ['public_id'], schema='research')
    op.create_table(
        'admission',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('writes_enabled', sa.Boolean(), nullable=True),
        sa.Column('runs_enabled', sa.Boolean(), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_admission')),
        schema='research',
    )
    op.create_table(
        'admission_audit',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        _jsonb('before'),
        _jsonb('after'),
        sa.Column('env_ceiling', sa.Text(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_admission_audit')),
        schema='research',
    )
    op.create_table(
        'draft',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('branch_id', sa.BigInteger(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('module', sa.Text(), nullable=True),
        _jsonb('payload'),
        sa.Column('draft_revision', sa.Integer(), nullable=True),
        sa.Column('base_artifact_id', sa.BigInteger(), nullable=True),
        sa.Column('confirmed_as_artifact_id', sa.BigInteger(), nullable=True),
        sa.Column('confirmed_at_draft_revision', sa.Integer(), nullable=True),
        sa.Column('saved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('saved_from', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_draft')),
        schema='research',
    )
    op.create_index(op.f('ix_draft_branch_id'), 'draft', ['branch_id'], schema='research')


def downgrade() -> None:
    for table in ('draft', 'admission_audit', 'admission', 'branch', 'project'):
        op.drop_table(table, schema='research')
