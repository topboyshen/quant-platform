"""M16 performance attribution: matured outcomes by eligibility, sector and signal type."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from qp.models.chain import ChainAttempt, ChainDecision, ChainEligibility, ChainOutcome
from qp.models.market import TradingDay
from qp.models.signals import EodRun, TargetDecision
from qp.signals import attribution, chain

DAY = dt.date(2027, 4, 1)  # Thursday, outside the calendar fixtures used by other tests
CST = dt.timezone(dt.timedelta(hours=8))


def _row(
    *,
    code: str = "510300",
    verdict: str = "eligible",
    reason_code: str = "eligible",
    sector: str = "宽基",
    origin: str = "native",
    signal_state: str = "triggered",
    risk_state: str = "allowed",
    result_state: str = "matured",
    execution_state: str = "filled",
    net_return: float | None = 0.01,
    excess_return: float | None = 0.005,
    benchmark_return: float | None = 0.002,
    signal_types: list[str] | None = None,
    signal_state_source: str = "chain",
    risk_state_source: str = "chain",
) -> dict:
    return {
        "code": code,
        "trade_date": DAY,
        "verdict": verdict,
        "reason_code": reason_code,
        "sector": sector,
        "origin": origin,
        "signal_state": signal_state,
        "risk_state": risk_state,
        "result_state": result_state,
        "execution_state": execution_state,
        "net_return": net_return,
        "excess_return": excess_return,
        "benchmark_return": benchmark_return,
        "signal_types": signal_types or ["unknown"],
        "signal_state_source": signal_state_source,
        "risk_state_source": risk_state_source,
    }


def _calendar(engine, start: dt.date, days: int = 40) -> None:
    with Session(engine) as session:
        for i in range(days):
            session.merge(TradingDay(origin="qp", date=start + dt.timedelta(days=i), is_open=True, source="test"))
        session.commit()


def _decision(
    code: str,
    state: str,
    *,
    trigger: int,
    risk: str = "allowed",
    sector: str = "测试",
    raw_signals: list[dict] | None = None,
) -> dict:
    return {
        "code": code,
        "name": code,
        "sector": sector,
        "data_status": "ok",
        "decision_state": state,
        "risk_action": risk,
        "risk_level": "ok",
        "reason_code": "r",
        "reference_close": 1.0,
        "raw_trigger_count": trigger,
        "raw_signals": raw_signals or [],
        "validation_split": "live_oos",
    }


def _seed(engine, monkeypatch, day: dt.date = DAY) -> dict:
    monkeypatch.setenv("QP_RECORD_CHAIN", "1")
    _calendar(engine, day)
    decisions = [
        _decision(
            "510300",
            "allowed",
            trigger=1,
            sector="宽基",
            raw_signals=[{"signal_type": "weekly_macd_golden_cross"}],
        ),
        _decision("510500", "no_trigger", trigger=0, sector="宽基"),
        _decision("159915", "blocked", trigger=1, risk="blocked", sector="成长"),
    ]
    with Session(engine) as session:
        run = EodRun(
            decision_date=day,
            attempt_no=1,
            status="success",
            decision_outcome="emitted",
            target_count=3,
            evaluated_count=3,
            failed_count=0,
            signal_count=1,
            allowed_signal_count=1,
            blocked_signal_count=1,
            reason_codes=[],
        )
        run.started_at = dt.datetime.combine(day, dt.time(15, 30), tzinfo=CST)
        run.finished_at = run.started_at + dt.timedelta(minutes=3)
        session.add(run)
        session.commit()
        run_id = run.run_id
        for row in decisions:
            session.add(
                TargetDecision(
                    run_id=run_id,
                    decision_date=day,
                    code=row["code"],
                    name=row["name"],
                    sector=row["sector"],
                    data_status=row["data_status"],
                    decision_state=row["decision_state"],
                    risk_action=row["risk_action"],
                    risk_level=row["risk_level"],
                    reason_code=row["reason_code"],
                    reference_close=row["reference_close"],
                    raw_trigger_count=row["raw_trigger_count"],
                    raw_signals=row["raw_signals"],
                    validation_split=row["validation_split"],
                )
            )
        session.commit()
        run = session.get(EodRun, run_id)
        out = chain.register_run(session, run=run, decisions=decisions, run_status="success", source="timer")
        assert out["enabled"] is True and out["outcomes"] == 5
        uid = chain.decision_uid_for(session, day, "510300")
        assert uid is not None
        expected = chain.expected_maturity(session, uid, 20)
        assert expected is not None
        assert chain.append_outcome_revision(
            session,
            decision_uid=uid,
            horizon=20,
            row={
                "fill_status": "filled",
                "entry_date": day + dt.timedelta(days=1),
                "exit_date": expected,
                "entry_price": 1.0,
                "exit_price": 1.05,
                "benchmark_return": 0.02,
                "gross_return": 0.05,
                "net_return": 0.04,
                "excess_return": 0.02,
                "fill_reason": "next_session_open",
            },
            expected=expected,
            as_of=expected,
        )
        session.commit()
        return {"run_id": run_id, "batch_key": out["batch_key"], "universe_version": out["universe_version"]}


def test_stats_include_only_matured_filled_rows():
    rows = [
        _row(net_return=0.10),
        _row(result_state="pending_maturity", execution_state="unverified", net_return=None),
        _row(result_state="invalid", execution_state="unfilled", net_return=None),
        _row(result_state="matured", execution_state="filled", net_return=-0.02, excess_return=-0.01),
    ]
    data = attribution.build_report(rows, horizon=20, group_by=["eligibility"], as_of=DAY)
    assert data["overall"]["n"] == 2
    assert data["overall"]["win_rate"] == pytest.approx(0.5)
    assert data["overall"]["avg_net_return"] == pytest.approx(0.04)
    assert data["sample"]["matured_filled"] == 2
    assert data["sample"]["eligible_matured"] == 2


def test_group_by_sector_and_signal_type_marks_overlap():
    rows = [
        _row(sector="宽基", signal_types=["weekly_macd_golden_cross"]),
        _row(sector="宽基", signal_types=["weekly_macd_golden_cross", "volume_breakout_ma60"]),
        _row(sector="成长", signal_types=["volume_breakout_ma60"]),
    ]
    data = attribution.build_report(rows, horizon=20, group_by=["sector", "signal_type"], as_of=DAY)
    sectors = {item["bucket"]: item for item in data["groups"]["sector"]}
    assert sectors["宽基"]["n"] == 2
    assert sectors["成长"]["n"] == 1
    signals = {item["bucket"]: item for item in data["groups"]["signal_type"]}
    assert signals["weekly_macd_golden_cross"]["n"] == 2
    assert signals["volume_breakout_ma60"]["n"] == 2
    assert data["overlap"]["signal_type"] is True


def test_sample_status_uses_eligible_count():
    sufficient = attribution.build_report([_row(net_return=0.01) for _ in range(30)], horizon=20, as_of=DAY)
    insufficient = attribution.build_report([_row(net_return=0.01) for _ in range(29)], horizon=20, as_of=DAY)
    baseline = attribution.build_report(
        [_row(verdict="excluded", reason_code="no_trigger", net_return=0.01) for _ in range(5)],
        horizon=20,
        as_of=DAY,
    )
    assert sufficient["overall"]["sample_status"] == "sufficient"
    assert insufficient["overall"]["sample_status"] == "insufficient"
    assert baseline["overall"]["sample_status"] == "baseline_only"


def test_unknown_signal_type_is_reported_not_inferred():
    data = attribution.build_report([_row(signal_types=["unknown"])], horizon=20, as_of=DAY)
    assert data["sample"]["signal_type_unknown"] == 1
    assert data["sample"]["signal_type_known"] == 0
    assert {warning["code"] for warning in data["warnings"]} >= {"signal_type_detail_missing"}


def test_legacy_state_fallback_is_counted_and_warned():
    data = attribution.build_report(
        [
            _row(
                origin="migrated_v1",
                signal_state_source="legacy_target_decision",
                risk_state_source="legacy_target_decision",
            )
        ],
        horizon=20,
        as_of=DAY,
    )
    assert data["sample"]["state_fallback_legacy"] == 1
    assert data["sample"]["signal_state_legacy"] == 1
    assert "state_fallback_legacy" in {warning["code"] for warning in data["warnings"]}


def test_invalid_horizon_and_group_by_are_rejected():
    with pytest.raises(ValueError):
        attribution.build_report([], horizon=2)
    with pytest.raises(ValueError):
        attribution.build_report([], horizon=20, group_by=["unknown_dimension"])


def test_markdown_renders_group_table():
    data = attribution.build_report([_row(sector="宽基")], horizon=20, group_by=["sector"], as_of=DAY)
    rendered = attribution.markdown(data)
    assert "信号链绩效归因（20 日）" in rendered
    assert "| 宽基 | 1 |" in rendered


def test_attribution_cli_is_registered():
    from qp.cli import main

    with pytest.raises(SystemExit) as out:
        main(["chain", "attribution", "--help"])
    assert out.value.code == 0


def test_report_uses_latest_revision_and_chain_joins(engine, monkeypatch):
    _seed(engine, monkeypatch)
    with Session(engine) as session:
        data = attribution.report(session, horizon=20, start=DAY, end=DAY)
    sample = data["sample"]
    assert (sample["decisions"], sample["triggered"], sample["matured_filled"]) == (3, 2, 1)
    assert (sample["eligible_matured"], sample["native_matured"], sample["legacy_matured"]) == (1, 1, 0)
    assert (sample["signal_type_known"], sample["signal_type_unknown"]) == (1, 0)
    assert sample["state_fallback_legacy"] == 0
    assert data["overall"]["n"] == 1
    assert data["overall"]["avg_net_return"] == pytest.approx(0.04)
    assert data["eligible"]["avg_net_return"] == pytest.approx(0.04)
    sectors = {item["bucket"]: item for item in data["groups"]["sector"]}
    assert sectors["宽基"]["n"] == 1
    assert data["groups"]["signal_type"][0]["bucket"] == "weekly_macd_golden_cross"
    assert data["sample"]["eligible_matured"] < data["threshold"]

    with Session(engine) as session:
        uid = chain.decision_uid_for(session, DAY, "510300")
        expected = chain.expected_maturity(session, uid, 20)
        assert chain.append_outcome_revision(
            session,
            decision_uid=uid,
            horizon=20,
            row={
                "fill_status": "filled",
                "entry_date": DAY + dt.timedelta(days=1),
                "exit_date": expected,
                "entry_price": 1.0,
                "exit_price": 1.09,
                "benchmark_return": 0.02,
                "gross_return": 0.09,
                "net_return": 0.09,
                "excess_return": 0.07,
                "fill_reason": "next_session_open",
            },
            expected=expected,
            as_of=expected,
        )
        session.commit()
    with Session(engine) as session:
        updated = attribution.report(session, horizon=20, start=DAY, end=DAY)
    assert updated["overall"]["n"] == 1
    assert updated["overall"]["avg_net_return"] == pytest.approx(0.09)


def _add_legacy(engine, seed: dict, day: dt.date) -> None:
    now = dt.datetime.combine(day, dt.time(16, 0), tzinfo=CST)
    uid = "qd_" + "a" * 32
    attempt_id = "att_" + "b" * 32
    with Session(engine) as session:
        session.add(
            ChainDecision(
                decision_uid=uid,
                trade_date=day,
                code="512000",
                name="券商ETF",
                batch_key=seed["batch_key"],
                rule_version=chain.RULE_VERSION,
                universe_version=seed["universe_version"],
                origin="migrated_v1",
                attribution_status="confirmed",
                input_evidence_ref="test",
                input_evidence_sha256="0" * 64,
                imported_at=now,
            )
        )
        session.flush()
        session.add(
            ChainAttempt(
                attempt_id=attempt_id,
                decision_uid=uid,
                run_id=seed["run_id"],
                started_at=now,
                completed_at=now,
                engine_version=chain.ENGINE_VERSION,
            )
        )
        session.flush()
        session.add(
            ChainEligibility(
                eligibility_id="el_" + "c" * 32,
                decision_uid=uid,
                policy_version=chain.ELIGIBILITY_POLICY,
                verdict="excluded",
                reason_code="run_incomplete",
                reason_detail={"run_id": seed["run_id"]},
                evidence_refs=[],
                rule_version=chain.RULE_VERSION,
                universe_version=seed["universe_version"],
                evaluated_at=now,
                evaluation_seq=1,
            )
        )
        session.add(
            ChainOutcome(
                outcome_uid="oc_" + "d" * 32,
                decision_uid=uid,
                source_attempt_id=None,
                horizon_sessions=20,
                revision=1,
                execution_state="filled",
                result_state="matured",
                net_return=0.03,
                excess_return=0.01,
                benchmark_return=0.02,
                source_kind="migrated_v1",
            )
        )
        session.add(
            TargetDecision(
                run_id=seed["run_id"],
                decision_date=day,
                code="512000",
                name="券商ETF",
                sector="行业",
                data_status="ok",
                decision_state="allowed",
                risk_action="allowed",
                risk_level="ok",
                reason_code="signal_allowed",
                reference_close=1.0,
                raw_trigger_count=1,
                raw_signals=[{"signal_type": "volume_breakout_ma60"}],
                validation_split="live_oos",
            )
        )
        session.commit()


def test_legacy_rows_fall_back_to_target_decision_state(engine, monkeypatch):
    seed = _seed(engine, monkeypatch, DAY + dt.timedelta(days=2))
    day = DAY + dt.timedelta(days=2)
    _add_legacy(engine, seed, day)
    with Session(engine) as session:
        data = attribution.report(session, horizon=20, start=day, end=day)
    assert data["sample"]["matured_filled"] == 2
    assert data["sample"]["state_fallback_legacy"] == 1
    assert data["sample"]["signal_state_legacy"] == 1
    assert {item["bucket"] for item in data["groups"]["signal_type"]} == {
        "weekly_macd_golden_cross",
        "volume_breakout_ma60",
    }
    assert "state_fallback_legacy" in {warning["code"] for warning in data["warnings"]}


def test_attribution_api_is_read_only_view(engine, client, monkeypatch):
    api_day = DAY + dt.timedelta(days=1)
    _seed(engine, monkeypatch, api_day)
    import uuid

    from qp.auth.sessions import create_service_token

    with Session(engine) as session:
        token = create_service_token(session, f"m16-attribution-{uuid.uuid4().hex[:8]}", ["signals:read"])
        session.commit()
    response = client.get(
        "/api/signals/chain/attribution",
        params={"horizon": 20, "start": api_day.isoformat(), "end": api_day.isoformat()},
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["horizon"] == 20
    assert body["sample"]["matured_filled"] == 1
    assert body["groups"]["eligibility"][0]["bucket"] == "eligible"

    bad = client.get(
        "/api/signals/chain/attribution?group_by=not_a_dimension",
        headers={"authorization": f"Bearer {token}"},
    )
    assert bad.status_code == 400
