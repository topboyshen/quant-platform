"""M5 research shadow jobs: read-only imports from the legacy research stores.

No job in this module writes a legacy database, triggers a legacy task, sends a
notification or calls an LLM.  Sources are configured by path/DSN only; when a
source is not configured the job is a no-op so the timer can stay enabled.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext, job
from qp.research.compare import compare_research, write_report
from qp.research.importer import (import_ai, import_backtests, import_ledger, import_phase2,
                                  import_profit, import_projects)
from qp.research.sources import LedgerReader, Phase2Reader, QDReader

DEFAULT_PHASE2_DB = "/var/lib/research-system-phase2/phase2.db"
DEFAULT_REPORT_DIR = "/var/lib/qp/research-reports"


@job("research.import_ledger", "Mirror research_ledger into research.* (read-only source)",
     schedule_hint="every 10 min (qp-research-ledger.timer)")
def import_ledger_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_LEDGER_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_LEDGER_DSN is not configured"}
    reader = LedgerReader(dsn)
    try:
        counts = import_ledger(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_phase2", "Mirror phase2 lineage into research.* (SQLite read-only)",
     schedule_hint="every 10 min (qp-research-phase2.timer)")
def import_phase2_job(ctx: JobContext) -> dict:
    path = os.getenv("QP_PHASE2_DB", DEFAULT_PHASE2_DB).strip()
    if not Path(path).is_file():
        return {"ok": True, "skipped": f"phase2 database not found: {path}"}
    counts = import_phase2(ctx.engine, Phase2Reader(path))
    return {"ok": True, "counts": counts}


@job("research.import_backtests", "Mirror QuantDinger backtest history into research.* "
                                  "(read-only; never re-runs a backtest)",
     schedule_hint="daily 17:30 CST (qp-research-backtests.timer)")
def import_backtests_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_backtests(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_ai", "Mirror QuantDinger AI analysis/chat records into research.* "
                           "(read-only; no LLM calls)",
     schedule_hint="daily 17:40 CST (qp-research-ai.timer)")
def import_ai_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_ai(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_profit", "Mirror QuantDinger profit-research history into research.* "
                               "(read-only; never triggers a run)",
     schedule_hint="daily 17:45 CST (qp-research-profit.timer)")
def import_profit_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_profit(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_projects", "Mirror QuantDinger research projects/branches into research.* "
                                 "(read-only; writes stay with QD during shadow)",
     schedule_hint="daily 17:50 CST (qp-research-projects.timer)")
def import_projects_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_projects(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.compare", "R1-R10 research shadow comparison (read-only; report only)",
     schedule_hint="daily 18:10 CST (qp-research-compare.timer)")
def compare_job(ctx: JobContext) -> dict:
    ledger = LedgerReader(os.environ["QP_LEDGER_DSN"]) if os.getenv("QP_LEDGER_DSN") else None
    qd = QDReader(os.environ["QP_QD_DSN"]) if os.getenv("QP_QD_DSN") else None
    path = os.getenv("QP_PHASE2_DB", DEFAULT_PHASE2_DB).strip()
    phase2 = Phase2Reader(path) if path and Path(path).is_file() else None
    try:
        with Session(ctx.engine) as session:
            result = compare_research(session, ledger=ledger, phase2=phase2, qd=qd)
    finally:
        for reader in (ledger, qd):
            if reader is not None:
                reader.close()
    files = write_report(result, os.getenv("QP_RESEARCH_REPORT_DIR", DEFAULT_REPORT_DIR))
    return {"overall": result["overall"], "counts": result["counts"], "files": files}
