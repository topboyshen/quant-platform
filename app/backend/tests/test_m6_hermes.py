"""M6.4 Hermes cron summaries: envelope lookup, body verification, versioned freeze."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.reports.archive import ReportConflict  # noqa: F401  (imported for API parity)
from qp.reports.hermes import find_envelope, import_hermes_summary, load_envelopes

DAY = "2026-11-12"
PROFIT_JOB = "ec6c86fd856c"
HEALTH_JOB = "026a1083d9e0"


def write_envelope(root: Path, job_id: str, mode: str, text: str,
                   business_date: str = DAY) -> Path:
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dispatch = hashlib.sha256((job_id + mode + business_date + text).encode()).hexdigest()[:32]
    body = text.encode("utf-8")
    body_path = job_dir / f"{dispatch}.raw"
    body_path.write_bytes(body)
    envelope = {
        "schema": "daily-summary-original-envelope.v1",
        "jobId": job_id,
        "mode": mode,
        "businessDate": business_date,
        "dispatchId": dispatch,
        "returnCode": 0,
        "silent": True,
        "bodyBytes": len(body),
        "bodySha256": hashlib.sha256(body).hexdigest(),
        "envelopeBytes": len(body) + 10,
        "envelopeFile": f"{dispatch}.envelope.raw",
        "originalFile": f"{dispatch}.raw",
    }
    path = job_dir / f"{dispatch}.json"
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return path


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.commit()


def test_envelope_discovery_and_body_verification(tmp_path: Path, engine):
    cleanup(engine)
    try:
        write_envelope(tmp_path, PROFIT_JOB, "profit", "profit body")
        write_envelope(tmp_path, HEALTH_JOB, "health", "health body")
        envelopes = load_envelopes(tmp_path, PROFIT_JOB)
        assert len(envelopes) == 1 and envelopes[0]["mode"] == "profit"
        envelope = find_envelope(tmp_path, HEALTH_JOB, dt.date(2026, 11, 12))
        assert envelope is not None and envelope["mode"] == "health"

        with Session(engine) as session:
            report, created = import_hermes_summary(session, envelope)
            session.commit()
            assert created is True and report.kind == "profit_radar_health"
            assert report.payload["text"] == "health body"
            assert report.payload["jobId"] == HEALTH_JOB
    finally:
        cleanup(engine)


def test_retry_with_new_body_bumps_version(tmp_path: Path, engine):
    cleanup(engine)
    try:
        path = write_envelope(tmp_path, PROFIT_JOB, "profit", "first body")
        envelope = find_envelope(tmp_path, PROFIT_JOB, dt.date(2026, 11, 12))
        with Session(engine) as session:
            report, created = import_hermes_summary(session, envelope)
            session.commit()
            assert created is True and report.version == 1

            # Same body again: idempotent.
            same, created_again = import_hermes_summary(session, envelope)
            session.commit()
            assert created_again is False and same.id == report.id

            # A legacy retry with a different body becomes version 2.
            write_envelope(tmp_path, PROFIT_JOB, "profit", "second body")
            newer = find_envelope(tmp_path, PROFIT_JOB, dt.date(2026, 11, 12))
            second, created_second = import_hermes_summary(session, newer)
            session.commit()
            assert created_second is True and second.version == 2
            assert second.payload["text"] == "second body"
    finally:
        cleanup(engine)


def test_tampered_body_is_rejected(tmp_path: Path, engine):
    cleanup(engine)
    try:
        path = write_envelope(tmp_path, PROFIT_JOB, "profit", "original body")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        (path.parent / envelope["originalFile"]).write_text("tampered", encoding="utf-8")
        envelope["_path"] = str(path)
        with Session(engine) as session:
            with pytest.raises(ValueError, match="sha256"):
                import_hermes_summary(session, envelope)
    finally:
        cleanup(engine)


def test_jobs_skip_without_envelopes(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_HERMES_ORIGINALS_DIR", str(tmp_path))
        monkeypatch.setenv("QP_REPORT_DATE", DAY)
        from qp.jobs.reports import import_health_summary_job, import_profit_summary_job
        assert import_profit_summary_job(JobContext(engine=engine, run_id=0,
                                                     name="reports.import_profit_summary"))["skipped"]
        assert import_health_summary_job(JobContext(engine=engine, run_id=0,
                                                    name="reports.import_health_summary"))["skipped"]
    finally:
        cleanup(engine)


def test_job_imports_profit_envelope(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        write_envelope(tmp_path, PROFIT_JOB, "profit", "profit body")
        monkeypatch.setenv("QP_HERMES_ORIGINALS_DIR", str(tmp_path))
        monkeypatch.setenv("QP_REPORT_DATE", DAY)
        from qp.jobs.reports import import_profit_summary_job
        result = import_profit_summary_job(JobContext(engine=engine, run_id=0,
                                                      name="reports.import_profit_summary"))
        assert result["ok"] and result["created"] is True
        assert result["kind"] == "profit_summary" and result["version"] == 1
    finally:
        cleanup(engine)
