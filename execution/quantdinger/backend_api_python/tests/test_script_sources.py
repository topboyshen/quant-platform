from contextlib import contextmanager
from pathlib import Path

import pytest

from app.routes import script_sources


class _Cursor:
    def __init__(self, *, returned=None, rows=None):
        self.returned = returned
        self.rows = rows or []
        self.executions = []
        self.closed = False

    def execute(self, query, params):
        self.executions.append((query, params))

    def fetchone(self):
        return self.returned

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


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


def _source_row(**overrides):
    row = {
        "id": 11,
        "user_id": 7,
        "name": "Trend strategy",
        "description": "",
        "code": "def on_init(ctx):\n    pass\n",
        "asset_type": "script",
        "template_key": "",
        "param_schema": {},
        "template_params": {},
        "metadata": {"last_run_config": {"market_category": "CNStock"}},
        "status": "draft",
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_list_sources_is_scoped_to_authenticated_user(monkeypatch):
    cursor = _Cursor(rows=[_source_row()])
    monkeypatch.setattr(script_sources, "get_db_connection", _connection_factory(cursor))

    result = script_sources._list_sources(7)

    assert result[0]["id"] == 11
    query, params = cursor.executions[0]
    assert "WHERE user_id=%s" in query
    assert params[0] == 7


def test_detail_query_cannot_read_another_users_source(monkeypatch):
    cursor = _Cursor(returned=None)
    monkeypatch.setattr(script_sources, "get_db_connection", _connection_factory(cursor))

    result = script_sources._get_source(11, 7)

    assert result is None
    query, params = cursor.executions[0]
    assert "id=%s AND user_id=%s" in query
    assert params == (11, 7)


def test_update_is_scoped_to_owner_and_uses_jsonb(monkeypatch):
    cursor = _Cursor(returned=_source_row(name="Updated"))
    monkeypatch.setattr(script_sources, "get_db_connection", _connection_factory(cursor))

    result = script_sources._update_source(
        11,
        7,
        {"name": "Updated", "metadata": {"script_verified": True}},
    )

    assert result["name"] == "Updated"
    query, params = cursor.executions[0]
    assert "metadata=%s::jsonb" in query
    assert "WHERE id=%s AND user_id=%s" in query
    assert params[-2:] == (11, 7)


def test_delete_is_scoped_to_owner(monkeypatch):
    cursor = _Cursor(returned=None)
    monkeypatch.setattr(script_sources, "get_db_connection", _connection_factory(cursor))

    assert script_sources._delete_source(11, 7) is False
    query, params = cursor.executions[0]
    assert "WHERE id=%s AND user_id=%s" in query
    assert params == (11, 7)


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"name": "", "code": "pass"}, "name is required"),
        ({"name": "valid", "code": ""}, "code is required"),
        ({"name": "valid", "code": "pass", "asset_type": "unknown"}, "asset_type"),
        ({"name": "valid", "code": "pass", "metadata": []}, "metadata must be an object"),
    ],
)
def test_create_payload_validation(payload, message):
    with pytest.raises(script_sources.SourcePayloadError, match=message):
        script_sources._validate_payload(payload, creating=True)


def test_hidden_source_does_not_return_code():
    source = script_sources._source_to_dict(
        _source_row(metadata={"code_hidden": True})
    )

    assert source["code_hidden"] is True
    assert source["code"] == ""


def test_update_rejects_explicit_empty_code():
    with pytest.raises(script_sources.SourcePayloadError, match="code is required"):
        script_sources._validate_payload({"code": "   "}, creating=False)


def test_script_source_route_requires_login(client):
    response = client.get("/api/strategies/script-sources")

    assert response.status_code == 401


def test_init_sql_defines_script_source_table():
    init_sql = Path(__file__).resolve().parents[1] / "migrations" / "init.sql"

    assert "CREATE TABLE IF NOT EXISTS qd_strategy_sources" in init_sql.read_text(encoding="utf-8")
