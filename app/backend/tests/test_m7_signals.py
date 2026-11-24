"""M7.3 signals read APIs and shadow compare file reader."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.signals import DecisionOutcome, EodRun, TargetDecision, TrendSignal

DAY = dt.date(2026, 11, 12)
NOW = dt.datetime(2026, 11, 12, 15, 30)


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (DecisionOutcome, TrendSignal, TargetDecision, EodRun):
            session.execute(delete(model))
        session.commit()


def seed(engine) -> None:
    with Session(engine) as session:
        run = EodRun(
            run_id=11,
            decision_date=DAY,
            attempt_no=1,
            status="success",
            decision_outcome="emitted",
            target_count=40,
            evaluated_count=40,
            failed_count=0,
            signal_count=2,
            allowed_signal_count=1,
            blocked_signal_count=1,
            started_at=NOW,
        )
        session.add(run)
        decision = TargetDecision(
            run_id=11,
            decision_date=DAY,
            code="510300",
            name="沪深300ETF",
            sector="宽基",
            data_status="ok",
            decision_state="allowed",
            risk_action="allowed",
            risk_level="growth_ok",
            reason_code="trigger",
            reference_close=4.12,
            raw_trigger_count=1,
        )
        session.add(decision)
        session.flush()
        session.add(
            TrendSignal(
                decision_date=DAY,
                code="510300",
                name="沪深300ETF",
                signal_type="volume_breakout_ma60",
                period="D",
                data_date=DAY,
                title="放量突破",
                detail={"close": 4.12},
                confidence=0.55,
            )
        )
        session.add(
            DecisionOutcome(
                decision_id=decision.id,
                horizon_sessions=5,
                entry_date=DAY,
                exit_date=DAY,
                net_return=0.01,
                benchmark_return=0.005,
                excess_return=0.005,
                fill_status="filled",
            )
        )
        session.commit()


def _token(engine) -> str:
    import uuid

    from qp.auth.sessions import create_service_token

    with Session(engine) as s:
        tok = create_service_token(s, f"m7-signals-{uuid.uuid4().hex[:8]}", ["signals:read"])
        s.commit()
    return tok


def test_signal_endpoints(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        h = {"authorization": f"Bearer {_token(engine)}"}

        runs = client.get("/api/signals/runs", headers=h).json()
        assert runs["total"] == 1 and runs["rows"][0]["run_id"] == 11

        detail = client.get("/api/signals/runs/11", headers=h).json()
        assert detail["decisions"][0]["code"] == "510300"
        assert detail["decisions"][0]["risk_level"] == "growth_ok"
        assert client.get("/api/signals/runs/999", headers=h).status_code == 404

        trends = client.get("/api/signals/trends?date=2026-11-12", headers=h).json()
        assert trends["rows"][0]["signal_type"] == "volume_breakout_ma60"
        assert client.get("/api/signals/trends?date=bad", headers=h).status_code == 422

        outcomes = client.get("/api/signals/outcomes", headers=h).json()
        assert outcomes["rows"][0]["code"] == "510300"
        assert outcomes["rows"][0]["fill_status"] == "filled"
    finally:
        cleanup(engine)


@pytest.fixture
def store_outcomes(engine):
    cleanup(engine)

    def store(attempt_no, *outcomes):
        day = dt.date(2026, 8, 27)
        with Session(engine) as session:
            run = EodRun(decision_date=day, attempt_no=attempt_no, status="success", decision_outcome="emitted")
            session.add(run)
            session.flush()
            decision = TargetDecision(
                run_id=run.run_id,
                decision_date=day,
                code="512000",
                data_status="ok",
                decision_state="allowed",
                risk_action="allowed",
                reason_code="trigger",
            )
            session.add(decision)
            session.flush()
            decision_id = decision.id
            for outcome in outcomes:
                outcome.decision_id = decision_id
                session.add(outcome)
            session.commit()
            return decision_id

    yield store
    cleanup(engine)


def _filled_outcome(horizon, entry_open, entry_fill, *, entry_date=dt.date(2026, 8, 28), **fields):
    prices = {"entry_open": entry_open, "entry_fill": entry_fill}
    return DecisionOutcome(
        horizon_sessions=horizon,
        entry_date=entry_date,
        fill_status="filled",
        details={key: value for key, value in prices.items() if value is not None},
        **fields,
    )


def test_outcome_basis_spans_pages_and_keeps_each_horizons_return(engine, client, store_outcomes):
    # Archived 512000 decision 8ec9d507a4cc484faae0b62d1416d479. Return columns have eight decimal places.
    historical = [
        (1, 0.530, 0.530265, dt.date(2026, 8, 28), -0.00159872, 0.0, -0.00159872),
        (3, 0.528, 0.528264, dt.date(2026, 9, 1), 0.00218310, -0.00085397, 0.00303707),
        (5, 0.528, 0.528264, dt.date(2026, 9, 3), -0.00538054, -0.01345004, 0.00806950),
    ]
    decision = store_outcomes(
        1,
        *[
            _filled_outcome(
                h,
                opening,
                fill,
                entry_price=opening,
                exit_date=end,
                net_return=net,
                benchmark_return=benchmark,
                excess_return=excess,
            )
            for h, opening, fill, end, net, benchmark, excess in historical
        ],
    )
    # A different decision on the same date and code has internally consistent prices.
    other_decision = store_outcomes(2, _filled_outcome(1, 0.50, 0.50025), _filled_outcome(3, 0.50, 0.50025))
    headers = {"authorization": f"Bearer {_token(engine)}"}
    rows = {}
    for offset in range(5):
        page = client.get("/api/signals/outcomes", params={"limit": 1, "offset": offset}, headers=headers)
        assert page.status_code == 200
        assert page.json()["total"] == 5
        [row] = page.json()["rows"]
        rows[row["decision_id"], row["horizon_sessions"]] = row

    for h, _, _, end, net, benchmark, excess in historical:
        row = rows[decision, h]
        assert row["code"] == "512000" and row["decision_date"] == "2026-08-27"
        assert row["entry_date"] == "2026-08-28" and row["exit_date"] == end.isoformat()
        assert (row["net_return"], row["benchmark_return"], row["excess_return"]) == (net, benchmark, excess)
        basis = row["entry_basis"]
        assert basis["status"] == "conflict" and basis["horizons"] == [1, 3, 5]
        conflicts = {item["field"]: item["values_by_horizon"] for item in basis["conflicts"]}
        assert set(conflicts) == {"entry_open", "entry_fill"}
        for field, field_index in (("entry_open", 1), ("entry_fill", 2)):
            assert all(isinstance(value, str) for value in conflicts[field].values())
            values = {key: Decimal(value) for key, value in conflicts[field].items()}
            assert values == {str(record[0]): Decimal(str(record[field_index])) for record in historical}

    for h in (1, 3):
        assert rows[other_decision, h]["entry_basis"] == {"status": "consistent", "horizons": [1, 3], "conflicts": []}


def test_outcome_basis_incomplete_evidence_and_known_date_conflict(engine, client, store_outcomes):
    single = store_outcomes(1, _filled_outcome(1, 0.53, 0.530265))
    incomplete = store_outcomes(
        2, _filled_outcome(1, 0.53, 0.530265), _filled_outcome(3, None, 0.530265, entry_price=0.53)
    )
    different_dates = store_outcomes(
        3,
        _filled_outcome(1, 0.53, 0.530265),
        _filled_outcome(3, 0.53, None, entry_date=dt.date(2026, 8, 31)),
        _filled_outcome(5, 0.53, 0.530265, entry_date=None),
    )
    headers = {"authorization": f"Bearer {_token(engine)}"}
    response = client.get("/api/signals/outcomes", headers=headers)
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 6
    for row in rows:
        if row["decision_id"] == different_dates:
            assert row["entry_basis"] == {
                "status": "conflict",
                "horizons": [1, 3, 5],
                "conflicts": [
                    {"field": "entry_date", "values_by_horizon": {"1": "2026-08-28", "3": "2026-08-31", "5": None}}
                ],
            }
        else:
            horizons = {single: [1], incomplete: [1, 3]}[row["decision_id"]]
            assert row["entry_basis"] == {"status": "insufficient", "horizons": horizons, "conflicts": []}


def test_compare_reads_shadow_report(engine, client, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_SHADOW_REPORT_DIR", str(tmp_path))
        h = {"authorization": f"Bearer {_token(engine)}"}
        assert client.get("/api/signals/compare?date=2026-11-12", headers=h).status_code == 404
        report = {"date": "2026-11-12", "overall": "PASS", "checks": [{"id": "C1", "status": "PASS"}]}
        (tmp_path / "2026-11-12.compare.json").write_text(json.dumps(report), encoding="utf-8")
        r = client.get("/api/signals/compare?date=2026-11-12", headers=h)
        assert r.status_code == 200 and r.json()["overall"] == "PASS"
        assert client.get("/api/signals/compare?date=bad", headers=h).status_code == 422
        # no date: newest report wins once there are several
        newer = {"date": "2026-11-13", "overall": "FAIL", "checks": []}
        (tmp_path / "2026-11-13.compare.json").write_text(json.dumps(newer), encoding="utf-8")
        assert client.get("/api/signals/compare", headers=h).json()["overall"] == "FAIL"
        (tmp_path / "2026-11-13.compare.json").unlink()
        assert client.get("/api/signals/compare", headers=h).json()["overall"] == "PASS"
    finally:
        cleanup(engine)


def test_etf_pool_returns_the_configured_universe(engine, client):
    cleanup(engine)
    try:
        seed(engine)
        h = {"authorization": f"Bearer {_token(engine)}"}
        data = client.get("/api/signals/etf-pool?date=2026-11-12", headers=h).json()
        assert data["decision_date"] == "2026-11-12"
        assert len(data["items"]) == 40
        item = next(i for i in data["items"] if i["code"] == "510300")
        assert item["state"] == "allowed" and item["risk_action"] == "allowed"
        assert item["reference_close"] == 4.12 and len(item["signals"]) == 1
        other = next(i for i in data["items"] if i["code"] != "510300")
        assert other["state"] == "missing"
        assert data["counts"]["allowed"] == 1 and data["counts"]["missing"] == 39

        latest = client.get("/api/signals/etf-pool", headers=h).json()
        assert latest["decision_date"] == "2026-11-12"
        assert client.get("/api/signals/etf-pool?date=bad", headers=h).status_code == 422
    finally:
        cleanup(engine)


def test_signals_requires_auth(client):
    assert client.get("/api/signals/runs").status_code == 401
