from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts import check_quant_monday_acceptance as acceptance

SHANGHAI = ZoneInfo("Asia/Shanghai")
DECISION_DATE = "2026-08-31"


@pytest.fixture(autouse=True)
def _stub_telegram_bot_api(monkeypatch):
    monkeypatch.setattr(
        acceptance,
        "_telegram_api_get",
        lambda **_kwargs: (True, 200, 1.25),
    )


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE macro_slice (
            date TEXT PRIMARY KEY,
            total_amount REAL,
            source TEXT,
            extra TEXT
        );
        CREATE TABLE swing_eod_run (
            id INTEGER PRIMARY KEY,
            run_id TEXT,
            decision_date TEXT,
            data_date TEXT,
            status TEXT,
            started_at TEXT,
            completed_at TEXT,
            target_count INTEGER,
            evaluated_count INTEGER,
            failed_count INTEGER,
            signal_count INTEGER,
            allowed_signal_count INTEGER,
            blocked_signal_count INTEGER,
            decision_outcome TEXT,
            reason_codes TEXT
        );
        CREATE TABLE eod_target_decision (
            run_id TEXT,
            snapshot_id TEXT,
            data_status TEXT,
            decision_state TEXT,
            risk_action TEXT,
            risk_level TEXT
        );
        CREATE TABLE eod_risk_snapshot (
            snapshot_id TEXT,
            run_id TEXT,
            decision_at TEXT,
            level TEXT,
            values_json TEXT,
            source_timestamps_json TEXT,
            available_at_json TEXT,
            sources_json TEXT,
            missing_fields_json TEXT
        );
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY,
            status TEXT,
            attempts INTEGER,
            created_at TEXT,
            attempted_at TEXT,
            sent_at TEXT,
            last_error TEXT
        );
        """
    )
    collect_status = {
        "collect_status": {
            "total_amount": {
                "available": True,
                "source": "tencent",
                "observed_date": DECISION_DATE,
                "stale": False,
                "anomalous": False,
            }
        }
    }
    connection.execute(
        "INSERT INTO macro_slice VALUES (?,?,?,?)",
        (DECISION_DATE, 21_017.15, "tencent", json.dumps(collect_status)),
    )
    connection.execute(
        "INSERT INTO swing_eod_run VALUES "
        "(1,'run-1',?,?, 'success','2026-08-31T07:30:01Z',"
        "'2026-08-31T07:31:01Z',2,2,0,1,1,0,'emitted','[]')",
        (DECISION_DATE, DECISION_DATE),
    )
    connection.executemany(
        "INSERT INTO eod_target_decision VALUES ('run-1','snapshot-1','ok',?,'allowed','ok')",
        [("allowed",), ("no_trigger",)],
    )
    risk_values = json.dumps({"vix": 15.2, "us_10y": 4.1, "crude_oil": 72.5})
    risk_times = json.dumps({"vix": 1_777_622_400, "us_10y": 1_777_622_400, "crude_oil": 1_777_622_400})
    risk_available = json.dumps(
        {
            "vix": "2026-08-31T07:29:00Z",
            "us_10y": "2026-08-31T07:29:00Z",
            "crude_oil": "2026-08-31T07:29:00Z",
        }
    )
    risk_sources = json.dumps({"vix": "fred", "us_10y": "fred", "crude_oil": "fred"})
    connection.execute(
        "INSERT INTO eod_risk_snapshot VALUES ('snapshot-1','run-1','2026-08-31T07:30:01Z','ok',?,?,?,?,?)",
        (risk_values, risk_times, risk_available, risk_sources, "[]"),
    )
    connection.executemany(
        "INSERT INTO notification_outbox VALUES (?,?,?,?,?,?,?)",
        [
            (
                index,
                "failed",
                1,
                "2026-08-28T07:40:00Z",
                "2026-08-28T07:50:00Z",
                None,
                "old",
            )
            for index in range(1, 7)
        ]
        + [
            (
                7,
                "sent",
                1,
                "2026-08-31T07:30:00Z",
                "2026-08-31T07:31:00Z",
                "2026-08-31T07:31:01Z",
                None,
            )
        ],
    )
    connection.commit()
    connection.close()


def _risk_state(path: Path, *, timestamp: float) -> None:
    path.write_text(
        json.dumps(
            {
                "level": "ok",
                "timestamp": timestamp,
                "vix": 15.2,
                "us_10y": 4.1,
                "crude_oil": 72.5,
                "missing_fields": [],
                "cached_fields": [],
                "sources": {
                    "vix": "fred",
                    "us_10y": "fred",
                    "crude_oil": "fred",
                },
            }
        ),
        encoding="utf-8",
    )


def _telegram_checker(path: Path, *, passing: bool = True) -> None:
    final_code = "0" if passing else "28"
    final_status = "ok" if passing else "failed"
    path.write_text(
        "#!/usr/bin/env bash\n"
        'test -z "${TELEGRAM_BOT_TOKEN:-}"\n'
        'test -z "${TELEGRAM_CHAT_ID:-}"\n'
        "echo 'telegram_http code=302 expected=302 curl_status=0'\n"
        f"echo 'telegram_socks code=302 expected=302 curl_status={final_code}'\n"
        "echo 'gstatic_http code=204 expected=204 curl_status=0'\n"
        "echo 'gstatic_socks code=204 expected=204 curl_status=0'\n"
        f"echo 'mihomo_egress_status={final_status}'\n"
        f"exit {0 if passing else 1}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _telegram_environment(path: Path) -> None:
    fake_token = "123456" + ":" + "unit-test-placeholder"
    fake_chat_id = "-" + "100123456"
    path.write_text(
        f"RADAR_ALL_PROXY=http://127.0.0.1:7890\nTELEGRAM_BOT_TOKEN={fake_token}\nTELEGRAM_CHAT_ID={fake_chat_id}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _healthy_macro_runtime(container: str) -> dict:
    return {
        "name": "macro_risk_runtime",
        "status": "pass",
        "summary": "healthy",
        "evidence": {"container": container},
    }


def _run(tmp_path: Path, *, now: datetime | None = None) -> dict:
    database = tmp_path / "swing.db"
    risk_state = tmp_path / "risk.json"
    checker = tmp_path / "telegram-check.sh"
    telegram_environment = tmp_path / "stock-radar.env"
    _database(database)
    current = now or datetime(2026, 8, 31, 15, 55, tzinfo=SHANGHAI)
    _risk_state(risk_state, timestamp=current.timestamp() - 60)
    _telegram_checker(checker)
    _telegram_environment(telegram_environment)
    return acceptance.run_acceptance(
        database=database,
        risk_state=risk_state,
        telegram_checker=checker,
        telegram_environment=telegram_environment,
        telegram_proxy="http://127.0.0.1:7890",
        macro_risk_container="radar-macro-risk",
        macro_runtime_check=_healthy_macro_runtime,
        decision_date=DECISION_DATE,
        now=current,
    )


def test_acceptance_passes_with_complete_read_only_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-reach-child")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "must-not-reach-child")
    database = tmp_path / "swing.db"
    risk_state = tmp_path / "risk.json"
    checker = tmp_path / "telegram-check.sh"
    telegram_environment = tmp_path / "stock-radar.env"
    _database(database)
    now = datetime(2026, 8, 31, 15, 55, tzinfo=SHANGHAI)
    _risk_state(risk_state, timestamp=now.timestamp() - 60)
    _telegram_checker(checker)
    _telegram_environment(telegram_environment)
    db_before = database.read_bytes()
    risk_before = risk_state.read_bytes()

    report = acceptance.run_acceptance(
        database=database,
        risk_state=risk_state,
        telegram_checker=checker,
        telegram_environment=telegram_environment,
        telegram_proxy="http://127.0.0.1:7890",
        macro_risk_container="radar-macro-risk",
        macro_runtime_check=_healthy_macro_runtime,
        decision_date=DECISION_DATE,
        now=now,
    )

    assert report["status"] == "pass"
    assert {item["name"] for item in report["checks"]} == {
        "eod",
        "timing",
        "total_amount",
        "audit",
        "outbox",
        "macro_risk",
        "macro_risk_runtime",
        "telegram",
    }
    assert all(item["status"] == "pass" for item in report["checks"])
    outbox = next(item for item in report["checks"] if item["name"] == "outbox")
    assert outbox["evidence"]["counts"]["failed"] == 6
    assert outbox["evidence"]["historical_failed_allowed"] == 6
    assert database.read_bytes() == db_before
    assert risk_state.read_bytes() == risk_before


def test_acceptance_is_not_ready_before_1552_without_reading_inputs(tmp_path):
    report = acceptance.run_acceptance(
        database=tmp_path / "missing.db",
        risk_state=tmp_path / "missing.json",
        telegram_checker=tmp_path / "missing.sh",
        telegram_environment=tmp_path / "missing.env",
        telegram_proxy="http://127.0.0.1:7890",
        macro_risk_container="radar-macro-risk",
        macro_runtime_check=_healthy_macro_runtime,
        decision_date=DECISION_DATE,
        now=datetime(2026, 8, 31, 15, 39, tzinfo=SHANGHAI),
    )

    assert report["status"] == "not_ready"
    assert report["checks"][0]["name"] == "timing"


def test_acceptance_fails_on_partial_run_and_new_outbox_failure(tmp_path):
    database = tmp_path / "swing.db"
    risk_state = tmp_path / "risk.json"
    checker = tmp_path / "telegram-check.sh"
    telegram_environment = tmp_path / "stock-radar.env"
    _database(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE swing_eod_run SET status='partial',failed_count=1,evaluated_count=1,decision_outcome='data_unavailable'"
    )
    connection.execute(
        "INSERT INTO notification_outbox VALUES (8,'failed',1,'2026-08-31T07:35:00Z','2026-08-31T07:36:00Z',NULL,'new')"
    )
    connection.commit()
    connection.close()
    now = datetime(2026, 8, 31, 15, 55, tzinfo=SHANGHAI)
    _risk_state(risk_state, timestamp=now.timestamp() - 60)
    _telegram_checker(checker)
    _telegram_environment(telegram_environment)

    report = acceptance.run_acceptance(
        database=database,
        risk_state=risk_state,
        telegram_checker=checker,
        telegram_environment=telegram_environment,
        telegram_proxy="http://127.0.0.1:7890",
        macro_risk_container="radar-macro-risk",
        macro_runtime_check=_healthy_macro_runtime,
        decision_date=DECISION_DATE,
        now=now,
    )

    assert report["status"] == "fail"
    by_name = {item["name"]: item for item in report["checks"]}
    assert by_name["eod"]["status"] == "fail"
    assert by_name["outbox"]["status"] == "fail"
    assert len(by_name["outbox"]["evidence"]["new_failed_since_local_midnight"]) == 1


def test_acceptance_fails_on_stale_cached_risk_and_telegram_probe(tmp_path):
    database = tmp_path / "swing.db"
    risk_state = tmp_path / "risk.json"
    checker = tmp_path / "telegram-check.sh"
    telegram_environment = tmp_path / "stock-radar.env"
    _database(database)
    now = datetime(2026, 8, 31, 15, 55, tzinfo=SHANGHAI)
    _risk_state(risk_state, timestamp=now.timestamp() - 901)
    payload = json.loads(risk_state.read_text(encoding="utf-8"))
    payload["cached_fields"] = ["vix"]
    risk_state.write_text(json.dumps(payload), encoding="utf-8")
    _telegram_checker(checker, passing=False)
    _telegram_environment(telegram_environment)

    report = acceptance.run_acceptance(
        database=database,
        risk_state=risk_state,
        telegram_checker=checker,
        telegram_environment=telegram_environment,
        telegram_proxy="http://127.0.0.1:7890",
        macro_risk_container="radar-macro-risk",
        macro_runtime_check=_healthy_macro_runtime,
        decision_date=DECISION_DATE,
        now=now,
    )

    by_name = {item["name"]: item for item in report["checks"]}
    assert report["status"] == "fail"
    assert by_name["macro_risk"]["status"] == "fail"
    assert by_name["telegram"]["status"] == "fail"


def test_duplicate_run_and_untrusted_total_amount_source_fail(tmp_path):
    database = tmp_path / "swing.db"
    risk_state = tmp_path / "risk.json"
    checker = tmp_path / "telegram-check.sh"
    telegram_environment = tmp_path / "stock-radar.env"
    _database(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO swing_eod_run SELECT 2,'run-2',decision_date,data_date,status,"
        "started_at,completed_at,target_count,evaluated_count,failed_count,signal_count,"
        "allowed_signal_count,blocked_signal_count,decision_outcome,reason_codes "
        "FROM swing_eod_run WHERE id=1"
    )
    extra = json.dumps(
        {
            "collect_status": {
                "total_amount": {
                    "available": True,
                    "source": "unknown",
                    "observed_date": DECISION_DATE,
                    "stale": False,
                    "anomalous": False,
                }
            }
        }
    )
    connection.execute("UPDATE macro_slice SET source='unknown',extra=?", (extra,))
    connection.commit()
    connection.close()
    now = datetime(2026, 8, 31, 15, 55, tzinfo=SHANGHAI)
    _risk_state(risk_state, timestamp=now.timestamp() - 60)
    _telegram_checker(checker)
    _telegram_environment(telegram_environment)

    report = acceptance.run_acceptance(
        database=database,
        risk_state=risk_state,
        telegram_checker=checker,
        telegram_environment=telegram_environment,
        telegram_proxy="http://127.0.0.1:7890",
        macro_risk_container="radar-macro-risk",
        macro_runtime_check=_healthy_macro_runtime,
        decision_date=DECISION_DATE,
        now=now,
    )

    by_name = {item["name"]: item for item in report["checks"]}
    assert by_name["eod"]["status"] == "fail"
    assert by_name["eod"]["evidence"]["run_count"] == 2
    assert by_name["total_amount"]["status"] == "fail"


def test_audit_rejects_target_link_to_other_snapshot(tmp_path):
    database = tmp_path / "swing.db"
    _database(database)
    connection = acceptance._open_readonly_sqlite(database)
    writable = sqlite3.connect(database)
    writable.execute("UPDATE eod_target_decision SET snapshot_id='other-snapshot'")
    writable.commit()
    writable.close()

    run = connection.execute("SELECT * FROM swing_eod_run LIMIT 1").fetchone()
    result = acceptance.inspect_audit(connection, run=run)
    connection.close()

    assert result["status"] == "fail"
    assert result["evidence"]["target_snapshot_matches_run_snapshot"] is False


def test_macro_risk_runtime_uses_read_only_docker_inspect(monkeypatch):
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "running",
                    "health": "healthy",
                    "restart_count": 0,
                    "oom_killed": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(acceptance.subprocess, "run", fake_run)

    result = acceptance.inspect_macro_risk_runtime("radar-macro-risk")

    assert result["status"] == "pass"
    assert calls[0][0][:3] == ["docker", "inspect", "--format"]
    assert calls[0][1]["check"] is False


def test_report_writer_emits_json_and_markdown(tmp_path):
    report = _run(tmp_path)

    json_path, markdown_path = acceptance.write_reports(report, tmp_path / "reports")

    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "pass"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "**PASS**" in markdown
    assert "read-only" in markdown
    assert "Reproducible evidence" in markdown
    assert "unit-test-placeholder" not in json_path.read_text(encoding="utf-8")
    assert "100123456" not in markdown


def test_source_keeps_acceptance_gate_read_only():
    source = Path(acceptance.__file__).read_text(encoding="utf-8")

    assert "mode=ro" in source
    assert "PRAGMA query_only=ON" in source
    assert "sendMessage" not in source
    assert 'method="POST"' not in source
    assert "SwingStore" not in source
    assert "systemctl restart" not in source
    assert "docker restart" not in source
    assert "docker compose" not in source
    assert "INSERT INTO" not in source
    assert "UPDATE swing_eod_run" not in source


def test_cli_defaults_reports_outside_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    args = acceptance.parse_args([])

    assert args.output_dir == Path(os.path.expanduser("~/quant-acceptance-reports"))
    assert acceptance.BASE_DIR not in args.output_dir.parents


def test_telegram_environment_rejects_broad_permissions(tmp_path):
    path = tmp_path / "stock-radar.env"
    _telegram_environment(path)
    path.chmod(0o640)

    with pytest.raises(ValueError, match="permissions"):
        acceptance._load_telegram_credentials(path)
