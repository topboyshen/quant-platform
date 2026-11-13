"""M7.4 report center APIs: reports, deliveries and receipts."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource

DAY = dt.date(2026, 11, 12)
NOW = dt.datetime(2026, 11, 12, 16, 30)


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.commit()


def seed(engine) -> None:
    with Session(engine) as session:
        daily = Report(kind="daily_research_summary", business_date=DAY, version=1,
                       status="frozen", content_sha256="a" * 64,
                       payload={"text": "daily body"})
        digest = Report(kind="research_digest", business_date=DAY, version=1,
                        status="frozen", content_sha256="b" * 64,
                        payload={"note": "digest"})
        session.add_all([daily, digest])
        session.flush()
        session.add(ReportSource(report_id=daily.id, source="legacy_daily_summary",
                                 locator="/tmp/x.json", sha256="c" * 64))
        delivery = ReportDelivery(report_id=daily.id, channel="telegram", target="ops",
                                  mode="shadow", status="shadowed", dedup_key="k-1",
                                  send_result={"note": "shadow mode: not sent"})
        session.add(delivery)
        session.flush()
        session.add(ReportReceipt(delivery_id=delivery.id, status="received",
                                  received_at=NOW, evidence={"execution_id": "e-1"}))
        session.commit()


def _token(engine) -> str:
    import uuid
    from qp.auth.sessions import create_service_token
    with Session(engine) as s:
        tok = create_service_token(s, f"m7-reports-{uuid.uuid4().hex[:8]}", ["ops:read"])
        s.commit()
    return tok


def test_report_endpoints(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        h = {"authorization": f"Bearer {_token(engine)}"}

        listing = client.get("/api/reports", headers=h).json()
        assert listing["total"] == 2
        assert listing["counts"]["daily_research_summary"] == 1
        assert listing["rows"][0]["kind"] in ("daily_research_summary", "research_digest")

        filtered = client.get("/api/reports?kind=research_digest", headers=h).json()
        assert filtered["total"] == 1 and filtered["rows"][0]["kind"] == "research_digest"

        daily_id = next(r["id"] for r in listing["rows"] if r["kind"] == "daily_research_summary")
        detail = client.get(f"/api/reports/{daily_id}", headers=h).json()
        assert detail["report"]["payload"]["text"] == "daily body"
        assert detail["sources"][0]["source"] == "legacy_daily_summary"
        assert detail["deliveries"][0]["mode"] == "shadow"
        assert detail["deliveries"][0]["receipts"][0]["status"] == "received"
        assert client.get("/api/reports/999", headers=h).status_code == 404

        deliveries = client.get("/api/reports/deliveries", headers=h).json()
        assert deliveries["total"] == 1
        row = deliveries["rows"][0]
        assert row["status"] == "shadowed" and row["receipt_status"] == "received"
        assert row["received_at"] is not None
    finally:
        cleanup(engine)


def test_reports_requires_auth(client):
    assert client.get("/api/reports").status_code == 401
