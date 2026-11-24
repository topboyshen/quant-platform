"""Read-only research source adapters: SQL guardrails and phase2 SQLite reader."""

from __future__ import annotations

import sqlite3

import pytest

from qp.research.sources import LedgerReader, Phase2Reader, PostgresReader, QDReader


class MappingRow:
    def __init__(self, data):
        self._mapping = data


class Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._scalar


class Conn:
    def __init__(self):
        self.sql = []

    def execute(self, statement):
        sql = str(statement)
        self.sql.append(sql)
        if "count(*)" in sql:
            return Result(scalar=3)
        if "SHOW transaction_read_only" in sql:
            return Result(scalar="on")
        if sql.startswith("SELECT *"):
            return Result(rows=[MappingRow({"id": 1, "name": "a"})])
        return Result(scalar=1)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Engine:
    def __init__(self):
        self.conn = Conn()
        self.disposed = False

    def connect(self):
        return self.conn

    def dispose(self):
        self.disposed = True


def test_postgres_reader_is_read_only_and_guards_names():
    engine = Engine()
    reader = PostgresReader("", "ledger", engine=engine)
    assert reader.fetch("users") == [{"id": 1, "name": "a"}]
    assert reader.count("users") == 3
    assert reader.scalar("select 1") == 1
    assert reader.read_only() is True
    assert engine.conn.sql[0] == "SET TRANSACTION READ ONLY"
    assert engine.conn.sql[1] == "SELECT * FROM ledger.users"
    with pytest.raises(ValueError, match="bad table"):
        reader.fetch("users; drop table x")
    with pytest.raises(ValueError, match="bad schema"):
        PostgresReader("", "bad-schema", engine=engine)
    with pytest.raises(ValueError, match="dsn is required"):
        PostgresReader("", "ledger")
    reader.close()
    assert engine.disposed is True


def test_specialised_readers_choose_schema():
    assert LedgerReader("", engine=Engine()).schema == "ledger"
    assert QDReader("", engine=Engine()).schema == "public"


def test_phase2_reader_counts_fetches_and_rejects_bad_names(tmp_path):
    path = tmp_path / "phase2.db"
    con = sqlite3.connect(path)
    con.execute("create table sample (id integer, name text)")
    con.execute("insert into sample values (1, 'a'), (2, 'b')")
    con.commit()
    con.close()

    reader = Phase2Reader(str(path))
    assert reader.count("sample") == 2
    assert reader.fetch("sample") == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert reader.read_only() is True
    with pytest.raises(ValueError, match="bad phase2 table"):
        reader.count("sample; drop table sample")


def test_reader_rejects_bad_names_for_count_and_phase2_fetch(tmp_path):
    reader = PostgresReader("", "ledger", engine=Engine())
    with pytest.raises(ValueError, match="bad table"):
        reader.count("bad-name")
    phase2 = Phase2Reader(str(tmp_path / "missing.db"))
    with pytest.raises(ValueError, match="bad phase2 table"):
        phase2.fetch("bad-name")
