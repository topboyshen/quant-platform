"""M4.4 stale-only total-amount repair: predicates, window, fill-only and idempotency."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.models.signals import EodRun, MacroSlice
from qp.signals.eod import CST
from qp.signals.repair import _write_repair, build_import_record, run_repair

DAY = dt.date(2027, 3, 2)  # Tuesday, beyond the test calendar fixtures


def at(hour: int, minute: int, *, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=CST)


def cleanup(engine, day: dt.date = DAY) -> None:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date == day))
        session.execute(delete(MacroSlice).where(MacroSlice.decision_date == day))
        session.commit()


def seed(
    engine,
    *,
    day: dt.date = DAY,
    status: str = "partial",
    reasons: list[str] | None = None,
    total_amount: float | None = None,
    create_slice: bool = True,
) -> int:
    cleanup(engine, day)
    with Session(engine) as session:
        run = EodRun(
            decision_date=day,
            attempt_no=1,
            status=status,
            decision_outcome="no_signal" if status != "failed" else "data_unavailable",
            target_count=0,
            evaluated_count=0,
            failed_count=0,
            signal_count=0,
            allowed_signal_count=0,
            blocked_signal_count=0,
            reason_codes=reasons if reasons is not None else ["macro_data_partial"],
        )
        session.add(run)
        session.flush()
        if create_slice:
            session.add(
                MacroSlice(
                    decision_date=day,
                    total_amount=total_amount,
                    margin_balance=22800.0,
                    margin_growth_pct=0.5,
                    limit_up=60,
                    limit_down=2,
                    limit_ratio=20.0,
                    source="test",
                    collect_status={
                        "total_amount": {
                            "available": False,
                            "source": "tencent/sina/eastmoney",
                            "message": "missing",
                            "observed_date": None,
                            "stale": True,
                            "anomalous": True,
                        },
                        "margin_balance": {
                            "available": True,
                            "source": "sse+szse",
                            "message": "",
                            "observed_date": day.isoformat(),
                            "stale": False,
                            "anomalous": False,
                        },
                    },
                )
            )
        session.commit()
        return run.run_id


def usable(day: dt.date = DAY, value: float = 13573.57) -> dict:
    return {
        "usable": True,
        "value": value,
        "source": "tencent",
        "observed_date": day.isoformat(),
        "status": {
            "available": True,
            "source": "tencent",
            "message": "",
            "observed_date": day.isoformat(),
            "stale": False,
            "anomalous": False,
        },
    }


def test_repair_fills_empty_value_and_appends_one_import(engine):
    run_id = seed(engine)
    try:
        with Session(engine) as session:
            out = run_repair(session, now=at(15, 47), fetch_total=lambda day: usable(day))
            session.commit()
            assert out["ok"] is True and out["action"] == "repaired"
            assert out["value"] == 13573.57 and out["run_id"] == run_id
        with Session(engine) as session:
            macro = session.get(MacroSlice, DAY)
            assert float(macro.total_amount) == 13573.57
            imports = macro.collect_status["factor_imports"]
            assert len(imports) == 1
            record = imports[0]
            assert record["batch_id"] == f"qp-total-amount-repair-{DAY.isoformat()}"
            assert record["qualifying_run_id"] == run_id
            assert record["value"] == 13573.57
            assert record["repair_policy"] == "same_day_partial_macro_data_stale_only"
            assert record["mutates_eod_evidence"] is False
            assert len(record["checksum"]) == 64
            assert macro.collect_status["total_amount"]["available"] is True
            assert macro.collect_status["total_amount"]["repair"]["batch_id"] == record["batch_id"]
        with Session(engine) as session:
            second = run_repair(session, now=at(15, 57), fetch_total=lambda day: usable(day))
            session.commit()
            assert second["ok"] is True and second["action"] == "no_op"
            assert second["reason"] == "total_amount_present"
        with Session(engine) as session:
            assert len(session.get(MacroSlice, DAY).collect_status["factor_imports"]) == 1
    finally:
        cleanup(engine)


def test_dry_run_writes_nothing(engine):
    seed(engine)
    try:
        with Session(engine) as session:
            out = run_repair(session, now=at(15, 47), dry_run=True, fetch_total=lambda day: usable(day))
            session.commit()
            assert out["action"] == "would_repair" and out["dry_run"] is True
            assert session.get(MacroSlice, DAY).total_amount is None
            assert "factor_imports" not in session.get(MacroSlice, DAY).collect_status
    finally:
        cleanup(engine)


def test_unusable_source_is_an_error_and_never_writes(engine):
    seed(engine)
    stale = usable(DAY - dt.timedelta(days=1))
    try:
        with Session(engine) as session:
            out = run_repair(session, now=at(15, 47), fetch_total=lambda day: stale)
            session.commit()
            assert out["ok"] is False and out["reason"] == "source_unusable"
        with Session(engine) as session:
            assert session.get(MacroSlice, DAY).total_amount is None
    finally:
        cleanup(engine)


def test_guards_do_not_fetch_or_write(engine):
    def never(_day):
        raise AssertionError("fetch must not run for an ineligible day")

    cases = []
    cleanup(engine)
    with Session(engine) as session:
        cases.append(("eod_run_missing", run_repair(session, now=at(15, 47), fetch_total=never)))
    seed(engine, status="success", reasons=[])
    with Session(engine) as session:
        cases.append(("latest_eod_not_partial", run_repair(session, now=at(15, 47), fetch_total=never)))
    seed(engine, reasons=["trend_data_partial"])
    with Session(engine) as session:
        cases.append(("macro_data_partial_absent", run_repair(session, now=at(15, 47), fetch_total=never)))
    seed(engine, create_slice=False)
    with Session(engine) as session:
        cases.append(("macro_slice_missing", run_repair(session, now=at(15, 47), fetch_total=never)))
    seed(engine, total_amount=12000.0)
    with Session(engine) as session:
        cases.append(("total_amount_present", run_repair(session, now=at(15, 47), fetch_total=never)))
    for expected, out in cases:
        assert out["ok"] is True and out["action"] == "no_op", (expected, out)
        assert out["reason"] == expected
    cleanup(engine)


def test_window_and_non_trading_day(engine):
    seed(engine)
    try:
        with Session(engine) as session:
            assert run_repair(session, now=at(15, 44), fetch_total=lambda day: usable(day))["reason"] == "too_early"
            assert run_repair(session, now=at(16, 0), fetch_total=lambda day: usable(day))["reason"] == "too_late"
        weekend = dt.date(2027, 3, 6)  # Saturday
        seed(engine, day=weekend)
        with Session(engine) as session:
            out = run_repair(session, now=at(15, 47, day=weekend), fetch_total=lambda day: usable(day))
            assert out["reason"] == "non_trading_day"
        cleanup(engine, weekend)
    finally:
        cleanup(engine)


def test_repair_is_same_day_only(engine):
    seed(engine)
    try:
        with Session(engine) as session:
            out = run_repair(
                session, now=at(15, 47, day=dt.date(2027, 3, 3)), decision_date=DAY, fetch_total=lambda day: usable(day)
            )
            assert out["reason"] == "not_same_day"
    finally:
        cleanup(engine)


def test_write_boundary_never_overwrites(engine):
    seed(engine, total_amount=12000.0)
    try:
        with Session(engine) as session:
            record = build_import_record(decision_date=DAY, run_id=1, collected_at=at(15, 47), result=usable())
            assert _write_repair(session, decision_date=DAY, value=13573.57, record=record) == "already_repaired"
            session.rollback()
        with Session(engine) as session:
            assert float(session.get(MacroSlice, DAY).total_amount) == 12000.0
            assert "factor_imports" not in session.get(MacroSlice, DAY).collect_status
    finally:
        cleanup(engine)


def test_repair_jobs_are_registered():
    assert "signals.total_amount_repair" in JOBS
    hint = JOBS["signals.total_amount_repair"].schedule_hint or ""
    assert "15:47" in hint and "15:57" in hint
