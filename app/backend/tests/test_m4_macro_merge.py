"""M4.4 partial-collection merge: retries may not erase earlier successful factors.

The 15:48 same-day EOD retry calls ``persist_macro`` again.  A retry whose upstream
for one factor fails must keep the value (and possibly the repair record) that an
earlier attempt already wrote; legacy ``SwingStore.upsert_partial`` behaved this way.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.signals import MacroSlice
from qp.signals.eod import persist_macro

DAY = dt.date(2027, 3, 4)  # Thursday, beyond the test calendar fixtures


def status(available: bool, *, source: str = "test", message: str = "") -> dict:
    return {
        "available": available,
        "source": source,
        "message": message,
        "observed_date": DAY.isoformat() if available else None,
        "stale": not available,
        "anomalous": False,
    }


def macro(**overrides) -> dict:
    payload = {
        "total_amount": 13573.57,
        "margin_balance": 22800.0,
        "margin_growth_pct": 0.5,
        "limit_up": 60,
        "limit_down": 2,
        "limit_ratio": 20.0,
        "source": "tencent",
        "collect_status": {
            "total_amount": status(True),
            "margin_balance": status(True),
            "margin_growth_pct": status(True),
            "limit_ratio": status(True),
        },
    }
    payload.update(overrides)
    return payload


def cleanup(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(MacroSlice).where(MacroSlice.decision_date == DAY))
        session.commit()


def test_retry_without_total_amount_keeps_earlier_value(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            persist_macro(session, DAY, macro())
            session.commit()
        failed = status(False, source="tencent/sina/eastmoney", message="missing")
        with Session(engine) as session:
            persist_macro(
                session,
                DAY,
                macro(
                    total_amount=None,
                    margin_balance=23100.0,
                    source="sina",
                    collect_status={"total_amount": failed, "margin_balance": status(True, source="sse")},
                ),
            )
            session.commit()
        with Session(engine) as session:
            row = session.get(MacroSlice, DAY)
            assert float(row.total_amount) == 13573.57
            assert float(row.margin_balance) == 23100.0
            assert row.collect_status["total_amount"]["available"] is True
            assert row.collect_status["last_collect_status"]["total_amount"] == failed
        with Session(engine) as session:
            persist_macro(session, DAY, macro(margin_balance=23200.0))
            session.commit()
        with Session(engine) as session:
            row = session.get(MacroSlice, DAY)
            assert "last_collect_status" not in row.collect_status
            assert float(row.margin_balance) == 23200.0
    finally:
        cleanup(engine)


def test_complete_new_value_replaces_old_value_and_status(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            persist_macro(
                session,
                DAY,
                macro(total_amount=None, collect_status={"total_amount": status(False, message="missing")}),
            )
            persist_macro(
                session,
                DAY,
                macro(total_amount=14000.0, collect_status={"total_amount": status(True, source="tencent")}),
            )
            session.commit()
        with Session(engine) as session:
            row = session.get(MacroSlice, DAY)
            assert float(row.total_amount) == 14000.0
            assert row.collect_status["total_amount"]["available"] is True
            assert "last_collect_status" not in row.collect_status
    finally:
        cleanup(engine)


def test_repair_record_and_value_survive_a_failed_retry(engine):
    cleanup(engine)
    try:
        record = {"batch_id": f"qp-total-amount-repair-{DAY.isoformat()}", "qualifying_run_id": 42, "value": 13573.57}
        with Session(engine) as session:
            persist_macro(
                session,
                DAY,
                macro(
                    collect_status={
                        "total_amount": {**status(True, source="tencent"), "repair": {"batch_id": record["batch_id"]}},
                        "factor_imports": [record],
                    }
                ),
            )
            session.commit()
        failed = status(False, source="tencent/sina/eastmoney", message="missing")
        with Session(engine) as session:
            persist_macro(
                session, DAY, macro(total_amount=None, source="sina/eastmoney", collect_status={"total_amount": failed})
            )
            session.commit()
        with Session(engine) as session:
            row = session.get(MacroSlice, DAY)
            assert float(row.total_amount) == 13573.57
            assert row.collect_status["factor_imports"] == [record]
            assert row.collect_status["total_amount"]["repair"]["batch_id"] == record["batch_id"]
    finally:
        cleanup(engine)


def test_sources_merge_without_duplicates(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            persist_macro(session, DAY, macro(source="tencent"))
            persist_macro(session, DAY, macro(source="sina+eastmoney"))
            persist_macro(session, DAY, macro(source="tencent+eastmoney"))
            session.commit()
        with Session(engine) as session:
            assert session.get(MacroSlice, DAY).source == "tencent+sina+eastmoney"
    finally:
        cleanup(engine)
