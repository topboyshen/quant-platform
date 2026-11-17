"""M8.5 A1-A6 chain checks inside qp reports compare."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.reports.archive import freeze_report
from qp.reports.compare import compare_reports

DAY = dt.date(2026, 11, 13)


def payload(**overrides) -> dict:
    body = {
        "date": DAY.isoformat(),
        "precollect": {"payload_sha256_ok": True},
        "generation": {"target_date": DAY.isoformat(), "status": "succeeded", "child_exit_code": 0},
        "seal": {
            "state": "SEALED",
            "target_matches": True,
            "report_sha_matches": True,
            "digest_present": True,
            "report_sha256": "a" * 64,
        },
        "relay": {
            "target_matches": True,
            "produced_date": DAY.isoformat(),
            "attempt": {"status": "sent", "reason": ""},
            "delivery": {"status": "confirmed", "content_match": True},
        },
        "alerts": {"business_atoms": []},
        "classification": {"state": "healthy", "matches_legacy": True, "missing_in_legacy": [], "extra_in_legacy": []},
    }
    body.update(overrides)
    return body


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.commit()


def a_checks(result: dict) -> dict[str, dict]:
    return {c["id"]: c for c in result["checks"] if c["id"].startswith("A")}


def test_morning_checks_pass_for_healthy_chain(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            freeze_report(session, kind="morning_chain", business_date=DAY, payload=payload())
            session.commit()
            result = compare_reports(session, day=DAY)
        checks = a_checks(result)
        assert set(checks) == {"A1", "A2", "A3", "A4", "A5", "A6"}
        assert all(c["status"] == "PASS" for c in checks.values()), checks
    finally:
        cleanup(engine)


def test_forbidden_key_fails_a6(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            freeze_report(session, kind="morning_chain", business_date=DAY, payload=payload(alerts={"chat_id": "x"}))
            session.commit()
            result = compare_reports(session, day=DAY)
        assert a_checks(result)["A6"]["status"] == "FAIL"
    finally:
        cleanup(engine)


def test_live_delivery_fails_a5(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            report, _ = freeze_report(session, kind="morning_chain", business_date=DAY, payload=payload())
            session.add(
                ReportDelivery(
                    report_id=report.id,
                    channel="telegram",
                    target="ops",
                    mode="live",
                    status="sent",
                    dedup_key="m8-live",
                )
            )
            session.commit()
            result = compare_reports(session, day=DAY)
        assert a_checks(result)["A5"]["status"] == "FAIL"
    finally:
        cleanup(engine)


def test_classification_mismatch_fails_a4(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            bad = payload(
                classification={
                    "state": "degraded",
                    "matches_legacy": False,
                    "missing_in_legacy": ["2026-11-13|relay_blocked|"],
                    "extra_in_legacy": [],
                }
            )
            freeze_report(session, kind="morning_chain", business_date=DAY, payload=bad)
            session.commit()
            result = compare_reports(session, day=DAY)
        assert a_checks(result)["A4"]["status"] == "FAIL"
    finally:
        cleanup(engine)


def test_no_morning_report_means_no_a_checks(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            result = compare_reports(session, day=DAY)
        assert a_checks(result) == {}
    finally:
        cleanup(engine)
