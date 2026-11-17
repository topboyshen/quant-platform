"""M6.6 S1-S8 report comparator."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.models.signals import ExecutionDigest
from qp.reports.compare import compare_reports
from qp.reports.daily import import_daily_summary
from qp.reports.delivery import shadow_deliver
from qp.reports.digests import import_execution_digest, import_research_digest
from qp.reports.hermes import import_hermes_summary
from qp.reports.legacy import _projection, digest
from qp.reports.receipts import attach_receipts

DAY = dt.date(2026, 11, 12)
PROFIT_JOB = "ec6c86fd856c"
HEALTH_JOB = "026a1083d9e0"


def daily_summary(text: str = "daily body") -> dict:
    details = {"accountId": "1", "checks": {}, "etfCoverage": {}, "notificationCoverage": {},
               "sources": {"profit": {}, "health": {}}}
    summary = {
        "accountId": "1", "businessDate": DAY.isoformat(), "details": details,
        "generatedAt": "2026-11-12T08:20:01+00:00",
        "presentationSchema": "quant.daily-readable.v1", "revision": "v1",
        "sourceReferences": [{"accountId": "1", "decisionDate": DAY.isoformat()}],
        "text": text, "textSha256": hashlib.sha256(text.encode()).hexdigest(),
        "version": "daily-research-summary.v1",
    }
    projection = _projection(summary, None)
    summary["evidenceBinding"] = {**projection, "sha256": digest(projection)}
    return summary


def write_envelope(root: Path, job_id: str, mode: str, text: str) -> None:
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    body = text.encode()
    dispatch = hashlib.sha256((job_id + mode + text).encode()).hexdigest()[:32]
    (job_dir / f"{dispatch}.raw").write_bytes(body)
    (job_dir / f"{dispatch}.json").write_text(json.dumps({
        "schema": "daily-summary-original-envelope.v1", "jobId": job_id, "mode": mode,
        "businessDate": DAY.isoformat(), "dispatchId": dispatch, "returnCode": 0,
        "silent": True, "bodyBytes": len(body), "bodySha256": hashlib.sha256(body).hexdigest(),
        "envelopeBytes": len(body), "envelopeFile": f"{dispatch}.envelope.raw",
        "originalFile": f"{dispatch}.raw",
    }), encoding="utf-8")


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.execute(delete(ExecutionDigest))
        session.execute(delete(Outbox))
        session.execute(delete(DeliveryAttempt))
        session.commit()


def seed(engine, tmp_path: Path) -> dict:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    summary = daily_summary()
    (daily_dir / f"{DAY.isoformat()}-v1.json").write_text(json.dumps(summary), encoding="utf-8")
    digest_path = tmp_path / "research_digest.json"
    digest_payload = {
        "generated_at": "2026-11-12T16:00:01+08:00",
        "index_close_cache": {"trade_date": DAY.isoformat()},
        "index_closes": [{"symbol": "sh000001", "trade_date": DAY.isoformat(), "close": 3400.0}],
        "macro": {"available": True, "date": DAY.isoformat()},
        "strategies": [{"id": 1, "name": "s"}],
        "note": "local",
    }
    digest_path.write_text(json.dumps(digest_payload), encoding="utf-8")
    originals = tmp_path / "originals"
    write_envelope(originals, PROFIT_JOB, "profit", "profit body")
    write_envelope(originals, HEALTH_JOB, "health", "health body")

    with Session(engine) as session:
        import_daily_summary(session, summary, source_path=daily_dir / f"{DAY.isoformat()}-v1.json")
        import_research_digest(session, digest_payload, source_path=digest_path,
                               source_sha256=hashlib.sha256(digest_path.read_bytes()).hexdigest())
        row = ExecutionDigest(digest_date=DAY, item_count=0, accepted=0, rejected=0,
                              body={"items": []}, sha256="e" * 64, push_state="shadowed")
        session.add(row)
        session.flush()
        import_execution_digest(session, row)
        for kind in ("profit_summary", "profit_radar_health"):
            from qp.reports.hermes import find_envelope
            job = PROFIT_JOB if kind == "profit_summary" else HEALTH_JOB
            import_hermes_summary(session, find_envelope(originals, job, DAY))
        session.commit()
        reports = session.query(Report).all()
        for report in reports:
            shadow_deliver(session, report)
        session.commit()
        session.query(ReportDelivery).first()
        attach_receipts(session, [{
            "execution_id": "exec-1", "job_id": PROFIT_JOB, "kind": "profit_summary",
            "created_at": "2026-11-12T15:10:23+08:00",
            "finished_at": "2026-11-12T15:10:25+08:00"}])
        session.commit()
    return {"daily_dir": daily_dir, "digest_path": digest_path, "originals": originals}


def test_compare_passes_with_consistent_mirror(engine, tmp_path: Path):
    cleanup(engine)
    try:
        paths = seed(engine, tmp_path)
        with Session(engine) as session:
            result = compare_reports(session, day=DAY, daily_dir=paths["daily_dir"],
                                     digest_path=paths["digest_path"],
                                     hermes_originals=paths["originals"])
        assert result["overall"] == "PASS", result
        assert all(c["status"] == "PASS" for c in result["checks"]), result["checks"]
    finally:
        cleanup(engine)


def test_compare_fails_when_legacy_file_changes(engine, tmp_path: Path):
    cleanup(engine)
    try:
        paths = seed(engine, tmp_path)
        (paths["daily_dir"] / f"{DAY.isoformat()}-v1.json").write_text(
            json.dumps({"changed": True}), encoding="utf-8")
        with Session(engine) as session:
            result = compare_reports(session, day=DAY, daily_dir=paths["daily_dir"],
                                     digest_path=paths["digest_path"],
                                     hermes_originals=paths["originals"])
        s1 = next(c for c in result["checks"] if c["id"] == "S1")
        s8 = next(c for c in result["checks"] if c["id"] == "S8")
        assert s1["status"] == "FAIL" and s8["status"] == "FAIL"
    finally:
        cleanup(engine)


def test_compare_fails_on_non_shadow_delivery(engine, tmp_path: Path):
    cleanup(engine)
    try:
        paths = seed(engine, tmp_path)
        with Session(engine) as session:
            delivery = session.query(ReportDelivery).first()
            delivery.mode = "live"
            session.commit()
            result = compare_reports(session, day=DAY, daily_dir=paths["daily_dir"],
                                     digest_path=paths["digest_path"],
                                     hermes_originals=paths["originals"])
        s6 = next(c for c in result["checks"] if c["id"] == "S6")
        assert s6["status"] == "FAIL"
    finally:
        cleanup(engine)
