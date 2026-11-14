"""M4 live acceptance: invariant checks that replace the legacy shadow compare."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.models.market import IndexClose, TradingDay
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.signals import DecisionOutcome, EodRun, RiskSnapshot, TargetDecision, TrendSignal
from qp.signals.acceptance import evaluate, latest_decision_date
from qp.signals.universe import TARGETS

DAY = dt.date(2027, 3, 8)  # Monday, beyond the fixture calendar
OLD_DAYS = [dt.date(2027, 2, 8) + dt.timedelta(days=i) for i in range(6)]


def cleanup(engine, days=(DAY, *OLD_DAYS)) -> None:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date.in_(days)))
        session.execute(delete(TrendSignal).where(TrendSignal.decision_date.in_(days)))
        session.execute(delete(TradingDay).where(TradingDay.origin == "qp",
                                                  TradingDay.date.in_(days)))
        session.execute(delete(IndexClose).where(IndexClose.origin == "qp",
                                                 IndexClose.trade_date.in_(days)))
        session.execute(delete(Outbox).where(Outbox.dedup_key.like(f"watchdog:{DAY.isoformat()}:%")))
        session.commit()


def seed_calendar(engine, days) -> None:
    with Session(engine) as session:
        for day in days:
            session.merge(TradingDay(origin="qp", date=day, is_open=True, source="test"))
        session.commit()


def seed_index(engine, day: dt.date = DAY, symbols: int = 4) -> None:
    with Session(engine) as session:
        for i, symbol in enumerate(["sh000001", "sz399001", "sz399006", "sh000300"][:symbols]):
            session.merge(IndexClose(origin="qp", symbol=symbol, trade_date=day,
                                     close=100 + i, source="test"))
        session.commit()


def seed_day(engine, *, day: dt.date = DAY, states: tuple[str, ...] = ("allowed", "blocked"),
             signal_count: int = 2, allowed_signal_count: int = 1,
             blocked_signal_count: int = 1, failed_count: int = 0,
             status: str = "success", attempt_no: int = 1,
             risk_level: str | None = "ok") -> int:
    with Session(engine) as session:
        run = EodRun(decision_date=day, attempt_no=attempt_no, status=status,
                     decision_outcome="emitted", target_count=len(TARGETS),
                     evaluated_count=len(TARGETS) - failed_count, failed_count=failed_count,
                     signal_count=signal_count, allowed_signal_count=allowed_signal_count,
                     blocked_signal_count=blocked_signal_count, reason_codes=[],
                     started_at=dt.datetime(day.year, day.month, day.day, 15, 30),
                     finished_at=dt.datetime(day.year, day.month, day.day, 15, 31))
        session.add(run)
        session.flush()
        state_by_code = {TARGETS[i].code: s for i, s in enumerate(states)}
        for target in TARGETS:
            state = state_by_code.get(target.code, "no_trigger")
            data_failed = state == "data_failed"
            session.add(TargetDecision(
                run_id=run.run_id, decision_date=day, code=target.code,
                name=target.name, sector=target.sector,
                data_status="failed" if data_failed else "ok",
                decision_state=state,
                risk_action="allowed" if state == "allowed" else
                            "blocked" if state == "blocked" else "not_applicable",
                risk_level="ok", reason_code="test",
                reference_close=None if data_failed else 10.0,
                raw_trigger_count=1 if state in ("allowed", "blocked") else 0,
                raw_signals=[], validation_split="live_oos"))
        for state in states:
            if state in ("allowed", "blocked"):
                code = next(t.code for t in TARGETS if state_by_code.get(t.code) == state)
                session.add(TrendSignal(decision_date=day, code=code, name="x",
                                        signal_type="weekly_breakout", period="W",
                                        data_date=day, title="t", detail={}, confidence=0.9))
        if risk_level is not None:
            session.add(RiskSnapshot(run_id=run.run_id,
                                     decision_at=dt.datetime(day.year, day.month, day.day, 15, 30),
                                     level=risk_level, values={}, sources={}, observed_at={},
                                     available_at={}, missing_fields=[]))
        session.commit()
        return run.run_id


def seed_notification(engine, *, mode: str = "live", status: str = "sent",
                      ok_attempt: bool = True) -> int:
    with Session(engine) as session:
        outbox = Outbox(channel="telegram", recipient="signals",
                        dedup_key=f"watchdog:{DAY.isoformat()}:daily", subject="daily",
                        body=json.dumps({"ok": True}), mode=mode, status=status)
        session.add(outbox)
        session.flush()
        session.add(DeliveryAttempt(outbox_id=outbox.id, mode=mode, ok=ok_attempt, detail={}))
        session.commit()
        return outbox.id


def run_acceptance(engine, day: dt.date = DAY, today: dt.date = DAY) -> dict:
    with Session(engine) as session:
        return evaluate(session, day, today=today)


def test_full_day_passes(engine, monkeypatch):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_index(engine)
        seed_day(engine)
        seed_notification(engine)
        out = run_acceptance(engine)
        assert out["verdict"] == "PASS", out
        assert out["failed"] == [] and out["warned"] == []
        assert [c["id"] for c in out["checks"]] == [
            "A1_run", "A2_universe", "A3_counts", "A4_data", "A5_risk",
            "A6_signals", "A7_outcomes", "A8_notify", "A9_duplicates", "A10_inputs"]
    finally:
        cleanup(engine)


def test_missing_run_fails(engine):
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_index(engine)
        out = run_acceptance(engine)
        assert out["verdict"] == "FAIL"
        assert {"A1_run", "A2_universe", "A3_counts"} <= set(out["failed"])
    finally:
        cleanup(engine)


def test_tampered_counts_fail(engine, monkeypatch):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_index(engine)
        seed_day(engine, signal_count=5)  # != allowed(1) + blocked(1)
        seed_notification(engine)
        out = run_acceptance(engine)
        assert out["verdict"] == "FAIL"
        assert out["failed"] == ["A3_counts"], out
    finally:
        cleanup(engine)


def test_partial_data_failure_warns(engine, monkeypatch):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_index(engine)
        seed_day(engine, states=("allowed", "data_failed"), signal_count=1,
                 allowed_signal_count=1, blocked_signal_count=0, failed_count=1,
                 status="partial")
        seed_notification(engine)
        out = run_acceptance(engine)
        assert out["verdict"] == "WARN", out
        assert out["failed"] == []
        assert "A4_data" in out["warned"]
    finally:
        cleanup(engine)


def test_matured_outcomes_backlog_fails(engine):
    cleanup(engine)
    try:
        # six open sessions: an allowed decision on the first one is due all horizons up to 5
        seed_calendar(engine, OLD_DAYS)
        seed_index(engine)
        seed_day(engine, day=DAY)
        with Session(engine) as session:
            old_run = EodRun(decision_date=OLD_DAYS[0], attempt_no=1, status="success",
                             decision_outcome="emitted", target_count=len(TARGETS),
                             evaluated_count=len(TARGETS), failed_count=0, signal_count=1,
                             allowed_signal_count=1, blocked_signal_count=0, reason_codes=[],
                             started_at=dt.datetime(2027, 2, 8, 15, 30),
                             finished_at=dt.datetime(2027, 2, 8, 15, 31))
            session.add(old_run)
            session.flush()
            session.add(TargetDecision(
                run_id=old_run.run_id, decision_date=OLD_DAYS[0], code=TARGETS[0].code,
                name=TARGETS[0].name, sector=TARGETS[0].sector, data_status="ok",
                decision_state="allowed", risk_action="allowed", risk_level="ok",
                reason_code="test", reference_close=10.0, raw_trigger_count=1,
                raw_signals=[], validation_split="live_oos"))
            session.commit()
        out = run_acceptance(engine, today=OLD_DAYS[-1])
        assert out["verdict"] == "FAIL"
        assert "A7_outcomes" in out["failed"]
        check = next(c for c in out["checks"] if c["id"] == "A7_outcomes")
        assert check["metrics"]["expected"] == 3  # horizons 1, 3, 5
        assert check["metrics"]["actual"] == 0
    finally:
        cleanup(engine)


def test_outcomes_materialised_pass(engine):
    cleanup(engine)
    try:
        seed_calendar(engine, OLD_DAYS)
        seed_index(engine)
        seed_day(engine, day=DAY)
        with Session(engine) as session:
            old_run = EodRun(decision_date=OLD_DAYS[0], attempt_no=1, status="success",
                             decision_outcome="emitted", target_count=len(TARGETS),
                             evaluated_count=len(TARGETS), failed_count=0, signal_count=1,
                             allowed_signal_count=1, blocked_signal_count=0, reason_codes=[],
                             started_at=dt.datetime(2027, 2, 8, 15, 30),
                             finished_at=dt.datetime(2027, 2, 8, 15, 31))
            session.add(old_run)
            session.flush()
            decision = TargetDecision(
                run_id=old_run.run_id, decision_date=OLD_DAYS[0], code=TARGETS[0].code,
                name=TARGETS[0].name, sector=TARGETS[0].sector, data_status="ok",
                decision_state="allowed", risk_action="allowed", risk_level="ok",
                reason_code="test", reference_close=10.0, raw_trigger_count=1,
                raw_signals=[], validation_split="live_oos")
            session.add(decision)
            session.flush()
            for horizon in (1, 3, 5):
                session.add(DecisionOutcome(decision_id=decision.id, horizon_sessions=horizon,
                                            fill_status="filled"))
            session.commit()
        out = run_acceptance(engine, today=OLD_DAYS[-1])
        check = next(c for c in out["checks"] if c["id"] == "A7_outcomes")
        assert (check["status"], check["metrics"]["expected"], check["metrics"]["actual"]) == (
            "PASS", 3, 3)
        assert "A7_outcomes" not in out["failed"]
    finally:
        cleanup(engine)


def test_shadow_notification_warns_not_fails(engine, monkeypatch):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_index(engine)
        seed_day(engine)
        seed_notification(engine, mode="shadow", status="shadowed")
        out = run_acceptance(engine)
        assert out["verdict"] == "WARN"
        assert "A8_notify" in out["warned"]
    finally:
        cleanup(engine)


def test_latest_decision_date(engine):
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_day(engine)
        with Session(engine) as session:
            assert latest_decision_date(session, DAY + dt.timedelta(days=3)) == DAY
            # the shared test database may hold runs from other tests, never from this day
            before = latest_decision_date(session, DAY - dt.timedelta(days=1))
            assert before is None or before < DAY
    finally:
        cleanup(engine)


def test_cli_acceptance_exit_codes(engine, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    from qp.cli import main
    cleanup(engine)
    try:
        seed_calendar(engine, [DAY])
        seed_index(engine)
        seed_day(engine)
        seed_notification(engine)
        out_file = tmp_path / "acceptance.json"
        code = main(["signals", "acceptance", "--date", DAY.isoformat(),
                     "--today", DAY.isoformat(), "--out", str(out_file)])
        assert code == 0
        assert json.loads(out_file.read_text())["verdict"] == "PASS"
        capsys.readouterr()  # drop the first report

        with Session(engine) as session:
            session.execute(delete(EodRun).where(EodRun.decision_date == DAY))
            session.commit()
        code = main(["signals", "acceptance", "--date", DAY.isoformat(),
                     "--today", DAY.isoformat()])
        assert code == 1
        assert json.loads(capsys.readouterr().out)["verdict"] == "FAIL"
    finally:
        cleanup(engine)


def test_acceptance_job_registered():
    from qp.jobs.registry import JOBS
    assert JOBS["signals.acceptance"].schedule_hint.startswith("Mon-Fri 16:50 CST")
