"""Daily report scope tests: qualified samples separated from context records."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radar.swing import daily_report as report

BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 18, 15, 50, tzinfo=BEIJING)


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE swing_eod_run (
            id INTEGER PRIMARY KEY, run_id TEXT, decision_date TEXT, status TEXT,
            started_at TEXT, completed_at TEXT, target_count INTEGER, evaluated_count INTEGER,
            failed_count INTEGER, signal_count INTEGER, allowed_signal_count INTEGER,
            blocked_signal_count INTEGER, decision_outcome TEXT, reason_codes TEXT, details TEXT
        );
        CREATE TABLE eod_target_decision (
            decision_id TEXT PRIMARY KEY, run_id TEXT, decision_date TEXT, decision_at TEXT,
            code TEXT, name TEXT, decision_state TEXT, raw_trigger_count INTEGER,
            validation_split TEXT
        );
        CREATE TABLE eod_decision_outcome (
            outcome_id TEXT PRIMARY KEY, decision_id TEXT, horizon_sessions INTEGER,
            evaluation_as_of TEXT, entry_date TEXT, exit_date TEXT, fill_status TEXT,
            net_return REAL, excess_return REAL, fees_return REAL, slippage_return REAL,
            mfe REAL, mae REAL
        );
        CREATE TABLE trend_signal (date TEXT, code TEXT);
        CREATE TABLE macro_slice (
            date TEXT PRIMARY KEY, total_amount REAL, limit_up INTEGER, limit_down INTEGER,
            source TEXT, extra TEXT
        );
        """
    )


def _run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    date_: str = "2026-09-18",
    evaluated: int = 10,
    failed: int = 0,
    signal_count: int = 0,
    reason_codes: str = "[]",
    raw: int = 0,
    dedup: int = 0,
) -> None:
    connection.execute(
        "INSERT INTO swing_eod_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            None, run_id, date_, status, f"{date_}T07:30:02Z", f"{date_}T07:30:09Z", 10,
            evaluated, failed, signal_count, 0, 0, "no_signal", reason_codes,
            json.dumps({
                "raw_detected_signal_count": raw,
                "deduplicated_signal_count": dedup,
                "target_decision_count": 10,
                "failed_targets": [],
            }),
        ),
    )


def _decision(
    connection: sqlite3.Connection,
    decision_id: str,
    run_id: str,
    *,
    code: str = "518880",
    state: str = "allowed",
    triggers: int = 1,
    date_: str = "2026-09-18",
    observed: str | None = None,
    split: str = "live_oos",
) -> None:
    connection.execute(
        "INSERT INTO eod_target_decision VALUES (?,?,?,?,?,?,?,?,?)",
        (
            decision_id, run_id, date_, observed or f"{date_}T07:30:05Z", code, code,
            state, triggers, split,
        ),
    )


def _outcomes(
    connection: sqlite3.Connection, decision_id: str, *, horizons=report.HORIZONS,
    net: float = -0.01, excess: float = -0.005, fill: str = "filled",
) -> None:
    for horizon in horizons:
        connection.execute(
            "INSERT INTO eod_decision_outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{decision_id}-{horizon}", decision_id, horizon, "2026-09-18",
                "2026-09-18", "2026-09-18", fill, net, excess, 0.0006, 0.001,
                0.01, -0.02,
            ),
        )


def _result(run_id: str = "run-ok", *, status: str = "success", details: dict | None = None,
            reason_codes: list | None = None) -> dict:
    run = {
        "run_id": run_id, "status": status, "target_count": 10, "evaluated_count": 10,
        "failed_count": 0, "signal_count": 0, "allowed_signal_count": 0, "blocked_signal_count": 0,
        "decision_outcome": "no_signal", "reason_codes": reason_codes or [],
        "details": details or {"raw_detected_signal_count": 0, "deduplicated_signal_count": 0,
                               "target_decision_count": 10, "failed_targets": []},
    }
    return {
        "ok": True, "reason": "success", "message": "EOD success",
        "run": run,
        "attempts": {
            "attempts": 1,
            "first": {"status": "success", "decision_outcome": "no_signal", "completed_at": "t1"},
            "latest": {"status": "success", "decision_outcome": "no_signal", "completed_at": "t1"},
        },
    }


def _build(tmp: Path, seed, result: dict) -> dict:
    db = tmp / "swing_macro.db"
    connection = sqlite3.connect(db)
    _schema(connection)
    connection.execute(
        "INSERT INTO macro_slice VALUES ('2026-09-18', 18000.0, 40, 6, 'sina+akshare', '{}')"
    )
    seed(connection)
    connection.commit()
    connection.close()
    return report.build_daily_report(str(db), result, NOW)


def test_notification_plan_slots():
    early = datetime(2026, 9, 18, 15, 40, tzinfo=BEIJING)
    late = datetime(2026, 9, 18, 15, 50, tzinfo=BEIJING)
    assert report.notification_plan({"ok": True}, early) == "none"
    assert report.notification_plan({"ok": False}, early) == "failure"
    assert report.notification_plan({"ok": True}, late) == "daily"
    assert report.notification_plan({"ok": False}, late) == "daily"


def test_qualified_records_are_separated_from_context(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-ok", status="success")
        _decision(connection, "d-qualified", "run-ok", code="518880")
        _outcomes(connection, "d-qualified")
        _run(connection, "run-partial", status="partial", reason_codes='["macro_data_partial"]',
             date_="2026-08-27", raw=1, dedup=1)
        _decision(connection, "d-partial", "run-partial", code="512000", date_="2026-08-27")
        _outcomes(connection, "d-partial", horizons=(1, 3, 5, 10), net=-0.05)
        _run(connection, "run-p", status="success", date_="2026-09-17")
        _decision(connection, "d-control", "run-p", code="510300", state="no_trigger",
                  triggers=0, date_="2026-09-17")
        _decision(connection, "d-failed", "run-p", code="510500", state="data_failed",
                  triggers=0, date_="2026-09-17")

    payload = _build(tmp_path, seed, _result())
    research = payload["research"]
    assert research["qualified"]["records"] == 1
    assert research["qualified"]["mature_records"] == 1
    assert research["qualified"]["codes"] == ["518880"]
    assert research["qualified"]["horizons"]["20"]["n"] == 1
    not_counted = research["not_counted"]
    assert not_counted["macro_partial_excluded"] == 1
    assert not_counted["control_no_trigger"]["decisions"] == 1
    assert not_counted["data_failed"]["decisions"] == 1

    text = payload["text"]
    assert "合格记录（与有效性卡片同口径" in text
    assert "T+1：n=1" in text
    assert "T+1：n=2" not in text
    assert "未触发对照：1 条决策" in text
    assert "宏观缺失导致批次不完整：1 条" in text
    assert "数据失败：1 条决策" in text
    assert "不计入有效性样本" in text


def test_no_qualified_sample_is_not_zero_return(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-partial", status="partial", reason_codes='["macro_data_partial"]')
        _decision(connection, "d-partial", "run-partial")
        _outcomes(connection, "d-partial", horizons=(1, 3, 5, 10))

    payload = _build(tmp_path, seed, _result(run_id="run-partial", status="partial"))
    text = payload["text"]
    assert "暂无可评价样本" in text
    assert "0.00%" not in text
    assert payload["conclusion"].startswith("暂无可评价样本")


def test_dedup_is_not_reported_as_market_data_failure(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-partial", status="partial", reason_codes='["macro_data_partial"]',
             date_="2026-08-25", raw=1, dedup=1)
        _decision(connection, "d-partial", "run-partial", date_="2026-08-25")

    payload = _build(tmp_path, seed, _result(
        run_id="run-partial", status="partial", reason_codes=["macro_data_partial"]))
    reading = payload["reading"]
    assert "去重后无新增" in reading["signals"]
    assert "10/10 合格" in reading["market_data"]
    assert "不可用" not in reading["market_data"]
    assert "未计入有效样本" in reading["research_eligibility"]
    assert "macro_data_partial" in reading["research_eligibility"]


def test_macro_partial_lists_missing_factor(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-partial", status="partial", reason_codes='["macro_data_partial"]')
        _decision(connection, "d-partial", "run-partial")
        connection.execute(
            "UPDATE macro_slice SET extra=? WHERE date='2026-09-18'",
            (json.dumps({
                "collect_status": {
                    "total_amount": {"available": False, "source": "tencent/akshare/eastmoney",
                                     "message": "Connection aborted", "stale": False, "anomalous": False},
                    "limit_ratio": {"available": True, "source": "akshare", "stale": False, "anomalous": False},
                }
            }),),
        )

    payload = _build(tmp_path, seed, _result(
        run_id="run-partial", status="partial", reason_codes=["macro_data_partial"]))
    assert payload["reading"]["macro_missing"] == ["total_amount（tencent/akshare/eastmoney：Connection aborted）"]
    assert "部分缺失" in payload["reading"]["macro"]
    assert "limit_ratio" not in payload["reading"]["macro"]


@pytest.mark.parametrize(
    "observed,split,expected",
    [
        ("2026-09-18T06:00:00Z", "live_oos", "outside_decision_window"),   # 14:00 CST, too early
        ("2026-09-18T07:30:00Z", "walk_forward", "non_oos"),
    ],
)
def test_window_and_oos_exclusions_never_count_as_qualified(tmp_path, observed, split, expected):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-ok", status="success")
        _decision(connection, "d-x", "run-ok", observed=observed, split=split)
        _outcomes(connection, "d-x")

    payload = _build(tmp_path, seed, _result())
    research = payload["research"]
    assert research["qualified"]["mature_records"] == 0
    assert research["not_counted"]["excluded_records"][expected] == 1
    assert "暂无可评价样本" in payload["text"]


def test_unfilled_or_invalid_outcomes_are_not_qualified(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-ok", status="success")
        _decision(connection, "d-u", "run-ok")
        _outcomes(connection, "d-u", fill="unfilled", net=None)

    payload = _build(tmp_path, seed, _result())
    assert payload["research"]["qualified"]["records"] == 1
    assert payload["research"]["qualified"]["mature_records"] == 0
    assert "暂无可评价样本" in payload["text"]


def test_report_remains_read_only_and_atomic(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-ok", status="success")

    payload = _build(tmp_path, seed, _result())
    db = tmp_path / "swing_macro.db"
    before = db.read_bytes()
    path = report.persist_daily_report(str(db), payload)
    assert db.read_bytes() == before
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 2
    assert stored["text"].startswith("EOD 日报")
    assert not list(path.parent.glob(".eod_daily_report.*"))
    read_only = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    read_only.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        read_only.execute("INSERT INTO trend_signal VALUES ('2026-09-18','510300')")
