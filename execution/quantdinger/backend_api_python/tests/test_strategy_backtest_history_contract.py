import inspect

from flask import Flask, g
import pytest

from app.routes import strategy as strategy_routes
from app.services import backtest as backtest_module
from app.services.backtest import BacktestService


class _HistoryService:
    def __init__(self):
        self.calls = []

    def list_runs(self, **kwargs):
        self.calls.append(kwargs)
        return [{"id": 33, "run_type": "strategy_script"}]


def _call_history(monkeypatch, query_string=""):
    app = Flask(__name__)
    service = _HistoryService()
    monkeypatch.setattr(strategy_routes, "get_backtest_service", lambda: service)
    path = "/api/strategies/backtest/history"
    if query_string:
        path += f"?{query_string}"
    with app.test_request_context(path):
        g.user_id = 7
        response = inspect.unwrap(strategy_routes.get_strategy_backtest_history)()
    return response, service


def test_history_without_strategy_id_lists_all_strategy_runs(monkeypatch):
    response, service = _call_history(monkeypatch, "limit=24&offset=2")

    assert response.status_code == 200
    assert response.get_json()["data"][0]["id"] == 33
    assert service.calls == [{
        "user_id": 7,
        "strategy_id": None,
        "run_type_prefix": "strategy_",
        "limit": 24,
        "offset": 2,
        "symbol": "",
        "market": "",
        "timeframe": "",
    }]


def test_history_with_strategy_id_keeps_existing_filter(monkeypatch):
    response, service = _call_history(monkeypatch, "strategyId=9")

    assert response.status_code == 200
    assert service.calls[0]["strategy_id"] == 9


@pytest.mark.parametrize("value", ["", "0", "-1", "abc"])
def test_history_rejects_invalid_explicit_strategy_id(monkeypatch, value):
    response, service = _call_history(monkeypatch, f"strategyId={value}")

    assert response[1] == 400
    assert service.calls == []


def test_run_type_prefix_is_filtered_before_pagination(monkeypatch):
    class _Cursor:
        def __init__(self):
            self.query = ""
            self.params = ()

        def execute(self, query, params):
            self.query = query
            self.params = params

        def fetchall(self):
            return []

        def close(self):
            pass

    class _Connection:
        def __init__(self):
            self.db_cursor = _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.db_cursor

    connection = _Connection()
    monkeypatch.setattr(backtest_module, "get_db_connection", lambda: connection)
    service = BacktestService()
    service.ensure_storage_schema = lambda: None

    result = service.list_runs(
        user_id=7,
        run_type_prefix="strategy_",
        limit=2,
        offset=4,
    )

    assert result == []
    assert "run_type LIKE ? ESCAPE '!'" in connection.db_cursor.query
    assert connection.db_cursor.params == (7, "strategy!_%", 2, 4)


def test_hydrate_exports_only_explicit_source_provenance():
    service = BacktestService()
    base_row = {
        "strategy_id": 1,
        "symbol": "600875",
        "timeframe": "1D",
        "strategy_config": "{}",
        "result_json": "{}",
    }

    legacy = service._hydrate_run_row({
        **base_row,
        "config_snapshot": '{"strategyMeta":{"strategyId":1}}',
    })
    source_backed = service._hydrate_run_row({
        **base_row,
        "config_snapshot": '{"sourceMeta":{"sourceId":2}}',
    })

    assert "source_id" not in legacy
    assert source_backed["source_id"] == 2
