import inspect
import threading
from unittest.mock import MagicMock, patch

from flask import Flask, g
import pytest

from app.routes import strategy as strategy_routes
from app.services import strategy as strategy_module
from app.services.strategy import StrategyService
from app.services.trading_executor import TradingExecutor


class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.executions = []

    def execute(self, query, params):
        self.executions.append((query, params))

    def close(self):
        pass


class _Connection:
    def __init__(self, rowcount):
        self.db_cursor = _Cursor(rowcount)
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.db_cursor

    def commit(self):
        self.commits += 1


class _BatchRouteService:
    def __init__(self):
        self.batch_call = None

    def get_strategy(self, strategy_id, user_id=None):
        if strategy_id == 11 and user_id == 7:
            return {"id": strategy_id, "user_id": user_id}
        return None

    def _result(self, strategy_ids, user_id):
        self.batch_call = (strategy_ids, user_id)
        return {
            "success": True,
            "success_ids": [11],
            "failed_ids": [{"id": 22, "error": "Strategy not found"}],
        }

    def batch_stop_strategies(self, strategy_ids, user_id=None):
        return self._result(strategy_ids, user_id)

    def batch_delete_strategies(self, strategy_ids, user_id=None):
        return self._result(strategy_ids, user_id)


@pytest.mark.parametrize(
    ("route_name", "method", "path"),
    [
        ("batch_stop_strategies", "POST", "/api/strategies/batch-stop"),
        ("batch_delete_strategies", "DELETE", "/api/strategies/batch-delete"),
    ],
)
def test_batch_routes_never_stop_executor_for_other_users_strategies(
    monkeypatch, route_name, method, path
):
    app = Flask(__name__)
    service = _BatchRouteService()
    executor = MagicMock()
    monkeypatch.setattr(strategy_routes, "get_strategy_service", lambda: service)
    monkeypatch.setattr(strategy_routes, "get_trading_executor", lambda: executor)

    with app.test_request_context(
        path, method=method, json={"strategy_ids": [11, 22]}
    ):
        g.user_id = 7
        response = inspect.unwrap(getattr(strategy_routes, route_name))()

    assert response.status_code == 200
    executor.stop_strategy.assert_called_once_with(11, user_id=7)
    assert service.batch_call == ([11, 22], 7)


@pytest.mark.parametrize("method_name", ["batch_start_strategies", "batch_stop_strategies"])
def test_batch_status_updates_only_count_real_successes(monkeypatch, method_name):
    service = StrategyService()
    monkeypatch.setattr(
        service,
        "update_strategy_status",
        lambda strategy_id, status, user_id=None: strategy_id == 11,
    )

    result = getattr(service, method_name)([11, 22], user_id=7)

    assert result["success"] is True
    assert result["success_ids"] == [11]
    assert [item["id"] for item in result["failed_ids"]] == [22]


def test_batch_delete_only_counts_real_successes(monkeypatch):
    service = StrategyService()
    monkeypatch.setattr(
        service,
        "delete_strategy",
        lambda strategy_id, user_id=None: strategy_id == 11,
    )

    result = service.batch_delete_strategies([11, 22], user_id=7)

    assert result["success"] is True
    assert result["success_ids"] == [11]
    assert [item["id"] for item in result["failed_ids"]] == [22]


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("update_strategy_status", (22, "stopped")),
        ("delete_strategy", (22,)),
    ],
)
def test_zero_row_writes_return_false(monkeypatch, method_name, args):
    connection = _Connection(rowcount=0)
    monkeypatch.setattr(strategy_module, "get_db_connection", lambda: connection)

    result = getattr(StrategyService(), method_name)(*args, user_id=7)

    assert result is False
    query, params = connection.db_cursor.executions[0]
    assert "user_id" in query
    assert params[-1] == 7


def test_scoped_executor_stop_has_no_side_effects_when_owner_does_not_match(monkeypatch):
    connection = _Connection(rowcount=0)
    monkeypatch.setattr(
        "app.services.trading_executor.get_db_connection", lambda: connection
    )
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.lock = threading.Lock()
    executor.running_strategies = {22: MagicMock()}
    executor._exchange_fee_cache = {22: 1.0}

    with patch("app.services.grid.runner.shutdown_grid_for_strategy") as shutdown_grid:
        result = executor.stop_strategy(22, user_id=7)

    assert result is False
    shutdown_grid.assert_not_called()
    assert 22 in executor.running_strategies
    assert executor._exchange_fee_cache == {22: 1.0}
    query, params = connection.db_cursor.executions[0]
    assert "user_id" in query
    assert params == (22, 7)
