"""M5.4 profit-research import: runs, picks, observations, attempts; idempotency."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.research import (
    ImportRun,
    ProfitObservation,
    ProfitPick,
    ProfitRun,
    ProfitRunAttempt,
)
from qp.research.importer import import_profit

NOW = dt.datetime(2026, 11, 2, 15, 10, tzinfo=dt.UTC)
EXECUTION = str(uuid.uuid4())


class FakeReader:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def fetch(self, name: str) -> list[dict]:
        return list(self.tables.get(name, []))


def profit_tables(state: str = "OPEN") -> dict[str, list[dict]]:
    return {
        "qd_profit_research_runs": [{
            "id": 47, "run_key": "run-47", "user_id": 1, "signal_date": dt.date(2026, 11, 2),
            "generated_at": NOW, "status": "PAPER_CANDIDATES", "data_ready": True,
            "reason": "ok", "rule_version": "quantdinger.profit-forward.v1",
            "release_sha256": "r" * 64, "payload_sha256": "p" * 64, "candidate_count": 3,
            "latest_market_date": dt.date(2026, 11, 2), "universe_count": 5668,
            "quote_coverage": 0.98, "preselected_count": 120, "history_coverage": 0.95,
            "benchmark_symbol": "510300", "backtest_json": '{"ok": true}',
            "paper_json": '{"picks": 3}', "created_at": NOW,
        }],
        "qd_profit_picks": [{
            "id": 108, "pick_key": "k" * 64, "source_sha256": "s" * 64, "run_id": 47,
            "user_id": 1, "signal_date": dt.date(2026, 11, 2),
            "rule_version": "quantdinger.profit-forward.v1", "symbol": "sh600000",
            "code": "600000", "name": "浦发银行", "rank": 1, "signal_price": 10.5,
            "paper_weight": 0.3, "signal_score": 0.8, "m20": 1.02, "m60": 1.05,
            "m120": 1.1, "atr_pct": 0.02, "entry_ma20": 10.3, "invalidation_ma60": 9.8,
            "entry_max_gap_pct": 0.03, "max_holding_sessions": 20, "single_side_cost": 0.0003,
            "provider": "tencent", "adjustment": "qfq", "benchmark_symbol": "510300",
            "state": state, "state_reason": "signal", "entry_date": dt.date(2026, 11, 3),
            "entry_price": 10.6, "entry_benchmark_price": 4.1,
            "holding_sessions": 0, "created_at": NOW, "updated_at": NOW,
        }],
        "qd_profit_pick_observations": [{
            "id": 1714, "pick_id": 108, "trade_date": dt.date(2026, 11, 3),
            "event": "ENTRY", "state_before": "PENDING", "state_after": "OPEN",
            "open": 10.5, "high": 10.7, "low": 10.4, "close": 10.6,
            "benchmark_open": 4.1, "benchmark_close": 4.11,
            "return_basis_price": 10.5, "benchmark_return_basis_price": 4.1,
            "threshold_scale": 1.0, "holding_sessions": 0, "gross_return": 0.0095,
            "net_return": 0.0089, "benchmark_return": 0.0024, "excess_return": 0.0065,
            "source_sha256": "o" * 64, "observed_at": NOW,
        }],
        "qd_profit_research_run_attempts": [{
            "execution_id": EXECUTION, "run_id": 47, "user_id": 1,
            "signal_date": dt.date(2026, 11, 2), "rule_version": "quantdinger.profit-forward.v1",
            "generated_at": NOW, "status": "PAPER_CANDIDATES", "release_sha256": "r" * 64,
            "request_sha256": "q" * 64, "run_document": '{"run_key": "run-47"}',
            "result_document": '{"picks": 3}', "previous_projection": '{}',
            "recorded_at": NOW,
        }],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ProfitRun, ProfitPick, ProfitObservation, ProfitRunAttempt, ImportRun):
            session.execute(delete(model))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_profit_import_is_idempotent(engine):
    cleanup(engine)
    try:
        first = import_profit(engine, FakeReader(profit_tables()))
        assert first == {"profit_run": 1, "profit_pick": 1, "profit_observation": 1,
                         "profit_run_attempt": 1}
        second = import_profit(engine, FakeReader(profit_tables(state="CLOSED")))
        assert second == first
        assert count(engine, ProfitRun) == 1 and count(engine, ProfitPick) == 1
        with Session(engine) as session:
            pick = session.get(ProfitPick, 108)
            assert pick.state == "CLOSED"
            assert pick.pick_key == "k" * 64
            run = session.get(ProfitRun, 47)
            assert run.backtest_json == {"ok": True}
            assert run.paper_json == {"picks": 3}
            attempt = session.get(ProfitRunAttempt, EXECUTION)
            assert attempt.result_document == {"picks": 3}
    finally:
        cleanup(engine)


def test_profit_observation_fields_are_preserved(engine):
    cleanup(engine)
    try:
        import_profit(engine, FakeReader(profit_tables()))
        with Session(engine) as session:
            obs = session.get(ProfitObservation, 1714)
            assert obs.pick_id == 108
            assert float(obs.net_return) == 0.0089
            assert float(obs.excess_return) == 0.0065
            assert obs.state_after == "OPEN"
        with Session(engine) as session:
            run = session.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
            assert run.source == "quantdinger_profit" and run.status == "succeeded"
    finally:
        cleanup(engine)
