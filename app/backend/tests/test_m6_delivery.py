"""M6.5 shadow delivery and Hermes receipt ingestion."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext
from qp.models.notify import Outbox
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.reports.archive import freeze_report
from qp.reports.delivery import shadow_deliver
from qp.reports.receipts import attach_receipts, load_delivered

DAY = dt.date(2026, 11, 12)
PROFIT_JOB = "ec6c86fd856c"
HEALTH_JOB = "026a1083d9e0"


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.execute(delete(Outbox))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def make_hermes_db(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> tuple[Path, Path]:
    deliveries = tmp_path / "deliveries.db"
    executions = tmp_path / "executions.db"
    con = sqlite3.connect(deliveries)
    con.execute(
        "CREATE TABLE deliveries (execution_id TEXT PRIMARY KEY, status TEXT, created_at TEXT, finished_at TEXT)"
    )
    con.executemany("INSERT INTO deliveries VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    con = sqlite3.connect(executions)
    con.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT)")
    con.executemany(
        "INSERT INTO executions VALUES (?,?)",
        [(row[0], PROFIT_JOB if "15:10" in row[2] else HEALTH_JOB) for row in rows],
    )
    con.commit()
    con.close()
    return deliveries, executions


def test_shadow_delivery_is_idempotent_and_never_sends(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            report, _ = freeze_report(
                session, kind="profit_summary", business_date=DAY, payload={"text": "hello", "jobId": PROFIT_JOB}
            )
            delivery, outbox = shadow_deliver(session, report)
            session.commit()
            assert delivery.mode == "shadow" and delivery.status == "shadowed"
            assert delivery.sent_at is not None
            assert delivery.send_result["note"].startswith("shadow mode")
            assert outbox is not None and outbox.mode == "shadow" and outbox.status == "pending"

            again, _ = shadow_deliver(session, report)
            session.commit()
            assert again.id == delivery.id
        assert count(engine, ReportDelivery) == 1
        assert count(engine, Outbox) == 1
    finally:
        cleanup(engine)


def test_receipts_attach_from_hermes_ledger(engine, tmp_path: Path):
    cleanup(engine)
    try:
        rows = [
            ("exec-profit-1", "delivered", "2026-11-12T15:10:23+08:00", "2026-11-12T15:10:25+08:00"),
            ("exec-health-1", "delivered", "2026-11-12T15:35:22+08:00", "2026-11-12T15:35:24+08:00"),
        ]
        deliveries_db, executions_db = make_hermes_db(tmp_path, rows)
        loaded = load_delivered(deliveries_db, executions_db)
        assert {row["kind"] for row in loaded} == {"profit_summary", "profit_radar_health"}

        with Session(engine) as session:
            report, _ = freeze_report(session, kind="profit_summary", business_date=DAY, payload={"text": "hello"})
            shadow_deliver(session, report)
            session.commit()
            counts = attach_receipts(session, loaded)
            session.commit()
            assert counts["attached"] == 1 and counts["skipped"] == 1
            receipt = session.scalar(select(ReportReceipt))
            assert receipt.status == "received"
            assert receipt.evidence["execution_id"] == "exec-profit-1"
            # Idempotent: the same execution is not recorded twice.
            again = attach_receipts(session, loaded)
            session.commit()
            assert again["attached"] == 0
        assert count(engine, ReportReceipt) == 1
    finally:
        cleanup(engine)


def test_jobs_skip_without_sources(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_HERMES_DELIVERIES_DB", str(tmp_path / "missing.db"))
        monkeypatch.setenv("QP_HERMES_EXECUTIONS_DB", str(tmp_path / "missing2.db"))
        from qp.jobs.reports import import_receipts_job

        assert import_receipts_job(JobContext(engine=engine, run_id=0, name="reports.import_receipts"))["skipped"]
    finally:
        cleanup(engine)


def test_shadow_deliver_job_covers_the_day(engine, monkeypatch):
    cleanup(engine)
    try:
        with Session(engine) as session:
            for kind in ("daily_research_summary", "research_digest"):
                freeze_report(session, kind=kind, business_date=DAY, payload={"text": kind})
            session.commit()
        monkeypatch.setenv("QP_REPORT_DATE", DAY.isoformat())
        from qp.jobs.reports import shadow_deliver_job

        result = shadow_deliver_job(JobContext(engine=engine, run_id=0, name="reports.shadow_deliver"))
        assert result["reports"] == 2
        assert count(engine, ReportDelivery) == 2
        assert count(engine, Outbox) == 2
    finally:
        cleanup(engine)
