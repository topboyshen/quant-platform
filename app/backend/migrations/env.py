from alembic import context
from sqlalchemy import text

from qp.db import get_engine
from qp.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ops"))
        conn.commit()
        context.configure(connection=conn, target_metadata=target_metadata, include_schemas=True,
                          version_table="alembic_version", version_table_schema="ops")
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
