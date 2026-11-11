"""Regression tests for PostgreSQL connection pool cleanup."""
from unittest.mock import MagicMock

import pytest

from app.utils import db_postgres


def _mock_connection(monkeypatch):
    pool = MagicMock()
    conn = MagicMock()
    conn.closed = 0
    monkeypatch.setattr(db_postgres, "_get_connection_pool", lambda: pool)
    monkeypatch.setattr(db_postgres, "_acquire_conn_with_wait", lambda pg_pool: conn)
    return pool, conn


def test_get_pg_connection_rolls_back_before_return_on_success(monkeypatch):
    pool, conn = _mock_connection(monkeypatch)

    with db_postgres.get_pg_connection():
        pass

    conn.rollback.assert_called_once_with()
    pool.putconn.assert_called_once_with(conn, close=False)


def test_get_pg_connection_rolls_back_once_before_return_on_error(monkeypatch):
    pool, conn = _mock_connection(monkeypatch)

    with pytest.raises(ValueError, match="query failed"):
        with db_postgres.get_pg_connection():
            raise ValueError("query failed")

    conn.rollback.assert_called_once_with()
    pool.putconn.assert_called_once_with(conn, close=False)


def test_get_pg_connection_discards_broken_connection(monkeypatch):
    pool, conn = _mock_connection(monkeypatch)

    with pytest.raises(db_postgres.OperationalError):
        with db_postgres.get_pg_connection():
            raise db_postgres.OperationalError("connection lost")

    pool.putconn.assert_called_once_with(conn, close=True)


def test_get_pg_connection_discards_connection_when_rollback_fails(monkeypatch):
    pool, conn = _mock_connection(monkeypatch)
    conn.rollback.side_effect = db_postgres.OperationalError("connection lost")

    with db_postgres.get_pg_connection():
        pass

    conn.rollback.assert_called_once_with()
    pool.putconn.assert_called_once_with(conn, close=True)


def test_postgres_connection_close_rolls_back_before_return(monkeypatch):
    pool, conn = _mock_connection(monkeypatch)
    pg_conn = db_postgres.PostgresConnection(conn)

    pg_conn.close()

    conn.rollback.assert_called_once_with()
    pool.putconn.assert_called_once_with(conn, close=False)
