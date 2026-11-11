"""Read-only universe/evidence regression, including the real no-signal empty UI case."""
import hashlib
import json
import sqlite3
from datetime import datetime

import pytest

from dashboard.etf_pool import load_etf_pool, pool_expected_date
from runtime_contracts.calendar import TZ

NOW = datetime(2026, 9, 12, 16, tzinfo=TZ)
DATE = "2026-09-11"


@pytest.fixture
def sample():
    config = {"targets": [{"code": str(510300+i), "name": "ETF"+str(i), "sector": "宽基", "secret": "DO_NOT_COPY"} for i in range(10)]}
    audit = {"decision_date": DATE, "run": {"run_id": "latest", "status": "success", "decision_eligible": True,
             "completed_at": "2026-09-11T15:48:20+08:00"},
             "coverage": {"target_count": 10, "evaluated_count": 10, "failed_count": 0},
             "decision": {"signal_count": 0, "allowed_signal_count": 0, "blocked_signal_count": 0}}
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE eod_target_decision (run_id TEXT, code TEXT, decision_date TEXT, data_date TEXT, "
                "data_status TEXT, decision_state TEXT, raw_trigger_count INTEGER, risk_action TEXT, risk_level TEXT, reason_code TEXT, reference_close REAL, snapshot_id TEXT)")
    con.execute("CREATE TABLE eod_risk_snapshot (run_id TEXT, snapshot_id TEXT, level TEXT, decision_at TEXT)")
    con.execute("INSERT INTO eod_risk_snapshot VALUES ('latest','risk1','critical','2026-09-11T15:48:00+08:00')")
    con.executemany("INSERT INTO eod_target_decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [("latest", t["code"], DATE, DATE, "ok", "no_trigger", 0, "not_applicable", "critical", "signal_not_triggered", .547, "risk1") for t in config["targets"]])
    yield config, con, audit
    con.close()


def load(sample, expected=DATE):
    config, con, audit = sample
    return load_etf_pool(config, con, audit, expected_date=expected, now=NOW)


def test_ten_evaluated_without_signals_remain_ten_research_rows(sample):
    pool = load(sample)
    assert pool["status"] == "ok"
    assert pool["configured_count"] == pool["counts"]["no_trigger"] == 10
    assert len(pool["items"]) == 10 and all(r["is_current"] for r in pool["items"])
    assert pool["items"][0]["reference_close"] == .547
    assert "DO_NOT_COPY" not in json.dumps(pool)


def test_weekend_and_holiday_calendar():
    from runtime_contracts.calendar import expected_session, sessions
    from tests.test_eod_contracts import calendar_payload
    payload = calendar_payload()
    assert expected_session(sessions(payload, NOW), NOW).isoformat() == DATE


def test_missing_calendar_never_blesses_a_row(sample, tmp_path):
    assert pool_expected_date(tmp_path / "absent.json", NOW) is None
    pool = load(sample, expected=None)
    assert pool["counts"]["unknown"] == 10 and pool["status"] != "ok"


def test_next_session_does_not_reuse_previous_success(sample):
    pool = load(sample, expected="2026-09-14")
    assert pool["counts"]["stale"] == 10 and not any(r["is_current"] for r in pool["items"])


def test_latest_failed_attempt_does_not_fall_back_to_success(sample):
    config, con, audit = sample
    con.execute("INSERT INTO eod_target_decision SELECT 'older', code,decision_date,data_date,data_status,decision_state,raw_trigger_count,risk_action,risk_level,reason_code,reference_close,snapshot_id FROM eod_target_decision")
    con.execute("UPDATE eod_target_decision SET data_status='failed',decision_state='data_failed',data_date=NULL,reference_close=NULL WHERE run_id='latest'")
    audit["run"]["status"] = "failed"
    audit["coverage"].update(evaluated_count=0, failed_count=10)
    pool = load(sample)
    assert pool["counts"]["data_failed"] == 10
    assert all(r["reference_close"] is None and not r["is_current"] for r in pool["items"])


@pytest.mark.parametrize("update", [
    "decision_date='2026-09-10'", "data_date='2026-09-10'", "data_date='2026-09-14'",
    "decision_state='bogus'", "data_status='failed'", "raw_trigger_count=1", "raw_trigger_count=-1",
    "reference_close=NULL", "reference_close=0", "reference_close=1e999", "risk_action='allowed'",
])
def test_bad_row_is_unknown_not_no_trigger(sample, update):
    sample[1].execute("UPDATE eod_target_decision SET " + update + " WHERE code='510300'")
    pool = load(sample)
    assert pool["counts"]["unknown"] == 1 and pool["status"] != "ok"
    assert pool["items"][0]["reference_close"] is None


@pytest.mark.parametrize("case", ["duplicate", "foreign_code", "coverage", "target_count", "future", "ineligible", "invalid_time"])
def test_ambiguous_run_fails_closed(sample, case):
    _, con, audit = sample
    if case == "duplicate": con.execute("INSERT INTO eod_target_decision SELECT * FROM eod_target_decision LIMIT 1")
    if case == "foreign_code": con.execute("UPDATE eod_target_decision SET code='588000' WHERE code='510300'")
    if case == "coverage": audit["coverage"]["evaluated_count"] = 9
    if case == "target_count": audit["coverage"]["target_count"] = 11
    if case == "future": audit["decision_date"] = "2026-09-14"
    if case == "ineligible": audit["run"]["decision_eligible"] = False
    if case == "invalid_time": audit["run"]["completed_at"] = "2026-09-11T15:48:20"
    pool = load(sample)
    assert pool["counts"]["unknown"] == 10 and pool["status"] != "ok"


def test_missing_record_is_not_hidden(sample):
    sample[1].execute("DELETE FROM eod_target_decision WHERE code='510300'")
    pool = load(sample)
    assert len(pool["items"]) == 10 and pool["counts"]["missing"] == 1 and pool["status"] != "ok"


def test_running_is_not_terminal(sample):
    sample[2]["run"].update(status="running", completed_at=None)
    pool = load(sample)
    assert pool["counts"]["running"] == 10


@pytest.mark.parametrize("targets", [None, [], [{"code": "510300"}]*2, [{"code": "000001"}], [{"code": 510300}], [{"code": "510300.json"}]])
def test_invalid_universe_cannot_be_inferred_from_signals(sample, targets):
    sample[0]["targets"] = targets
    pool = load(sample)
    assert pool["configured_count"] is None and pool["items"] == []


def test_read_only_connection_and_two_bounded_queries(sample, tmp_path):
    config, con, audit = sample
    path = tmp_path / "fixture.db"
    # This fixture is synthetic; never a production SQLite copy or backup.
    with sqlite3.connect(path) as output:
        for sql in con.iterdump(): output.execute(sql)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    read = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
    read.execute("PRAGMA query_only=ON")
    queries = []; read.set_trace_callback(queries.append)
    pool = load_etf_pool(config, read, audit, expected_date=DATE, now=NOW)
    read.close()
    assert pool["status"] == "ok"
    assert len(queries) == 2 and all(q.startswith("SELECT") for q in queries)
    assert "LIMIT 2" in queries[0] and "LIMIT 201" in queries[1]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_database_keeps_configured_assets_with_missing_evidence(sample):
    pool = load_etf_pool(sample[0], None, None, expected_date=DATE, now=NOW)
    assert pool["counts"]["missing"] == 10 and len(pool["items"]) == 10


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unbound"])
def test_missing_or_mismatched_risk_evidence_cannot_look_successful(sample, fault):
    con = sample[1]
    if fault == "missing": con.execute("DELETE FROM eod_risk_snapshot")
    if fault == "duplicate": con.execute("INSERT INTO eod_risk_snapshot SELECT * FROM eod_risk_snapshot")
    if fault == "unbound": con.execute("UPDATE eod_target_decision SET snapshot_id='wrong'")
    assert load(sample)["counts"]["unknown"] == 10


@pytest.mark.parametrize("state,allowed,blocked", [("allowed", 2, 0), ("blocked", 0, 2), ("allowed", 1, 1)])
def test_per_signal_gating_can_be_mixed_within_one_etf(sample, state, allowed, blocked):
    sample[1].execute("UPDATE eod_target_decision SET decision_state=?,risk_action=?,raw_trigger_count=2 WHERE code='510300'", (state, state))
    sample[2]["decision"].update(signal_count=2, allowed_signal_count=allowed, blocked_signal_count=blocked)
    pool = load(sample)
    assert pool["status"] == "ok" and pool["counts"][state] == 1
    # The exact same target records cannot be reconciled with a no-signal audit.
    sample[2]["decision"].update(signal_count=0, allowed_signal_count=0, blocked_signal_count=0)
    assert load(sample)["counts"]["unknown"] == 10
