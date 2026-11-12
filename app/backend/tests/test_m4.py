"""M4 signals schema: tables, keys, JSON defaults, cascade and numeric round-trip."""
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qp.models.signals import (ApprovedIntake, DecisionOutcome, EodRun, ExecutionDigest, MacroSlice, RiskSnapshot,
                               RiskState, TargetDecision, TrendSignal)

TABLES = {
    "eod_run", "risk_snapshot", "target_decision", "trend_signal", "decision_outcome",
    "macro_slice", "risk_state", "approved_intake", "execution_digest",
}
RUN = dt.datetime(2026, 11, 12, 7, 30, tzinfo=dt.timezone.utc)


def test_signals_tables_exist(engine):
    names = set(inspect(engine).get_table_names(schema="signals"))
    assert TABLES <= names


def test_run_and_snapshot_roundtrip(engine):
    with Session(engine, expire_on_commit=False) as s:
        run = EodRun(decision_date=dt.date(2026, 11, 12), attempt_no=1, data_date=dt.date(2026, 11, 12),
                     status="success", decision_outcome="blocked", target_count=40, evaluated_count=40,
                     failed_count=0, signal_count=2, allowed_signal_count=0, blocked_signal_count=2,
                     reason_codes=[], details={"source": "qp"}, legacy_run_id="abc")
        s.add(run)
        s.flush()
        s.add(RiskSnapshot(run_id=run.run_id, decision_at=RUN, level="critical",
                           values={"vix": 19.46, "us_10y": 4.983, "crude_oil": 86.17},
                           sources={"vix": "yahoo_chart_regular_market"}, observed_at={}, available_at={},
                           missing_fields=[]))
        s.add(TargetDecision(run_id=run.run_id, decision_date=dt.date(2026, 11, 12), code="159869", name="游戏ETF",
                             sector="成长", data_status="ok", decision_state="blocked", risk_action="blocked",
                             risk_level="critical", reason_code="macro_risk_blocked",
                             reference_close=Decimal("1.2345"), raw_trigger_count=1,
                             raw_signals=[{"signal_type": "weekly_macd_golden_cross"}]))
        s.commit()
        run_id = run.run_id

    with Session(engine) as s:
        got = s.get(EodRun, run_id)
        assert got.reason_codes == [] and got.details == {"source": "qp"}
        snap = s.scalar(select(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
        assert snap.level == "critical" and snap.values["vix"] == 19.46
        d = s.scalar(select(TargetDecision).where(TargetDecision.run_id == run_id))
        assert d.reference_close == Decimal("1.2345")
        assert d.raw_signals[0]["signal_type"] == "weekly_macd_golden_cross"
        assert d.validation_split == "live_oos"


def test_duplicate_run_rejected(engine):
    with Session(engine) as s:
        s.add(EodRun(decision_date=dt.date(2026, 11, 13), attempt_no=1, status="success", target_count=40))
        s.commit()
    with Session(engine) as s:
        s.add(EodRun(decision_date=dt.date(2026, 11, 13), attempt_no=1, status="failed", target_count=40))
        with pytest.raises(IntegrityError):
            s.commit()


def test_decision_outcome_and_cascade(engine):
    with Session(engine) as s:
        run = EodRun(decision_date=dt.date(2026, 11, 16), attempt_no=1, status="success", target_count=40)
        s.add(run)
        s.flush()
        d = TargetDecision(run_id=run.run_id, decision_date=dt.date(2026, 11, 16), code="510300", name="沪深300ETF",
                           sector="宽基", data_status="ok", decision_state="allowed", risk_action="allowed",
                           risk_level="ok", reason_code="signal_allowed", reference_close=Decimal("4.0000"),
                           raw_trigger_count=1)
        s.add(d)
        s.flush()
        s.add(DecisionOutcome(decision_id=d.id, horizon_sessions=1, entry_date=dt.date(2026, 11, 16),
                              exit_date=dt.date(2026, 11, 17), entry_price=Decimal("4.0000"),
                              exit_price=Decimal("4.0400"), gross_return=Decimal("0.01000000"),
                              net_return=Decimal("0.00850000"), benchmark_return=Decimal("0.00200000"),
                              excess_return=Decimal("0.00650000"), fill_status="filled"))
        s.commit()
        run_id, decision_id = run.run_id, d.id

    with Session(engine) as s:
        o = s.scalar(select(DecisionOutcome).where(DecisionOutcome.decision_id == decision_id))
        assert o.net_return == Decimal("0.00850000")
        s.execute(delete(EodRun).where(EodRun.run_id == run_id))
        s.commit()
    with Session(engine) as s:
        assert s.get(TargetDecision, decision_id) is None
        assert s.scalar(select(DecisionOutcome).where(DecisionOutcome.decision_id == decision_id)) is None


def test_trend_signal_unique(engine):
    day = dt.date(2026, 11, 17)
    with Session(engine) as s:
        s.add(TrendSignal(decision_date=day, code="513050", name="中概互联网ETF",
                          signal_type="weekly_macd_golden_cross", period="W", data_date=dt.date(2026, 11, 13),
                          title="x", detail={"dif": 0.1, "dea": 0.05}, confidence=Decimal("0.6")))
        s.commit()
    with Session(engine) as s:
        s.add(TrendSignal(decision_date=day, code="513050", name="中概互联网ETF",
                          signal_type="weekly_macd_golden_cross", period="W", data_date=dt.date(2026, 11, 13),
                          title="y", detail={}, confidence=Decimal("0.6")))
        with pytest.raises(IntegrityError):
            s.commit()


def test_macro_and_approved_and_digest(engine):
    with Session(engine) as s:
        s.add(MacroSlice(decision_date=dt.date(2026, 11, 17), total_amount=Decimal("16762.79"),
                         margin_balance=Decimal("22870.50"), margin_growth_pct=Decimal("-0.054"),
                         limit_up=85, limit_down=0, limit_ratio=Decimal("85.000"), source="tencent+akshare",
                         collect_status={"total_amount": {"available": True}}))
        s.add(RiskState(observed_at=RUN, level="growth_blocked", values={"vix": 15.44},
                        sources={"vix": "yahoo_chart_regular_market"}, override=False, message="",
                        raw={"level": "growth_blocked"}))
        s.add(ApprovedIntake(idempotency_key="k1", source="dedirock", received_at=RUN, status="accepted",
                             detail="queued_stub", risk_check={"ok": True}, payload_sha256="0" * 64,
                             payload={"items": []}))
        s.add(ExecutionDigest(digest_date=dt.date(2026, 11, 17), item_count=0, accepted=0, rejected=0,
                              body={"items": []}, sha256="0" * 64, push_state="shadowed"))
        s.commit()
    with Session(engine) as s:
        m = s.get(MacroSlice, dt.date(2026, 11, 17))
        assert m.total_amount == Decimal("16762.79") and m.limit_ratio == Decimal("85.000")
        a = s.scalar(select(ApprovedIntake).where(ApprovedIntake.idempotency_key == "k1"))
        assert a.status == "accepted" and a.risk_check == {"ok": True}
        g = s.get(ExecutionDigest, dt.date(2026, 11, 17))
        assert g.push_state == "shadowed" and g.body == {"items": []}
