"""M6.3 research/execution digest archiving."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.models.signals import ExecutionDigest
from qp.reports.archive import ReportConflict
from qp.reports.digests import (
    import_execution_digest,
    import_research_digest,
    research_digest_date,
)

DAY = dt.date(2026, 11, 13)


def research_payload(trade_date: str = "2026-11-13", generated_at: str = "2026-11-13T13:00:01+08:00") -> dict:
    return {
        "generated_at": generated_at,
        "index_close_cache": {"trade_date": trade_date, "available": True},
        "index_closes": [{"symbol": "sh000001", "trade_date": trade_date, "close": 3400.0,
                          "source": "legacy"}],
        "macro": {"available": True, "date": trade_date},
        "note": "Not OpenBB. Local digest only.",
        "recent_runs": [],
        "strategies": [],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.execute(delete(ExecutionDigest))
        session.commit()


def test_research_digest_date_resolution():
    assert research_digest_date(research_payload()) == dt.date(2026, 11, 13)
    fallback = research_payload(trade_date=None)
    fallback["index_close_cache"] = {}
    fallback["index_closes"] = [{"trade_date": "2026-11-12"}, {"trade_date": "2026-11-11"}]
    assert research_digest_date(fallback) == dt.date(2026, 11, 12)
    generated_only = {"generated_at": "2026-11-13T09:00:00+08:00"}
    assert research_digest_date(generated_only) == dt.date(2026, 11, 13)
    with pytest.raises(ValueError):
        research_digest_date({"generated_at": "not-a-date"})


def test_research_digest_import_is_idempotent(engine, tmp_path: Path):
    cleanup(engine)
    try:
        payload = research_payload()
        path = tmp_path / "research_digest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with Session(engine) as session:
            report, created = import_research_digest(session, payload, source_path=path)
            session.commit()
            assert created is True and report.kind == "research_digest"
            assert report.business_date == DAY
            assert report.payload == payload
            again, created_again = import_research_digest(session, payload, source_path=path)
            session.commit()
            assert created_again is False and again.id == report.id
            changed = research_payload(generated_at="2026-11-13T14:00:01+08:00")
            with pytest.raises(ReportConflict):
                import_research_digest(session, changed, source_path=path)
    finally:
        cleanup(engine)


def test_execution_digest_is_archived_by_reference(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            row = ExecutionDigest(digest_date=DAY, item_count=0, accepted=0, rejected=0,
                                  body={"as_of_day": DAY.isoformat(), "items": []},
                                  sha256="d" * 64, push_state="shadowed")
            session.add(row)
            session.commit()
            report, created = import_execution_digest(session, row)
            session.commit()
            assert created is True and report.kind == "execution_digest"
            assert report.payload["sha256"] == "d" * 64
            assert report.payload["body"] == row.body
            source = session.query(ReportSource).filter_by(report_id=report.id).one()
            assert source.source == "signals.execution_digest"
            assert source.sha256 == "d" * 64
            again, created_again = import_execution_digest(session, row)
            session.commit()
            assert created_again is False and again.id == report.id
    finally:
        cleanup(engine)


def test_jobs_skip_without_sources(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_RESEARCH_DIGEST_JSON", str(tmp_path / "missing.json"))
        monkeypatch.setenv("QP_REPORT_DATE", DAY.isoformat())
        from qp.jobs.reports import (
            import_execution_digest_job,
            import_research_digest_job,
        )
        assert import_research_digest_job(JobContext(engine=engine, run_id=0,
                                                     name="reports.import_research_digest"))["skipped"]
        assert import_execution_digest_job(JobContext(engine=engine, run_id=0,
                                                      name="reports.import_execution_digest"))["skipped"]
    finally:
        cleanup(engine)
