"""M4.6 read-only legacy exporter: file layout, manifest, sanitization, read-only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

import pytest

from qp.collect.signals import (
    canonical_payload_sha256,
    export_day,
    main,
    render_file,
    resolve_date,
    write_export,
)

DAY = "2027-05-04"
RUN_ID = "legacy-run-1"
UTC = dt.UTC


def _schema(db) -> None:
    db.executescript(
        """
        CREATE TABLE swing_eod_run (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE,
            decision_date TEXT NOT NULL, data_date TEXT, status TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT, target_count INTEGER NOT NULL DEFAULT 0,
            evaluated_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
            signal_count INTEGER NOT NULL DEFAULT 0, allowed_signal_count INTEGER NOT NULL DEFAULT 0,
            blocked_signal_count INTEGER NOT NULL DEFAULT 0,
            decision_outcome TEXT NOT NULL DEFAULT 'not_evaluated',
            reason_codes TEXT NOT NULL DEFAULT '[]', details TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE eod_target_decision (
            decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, snapshot_id TEXT,
            decision_date TEXT NOT NULL, decision_at TEXT NOT NULL, data_date TEXT,
            code TEXT NOT NULL, name TEXT NOT NULL, sector TEXT NOT NULL,
            data_status TEXT NOT NULL, decision_state TEXT NOT NULL,
            raw_trigger_count INTEGER NOT NULL, raw_signals_json TEXT NOT NULL,
            risk_action TEXT NOT NULL, risk_level TEXT NOT NULL, reason_code TEXT NOT NULL,
            reference_close REAL, validation_split TEXT NOT NULL DEFAULT 'live_oos',
            created_at TEXT NOT NULL
        );
        CREATE TABLE eod_risk_snapshot (
            snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, decision_at TEXT NOT NULL,
            level TEXT NOT NULL, values_json TEXT NOT NULL, source_timestamps_json TEXT NOT NULL,
            available_at_json TEXT NOT NULL, sources_json TEXT NOT NULL,
            missing_fields_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE trend_signal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, code TEXT NOT NULL,
            name TEXT, signal_type TEXT, period TEXT, data_date TEXT, title TEXT,
            detail TEXT, confidence REAL, notified_at TEXT, created_at TEXT
        );
        CREATE TABLE eod_decision_outcome (
            outcome_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
            horizon_sessions INTEGER NOT NULL, evaluation_as_of TEXT NOT NULL,
            entry_date TEXT, entry_open REAL, entry_fill REAL, exit_date TEXT,
            exit_close REAL, exit_fill REAL, benchmark_code TEXT NOT NULL,
            benchmark_return REAL, gap_return REAL, gross_return REAL, net_return REAL,
            excess_return REAL, mfe REAL, mae REAL, fees_return REAL, slippage_return REAL,
            fill_status TEXT NOT NULL, fill_reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE macro_slice (
            date TEXT PRIMARY KEY, total_amount REAL, margin_balance REAL,
            margin_growth_pct REAL, limit_up INTEGER, limit_down INTEGER,
            limit_ratio REAL, source TEXT, extra TEXT, created_at TEXT
        );
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL, operation TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, attempted_at TEXT, sent_at TEXT, last_error TEXT
        );
        CREATE TABLE notification_dispatch (
            id INTEGER PRIMARY KEY AUTOINCREMENT, business_key TEXT NOT NULL UNIQUE,
            namespace TEXT NOT NULL, channel TEXT NOT NULL, operation TEXT NOT NULL,
            payload_json TEXT NOT NULL, state TEXT NOT NULL, attempts_used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE notification_attempt (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL, started_at TEXT NOT NULL, outcome TEXT NOT NULL
        );
        """
    )


def make_legacy(tmp_path, *, with_db: bool = True) -> tuple:
    root = tmp_path / "legacy"
    (root / "data/approved-signals/digests").mkdir(parents=True)
    db_path = root / "data/swing_macro.db"
    if with_db:
        db = sqlite3.connect(db_path)
        _schema(db)
        db.execute(
            "INSERT INTO swing_eod_run(run_id,decision_date,data_date,status,started_at,"
            "completed_at,target_count,evaluated_count,failed_count,signal_count,"
            "allowed_signal_count,blocked_signal_count,decision_outcome,reason_codes,details) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (RUN_ID, DAY, DAY, "success", f"{DAY}T07:30:00Z", f"{DAY}T07:31:00Z",
             2, 2, 0, 1, 1, 0, "emitted", "[]", '{"generated_signals":1}'))
        decisions = [
            ("d1", "510300", "沪深300ETF", "宽基", "ok", "allowed", 1,
             '[{"signal_type":"weekly_macd_golden_cross","period":"W","title":"金叉",'
             '"confidence":0.6,"detail":{"dif":0.1,"dea":0.05,"weekly_close":4.0}}]',
             "allowed", "ok", "signal_allowed", 4.0),
            ("d2", "159915", "创业板ETF", "成长", "ok", "no_trigger", 0, "[]",
             "not_applicable", "ok", "signal_not_triggered", 2.0),
        ]
        for row in decisions:
            db.execute(
                "INSERT INTO eod_target_decision(decision_id,run_id,decision_date,decision_at,"
                "data_date,code,name,sector,data_status,decision_state,raw_trigger_count,"
                "raw_signals_json,risk_action,risk_level,reason_code,reference_close,"
                "validation_split,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row[0], RUN_ID, DAY, f"{DAY}T07:30:00Z", DAY, row[1], row[2], row[3],
                 row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11],
                 "live_oos", f"{DAY}T07:31:00Z"))
        db.execute(
            "INSERT INTO eod_risk_snapshot(snapshot_id,run_id,decision_at,level,values_json,"
            "source_timestamps_json,available_at_json,sources_json,missing_fields_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s1", RUN_ID, f"{DAY}T07:30:00Z", "ok",
             '{"vix":17.49,"us_10y":4.2,"crude_oil":80.0}', "{}", "{}",
             '{"vix":"cboe","us_10y":"fred","crude_oil":"fred"}', "[]",
             f"{DAY}T07:30:00Z"))
        db.execute(
            "INSERT INTO trend_signal(date,code,name,signal_type,period,data_date,title,detail,"
            "confidence,notified_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (DAY, "510300", "沪深300ETF", "weekly_macd_golden_cross", "W", "2027-04-30",
             "周线金叉", '{"dif":0.1,"dea":0.05,"weekly_close":4.0}', 0.6, None))
        db.execute(
            "INSERT INTO eod_decision_outcome(outcome_id,decision_id,horizon_sessions,"
            "evaluation_as_of,entry_date,entry_fill,exit_date,exit_fill,benchmark_code,"
            "benchmark_return,net_return,excess_return,fill_status,fill_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o1", "d1", 1, f"{DAY}T08:10:00Z", DAY, 4.0, "2027-05-05", 4.04,
             "510300", 0.01, 0.009, -0.001, "filled", "ok", f"{DAY}T08:10:00Z"))
        db.execute(
            "INSERT INTO macro_slice(date,total_amount,margin_balance,margin_growth_pct,"
            "limit_up,limit_down,limit_ratio,source,extra) VALUES (?,?,?,?,?,?,?,?,?)",
            (DAY, 13573.57, 22948.01, 0.339, 85, 0, 85.0, "tencent+akshare",
             '{"collect_status":{"total_amount":{"available":true}}}'))
        db.execute(
            "INSERT INTO notification_outbox(idempotency_key,channel,operation,payload_json,"
            "status,attempts,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"watchdog:{DAY}:daily", "telegram", "send_system_alert",
             json.dumps({"text": "EOD 日报", "chat_id": "123", "token": "secret-value"}),
             "sent", 1, f"{DAY}T07:50:00Z"))
        db.execute(
            "INSERT INTO notification_dispatch(business_key,namespace,channel,operation,"
            "payload_json,state,attempts_used) VALUES (?,?,?,?,?,?,?)",
            (f"key-{DAY}", f"watchdog:{DAY}:daily", "telegram", "send_system_alert",
             json.dumps({"text": "EOD 日报", "chat_id": "123"}), "confirmed", 1))
        db.execute(
            "INSERT INTO notification_attempt(dispatch_id,attempt_no,started_at,outcome) "
            "VALUES (1,1,?, 'confirmed')", (f"{DAY}T07:50:01Z",))
        db.commit()
        db.close()
    intake = sqlite3.connect(root / "data/approved-signals/intake.sqlite3")
    intake.execute(
        "CREATE TABLE intakes (idempotency_key TEXT PRIMARY KEY, received_at TEXT NOT NULL,"
        " status TEXT NOT NULL, payload_json TEXT NOT NULL, detail TEXT)")
    intake.commit()
    intake.close()
    return root, db_path


def seed_intake(root, payload: dict, *, key: str = "ak-1") -> None:
    db = sqlite3.connect(root / "data/approved-signals/intake.sqlite3")
    db.execute("INSERT INTO intakes VALUES (?,?,?,?,?)",
               (key, f"{DAY}T01:02:03Z", "accepted",
                json.dumps(payload, ensure_ascii=False), "ok"))
    db.commit()
    db.close()


def write_digest(root, day: str = DAY) -> dict:
    body = {
        "schema_version": "0.2", "report_type": "execution_digest", "as_of_day": day,
        "generated_at": f"{day}T08:35:00+08:00", "source": "quant",
        "summary": {"accepted": 0, "rejected": 0, "fills": 0, "pnl": None},
        "text": "【执行摘要】\n", "markdown": "【执行摘要】\n", "items": [],
    }
    (root / f"data/approved-signals/digests/execution_digest_{day}.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return body


def test_export_writes_all_files_and_manifest(tmp_path):
    root, db_path = make_legacy(tmp_path)
    seed_intake(root, {"idempotency_key": "ak-1", "symbol": "510300", "chat_id": "999"})
    write_digest(root)
    before = db_path.stat().st_mtime_ns
    manifest = export_day(root, DAY, tmp_path / "out",
                          now=dt.datetime(2027, 5, 5, 0, 45, tzinfo=UTC))
    after = db_path.stat().st_mtime_ns
    assert before == after
    assert manifest["ok"] is True and manifest["decision_date"] == DAY
    assert len(manifest["files"]) == 10
    target = tmp_path / "out" / DAY
    for name, meta in manifest["files"].items():
        data = (target / name).read_bytes()
        assert len(data) == meta["bytes"]
        assert hashlib.sha256(data).hexdigest() == meta["sha256"]
    run = json.loads((target / "eod_run.json").read_text())
    assert run[0]["reason_codes"] == [] and run[0]["details"]["generated_signals"] == 1
    decisions = json.loads((target / "target_decisions.json").read_text())
    assert [row["code"] for row in decisions] == ["159915", "510300"]
    assert decisions[1]["raw_signals"][0]["signal_type"] == "weekly_macd_golden_cross"
    snapshots = json.loads((target / "risk_snapshot.json").read_text())
    assert snapshots[0]["values"]["vix"] == 17.49
    outcomes = json.loads((target / "outcomes.json").read_text())
    assert outcomes[0]["code"] == "510300" and outcomes[0]["entry_fill"] == 4.0
    macro = json.loads((target / "macro_slice.json").read_text())
    assert macro["extra"]["collect_status"]["total_amount"]["available"] is True
    notifications = json.loads((target / "notifications.json").read_text())
    assert notifications["dispatch"][0]["namespace"] == f"watchdog:{DAY}:daily"
    assert notifications["outbox"][0]["payload"]["chat_id"] == "[redacted]"
    assert "secret-value" not in json.dumps(notifications, ensure_ascii=False)
    intake = json.loads((target / "intake.json").read_text())[0]
    assert intake["payload"]["chat_id"] == "[redacted]"
    assert intake["payload_sha256"] == canonical_payload_sha256(
        {"idempotency_key": "ak-1", "symbol": "510300", "chat_id": "999"})
    digest = json.loads((target / "digest.json").read_text())
    assert digest["as_of_day"] == DAY


def test_export_emit_json_does_not_write(tmp_path, capsys):
    root, _ = make_legacy(tmp_path)
    out_root = tmp_path / "out"
    rc = main(["--date", DAY, "--legacy-root", str(root), "--out-root", str(out_root),
               "--emit-json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert set(payload["files"]) == {
        "eod_run.json", "target_decisions.json", "risk_snapshot.json", "trend_signals.json",
        "outcomes.json", "macro_slice.json", "notifications.json", "risk_state.json",
        "intake.json", "digest.json"}
    assert not out_root.exists()


def test_missing_db_is_blocked_not_crashed(tmp_path):
    root, _ = make_legacy(tmp_path, with_db=False)
    rc = main(["--date", DAY, "--legacy-root", str(root), "--out-root", str(tmp_path / "out")])
    assert rc == 2
    manifest = json.loads((tmp_path / "out" / DAY / "manifest.json").read_text())
    assert manifest["ok"] is False and manifest["missing"]


def test_digest_selection_uses_decision_date_not_previous_day(tmp_path):
    root, _ = make_legacy(tmp_path)
    write_digest(root, DAY)
    write_digest(root, "2027-05-03")
    write_export(tmp_path / "out", DAY, _raw(root), "2027-05-05T00:45:00+00:00")
    digest = json.loads((tmp_path / "out" / DAY / "digest.json").read_text())
    assert digest["as_of_day"] == DAY


def _raw(root):
    from qp.collect.signals import collect_raw
    return collect_raw(root, DAY)


def test_relative_dates_resolve_in_cst_across_utc_midnight():
    # 16:30 UTC on 11-11 is already 00:30 CST on 11-12.
    now = dt.datetime(2026, 11, 11, 16, 30, tzinfo=UTC)
    assert resolve_date("today", now=now) == dt.date(2026, 11, 12)
    assert resolve_date("yesterday", now=now) == dt.date(2026, 11, 11)
    # 15:59 UTC on 11-12 is 23:59 CST the same day.
    now = dt.datetime(2026, 11, 12, 15, 59, tzinfo=UTC)
    assert resolve_date("today", now=now) == dt.date(2026, 11, 12)
    with pytest.raises(ValueError):
        resolve_date("not-a-date")


def test_export_accepts_relative_date_and_writes_canonical_manifest(tmp_path, capsys):
    from unittest.mock import patch

    root, _ = make_legacy(tmp_path)
    with patch("qp.collect.signals.resolve_date", return_value=dt.date(2027, 5, 4)):
        rc = main(["--date", "yesterday", "--legacy-root", str(root),
                   "--out-root", str(tmp_path / "out")])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["decision_date"] == DAY
    manifest = json.loads((tmp_path / "out" / DAY / "manifest.json").read_text())
    assert manifest["decision_date"] == DAY


def test_render_file_is_sorted_and_trailing_newline():
    raw = render_file({"b": 1, "a": 2})
    assert raw == b'{\n  "a": 2,\n  "b": 1\n}\n'
