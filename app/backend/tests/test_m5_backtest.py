"""M5.2 backtest history import: composite keys, JSON text, chunking, idempotency."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.research import (BacktestAccountPoint, BacktestEquity, BacktestFill, BacktestOrder,
                                BacktestRun, BacktestTrade, ImportRun)
from qp.research.importer import import_backtests


class FakeReader:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def fetch(self, name: str) -> list[dict]:
        return list(self.tables.get(name, []))


def qd_tables(status: str = "success", equity_rows: int = 2) -> dict[str, list[dict]]:
    now = dt.datetime(2026, 8, 20, 15, 30)
    return {
        "qd_backtest_runs": [{
            "run_id": 7, "user_id": 1, "strategy_id": 3, "strategy_name": "etf-trend",
            "run_type": "backtest", "market": "cn", "symbol": "510300",
            "timeframe": "1d", "start_date": "2025-01-01", "end_date": "2026-08-01",
            "initial_capital": 100000, "commission": 0.0003, "slippage": 0.0,
            "leverage": 1, "trade_direction": "long",
            "strategy_config": '{"ma": 60}', "config_snapshot": '{"v": 1}',
            "engine_version": "1.2", "code_hash": "c" * 64, "status": status,
            "error_message": None, "result_json": '{"total_return": 0.123}',
            "created_at": now,
        }],
        "qd_backtest_equity_points": [
            {"run_id": 7, "point_index": i, "point_time": f"2026-08-{i + 1:02d}",
             "point_value": 100000.0 + i, "created_at": now} for i in range(equity_rows)
        ],
        "qd_backtest_trades": [{
            "run_id": 7, "trade_index": 0, "user_id": 1, "strategy_id": 3,
            "trade_time": "2026-08-01", "trade_type": "buy", "side": "long",
            "price": 4.1, "amount": 1000, "profit": 12.5, "balance": 100012.5,
            "reason": "signal", "payload_json": '{"note": "x"}', "created_at": now,
        }],
        "qd_backtest_orders": [{
            "run_id": 7, "order_seq": 0, "user_id": 1, "order_id": "o-1",
            "intent": "open", "side": "buy", "order_type": "market", "status": "filled",
            "payload_json": "{}", "created_at": now,
        }],
        "qd_backtest_fills": [{
            "run_id": 7, "fill_seq": 0, "user_id": 1, "fill_id": "f-1", "order_seq": 0,
            "qty": "1000", "price": "4.1", "payload_json": "{}", "created_at": now,
        }],
        "qd_backtest_account_points": [{
            "run_id": 7, "point_seq": 0, "user_id": 1, "bar_index": 1,
            "point_time": "2026-08-01", "mark_price": "4.1", "equity": "100012.5",
            "payload_json": "{}", "created_at": now,
        }],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (BacktestRun, BacktestEquity, BacktestTrade, BacktestOrder, BacktestFill,
                      BacktestAccountPoint, ImportRun):
            session.execute(delete(model))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_backtest_import_is_idempotent_and_parses_json(engine):
    cleanup(engine)
    try:
        first = import_backtests(engine, FakeReader(qd_tables()))
        assert first["backtest_run"] == 1
        assert first["backtest_equity"] == 2
        assert first["backtest_trade"] == 1
        assert count(engine, BacktestRun) == 1
        assert count(engine, BacktestEquity) == 2

        second = import_backtests(engine, FakeReader(qd_tables(status="failed")))
        assert second["backtest_run"] == 1
        assert count(engine, BacktestRun) == 1
        with Session(engine) as session:
            run = session.get(BacktestRun, 7)
            assert run.status == "failed"
            assert run.strategy_config == {"ma": 60}
            assert run.result_json == {"total_return": 0.123}
            trade = session.get(BacktestTrade, (7, 0))
            assert trade.payload_json == {"note": "x"}
            assert trade.price == 4.1
    finally:
        cleanup(engine)


def test_composite_key_deduplicates_within_one_batch(engine):
    cleanup(engine)
    try:
        tables = qd_tables(equity_rows=0)
        tables["qd_backtest_equity_points"] = [
            {"run_id": 7, "point_index": 0, "point_value": 1.0},
            {"run_id": 7, "point_index": 0, "point_value": 2.0},
        ]
        counts = import_backtests(engine, FakeReader(tables))
        assert counts["backtest_equity"] == 1
        with Session(engine) as session:
            assert session.get(BacktestEquity, (7, 0)).point_value == 2.0
    finally:
        cleanup(engine)


def test_large_equity_table_is_chunked(engine):
    cleanup(engine)
    try:
        counts = import_backtests(engine, FakeReader(qd_tables(equity_rows=1001)))
        assert counts["backtest_equity"] == 1001
        assert count(engine, BacktestEquity) == 1001
        again = import_backtests(engine, FakeReader(qd_tables(equity_rows=1001)))
        assert again["backtest_equity"] == 1001
        assert count(engine, BacktestEquity) == 1001
    finally:
        cleanup(engine)


def test_import_run_records_quantdinger_source(engine):
    cleanup(engine)
    try:
        import_backtests(engine, FakeReader(qd_tables()))
        with Session(engine) as session:
            run = session.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
            assert run.source == "quantdinger"
            assert run.status == "succeeded"
            assert run.counts["backtest_run"] == 1
    finally:
        cleanup(engine)
