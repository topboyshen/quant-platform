"""m5 research schema

Revision ID: 0005
Revises: 0004

Creates the research schema tables for the M5 shadow module:
ledger mirror (forecast / batch / outcome / source / audit / calibration / artifacts /
lineage), phase2 lineage, and importer bookkeeping.  All additive.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()),
                     server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    # ---------------------------------------------------------------- ledger mirror
    op.create_table(
        'forecast_protocol',
        sa.Column('protocol_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('protocol_version', sa.Text(), nullable=True),
        sa.Column('protocol_hash', sa.Text(), nullable=True),
        sa.Column('universe_rule_hash', sa.Text(), nullable=True),
        sa.Column('target_definition_hash', sa.Text(), nullable=True),
        sa.Column('source_policy_hash', sa.Text(), nullable=True),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('protocol_id', name=op.f('pk_forecast_protocol')),
        schema='research',
    )
    op.create_table(
        'forecast_batch',
        sa.Column('batch_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('protocol_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('external_batch_key', sa.Text(), nullable=True),
        sa.Column('target_session', sa.Date(), nullable=True),
        sa.Column('correlation_group', sa.Text(), nullable=True),
        sa.Column('expected_universe_size', sa.Integer(), nullable=True),
        sa.Column('qualified_coverage', sa.Integer(), nullable=True),
        sa.Column('batch_status', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sealed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('imported_from', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('batch_id', name=op.f('pk_forecast_batch')),
        schema='research',
    )
    op.create_index(op.f('ix_forecast_batch_target_session'), 'forecast_batch',
                    ['target_session'], schema='research')
    op.create_table(
        'forecast',
        sa.Column('forecast_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('case_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('batch_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('protocol_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('parent_forecast_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('forecast_type', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('target_key', sa.Text(), nullable=True),
        sa.Column('metric_definition_hash', sa.Text(), nullable=True),
        sa.Column('target_session', sa.Date(), nullable=True),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('probability', sa.Numeric(), nullable=True),
        sa.Column('confidence_level', sa.Numeric(), nullable=True),
        sa.Column('lower_bound', sa.Numeric(), nullable=True),
        sa.Column('upper_bound', sa.Numeric(), nullable=True),
        sa.Column('baseline_hash', sa.Text(), nullable=True),
        sa.Column('invalidation_hash', sa.Text(), nullable=True),
        sa.Column('settlement_rule_hash', sa.Text(), nullable=True),
        sa.Column('model_version', sa.Text(), nullable=True),
        sa.Column('feature_snapshot_hash', sa.Text(), nullable=True),
        _jsonb('source_hashes', "'[]'::jsonb"),
        sa.Column('data_cutoff', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sealed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('prospective_eligible', sa.Boolean(), nullable=True),
        sa.Column('retrospective_reason', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('forecast_id', name=op.f('pk_forecast')),
        schema='research',
    )
    op.create_index(op.f('ix_forecast_batch_id'), 'forecast', ['batch_id'], schema='research')
    op.create_index(op.f('ix_forecast_target_session'), 'forecast', ['target_session'], schema='research')
    op.create_table(
        'forecast_outcome',
        sa.Column('outcome_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('forecast_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('revision_no', sa.Integer(), nullable=True),
        sa.Column('outcome_status', sa.Text(), nullable=True),
        sa.Column('observed_value', sa.Numeric(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('input_hash', sa.Text(), nullable=True),
        sa.Column('rule_version', sa.Text(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recorded_by', sa.Text(), nullable=True),
        sa.Column('prior_outcome_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.PrimaryKeyConstraint('outcome_id', name=op.f('pk_forecast_outcome')),
        schema='research',
    )
    op.create_index(op.f('ix_forecast_outcome_forecast_id'), 'forecast_outcome',
                    ['forecast_id'], schema='research')
    op.create_table(
        'source_record',
        sa.Column('source_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('source_type', sa.Text(), nullable=True),
        sa.Column('source_locator', sa.Text(), nullable=True),
        sa.Column('publisher', sa.Text(), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('data_cutoff', sa.DateTime(timezone=True), nullable=True),
        sa.Column('license_class', sa.Text(), nullable=True),
        sa.Column('reliability_tier', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('captured_by', sa.Text(), nullable=True),
        _jsonb('metadata'),
        sa.PrimaryKeyConstraint('source_id', name=op.f('pk_source_record')),
        schema='research',
    )
    op.create_table(
        'ledger_audit',
        sa.Column('audit_id', sa.BigInteger(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('actor_id', sa.Text(), nullable=True),
        sa.Column('action', sa.Text(), nullable=True),
        sa.Column('object_type', sa.Text(), nullable=True),
        sa.Column('object_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('input_hash', sa.Text(), nullable=True),
        sa.Column('result_hash', sa.Text(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=False), nullable=True),
        _jsonb('metadata'),
        sa.PrimaryKeyConstraint('audit_id', name=op.f('pk_ledger_audit')),
        schema='research',
    )
    op.create_table(
        'calibration_snapshot',
        sa.Column('calibration_snapshot_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('metric_type', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('eligible_forecast_count', sa.Integer(), nullable=True),
        sa.Column('independent_batch_count', sa.Integer(), nullable=True),
        sa.Column('excluded_count', sa.Integer(), nullable=True),
        sa.Column('input_filter_hash', sa.Text(), nullable=True),
        _jsonb('metric_payload'),
        sa.Column('computed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('computed_by', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('raw_due_forecast_count', sa.Integer(), nullable=True),
        sa.Column('raw_due_independent_batch_count', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('calibration_snapshot_id',
                                name=op.f('pk_calibration_snapshot')),
        schema='research',
    )
    op.create_table(
        'model_artifact',
        sa.Column('model_artifact_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('source_system', sa.Text(), nullable=True),
        sa.Column('artifact_uri', sa.Text(), nullable=True),
        sa.Column('artifact_hash', sa.Text(), nullable=True),
        sa.Column('artifact_role', sa.Text(), nullable=True),
        sa.Column('data_cutoff', sa.DateTime(timezone=True), nullable=True),
        sa.Column('universe_hash', sa.Text(), nullable=True),
        sa.Column('feature_hash', sa.Text(), nullable=True),
        sa.Column('label_hash', sa.Text(), nullable=True),
        sa.Column('code_hash', sa.Text(), nullable=True),
        sa.Column('config_hash', sa.Text(), nullable=True),
        sa.Column('random_seed', sa.Text(), nullable=True),
        sa.Column('split_hash', sa.Text(), nullable=True),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('registered_by', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('model_artifact_id', name=op.f('pk_model_artifact')),
        schema='research',
    )
    op.create_table(
        'import_lineage',
        sa.Column('import_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('source_system', sa.Text(), nullable=True),
        sa.Column('source_database_hash', sa.Text(), nullable=True),
        sa.Column('source_table', sa.Text(), nullable=True),
        sa.Column('source_record_id', sa.Text(), nullable=True),
        sa.Column('source_record_hash', sa.Text(), nullable=True),
        sa.Column('original_recorded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('original_sealed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('import_batch_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('import_status', sa.Text(), nullable=True),
        sa.Column('target_object_type', sa.Text(), nullable=True),
        sa.Column('target_object_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('imported_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('imported_by', sa.Text(), nullable=True),
        sa.Column('reason_code', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('import_id', name=op.f('pk_import_lineage')),
        schema='research',
    )
    op.create_index(op.f('ix_import_lineage_source_record_id'), 'import_lineage',
                    ['source_record_id'], schema='research')
    op.create_index(op.f('ix_import_lineage_target_object_id'), 'import_lineage',
                    ['target_object_id'], schema='research')

    # ---------------------------------------------------------------- phase2 lineage
    op.create_table(
        'phase2_batch',
        sa.Column('batch_id', sa.Text(), nullable=False),
        sa.Column('cohort_id', sa.Text(), nullable=True),
        sa.Column('protocol_hash', sa.Text(), nullable=True),
        sa.Column('data_cutoff', sa.Text(), nullable=True),
        sa.Column('target_session', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('expected_n', sa.Integer(), nullable=True),
        sa.Column('nominal_n', sa.Integer(), nullable=True),
        sa.Column('qualified_n', sa.Integer(), nullable=True),
        sa.Column('independent_batch_count', sa.Integer(), nullable=True),
        sa.Column('block_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.Text(), nullable=True),
        sa.Column('sealed_at', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('batch_id', name=op.f('pk_phase2_batch')),
        schema='research',
    )
    op.create_table(
        'phase2_source_snapshot',
        sa.Column('snapshot_id', sa.Text(), nullable=False),
        sa.Column('batch_id', sa.Text(), nullable=True),
        sa.Column('symbol', sa.Text(), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('normalized_hash', sa.Text(), nullable=True),
        sa.Column('latest_session', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('snapshot_id', name=op.f('pk_phase2_source_snapshot')),
        schema='research',
    )
    op.create_index(op.f('ix_phase2_source_snapshot_batch_id'), 'phase2_source_snapshot',
                    ['batch_id'], schema='research')
    op.create_table(
        'phase2_exclusion',
        sa.Column('exclusion_id', sa.Text(), nullable=False),
        sa.Column('batch_id', sa.Text(), nullable=True),
        sa.Column('symbol', sa.Text(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('source_hash', sa.Text(), nullable=True),
        sa.Column('created_at', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('exclusion_id', name=op.f('pk_phase2_exclusion')),
        schema='research',
    )
    op.create_table(
        'phase2_protocol',
        sa.Column('protocol_hash', sa.Text(), nullable=False),
        sa.Column('protocol_id', sa.Text(), nullable=True),
        sa.Column('version', sa.Text(), nullable=True),
        _jsonb('protocol_json'),
        sa.Column('frozen_at', sa.Text(), nullable=True),
        sa.Column('created_at', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('protocol_hash', name=op.f('pk_phase2_protocol')),
        schema='research',
    )
    op.create_table(
        'phase2_run_state',
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('name', name=op.f('pk_phase2_run_state')),
        schema='research',
    )
    op.create_table(
        'phase2_audit',
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=True),
        sa.Column('actor', sa.Text(), nullable=True),
        sa.Column('occurred_at', sa.Text(), nullable=True),
        sa.Column('payload_hash', sa.Text(), nullable=True),
        _jsonb('payload_json'),
        sa.PrimaryKeyConstraint('event_id', name=op.f('pk_phase2_audit')),
        schema='research',
    )

    # ---------------------------------------------------------------- importer bookkeeping
    op.create_table(
        'import_run',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_import_run')),
        schema='research',
    )
    op.create_index(op.f('ix_import_run_source_started_at'), 'import_run',
                    ['source', 'started_at'], schema='research')


def downgrade() -> None:
    for table in ('import_run', 'phase2_audit', 'phase2_run_state', 'phase2_protocol',
                  'phase2_exclusion', 'phase2_source_snapshot', 'phase2_batch',
                  'import_lineage', 'model_artifact', 'calibration_snapshot',
                  'ledger_audit', 'source_record', 'forecast_outcome', 'forecast',
                  'forecast_batch', 'forecast_protocol'):
        op.drop_table(table, schema='research')
