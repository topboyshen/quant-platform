"""M4.6 shadow comparator C1-C10: pass, fail, blocked and warn paths."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.orm import Session

from qp.collect.signals import canonical_payload_sha256, write_export
from qp.models.signals import (ApprovedIntake, DecisionOutcome, EodRun, ExecutionDigest,
                               MacroSlice, RiskSnapshot, TargetDecision, TrendSignal)
from qp.shadow.compare import compare_day, render_markdown, write_report
from qp.signals.approved import body_sha256

UTC = dt.timezone.utc
DAY = "2027-05-04"


def _key(day: str) -> str:
    return f"ak-{day}"


def _digest_body(day: str = DAY) -> dict:
    text = f"【执行摘要】{day}\n受理 1 条\n"
    return {
        "schema_version": "0.2", "report_type": "execution_digest", "as_of_day": day,
        "generated_at": f"{day}T08:35:00+08:00", "source": "quant",
        "summary": {"accepted": 1, "rejected": 0, "fills": 0, "pnl": None,
                    "pnl_status": "not_enabled",
                    "note": "accepted=queued_stub占位；无实盘下单；成交/盈亏未启用"},
        "text": text, "markdown": text,
        "items": [{"idempotency_key": _key(day), "received_at": f"{day}T01:02:03Z",
                   "status": "accepted", "detail": "ok", "symbol": "510300",
                   "side": "long", "signal_type": "weekly_macd_golden_cross",
                   "confidence": 0.8}],
    }


def _raw(day: str = DAY, *, decisions: list | None = None) -> dict:
    body = _digest_body(day)
    payload = {"idempotency_key": _key(day), "signal_type": "weekly_macd_golden_cross",
               "symbol": "510300", "confidence": 0.8, "expires_at": f"{day}T13:00:00Z",
               "provenance": {"source": "quant"}, "decision": "approve", "side": "long"}
    return {
        "decision_date": day, "source_mtime": {},
        "runs": [{"id": 1, "run_id": "legacy-run", "decision_date": day, "data_date": day,
                  "status": "success", "decision_outcome": "emitted", "target_count": 2,
                  "evaluated_count": 2, "failed_count": 0, "signal_count": 1,
                  "allowed_signal_count": 1, "blocked_signal_count": 0,
                  "reason_codes": "[]", "details": "{}",
                  "started_at": f"{day}T07:30:00Z", "completed_at": f"{day}T07:31:00Z"}],
        "decisions": decisions or [
            {"decision_id": "d1", "run_id": "legacy-run", "decision_date": day,
             "code": "510300", "name": "沪深300ETF", "sector": "宽基", "data_status": "ok",
             "decision_state": "allowed", "raw_trigger_count": 1,
             "raw_signals_json": '[{"signal_type":"weekly_macd_golden_cross","period":"W",'
                                 '"title":"金叉","confidence":0.6,'
                                 '"detail":{"dif":0.1,"dea":0.05,"weekly_close":4.0}}]',
             "risk_action": "allowed", "risk_level": "ok", "reason_code": "signal_allowed",
             "reference_close": 4.0, "validation_split": "live_oos"},
            {"decision_id": "d2", "run_id": "legacy-run", "decision_date": day,
             "code": "159915", "name": "创业板ETF", "sector": "成长", "data_status": "ok",
             "decision_state": "no_trigger", "raw_trigger_count": 0,
             "raw_signals_json": "[]", "risk_action": "not_applicable",
             "risk_level": "ok", "reason_code": "signal_not_triggered",
             "reference_close": 2.0, "validation_split": "live_oos"},
        ],
        "snapshots": [{"run_id": "legacy-run", "level": "ok",
                       "values_json": '{"vix":17.49,"us_10y":4.2,"crude_oil":80.0}',
                       "source_timestamps_json": "{}", "available_at_json": "{}",
                       "sources_json": '{"vix":"cboe","us_10y":"fred","crude_oil":"fred"}',
                       "missing_fields_json": "[]"}],
        "signals": [{"date": day, "code": "510300", "name": "沪深300ETF",
                     "signal_type": "weekly_macd_golden_cross", "period": "W",
                     "data_date": "2027-04-30", "title": "周线金叉",
                     "detail": '{"dif":0.1,"dea":0.05,"weekly_close":4.0}',
                     "confidence": 0.6}],
        "outcomes": [{"decision_id": "d1", "decision_date": day, "code": "510300",
                      "horizon_sessions": 1, "entry_date": day, "entry_fill": 4.0,
                      "exit_date": "2027-05-05", "exit_fill": 4.04,
                      "benchmark_return": 0.01, "net_return": 0.009,
                      "excess_return": -0.001, "fill_status": "filled"}],
        "macro": {"date": day, "total_amount": 13573.57, "margin_balance": 22948.01,
                  "margin_growth_pct": 0.339, "limit_up": 85, "limit_down": 0,
                  "limit_ratio": 85.0, "source": "tencent+akshare", "extra": "{}"},
        "outbox": [], "dispatch": [], "attempts": [],
        "risk_state": None, "risk_override": None,
        "intake": [{"idempotency_key": _key(day), "received_at": f"{day}T01:02:03Z",
                    "status": "accepted", "detail": "ok",
                    "payload_json": json.dumps(payload, ensure_ascii=False)}],
        "digest": body,
        "missing": [],
    }


def seed_qp(engine, day: str = DAY, *, mutate=None) -> None:
    d = dt.date.fromisoformat(day)
    body = _digest_body(day)
    payload = {"idempotency_key": _key(day), "signal_type": "weekly_macd_golden_cross",
               "symbol": "510300", "confidence": 0.8, "expires_at": f"{day}T13:00:00Z",
               "provenance": {"source": "quant"}, "decision": "approve", "side": "long"}
    with Session(engine) as session:
        run = EodRun(decision_date=d, attempt_no=1, data_date=d, status="success",
                     decision_outcome="emitted", target_count=2, evaluated_count=2,
                     failed_count=0, signal_count=1, allowed_signal_count=1,
                     blocked_signal_count=0, reason_codes=[], details={})
        session.add(run)
        session.flush()
        session.add(RiskSnapshot(
            run_id=run.run_id, decision_at=dt.datetime(2027, 5, 4, 7, 30, tzinfo=UTC),
            level="ok", values={"vix": 17.49, "us_10y": 4.2, "crude_oil": 80.0},
            sources={"vix": "cboe", "us_10y": "fred", "crude_oil": "fred"},
            observed_at={}, available_at={}, missing_fields=[]))
        d1 = TargetDecision(
            run_id=run.run_id, decision_date=d, code="510300", name="沪深300ETF",
            sector="宽基", data_status="ok", decision_state="allowed",
            risk_action="allowed", risk_level="ok", reason_code="signal_allowed",
            reference_close=4.0, raw_trigger_count=1,
            raw_signals=[{"signal_type": "weekly_macd_golden_cross", "period": "W",
                          "title": "金叉", "confidence": 0.6,
                          "detail": {"dif": 0.1, "dea": 0.05, "weekly_close": 4.0}}],
            validation_split="live_oos")
        d2 = TargetDecision(
            run_id=run.run_id, decision_date=d, code="159915", name="创业板ETF",
            sector="成长", data_status="ok", decision_state="no_trigger",
            risk_action="not_applicable", risk_level="ok",
            reason_code="signal_not_triggered", reference_close=2.0,
            raw_trigger_count=0, raw_signals=[], validation_split="live_oos")
        session.add_all([d1, d2])
        session.flush()
        session.add(DecisionOutcome(
            decision_id=d1.id, horizon_sessions=1, entry_date=d,
            exit_date=dt.date(2027, 5, 5), entry_price=4.0, exit_price=4.04,
            benchmark_return=0.01, net_return=0.009, excess_return=-0.001,
            fill_status="filled", fill_reason="ok"))
        session.add(TrendSignal(
            decision_date=d, code="510300", name="沪深300ETF",
            signal_type="weekly_macd_golden_cross", period="W",
            data_date=dt.date(2027, 4, 30), title="周线金叉",
            detail={"dif": 0.1, "dea": 0.05, "weekly_close": 4.0}, confidence=0.6))
        session.add(MacroSlice(
            decision_date=d, total_amount=13573.57, margin_balance=22948.01,
            margin_growth_pct=0.339, limit_up=85, limit_down=0, limit_ratio=85.0,
            source="tencent+akshare", collect_status={}))
        session.add(ApprovedIntake(
            idempotency_key=_key(day), source="dedirock",
            received_at=dt.datetime(d.year, d.month, d.day, 1, 2, 3, tzinfo=UTC),
            status="accepted", detail="ok", risk_check={"passed": True, "detail": "ok"},
            payload_sha256=canonical_payload_sha256(payload), payload=payload))
        session.add(ExecutionDigest(
            digest_date=d, item_count=1, accepted=1, rejected=0, body=body,
            sha256=body_sha256(body), push_state="shadowed"))
        session.commit()
        if mutate:
            mutate(session, run, d1)
            session.commit()


def _export(tmp_path, day: str = DAY, raw: dict | None = None) -> object:
    out = tmp_path / "legacy"
    write_export(out, day, raw or _raw(day), "2027-05-05T00:45:00+00:00")
    return out


def test_compare_all_pass(engine, tmp_path):
    seed_qp(engine)
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 4), _export(tmp_path),
                             hmac_secret=b"test-key")
    assert result["overall"] == "PASS", json.dumps(result, ensure_ascii=False, indent=2)
    assert result["gate_ok"] is True
    assert {check["id"] for check in result["checks"]} >= {f"C{i}" for i in range(1, 11)}
    assert "M4 shadow compare 2027-05-04" in render_markdown(result)


def test_compare_decision_drift_fails(engine, tmp_path):
    def mutate(session, run, decision):
        decision.reason_code = "macro_risk_blocked"

    seed_qp(engine, "2027-05-05", mutate=mutate)
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 5),
                             _export(tmp_path, "2027-05-05", _raw("2027-05-05")),
                             hmac_secret=b"test-key")
    assert result["overall"] == "FAIL"
    c2 = next(check for check in result["checks"] if check["id"] == "C2")
    assert c2["status"] == "FAIL"
    assert any("reason_code" in diff for diff in c2["diffs"])


def test_compare_bad_manifest_blocks(engine, tmp_path):
    seed_qp(engine, "2027-05-06")
    export = _export(tmp_path, "2027-05-06", _raw("2027-05-06"))
    (export / "2027-05-06" / "target_decisions.json").write_text("[]", encoding="utf-8")
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 6), export, hmac_secret=b"test-key")
    assert result["overall"] == "BLOCKED"
    assert next(c for c in result["checks"] if c["id"] == "EXPORT")["status"] == "BLOCKED"


def test_compare_source_divergence_is_warn(engine, tmp_path):
    def mutate(session, run, decision):
        snap = session.query(RiskSnapshot).filter_by(run_id=run.run_id).one()
        snap.sources = {"vix": "yahoo", "us_10y": "fred", "crude_oil": "fred"}

    seed_qp(engine, "2027-05-07", mutate=mutate)
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 7),
                             _export(tmp_path, "2027-05-07", _raw("2027-05-07")),
                             hmac_secret=b"test-key")
    assert result["overall"] == "WARN" and result["gate_ok"] is True, \
        json.dumps(result, ensure_ascii=False, indent=2)
    c3 = next(check for check in result["checks"] if check["id"] == "C3")
    assert c3["status"] == "WARN"
    assert any("source_divergence" in diff for diff in c3["diffs"])


def test_compare_intake_hash_mismatch_fails(engine, tmp_path):
    def mutate(session, run, decision):
        row = session.query(ApprovedIntake).filter_by(idempotency_key=_key("2027-05-08")).one()
        row.payload_sha256 = "0" * 64

    seed_qp(engine, "2027-05-08", mutate=mutate)
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 8),
                             _export(tmp_path, "2027-05-08", _raw("2027-05-08")),
                             hmac_secret=b"test-key")
    assert result["overall"] == "FAIL"
    c8 = next(check for check in result["checks"] if check["id"] == "C8")
    assert c8["status"] == "FAIL"


def test_compare_missing_watchdog_notification_fails(engine, tmp_path):
    raw = _raw("2027-05-09")
    raw["dispatch"] = [{
        "namespace": "watchdog:2027-05-09:daily", "business_key": "k",
        "payload_json": json.dumps({"text": "EOD 日报\n不支持推荐"})}]
    seed_qp(engine, "2027-05-09")
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 9),
                             _export(tmp_path, "2027-05-09", raw), hmac_secret=b"test-key")
    assert result["overall"] == "FAIL"
    c7 = next(check for check in result["checks"] if check["id"] == "C7")
    assert c7["status"] == "FAIL"
    assert any("existence" in diff for diff in c7["diffs"])


def test_compare_digest_items_drift_fails(engine, tmp_path):
    def mutate(session, run, decision):
        row = session.query(ExecutionDigest).filter_by(
            digest_date=dt.date(2027, 5, 10)).one()
        body = dict(row.body)
        body["items"] = []
        row.body = body

    seed_qp(engine, "2027-05-10", mutate=mutate)
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 10),
                             _export(tmp_path, "2027-05-10", _raw("2027-05-10")),
                             hmac_secret=b"test-key")
    assert result["overall"] == "FAIL"
    c9 = next(check for check in result["checks"] if check["id"] == "C9")
    assert c9["status"] == "FAIL"


def test_shadow_compare_cli_blocks_when_qp_run_missing(engine, tmp_path, capsys):
    from qp.cli import main

    fixtures = Path(__file__).parent / "fixtures/m4"
    rc = main(["shadow", "compare", "--date", "2026-08-21",
               "--legacy", str(fixtures), "--out", str(tmp_path / "reports")])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "BLOCKED"
    assert (tmp_path / "reports/2026-08-21.compare.json").is_file()


def test_write_report_outputs_json_and_markdown(engine, tmp_path):
    seed_qp(engine, "2027-05-11")
    with Session(engine) as session:
        result = compare_day(session, dt.date(2027, 5, 11),
                             _export(tmp_path, "2027-05-11", _raw("2027-05-11")),
                             hmac_secret=b"test-key")
    files = write_report(result, tmp_path / "reports")
    assert files == ["2027-05-11.compare.json", "2027-05-11.compare.md"]
    written = json.loads((tmp_path / "reports/2027-05-11.compare.json").read_text())
    assert written["overall"] == "PASS", json.dumps(written, ensure_ascii=False, indent=2)
