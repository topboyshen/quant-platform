"""M6.2 daily summary: legacy integrity validation, freezing, job behaviour."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.reports.archive import ReportConflict
from qp.reports.daily import LegacyDailyInvalid, import_daily_summary
from qp.reports.legacy import _projection, digest, validate_legacy_daily

DAY = "2026-11-12"


def make_summary(text: str = "2026-11-12 收盘研究日报｜仅研究/模拟，不下单\n\n今日要点｜ok\n") -> dict:
    details = {
        "accountId": "1",
        "checks": {},
        "etfCoverage": {},
        "notificationCoverage": {},
        "sources": {
            "profit": {f: None for f in ("executionId", "runId", "marketDate", "generatedAt",
                                         "status", "releaseSha256", "resultSha256")},
            "health": {f: None for f in ("executionId", "runId", "marketDate", "generatedAt",
                                         "status", "releaseSha256", "resultSha256")},
        },
    }
    summary = {
        "accountId": "1",
        "businessDate": DAY,
        "details": details,
        "generatedAt": "2026-11-12T08:20:01+00:00",
        "presentationSchema": "quant.daily-readable.v1",
        "revision": "v1",
        "sourceReferences": [{"accountId": "1", "decisionDate": DAY}],
        "text": text,
        "textSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "version": "daily-research-summary.v1",
    }
    projection = _projection(summary, None)
    summary["evidenceBinding"] = {**projection, "sha256": digest(projection)}
    return summary


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.commit()


def test_valid_summary_passes_integrity_checks():
    assert validate_legacy_daily(make_summary()) == []


def test_import_freezes_report_and_binds_source(engine, tmp_path: Path):
    cleanup(engine)
    try:
        summary = make_summary()
        path = tmp_path / f"{DAY}-v1.json"
        path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with Session(engine) as session:
            report, created = import_daily_summary(session, summary, source_path=path)
            session.commit()
            assert created is True and report.kind == "daily_research_summary"
            assert report.business_date == dt.date(2026, 11, 12)
            assert report.payload == summary
            source = session.scalar(select(ReportSource))
            assert source.sha256 == file_sha
            assert source.source == "legacy_daily_summary"

            again, created_again = import_daily_summary(session, summary, source_path=path)
            session.commit()
            assert created_again is False and again.id == report.id
    finally:
        cleanup(engine)


def test_tampered_text_is_rejected(engine):
    cleanup(engine)
    try:
        summary = make_summary()
        summary["text"] = summary["text"] + "tampered"
        with Session(engine) as session:
            with pytest.raises(LegacyDailyInvalid) as exc:
                import_daily_summary(session, summary)
            assert "text_sha256" in str(exc.value)
    finally:
        cleanup(engine)


def test_changed_body_cannot_replace_frozen_report(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            import_daily_summary(session, make_summary())
            session.commit()
            changed = make_summary(text="different body")
            with pytest.raises(ReportConflict):
                import_daily_summary(session, changed)
    finally:
        cleanup(engine)


def test_job_skips_when_file_missing(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_LEGACY_DAILY_DIR", str(tmp_path))
        monkeypatch.setenv("QP_REPORT_DATE", DAY)
        from qp.jobs.reports import import_daily_job
        result = import_daily_job(JobContext(engine=engine, run_id=0, name="reports.import_daily"))
        assert result["skipped"]
    finally:
        cleanup(engine)


def test_job_skips_operational_failure_record(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        path = tmp_path / f"{DAY}-operational-failure.json"
        path.write_text(json.dumps({"businessDate": DAY, "error": "x"}), encoding="utf-8")
        monkeypatch.setenv("QP_LEGACY_DAILY_DIR", str(tmp_path))
        monkeypatch.setenv("QP_REPORT_DATE", DAY)
        from qp.jobs.reports import import_daily_job
        result = import_daily_job(JobContext(engine=engine, run_id=0, name="reports.import_daily"))
        assert "operational" in result["skipped"] or "not a readable" in result["skipped"]
    finally:
        cleanup(engine)


def test_job_imports_existing_file(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        path = tmp_path / f"{DAY}-v1.json"
        path.write_text(json.dumps(make_summary(), ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("QP_LEGACY_DAILY_DIR", str(tmp_path))
        monkeypatch.setenv("QP_REPORT_DATE", DAY)
        from qp.jobs.reports import import_daily_job
        result = import_daily_job(JobContext(engine=engine, run_id=0, name="reports.import_daily"))
        assert result["ok"] and result["created"] is True
        assert result["date"] == DAY
    finally:
        cleanup(engine)
