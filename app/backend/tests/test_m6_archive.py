"""M6.1 report archive: immutability, idempotency, delivery vs receipt separation."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.reports.archive import (
    ReportConflict,
    SourceRef,
    freeze_report,
    mark_delivery,
    payload_sha256,
    record_delivery,
    record_receipt,
)

DAY = dt.date(2026, 11, 13)
PAYLOAD = {"title": "日报", "sections": [{"name": "signals", "n": 3}], "version": 1}


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_freeze_is_idempotent_and_records_sources(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            report, created = freeze_report(
                session,
                kind="daily_research_summary",
                business_date=DAY,
                payload=PAYLOAD,
                sources=[SourceRef(source="legacy_state", locator="2026-11-13-v1.json", sha256="a" * 64)],
            )
            session.commit()
            assert created is True and report.id is not None
            assert report.content_sha256 == payload_sha256(PAYLOAD)

            same, created_again = freeze_report(
                session, kind="daily_research_summary", business_date=DAY, payload=PAYLOAD
            )
            session.commit()
            assert created_again is False and same.id == report.id
        assert count(engine, Report) == 1
        assert count(engine, ReportSource) == 1
    finally:
        cleanup(engine)


def test_frozen_report_cannot_be_replaced(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            freeze_report(session, kind="daily_research_summary", business_date=DAY, payload=PAYLOAD)
            session.commit()
            changed = {**PAYLOAD, "title": "changed"}
            with pytest.raises(ReportConflict):
                freeze_report(session, kind="daily_research_summary", business_date=DAY, payload=changed)
            # A new version is the only way to publish changed content.
            report, created = freeze_report(
                session, kind="daily_research_summary", business_date=DAY, payload=changed, version=2
            )
            session.commit()
            assert created is True and report.version == 2
        assert count(engine, Report) == 2
    finally:
        cleanup(engine)


def test_delivery_and_receipt_are_separate(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            report, _ = freeze_report(session, kind="daily_research_summary", business_date=DAY, payload=PAYLOAD)
            delivery = record_delivery(session, report, channel="telegram", target="ops")
            assert delivery.mode == "shadow" and delivery.status == "pending"
            mark_delivery(session, delivery, status="shadowed", send_result={"note": "shadow mode: not sent"})
            session.commit()
            assert delivery.sent_at is not None
            assert delivery.send_result == {"note": "shadow mode: not sent"}

            receipt = record_receipt(session, delivery, status="received", evidence={"source": "relay"})
            session.commit()
            assert receipt.received_at is not None
            # Receipt must not rewrite the sender-side result.
            assert delivery.send_result == {"note": "shadow mode: not sent"}
        assert count(engine, ReportReceipt) == 1
    finally:
        cleanup(engine)


def test_delivery_dedup_key_is_stable(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            report, _ = freeze_report(session, kind="daily_research_summary", business_date=DAY, payload=PAYLOAD)
            first = record_delivery(session, report, channel="telegram", target="ops")
            second = record_delivery(session, report, channel="telegram", target="ops")
            session.commit()
            assert first.id == second.id
        assert count(engine, ReportDelivery) == 1
    finally:
        cleanup(engine)
