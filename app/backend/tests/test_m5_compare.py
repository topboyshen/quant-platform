"""M5.6 research comparator: R1-R10 pass/fail/blocked behaviour."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.base import Base
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.research import Forecast, LlmCall
from qp.research.compare import compare_research
from qp.research.importer import (
    import_ai,
    import_backtests,
    import_ledger,
    import_phase2,
    import_profit,
    import_projects,
)

NOW = dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.UTC)
BATCH = str(uuid.uuid4())
FORECAST = str(uuid.uuid4())


class StubReader:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def fetch(self, name: str) -> list[dict]:
        return [dict(r) for r in self.tables.get(name, [])]

    def count(self, name: str) -> int:
        return len(self.tables.get(name, []))

    def read_only(self) -> bool:
        return True

    def scalar(self, sql: str):
        s = sql.lower()
        if "qd_backtest_equity_points" in s:
            return sum(float(r.get("point_value") or 0) for r in self.tables["qd_backtest_equity_points"])
        if "qd_backtest_trades" in s:
            return sum(float(r.get("profit") or 0) for r in self.tables["qd_backtest_trades"])
        if "qd_profit_picks" in s:
            return len({r.get("pick_key") for r in self.tables["qd_profit_picks"]})
        if "qd_profit_pick_observations" in s:
            return sum(float(r.get("net_return") or 0) for r in self.tables["qd_profit_pick_observations"])
        if "ai_chat_messages" in s:
            return len({r.get("session_id") for r in self.tables["ai_chat_messages"]})
        if "qd_research_projects" in s:
            return sum(int(r.get("revision") or 0) for r in self.tables["qd_research_projects"])
        raise AssertionError(f"unexpected scalar sql: {sql}")


def source_tables() -> dict[str, list[dict]]:
    return {
        "forecast_protocol": [{"protocol_id": str(uuid.uuid4()), "status": "ACTIVE"}],
        "forecast_batch": [{"batch_id": BATCH, "batch_status": "SEALED"}],
        "forecast": [{"forecast_id": FORECAST, "batch_id": BATCH, "status": "SEALED",
                      "target_key": "600104.SH"}],
        "outcome_observation": [{"outcome_id": str(uuid.uuid4()), "forecast_id": FORECAST,
                                 "outcome_status": "APPROVED"}],
        "source_record": [{"source_id": str(uuid.uuid4()), "source_type": "FILING"}],
        "audit_event": [{"audit_id": 1, "action": "SEAL"}],
        "calibration_snapshot": [{"calibration_snapshot_id": str(uuid.uuid4()),
                                  "metric_type": "BINARY"}],
        "model_artifact": [{"model_artifact_id": str(uuid.uuid4()), "source_system": "astock"}],
        "import_ledger": [{"import_id": str(uuid.uuid4()), "target_object_type": "forecast"}],
        "batch": [{"batch_id": "b1", "status": "SEALED"}],
        "source_snapshot": [{"snapshot_id": "s1", "batch_id": "b1"}],
        "exclusion": [{"exclusion_id": "e1", "batch_id": "b1"}],
        "protocol": [{"protocol_hash": "h" * 64, "protocol_json": '{"v": 1}'}],
        "run_state": [{"name": "state", "value": "ok"}],
        "audit": [{"event_id": "ev1", "payload_json": '{"a": 1}'}],
        "qd_backtest_runs": [{"run_id": 1, "status": "success"}],
        "qd_backtest_equity_points": [{"run_id": 1, "point_index": 0, "point_value": 100.0}],
        "qd_backtest_trades": [{"run_id": 1, "trade_index": 0, "profit": 5.0}],
        "qd_backtest_orders": [{"run_id": 1, "order_seq": 0}],
        "qd_backtest_fills": [{"run_id": 1, "fill_seq": 0}],
        "qd_backtest_account_points": [{"run_id": 1, "point_seq": 0}],
        "qd_profit_research_runs": [{"id": 1, "run_key": "r1"}],
        "qd_profit_picks": [{"id": 1, "pick_key": "p" * 64}],
        "qd_profit_pick_observations": [{"id": 1, "pick_id": 1, "net_return": 0.01}],
        "qd_profit_research_run_attempts": [{"execution_id": str(uuid.uuid4()), "run_id": 1}],
        "qd_analysis_tasks": [{"id": 1, "result_json": '{"a": 1}'}],
        "qd_analysis_memory": [{"id": 1, "reasons": '[]', "scores": "{}",
                                "indicators_snapshot": "{}", "raw_result": "{}"}],
        "ai_chat_sessions": [{"id": "s1"}],
        "ai_chat_messages": [{"id": 1, "session_id": "s1"}],
        "qd_research_projects": [{"id": 1, "revision": 2, "subject": "{}"}],
        "qd_research_branches": [{"id": 1, "project_id": 1}],
        "qd_research_admission": [{"id": 1}],
        "qd_research_admission_audit": [{"id": 1, "before": "{}", "after": "{}"}],
        "qd_research_drafts": [{"id": 1, "payload": "{}"}],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for table in reversed(Base.metadata.sorted_tables):
            if table.schema == "research":
                session.execute(table.delete())
        session.execute(delete(Outbox))
        session.execute(delete(DeliveryAttempt))
        session.commit()


def seed(engine, reader: StubReader) -> None:
    import_ledger(engine, reader)
    import_phase2(engine, reader)
    import_backtests(engine, reader)
    import_ai(engine, reader)
    import_profit(engine, reader)
    import_projects(engine, reader)


def test_compare_passes_when_mirror_matches(engine):
    cleanup(engine)
    try:
        reader = StubReader(source_tables())
        seed(engine, reader)
        with Session(engine) as session:
            result = compare_research(session, ledger=reader, phase2=reader, qd=reader)
        assert result["overall"] == "PASS", result
        assert all(c["status"] == "PASS" for c in result["checks"]), result["checks"]
    finally:
        cleanup(engine)


def test_compare_fails_on_missing_forecast(engine):
    cleanup(engine)
    try:
        reader = StubReader(source_tables())
        seed(engine, reader)
        with Session(engine) as session:
            session.execute(delete(Forecast).where(Forecast.forecast_id == FORECAST))
            session.commit()
            result = compare_research(session, ledger=reader, phase2=reader, qd=reader)
        assert result["overall"] == "FAIL"
        r1 = next(c for c in result["checks"] if c["id"] == "R1")
        assert r1["status"] == "FAIL" and any("forecast" in d for d in r1["diffs"])
    finally:
        cleanup(engine)


def test_compare_is_blocked_without_sources(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            result = compare_research(session)
        assert result["overall"] == "BLOCKED"
        assert any(c["id"] == "R1" and c["status"] == "BLOCKED" for c in result["checks"])
    finally:
        cleanup(engine)


def test_compare_fails_on_live_notification(engine):
    cleanup(engine)
    try:
        reader = StubReader(source_tables())
        seed(engine, reader)
        with Session(engine) as session:
            session.add(Outbox(channel="telegram", recipient="ops", dedup_key="x-1",
                               body="x", mode="live", status="pending"))
            session.commit()
            result = compare_research(session, ledger=reader, phase2=reader, qd=reader)
        r10 = next(c for c in result["checks"] if c["id"] == "R10")
        assert r10["status"] == "FAIL"
    finally:
        cleanup(engine)


def test_compare_fails_on_non_refused_live_llm_call(engine):
    cleanup(engine)
    try:
        reader = StubReader(source_tables())
        seed(engine, reader)
        with Session(engine) as session:
            session.add(LlmCall(request_sha256="0" * 64, mode="live", shadow=False,
                                status="sent"))
            session.commit()
            result = compare_research(session, ledger=reader, phase2=reader, qd=reader)
        r7 = next(c for c in result["checks"] if c["id"] == "R7")
        assert r7["status"] == "FAIL"
    finally:
        cleanup(engine)
