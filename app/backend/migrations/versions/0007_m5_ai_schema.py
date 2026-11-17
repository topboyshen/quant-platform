"""m5 AI records schema

Revision ID: 0007
Revises: 0006

Mirror of the QuantDinger AI analysis/chat records plus the LLM call audit table.
The M5 LLM entry point is replay-only: live provider calls are not implemented and
must not be enabled in the shadow window.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text("'{}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        'ai_task',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('market', sa.String(length=64), nullable=True),
        sa.Column('symbol', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('language', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        _jsonb('result_json'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_task')),
        schema='research',
    )
    op.create_table(
        'ai_memory',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('market', sa.String(length=64), nullable=True),
        sa.Column('symbol', sa.String(length=64), nullable=True),
        sa.Column('decision', sa.String(length=32), nullable=True),
        sa.Column('confidence', sa.Integer(), nullable=True),
        sa.Column('price_at_analysis', sa.Numeric(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        _jsonb('reasons'),
        _jsonb('scores'),
        _jsonb('indicators_snapshot'),
        _jsonb('raw_result'),
        sa.Column('consensus_score', sa.Numeric(), nullable=True),
        sa.Column('consensus_abs', sa.Numeric(), nullable=True),
        sa.Column('agreement_ratio', sa.Numeric(), nullable=True),
        sa.Column('quality_multiplier', sa.Numeric(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('validated_at', sa.DateTime(), nullable=True),
        sa.Column('actual_outcome', sa.String(length=32), nullable=True),
        sa.Column('actual_return_pct', sa.Numeric(), nullable=True),
        sa.Column('was_correct', sa.Boolean(), nullable=True),
        sa.Column('user_feedback', sa.String(length=32), nullable=True),
        sa.Column('feedback_at', sa.DateTime(), nullable=True),
        sa.Column('task_status', sa.String(length=32), nullable=True),
        sa.Column('task_error', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_memory')),
        schema='research',
    )
    op.create_table(
        'ai_session',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('market', sa.Text(), nullable=True),
        sa.Column('symbol', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_session')),
        schema='research',
    )
    op.create_table(
        'ai_message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Text(), nullable=True),
        sa.Column('role', sa.Text(), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_message')),
        schema='research',
    )
    op.create_index(op.f('ix_ai_message_session_id'), 'ai_message', ['session_id'], schema='research')
    op.create_table(
        'llm_call',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('request_sha256', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('shadow', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('response_sha256', sa.String(length=64), nullable=True),
        _jsonb('usage'),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_call')),
        schema='research',
    )
    op.create_index(op.f('ix_llm_call_request_sha256'), 'llm_call', ['request_sha256'],
                    schema='research')
    op.create_index(op.f('ix_llm_call_created_at'), 'llm_call', ['created_at'], schema='research')


def downgrade() -> None:
    for table in ('llm_call', 'ai_message', 'ai_session', 'ai_memory', 'ai_task'):
        op.drop_table(table, schema='research')
