from contextlib import contextmanager

import pytest

from app.routes import ai_chat


class _Cursor:
    def __init__(self, *, returned=None, rowcount=1, rows=None):
        self.returned = returned
        self.rowcount = rowcount
        self.rows = rows or []
        self.executions = []

    def execute(self, query, params):
        self.executions.append((query, params))

    def fetchone(self):
        return self.returned

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _connection_factory(cursor):
    @contextmanager
    def factory():
        yield _Connection(cursor)

    return factory


def test_upsert_rejects_session_owned_by_another_user(monkeypatch):
    cursor = _Cursor(returned=None)
    monkeypatch.setattr(ai_chat, "get_db_connection", _connection_factory(cursor))

    with pytest.raises(ai_chat.SessionAccessError):
        ai_chat._upsert_session("foreign-session", 7, "title", "CN", "000001")

    query, params = cursor.executions[0]
    assert "ai_chat_sessions.user_id=EXCLUDED.user_id" in query
    assert params[1] == 7


def test_message_insert_is_scoped_to_session_owner(monkeypatch):
    cursor = _Cursor(rowcount=0)
    monkeypatch.setattr(ai_chat, "get_db_connection", _connection_factory(cursor))

    with pytest.raises(ai_chat.SessionAccessError):
        ai_chat._persist_message("foreign-session", 7, "user", "hello")

    query, params = cursor.executions[0]
    assert "s.user_id" not in query
    assert "user_id=%s" in query
    assert params[-1] == 7


def test_history_query_is_scoped_to_session_owner(monkeypatch):
    cursor = _Cursor(rows=[("user", "hello")])
    monkeypatch.setattr(ai_chat, "get_db_connection", _connection_factory(cursor))

    history = ai_chat._get_history("owned-session", 7)

    assert history == [{"role": "user", "content": "hello"}]
    query, params = cursor.executions[0]
    assert "s.user_id=%s" in query
    assert params == ("owned-session", 7)


def test_chat_session_route_requires_login(client):
    response = client.get("/api/ai/chat/sessions")

    assert response.status_code == 401
