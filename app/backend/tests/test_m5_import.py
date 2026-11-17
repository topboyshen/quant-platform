"""M5.1 research import: idempotency, status mirroring, phase2 JSON, failure bookkeeping."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.research import (
    Forecast,
    ForecastBatch,
    ForecastProtocol,
    ImportRun,
    Phase2Audit,
    Phase2Protocol,
)
from qp.research.importer import (
    LEDGER_SPECS,
    PHASE2_SPECS,
    TableSpec,
    import_ledger,
    import_phase2,
    upsert_rows,
)

BATCH = str(uuid.uuid4())
FORECAST = str(uuid.uuid4())


class FakeReader:
    def __init__(self, tables: dict[str, list[dict]], *, fail_on: str | None = None):
        self.tables = tables
        self.fail_on = fail_on

    def fetch(self, name: str) -> list[dict]:
        if name == self.fail_on:
            raise RuntimeError(f"source unavailable: {name}")
        return list(self.tables.get(name, []))


def ledger_tables(status: str = "SEALED") -> dict[str, list[dict]]:
    return {
        "forecast_protocol": [{
            "protocol_id": str(uuid.uuid4()), "protocol_version": "v1",
            "protocol_hash": "p" * 64, "status": "ACTIVE",
            "created_at": dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        }],
        "forecast_batch": [{
            "batch_id": BATCH, "target_session": dt.date(2026, 8, 14),
            "batch_status": "SEALED", "expected_universe_size": 40,
            "qualified_coverage": 40, "imported_from": "RESEARCH_SYSTEM_PHASE2",
        }],
        "forecast": [{
            "forecast_id": FORECAST, "batch_id": BATCH, "status": status,
            "forecast_type": "BINARY_EVENT", "target_key": "600104.SH",
            "target_session": dt.date(2026, 8, 14), "probability": 0.61,
            "source_hashes": ["a" * 64], "prospective_eligible": True,
            "created_at": dt.datetime(2026, 8, 13, tzinfo=dt.UTC),
        }],
    }


def phase2_tables() -> dict[str, list[dict]]:
    return {
        "protocol": [{
            "protocol_hash": "h" * 64, "protocol_id": "phase2-v1", "version": "1",
            "protocol_json": '{"cohort": "binary"}', "frozen_at": "2026-08-01T00:00:00+08:00",
            "created_at": "2026-08-01T00:00:00+08:00",
        }],
        "audit": [{
            "event_id": "event-1", "action": "SEAL", "actor": "worker",
            "occurred_at": "2026-08-13T16:00:00+08:00", "payload_hash": "x" * 64,
            "payload_json": '{"batch": "b1"}',
        }],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (Forecast, ForecastBatch, ForecastProtocol, Phase2Protocol, Phase2Audit,
                      ImportRun):
            session.execute(delete(model))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_ledger_import_is_idempotent_and_mirrors_status(engine):
    cleanup(engine)
    try:
        first = import_ledger(engine, FakeReader(ledger_tables("SEALED")))
        assert first["forecast"] == 1 and first["forecast_batch"] == 1
        assert count(engine, Forecast) == 1
        assert count(engine, ForecastBatch) == 1

        second = import_ledger(engine, FakeReader(ledger_tables("SETTLED")))
        assert second["forecast"] == 1
        assert count(engine, Forecast) == 1
        with Session(engine) as session:
            assert session.get(Forecast, FORECAST).status == "SETTLED"
            run = session.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
            assert run.status == "succeeded" and run.source == "research_ledger"
    finally:
        cleanup(engine)


def test_phase2_import_parses_json_text(engine):
    cleanup(engine)
    try:
        counts = import_phase2(engine, FakeReader(phase2_tables()))
        assert counts["phase2_protocol"] == 1 and counts["phase2_audit"] == 1
        with Session(engine) as session:
            protocol = session.get(Phase2Protocol, "h" * 64)
            assert protocol.protocol_json == {"cohort": "binary"}
            audit = session.get(Phase2Audit, "event-1")
            assert audit.payload_json == {"batch": "b1"}
    finally:
        cleanup(engine)


def test_failed_import_is_recorded_and_partial_rows_kept(engine):
    cleanup(engine)
    try:
        tables = {"forecast_protocol": ledger_tables()["forecast_protocol"]}
        with pytest.raises(RuntimeError):
            import_ledger(engine, FakeReader(tables, fail_on="forecast_batch"))
        with Session(engine) as session:
            run = session.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
            assert run.status == "failed"
            assert "source unavailable" in (run.error or "")
            assert run.counts.get("forecast_protocol") == 1
        assert count(engine, ForecastProtocol) == 1
    finally:
        cleanup(engine)


def test_upsert_deduplicates_by_natural_key(engine):
    cleanup(engine)
    try:
        spec = TableSpec(ForecastBatch, "batch_id", "forecast_batch")
        with Session(engine) as session:
            n = upsert_rows(session, spec, [
                {"batch_id": BATCH, "batch_status": "OPEN"},
                {"batch_id": BATCH, "batch_status": "SEALED"},
            ])
            session.commit()
            assert n == 1
            assert session.get(ForecastBatch, BATCH).batch_status == "SEALED"
    finally:
        cleanup(engine)


def test_specs_cover_every_source_table_once():
    for specs in (LEDGER_SPECS, PHASE2_SPECS):
        tables = [spec.source_table for spec in specs]
        assert len(tables) == len(set(tables))
