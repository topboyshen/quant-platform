"""Idempotent importers for the M5 research shadow.

Both importers are read-only on the source side and only write the qp
``research`` schema.  Every import is recorded in ``research.import_run``;
rows are upserted by their natural primary key so a re-run never duplicates
and upstream status transitions are mirrored.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.research import (
    Admission,
    AdmissionAudit,
    AiMemory,
    AiMessage,
    AiSession,
    AiTask,
    BacktestAccountPoint,
    BacktestEquity,
    BacktestFill,
    BacktestOrder,
    BacktestRun,
    BacktestTrade,
    Branch,
    CalibrationSnapshot,
    Draft,
    Forecast,
    ForecastBatch,
    ForecastOutcome,
    ForecastProtocol,
    ImportLineage,
    ImportRun,
    LedgerAudit,
    ModelArtifact,
    Phase2Audit,
    Phase2Batch,
    Phase2Exclusion,
    Phase2Protocol,
    Phase2RunState,
    Phase2SourceSnapshot,
    ProfitObservation,
    ProfitPick,
    ProfitRun,
    ProfitRunAttempt,
    Project,
    SourceRecord,
)


@dataclass(frozen=True)
class TableSpec:
    model: type
    pk: str | tuple[str, ...]
    source_table: str
    json_columns: tuple[str, ...] = ()
    column_map: tuple[tuple[str, str], ...] = ()

    @property
    def pk_columns(self) -> tuple[str, ...]:
        return (self.pk,) if isinstance(self.pk, str) else tuple(self.pk)


LEDGER_SPECS: tuple[TableSpec, ...] = (
    TableSpec(ForecastProtocol, "protocol_id", "forecast_protocol"),
    TableSpec(ForecastBatch, "batch_id", "forecast_batch"),
    TableSpec(Forecast, "forecast_id", "forecast"),
    TableSpec(ForecastOutcome, "outcome_id", "outcome_observation"),
    TableSpec(SourceRecord, "source_id", "source_record"),
    TableSpec(LedgerAudit, "audit_id", "audit_event"),
    TableSpec(CalibrationSnapshot, "calibration_snapshot_id", "calibration_snapshot"),
    TableSpec(ModelArtifact, "model_artifact_id", "model_artifact"),
    TableSpec(ImportLineage, "import_id", "import_ledger"),
)

PHASE2_SPECS: tuple[TableSpec, ...] = (
    TableSpec(Phase2Batch, "batch_id", "batch"),
    TableSpec(Phase2SourceSnapshot, "snapshot_id", "source_snapshot"),
    TableSpec(Phase2Exclusion, "exclusion_id", "exclusion"),
    TableSpec(Phase2Protocol, "protocol_hash", "protocol", json_columns=("protocol_json",)),
    TableSpec(Phase2RunState, "name", "run_state"),
    TableSpec(Phase2Audit, "event_id", "audit", json_columns=("payload_json",)),
)

QD_SPECS: tuple[TableSpec, ...] = (
    TableSpec(BacktestRun, "run_id", "qd_backtest_runs",
              json_columns=("strategy_config", "config_snapshot", "result_json"),
              column_map=(("id", "run_id"),)),
    TableSpec(BacktestEquity, ("run_id", "point_index"), "qd_backtest_equity_points"),
    TableSpec(BacktestTrade, ("run_id", "trade_index"), "qd_backtest_trades",
              json_columns=("payload_json",)),
    TableSpec(BacktestOrder, ("run_id", "order_seq"), "qd_backtest_orders",
              json_columns=("payload_json",)),
    TableSpec(BacktestFill, ("run_id", "fill_seq"), "qd_backtest_fills",
              json_columns=("payload_json",)),
    TableSpec(BacktestAccountPoint, ("run_id", "point_seq"), "qd_backtest_account_points",
              json_columns=("payload_json",)),
)

AI_SPECS: tuple[TableSpec, ...] = (
    TableSpec(AiTask, "id", "qd_analysis_tasks", json_columns=("result_json",)),
    TableSpec(AiMemory, "id", "qd_analysis_memory",
              json_columns=("reasons", "scores", "indicators_snapshot", "raw_result")),
    TableSpec(AiSession, "id", "ai_chat_sessions"),
    TableSpec(AiMessage, "id", "ai_chat_messages"),
)

PROJECT_SPECS: tuple[TableSpec, ...] = (
    TableSpec(Project, "id", "qd_research_projects", json_columns=("subject",)),
    TableSpec(Branch, "id", "qd_research_branches"),
    TableSpec(Admission, "id", "qd_research_admission"),
    TableSpec(AdmissionAudit, "id", "qd_research_admission_audit",
              json_columns=("before", "after")),
    TableSpec(Draft, "id", "qd_research_drafts", json_columns=("payload",)),
)

PROFIT_SPECS: tuple[TableSpec, ...] = (
    TableSpec(ProfitRun, "id", "qd_profit_research_runs",
              json_columns=("backtest_json", "paper_json")),
    TableSpec(ProfitPick, "id", "qd_profit_picks"),
    TableSpec(ProfitObservation, "id", "qd_profit_pick_observations"),
    TableSpec(ProfitRunAttempt, "execution_id", "qd_profit_research_run_attempts",
              json_columns=("run_document", "result_document", "previous_projection")),
)


def _normalize(spec: TableSpec, row: dict[str, Any]) -> dict[str, Any] | None:
    columns = {c.name for c in spec.model.__table__.columns}
    mapping = dict(spec.column_map)
    out: dict[str, Any] = {}
    for key, value in row.items():
        target = mapping.get(key, key)
        if target not in columns:
            continue
        if target in spec.json_columns and isinstance(value, (str, bytes)):
            try:
                value = json.loads(value)
            except ValueError:
                value = {}
        out[target] = value
    for key in spec.pk_columns:
        if out.get(key) in (None, ""):
            return None
    return out


def upsert_rows(session: Session, spec: TableSpec, rows: Iterable[dict[str, Any]]) -> int:
    """Upsert rows by natural key; returns the number of distinct rows sent."""
    table = spec.model.__table__
    pks = spec.pk_columns
    prepared: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        norm = _normalize(spec, row)
        if norm is not None:
            prepared[tuple(norm[key] for key in pks)] = norm
    if not prepared:
        return 0
    values = list(prepared.values())
    # Keep statements bounded: a 50k-row equity mirror must not build one giant INSERT.
    for start in range(0, len(values), 1000):
        stmt = pg_insert(table).values(values[start:start + 1000])
        update_cols = {c.name: stmt.excluded[c.name]
                       for c in table.columns if c.name not in pks}
        session.execute(stmt.on_conflict_do_update(index_elements=list(pks), set_=update_cols))
    return len(values)


def _run_import(engine: Engine, source: str, specs: tuple[TableSpec, ...],
                reader: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with Session(engine) as session:
        run = ImportRun(source=source, status="running", counts={})
        session.add(run)
        session.commit()
        run_id = run.id
        try:
            for spec in specs:
                rows = reader.fetch(spec.source_table)
                counts[spec.model.__tablename__] = upsert_rows(session, spec, rows)
                session.commit()
            run = session.get(ImportRun, run_id)
            run.status = "succeeded"
            run.finished_at = utcnow()
            run.counts = counts
            session.commit()
        except Exception as exc:
            session.rollback()
            run = session.get(ImportRun, run_id)
            run.status = "failed"
            run.finished_at = utcnow()
            run.counts = counts
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
            raise
    return counts


def import_ledger(engine: Engine, reader: Any, *, specs: tuple[TableSpec, ...] = LEDGER_SPECS) -> dict[str, int]:
    return _run_import(engine, "research_ledger", specs, reader)


def import_phase2(engine: Engine, reader: Any, *, specs: tuple[TableSpec, ...] = PHASE2_SPECS) -> dict[str, int]:
    return _run_import(engine, "phase2", specs, reader)


def import_backtests(engine: Engine, reader: Any, *,
                     specs: tuple[TableSpec, ...] = QD_SPECS) -> dict[str, int]:
    return _run_import(engine, "quantdinger", specs, reader)


def import_ai(engine: Engine, reader: Any, *,
              specs: tuple[TableSpec, ...] = AI_SPECS) -> dict[str, int]:
    return _run_import(engine, "quantdinger_ai", specs, reader)


def import_profit(engine: Engine, reader: Any, *,
                  specs: tuple[TableSpec, ...] = PROFIT_SPECS) -> dict[str, int]:
    return _run_import(engine, "quantdinger_profit", specs, reader)


def import_projects(engine: Engine, reader: Any, *,
                    specs: tuple[TableSpec, ...] = PROJECT_SPECS) -> dict[str, int]:
    return _run_import(engine, "quantdinger_projects", specs, reader)


def latest_import_run(session: Session, source: str) -> ImportRun | None:
    return session.scalar(select(ImportRun).where(ImportRun.source == source)
                          .order_by(ImportRun.id.desc()).limit(1))


def compare_counts(engine: Engine, reader: Any, specs: tuple[TableSpec, ...],
                   source_schema: str) -> list[dict[str, Any]]:
    """Read-only row-count comparison: source table vs qp mirror."""
    out: list[dict[str, Any]] = []
    with Session(engine) as session:
        for spec in specs:
            source_n = len(reader.fetch(spec.source_table))
            qp_n = session.scalar(select(func.count()).select_from(spec.model.__table__)) or 0
            out.append({"table": spec.model.__tablename__, "source": source_n, "qp": qp_n,
                        "match": source_n == qp_n})
    return out
