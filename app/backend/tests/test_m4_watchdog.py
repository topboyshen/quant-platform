"""M4.4 watchdog: terminal inspection, retry deferral, shadow failure/daily outbox."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.signals import (EodRun, MacroSlice, RiskSnapshot, TargetDecision, TrendSignal)
from qp.signals.eod import CST
from qp.signals.watchdog import build_daily_report, run_watchdog, summarize_attempts

DAY = dt.date(2027, 3, 8)  # Monday, beyond the test calendar fixtures
STATE_CODES = {"allowed": "510300", "blocked": "512890", "no_trigger": "159915",
               "data_failed": "518880"}


def at(hour: int, minute: int, *, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=CST)


def cleanup(engine, day: dt.date = DAY) -> None:
    with Session(engine) as session:
        session.execute(delete(EodRun).where(EodRun.decision_date == day))
        session.execute(delete(MacroSlice).where(MacroSlice.decision_date == day))
        session.execute(delete(TrendSignal).where(TrendSignal.decision_date == day))
        session.execute(delete(Outbox).where(Outbox.dedup_key.like(f"watchdog:{day.isoformat()}:%")))
        session.commit()


def seed_run(engine, *, day: dt.date = DAY, status: str = "success",
             outcome: str = "no_signal", reasons: list[str] | None = None,
             attempt_no: int = 1, started_at: dt.datetime | None = None,
             target_count: int = 0, decision_states: tuple[str, ...] = (),
             with_snapshot: bool = False, signal_count: int = 0,
             allowed_signal_count: int = 0, blocked_signal_count: int = 0) -> int:
    with Session(engine) as session:
        run = EodRun(
            decision_date=day, attempt_no=attempt_no, status=status,
            decision_outcome=outcome,
            target_count=target_count, evaluated_count=target_count, failed_count=0,
            signal_count=signal_count, allowed_signal_count=allowed_signal_count,
            blocked_signal_count=blocked_signal_count,
            reason_codes=reasons or [],
            started_at=started_at or at(15, 30, day=day),
            finished_at=None if status == "running" else at(15, 31, day=day))
        session.add(run)
        session.flush()
        for state in decision_states:
            session.add(TargetDecision(
                run_id=run.run_id, decision_date=day, code=STATE_CODES[state],
                name=state, sector="宽基",
                data_status="failed" if state == "data_failed" else "ok",
                decision_state=state,
                risk_action="allowed" if state == "allowed" else "not_applicable",
                risk_level="ok",
                reason_code=("signal_allowed" if state == "allowed" else
                             "macro_risk_blocked" if state == "blocked" else "signal_not_triggered"),
                reference_close=10.0 if state != "data_failed" else None,
                raw_trigger_count=1 if state in ("allowed", "blocked") else 0,
                raw_signals=[], validation_split="live_oos"))
        if with_snapshot:
            session.add(RiskSnapshot(
                run_id=run.run_id, decision_at=started_at or at(15, 30, day=day),
                level="ok", values={}, sources={}, observed_at={}, available_at={},
                missing_fields=[]))
        session.commit()
        return run.run_id


def outbox_rows(engine, day: dt.date = DAY) -> list[Outbox]:
    with Session(engine) as session:
        return list(session.execute(
            select(Outbox).where(Outbox.dedup_key.like(f"watchdog:{day.isoformat()}:%"))
            .order_by(Outbox.id)).scalars())


def test_missing_run_pages_at_1540_and_deduplicates(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            first = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert (first["ok"], first["reason"], first["notification_plan"]) == (
            False, "missing", "failure")
        assert first["shadow_outbox"]["enqueued"] is True
        assert first["shadow_outbox"]["namespace"] == f"watchdog:{DAY.isoformat()}:failure"
        with Session(engine) as session:
            second = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert second["shadow_outbox"]["enqueued"] is False
        rows = outbox_rows(engine)
        assert len(rows) == 1
        assert rows[0].mode == "shadow" and rows[0].status == "pending"
        body = json.loads(rows[0].body)
        assert body["report_type"] == "eod_watchdog_failure"
        assert body["terminal_reason"] == "missing"
        assert body["attempts"]["attempts"] == 0
        with Session(engine) as session:
            attempts = session.scalar(
                select(func.count()).select_from(DeliveryAttempt)
                .where(DeliveryAttempt.outbox_id == rows[0].id))
            assert attempts == 0
    finally:
        cleanup(engine)


def test_running_is_deferred_before_1548_and_reported_at_1550(engine):
    cleanup(engine)
    try:
        seed_run(engine, status="running", outcome=None, started_at=at(15, 35))
        with Session(engine) as session:
            short = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert (short["ok"], short["reason"], short["deferred"]) == (True, "awaiting_same_day_retry", True)
        assert short["notification_plan"] == "none" and not outbox_rows(engine)
        cleanup(engine)
        seed_run(engine, status="running", outcome=None, started_at=at(15, 30))
        with Session(engine) as session:
            timeout = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert (timeout["ok"], timeout["deferred"]) == (True, True)
        assert timeout["reason"] == "awaiting_same_day_retry"
        assert not outbox_rows(engine)
        with Session(engine) as session:
            daily = run_watchdog(session, now=at(15, 50))
            session.commit()
        assert daily["notification_plan"] == "daily"
        rows = outbox_rows(engine)
        assert len(rows) == 1
        body = json.loads(rows[0].body)
        assert body["report_type"] == "eod_daily_report"
        assert body["terminal_ok"] is False
        assert body["terminal_reason"] == "running_timeout"
        assert "不支持推荐" in body["conclusion"]
    finally:
        cleanup(engine)


def test_expected_data_gap_is_deferred_but_unexpected_failure_pages(engine):
    cleanup(engine)
    try:
        seed_run(engine, status="failed", outcome="data_unavailable",
                 reasons=["trend_data_unavailable"])
        with Session(engine) as session:
            expected = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert expected["deferred"] is True and expected["notification_plan"] == "none"
        assert not outbox_rows(engine)
        cleanup(engine)
        seed_run(engine, status="failed", outcome="data_unavailable",
                 reasons=["trend_data_partial"])
        with Session(engine) as session:
            unexpected = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert unexpected["deferred"] is False
        assert unexpected["notification_plan"] == "failure"
        assert len(outbox_rows(engine)) == 1
        cleanup(engine)
        seed_run(engine, status="failed", outcome="data_unavailable",
                 reasons=["trend_data_unavailable"])
        with Session(engine) as session:
            daily = run_watchdog(session, now=at(15, 50))
            session.commit()
        assert daily["notification_plan"] == "daily"
        assert json.loads(outbox_rows(engine)[0].body)["report_type"] == "eod_daily_report"
    finally:
        cleanup(engine)


def test_combined_mode_defers_daily_to_1740(engine, monkeypatch):
    cleanup(engine)
    monkeypatch.setenv("QP_SIGNALS_COMBINED_REPORT", "1")
    try:
        seed_run(engine, target_count=1, decision_states=("no_trigger",), with_snapshot=True)
        with Session(engine) as session:
            out = run_watchdog(session, now=at(15, 50))
            session.commit()
        assert out["notification_plan"] == "combined_at_1740"
        assert out["combined_deferred"] is True
        assert not outbox_rows(engine)
    finally:
        cleanup(engine)


def test_success_evidence_and_incomplete_evidence(engine):
    cleanup(engine)
    try:
        seed_run(engine, target_count=1, decision_states=("no_trigger",), with_snapshot=True)
        with Session(engine) as session:
            ok = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert (ok["ok"], ok["reason"], ok["notification_plan"]) == (True, "success", "none")
        assert not outbox_rows(engine)
        cleanup(engine)
        seed_run(engine, target_count=2, decision_states=("no_trigger",), with_snapshot=True)
        with Session(engine) as session:
            incomplete = run_watchdog(session, now=at(15, 40))
            session.commit()
        assert (incomplete["ok"], incomplete["reason"]) == (False, "evidence_incomplete")
        assert incomplete["notification_plan"] == "failure"
        body = json.loads(outbox_rows(engine)[0].body)
        assert body["terminal_reason"] == "evidence_incomplete"
    finally:
        cleanup(engine)


def test_two_attempts_report_recovery(engine):
    cleanup(engine)
    try:
        seed_run(engine, status="failed", outcome="data_unavailable",
                 reasons=["trend_data_unavailable"], attempt_no=1)
        seed_run(engine, status="success", outcome="emitted", attempt_no=2,
                 target_count=1, decision_states=("allowed",), with_snapshot=True,
                 signal_count=1, allowed_signal_count=1)
        with Session(engine) as session:
            result = run_watchdog(session, now=at(15, 50))
            session.commit()
        assert result["ok"] is True and result["attempts"]["attempts"] == 2
        assert result["attempts"]["recovered_after_data_gap"] is True
        assert result["attempts"]["first"]["status"] == "failed"
        assert result["attempts"]["latest"]["status"] == "success"
        body = json.loads(outbox_rows(engine)[0].body)
        assert body["attempts"]["recovered_after_data_gap"] is True
    finally:
        cleanup(engine)


def test_daily_report_structured_fields(engine):
    cleanup(engine)
    try:
        seed_run(engine, target_count=3, decision_states=("allowed", "blocked", "no_trigger"),
                 with_snapshot=True, signal_count=2, allowed_signal_count=1,
                 blocked_signal_count=1)
        with Session(engine) as session:
            session.add(MacroSlice(
                decision_date=DAY, total_amount=None, margin_balance=22800.0,
                margin_growth_pct=0.5, limit_up=60, limit_down=2, limit_ratio=20.0,
                source="test", collect_status={
                    "total_amount": {"available": False, "source": "tencent/sina/eastmoney",
                                     "message": "missing", "observed_date": None,
                                     "stale": True, "anomalous": True},
                    "margin_balance": {"available": True, "source": "sse+szse",
                                       "message": "", "observed_date": DAY.isoformat(),
                                       "stale": False, "anomalous": False},
                }))
            session.add(TrendSignal(
                decision_date=DAY, code="510300", name="沪深300ETF",
                signal_type="weekly_macd_golden_cross", period="W", data_date=DAY,
                title="t", detail={}, confidence=0.6))
            session.commit()
        with Session(engine) as session:
            result = run_watchdog(session, now=at(15, 50))
            session.commit()
        body = json.loads(outbox_rows(engine)[0].body)
        assert body["schema_version"] == 2
        assert body["report_type"] == "eod_daily_report"
        assert body["terminal_ok"] is True and body["terminal_reason"] == "success"
        assert body["attempts"]["first"]["status"] == "success"
        counts = body["counts"]
        assert (counts["targets"], counts["allowed"], counts["blocked"], counts["no_trigger"]) == (
            3, 1, 1, 1)
        assert (counts["signals"], counts["allowed_signals"], counts["blocked_signals"]) == (2, 1, 1)
        assert body["today_trend_signals"] == 1
        assert body["macro"]["date"] == DAY.isoformat()
        assert body["macro"]["total_amount"] is None
        assert any(item.startswith("total_amount") for item in body["reading"]["macro_missing"])
        assert body["research"]["qualified"]["label"] == "样本不足"
        assert body["research"]["qualified"]["sample_size"] == 0
        assert body["execution_digest"]["status"] == "stub"
        assert "510300" in body["fee_basis"]
        assert "不支持推荐" in body["conclusion"]
        assert result["daily_report"]["conclusion"] == body["conclusion"]
    finally:
        cleanup(engine)


def test_attempt_summary_and_daily_builder_are_exposed(engine):
    cleanup(engine)
    try:
        seed_run(engine, status="success", target_count=0, decision_states=(), with_snapshot=False)
        with Session(engine) as session:
            summary = summarize_attempts(list(session.execute(
                select(EodRun).where(EodRun.decision_date == DAY)).scalars()))
            assert summary["attempts"] == 1
            assert summary["first"] == summary["latest"]
            payload = build_daily_report(
                session, result={"ok": False, "reason": "missing", "message": "m",
                                 "run": None, "attempts": summary}, now=at(15, 50))
            assert payload["terminal_ok"] is False
            assert "不支持推荐" in payload["conclusion"]
    finally:
        cleanup(engine)


def test_watchdog_job_is_registered():
    assert "signals.watchdog" in JOBS
    hint = JOBS["signals.watchdog"].schedule_hint or ""
    assert "15:40" in hint and "15:50" in hint


def test_retry_gate_matches_legacy_exec_condition():
    from qp.signals.watchdog import retryable_data_failure

    gap = {"decision_date": DAY.isoformat(), "decision_outcome": "data_unavailable"}
    assert retryable_data_failure({**gap, "status": "failed"}, decision_date=DAY.isoformat())
    assert retryable_data_failure(
        {**gap, "status": "partial", "signal_count": 0, "allowed_signal_count": 0,
         "blocked_signal_count": 0}, decision_date=DAY.isoformat())
    # Never a first run, never a success, never a same-day partial that already emitted.
    assert not retryable_data_failure(
        {**gap, "status": "success", "signal_count": 0, "allowed_signal_count": 0,
         "blocked_signal_count": 0}, decision_date=DAY.isoformat())
    assert not retryable_data_failure(
        {**gap, "status": "partial", "signal_count": 1, "allowed_signal_count": 0,
         "blocked_signal_count": 0}, decision_date=DAY.isoformat())
    assert not retryable_data_failure(
        {"decision_date": DAY.isoformat(), "decision_outcome": "emitted",
         "status": "failed"}, decision_date=DAY.isoformat())
    assert not retryable_data_failure(
        {**gap, "status": "partial", "signal_count": 0, "allowed_signal_count": 0,
         "blocked_signal_count": 0}, decision_date="2027-03-09")


def test_latest_retryable_run_uses_newest_attempt(engine):
    from qp.signals.watchdog import latest_retryable_run

    cleanup(engine)
    with Session(engine) as session:
        assert latest_retryable_run(session, DAY) == (None, False)

    seed_run(engine, status="failed", outcome="data_unavailable", attempt_no=1)
    seed_run(engine, status="success", outcome="emitted", attempt_no=2, signal_count=1,
             allowed_signal_count=1)
    with Session(engine) as session:
        latest, retryable = latest_retryable_run(session, DAY)
    assert latest is not None and latest.attempt_no == 2 and latest.status == "success"
    assert retryable is False

    cleanup(engine)
    seed_run(engine, status="success", outcome="emitted", attempt_no=1, signal_count=1,
             allowed_signal_count=1)
    seed_run(engine, status="failed", outcome="data_unavailable", attempt_no=2)
    with Session(engine) as session:
        latest, retryable = latest_retryable_run(session, DAY)
    assert latest is not None and latest.attempt_no == 2
    assert retryable is True


def test_eod_retry_job_never_starts_a_first_run(engine, monkeypatch):
    from qp.jobs.registry import JobContext
    from qp.jobs.signals import eod_retry_job

    cleanup(engine)
    calls: list[dict] = []
    monkeypatch.setattr("qp.jobs.signals.now_cst", lambda: at(15, 48))
    monkeypatch.setattr("qp.jobs.signals.run_eod",
                        lambda session, **kwargs: calls.append(kwargs) or {"status": "success"})
    ctx = JobContext(engine=engine, run_id=0, name="signals.eod_retry")

    out = eod_retry_job(ctx)
    assert out["retried"] is False and out["reason"] == "not_retryable"
    assert calls == []

    seed_run(engine, status="success", outcome="emitted", signal_count=1,
             allowed_signal_count=1)
    out = eod_retry_job(ctx)
    assert out["retried"] is False and out["reason"] == "not_retryable"
    assert out["latest_status"] == "success" and calls == []


def test_eod_retry_job_retries_a_zero_signal_data_gap(engine, monkeypatch):
    from qp.jobs.registry import JobContext
    from qp.jobs.signals import eod_retry_job

    cleanup(engine)
    seed_run(engine, status="failed", outcome="data_unavailable", attempt_no=1)
    calls: list[dict] = []

    def fake_run_eod(session, **kwargs):
        calls.append(kwargs)
        return {"status": "success", "decision_outcome": "emitted", "run_id": "retry-run"}

    monkeypatch.setattr("qp.jobs.signals.now_cst", lambda: at(15, 48))
    monkeypatch.setattr("qp.jobs.signals.run_eod", fake_run_eod)
    out = eod_retry_job(JobContext(engine=engine, run_id=0, name="signals.eod_retry"))
    assert out["retried"] is True and out["ok"] is True
    assert calls and calls[0]["decision_date"] == DAY


def test_eod_retry_job_is_registered():
    assert "signals.eod_retry" in JOBS
    hint = JOBS["signals.eod_retry"].schedule_hint or ""
    assert "15:48" in hint
