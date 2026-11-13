"""Read-only source adapters for M5.

The legacy research stores are never written by qp:

* ``research_ledger`` (PostgreSQL, local peer auth) is read inside a
  ``SET TRANSACTION READ ONLY`` transaction.
* the QuantDinger PostgreSQL (containers) is read the same way, ``public`` schema only.
* the phase2 SQLite database is opened with ``mode=ro``.

All adapters return plain ``dict`` rows so the importer stays testable
without either source.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


class PostgresReader:
    """Read-only ``SELECT *`` reader for one PostgreSQL schema."""

    def __init__(self, dsn: str, schema: str, *, engine: Engine | None = None):
        if engine is None and not dsn:
            raise ValueError("dsn is required")
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"bad schema name: {schema!r}")
        self.schema = schema
        self._engine = engine or create_engine(dsn, pool_pre_ping=True, future=True)

    def fetch(self, table: str) -> list[dict[str, Any]]:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad table name: {table!r}")
        with self._engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            result = conn.execute(text(f"SELECT * FROM {self.schema}.{table}"))
            return [dict(row._mapping) for row in result]

    def count(self, table: str) -> int:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad table name: {table!r}")
        with self._engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            return int(conn.execute(text(f"SELECT count(*) FROM {self.schema}.{table}")).scalar() or 0)

    def scalar(self, sql: str) -> Any:
        with self._engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            return conn.execute(text(sql)).scalar()

    def read_only(self) -> bool:
        with self._engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            return str(conn.execute(text("SHOW transaction_read_only")).scalar()).lower() == "on"

    def close(self) -> None:
        self._engine.dispose()


class LedgerReader(PostgresReader):
    """Read-only reader for the `ledger` schema of the research ledger database."""

    def __init__(self, dsn: str, *, engine: Engine | None = None):
        super().__init__(dsn, "ledger", engine=engine)


class QDReader(PostgresReader):
    """Read-only reader for the QuantDinger PostgreSQL (`public` schema)."""

    def __init__(self, dsn: str, *, engine: Engine | None = None):
        super().__init__(dsn, "public", engine=engine)


class Phase2Reader:
    """Read-only reader for the phase2 SQLite database."""

    def __init__(self, path: str):
        self.path = path

    def count(self, table: str) -> int:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad phase2 table name: {table!r}")
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        finally:
            con.close()

    def read_only(self) -> bool:
        return True  # opened with mode=ro; a write attempt would raise

    def fetch(self, table: str) -> list[dict[str, Any]]:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad phase2 table name: {table!r}")
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows: Iterable[sqlite3.Row] = con.execute(f'SELECT * FROM "{table}"')
            return [dict(row) for row in rows]
        finally:
            con.close()
