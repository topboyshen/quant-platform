"""Backfill chain_import_register.original_business_date

Revision ID: 0013
Revises: 0012

The column existed but the first history import did not populate it.  The table
is append-only (a trigger rejects UPDATE), so the repair runs as a migration
with the trigger disabled, exactly once.  Future imports write the date inline.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE signals.chain_import_register DISABLE TRIGGER USER")
    op.execute("""
        UPDATE signals.chain_import_register r
        SET original_business_date = d.trade_date
        FROM signals.chain_decision d
        WHERE d.decision_uid = r.decision_uid
          AND r.original_business_date IS NULL
    """)
    op.execute("ALTER TABLE signals.chain_import_register ENABLE TRIGGER USER")


def downgrade() -> None:
    # A metadata repair is not reversed; the column keeps its values.
    pass
