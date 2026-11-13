"""M6 report jobs.  Shadow only: read legacy frozen reports, never send."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.notify import Outbox
from qp.models.reports import Report
from qp.models.signals import ExecutionDigest
from qp.jobs.registry import JobContext, job
from qp.reports.daily import import_daily_summary
from qp.reports.digests import (import_execution_digest, import_research_digest,
                                load_research_digest)
from qp.reports.delivery import shadow_deliver
from qp.reports.hermes import find_envelope, import_hermes_summary
from qp.reports.compare import compare_reports, write_report
from qp.reports.receipts import attach_receipts, load_delivered
from qp.reports.legacy import file_sha256, is_readable_daily, legacy_daily_path, load_legacy_daily

CST = ZoneInfo("Asia/Shanghai")
DEFAULT_LEGACY_DAILY_DIR = "/home/ubuntu/trading-radar/data/daily-research-summary/1"
DEFAULT_RESEARCH_DIGEST = "/home/ubuntu/trading-radar/data/research_digest.json"
DEFAULT_HERMES_ORIGINALS = "/home/ubuntu/trading-radar/data/daily-research-summary/1/originals"
DEFAULT_HERMES_DELIVERIES_DB = "/home/ubuntu/.hermes/cron/deliveries.db"
DEFAULT_REPORT_OUT_DIR = "/var/lib/qp/report-reports"
DEFAULT_HERMES_EXECUTIONS_DB = "/home/ubuntu/.hermes/cron/executions.db"
HERMES_PROFIT_JOB = "ec6c86fd856c"
HERMES_HEALTH_JOB = "026a1083d9e0"


@job("reports.import_daily", "Freeze the legacy daily research summary into reports.* "
                             "(read-only legacy file; no delivery)",
     schedule_hint="Mon-Fri 16:40 CST (qp-reports-daily.timer)")
def import_daily_job(ctx: JobContext) -> dict:
    root = os.getenv("QP_LEGACY_DAILY_DIR", DEFAULT_LEGACY_DAILY_DIR).strip()
    raw_day = os.getenv("QP_REPORT_DATE", "").strip()
    day = (dt.date.fromisoformat(raw_day) if raw_day
           else dt.datetime.now(CST).date() - dt.timedelta(days=1))
    path = legacy_daily_path(root, day)
    if path is None:
        return {"ok": True, "skipped": f"no legacy daily summary for {day} under {root}"}
    summary = load_legacy_daily(path)
    if not is_readable_daily(summary):
        return {"ok": True, "skipped": f"not a readable daily summary: {path.name}"}
    with Session(ctx.engine) as session:
        report, created = import_daily_summary(session, summary, source_path=path)
        session.commit()
        return {"ok": True, "date": day.isoformat(), "report_id": report.id,
                "created": created, "kind": report.kind,
                "content_sha256": report.content_sha256}


@job("reports.import_research_digest", "Freeze the legacy research digest JSON into reports.* "
                                       "(read-only file; no delivery)",
     schedule_hint="daily 16:50 CST (qp-reports-research-digest.timer)")
def import_research_digest_job(ctx: JobContext) -> dict:
    path = Path(os.getenv("QP_RESEARCH_DIGEST_JSON", DEFAULT_RESEARCH_DIGEST))
    if not path.is_file():
        return {"ok": True, "skipped": f"research digest not found: {path}"}
    payload = load_research_digest(path)
    with Session(ctx.engine) as session:
        report, created = import_research_digest(session, payload, source_path=path,
                                                 source_sha256=file_sha256(path))
        session.commit()
        return {"ok": True, "date": report.business_date.isoformat(), "report_id": report.id,
                "created": created, "kind": report.kind}


@job("reports.import_execution_digest", "Archive the M4 execution digest reference into reports.* "
                                        "(no recomputation, no send)",
     schedule_hint="daily 09:00 CST (qp-reports-execution-digest.timer)")
def import_execution_digest_job(ctx: JobContext) -> dict:
    raw_day = os.getenv("QP_REPORT_DATE", "").strip()
    day = (dt.date.fromisoformat(raw_day) if raw_day
           else dt.datetime.now(CST).date() - dt.timedelta(days=1))
    with Session(ctx.engine) as session:
        row = session.get(ExecutionDigest, day)
        if row is None:
            return {"ok": True, "skipped": f"no execution digest for {day}"}
        report, created = import_execution_digest(session, row)
        session.commit()
        return {"ok": True, "date": day.isoformat(), "report_id": report.id,
                "created": created, "kind": report.kind}


def _import_hermes(ctx: JobContext, *, job_env: str, default_job: str) -> dict:
    root = os.getenv("QP_HERMES_ORIGINALS_DIR", DEFAULT_HERMES_ORIGINALS)
    job_id = os.getenv(job_env, default_job)
    raw_day = os.getenv("QP_REPORT_DATE", "").strip()
    day = (dt.date.fromisoformat(raw_day) if raw_day
           else dt.datetime.now(CST).date() - dt.timedelta(days=1))
    envelope = find_envelope(root, job_id, day)
    if envelope is None:
        return {"ok": True, "skipped": f"no hermes envelope for {day} job {job_id}"}
    with Session(ctx.engine) as session:
        report, created = import_hermes_summary(session, envelope)
        session.commit()
        return {"ok": True, "date": day.isoformat(), "report_id": report.id,
                "created": created, "kind": report.kind, "version": report.version}


@job("reports.import_profit_summary", "Freeze the Hermes profit summary into reports.* "
                                      "(read-only archive; no delivery)",
     schedule_hint="Mon-Fri 15:20 CST (qp-reports-profit.timer)")
def import_profit_summary_job(ctx: JobContext) -> dict:
    return _import_hermes(ctx, job_env="QP_HERMES_PROFIT_JOB", default_job=HERMES_PROFIT_JOB)


@job("reports.import_health_summary", "Freeze the Hermes Profit Radar health summary into "
                                      "reports.* (read-only archive; no delivery)",
     schedule_hint="Mon-Fri 15:45 CST (qp-reports-health.timer)")
def import_health_summary_job(ctx: JobContext) -> dict:
    return _import_hermes(ctx, job_env="QP_HERMES_HEALTH_JOB", default_job=HERMES_HEALTH_JOB)


@job("reports.shadow_deliver", "Mark frozen reports as shadow deliveries (never sends)",
     schedule_hint="daily 18:30 CST (qp-reports-shadow-deliver.timer)")
def shadow_deliver_job(ctx: JobContext) -> dict:
    raw_day = os.getenv("QP_REPORT_DATE", "").strip()
    day = (dt.date.fromisoformat(raw_day) if raw_day
           else dt.datetime.now(CST).date() - dt.timedelta(days=1))
    with Session(ctx.engine) as session:
        reports = session.scalars(select(Report).where(Report.business_date == day)).all()
        delivered = 0
        for report in reports:
            shadow_deliver(session, report)
            delivered += 1
        session.commit()
        return {"ok": True, "date": day.isoformat(), "reports": delivered}


@job("reports.import_receipts", "Record Hermes delivery receipts for frozen reports "
                                "(read-only SQLite; no send)",
     schedule_hint="daily 18:40 CST (qp-reports-receipts.timer)")
def import_receipts_job(ctx: JobContext) -> dict:
    deliveries_db = os.getenv("QP_HERMES_DELIVERIES_DB", DEFAULT_HERMES_DELIVERIES_DB)
    executions_db = os.getenv("QP_HERMES_EXECUTIONS_DB", DEFAULT_HERMES_EXECUTIONS_DB)
    if not (Path(deliveries_db).is_file() and Path(executions_db).is_file()):
        return {"ok": True, "skipped": "hermes delivery ledger not found"}
    rows = load_delivered(deliveries_db, executions_db)
    with Session(ctx.engine) as session:
        counts = attach_receipts(session, rows)
        session.commit()
        return {"ok": True, "delivered_rows": len(rows), **counts}


@job("reports.compare", "S1-S8 report shadow comparison (read-only; report only)",
     schedule_hint="daily 18:50 CST (qp-reports-compare.timer)")
def compare_job(ctx: JobContext) -> dict:
    raw_day = os.getenv("QP_REPORT_DATE", "").strip()
    day = (dt.date.fromisoformat(raw_day) if raw_day
           else dt.datetime.now(CST).date() - dt.timedelta(days=1))
    with Session(ctx.engine) as session:
        result = compare_reports(
            session, day=day,
            daily_dir=os.getenv("QP_LEGACY_DAILY_DIR", DEFAULT_LEGACY_DAILY_DIR),
            digest_path=os.getenv("QP_RESEARCH_DIGEST_JSON", DEFAULT_RESEARCH_DIGEST),
            hermes_originals=os.getenv("QP_HERMES_ORIGINALS_DIR", DEFAULT_HERMES_ORIGINALS))
    files = write_report(result, os.getenv("QP_REPORTS_REPORT_DIR", DEFAULT_REPORT_OUT_DIR))
    return {"ok": True, "date": day.isoformat(), "overall": result["overall"],
            "counts": result["counts"], "files": files}
