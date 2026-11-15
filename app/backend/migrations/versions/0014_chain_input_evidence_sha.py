"""Backfill chain_decision.input_evidence_sha256

Revision ID: 0014
Revises: 0013

The digest is a stable pipe-joined string of the decision inputs, so the same
value can be recomputed in SQL from target_decision.  chain_decision is
append-only, so the repair pauses its trigger once.
"""
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE signals.chain_decision DISABLE TRIGGER USER")
    op.execute("""
        UPDATE signals.chain_decision d
        SET input_evidence_sha256 = encode(sha256(convert_to(concat_ws('|',
                td.run_id::text, td.decision_date::text, td.code, td.data_status,
                td.raw_trigger_count::text, jsonb_array_length(td.raw_signals)::text,
                coalesce(td.reference_close::text, '')), 'UTF8')), 'hex')
        FROM signals.target_decision td
        WHERE d.input_evidence_sha256 IS NULL
          AND ((d.input_evidence_ref = 'target_decision:' || td.id::text)
               OR (d.input_evidence_ref = 'run:' || td.run_id::text AND td.code = d.code))
    """)
    op.execute("ALTER TABLE signals.chain_decision ENABLE TRIGGER USER")


def downgrade() -> None:
    # Digest values are facts; the repair is not reversed.
    pass
