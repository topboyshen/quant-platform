"""Isolation, authentication and read-only data tests for port 8092."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime

import pytest

from dashboard import integration_app as service

TOKEN = "integration-token-abcdefghijklmnopqrstuvwxyz"


def _configure_token_file(monkeypatch, tmp_path, token=TOKEN):
    path = tmp_path / "integration-token"
    path.write_text(f"{token}\n", encoding="ascii")
    path.chmod(0o600)
    monkeypatch.setenv("RADAR_INTEGRATION_TOKEN_FILE", str(path))
    return path


def _overview() -> dict:
    return {
        "available": True,
        "updated": "2026-07-20",
        "source": "test-source",
        "limit_up": 42,
        "limit_down": 3,
        "margin_balance": 18_000.5,
        "error": "/private/path/must-not-leak",
        "targets": [{"code": "private-watchlist"}],
        "strategy": {
            "role": "core_trend_swing_eod",
            "decision_time": "15:10 Asia/Shanghai",
            "ma_periods": [20, 60],
            "volume_breakout_ratio": 1.5,
            "private_config": "must-not-leak",
        },
        "factors": [
            {
                "factor": "total_amount",
                "label": "全市场成交额",
                "unit": "亿元",
                "value": math.nan,
                "value_date": "2026-07-20",
                "is_stale": False,
                "percentile": 82.0,
                "level": "正常",
                "sample_size": 200,
                "history_ready": True,
                "collect_available": True,
                "collect_source": "eastmoney",
                "collect_message": "internal collection details",
            }
        ],
        "signals": [
            {
                "decision_date": "2026-07-20",
                "data_date": "2026-07-17",
                "code": "510300",
                "name": "沪深300ETF",
                "signal_type": "weekly_macd_golden_cross",
                "period": "W",
                "title": "周线 MACD 金叉",
                "confidence": 0.72,
                "detail": {"private_model_input": True},
            }
        ],
        "latest_decision_date": "2026-07-20",
        "recent_signals": [],
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("RADAR_INTEGRATION_TOKEN", raising=False)
    monkeypatch.delenv("RADAR_INTEGRATION_TOKEN_FILE", raising=False)
    monkeypatch.setattr(service, "_load_market_overview", _overview)
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    monkeypatch.setattr(
        service,
        "_load_persisted_risk_state",
        lambda: {
            "level": "normal",
            "timestamp": time.time(),
            "missing_fields": [],
            "cached_fields": ["vix"],
            "vix": 15.2,
            "us_10y": 4.1,
            "crude_oil": 72.5,
            "secret": "must-not-leak",
        },
    )
    service.app.config.update(TESTING=True)
    return service.app.test_client()


def test_route_table_contains_only_liveness_readiness_and_market_context():
    rules = {rule.rule: rule for rule in service.app.url_map.iter_rules()}
    assert set(rules) == {"/health", "/ready", "/integration/v1/market-context"}
    assert rules["/integration/v1/market-context"].methods == {"GET", "HEAD"}
    assert rules["/health"].methods == {"GET", "HEAD"}
    assert rules["/ready"].methods == {"GET", "HEAD"}


def test_dashboard_routes_are_not_reachable(client, monkeypatch, tmp_path):
    _configure_token_file(monkeypatch, tmp_path)
    assert client.get("/").status_code == 404
    assert client.get("/api/portfolio").status_code == 404
    assert client.get("/api/logs").status_code == 404
    assert client.get("/api/stream").status_code == 404


def test_health_is_liveness_and_ready_is_dependency_gate(client, monkeypatch, tmp_path):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
    _configure_token_file(monkeypatch, tmp_path, token="too-short")
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
    _configure_token_file(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "_readiness_ok", lambda: True)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {
        "service": "quant-integration-api",
        "status": "ok",
    }
    assert client.get("/ready").get_json()["status"] == "ready"


def test_market_context_fails_closed_before_reading_data(client, monkeypatch):
    called = False

    def must_not_load():
        nonlocal called
        called = True
        raise AssertionError("data must not load before authentication")

    monkeypatch.setattr(service, "_load_market_overview", must_not_load)
    response = client.get("/integration/v1/market-context")
    assert response.status_code == 503
    assert response.get_json() == {"error": "service_unavailable"}
    assert called is False


def test_market_context_rejects_query_and_wrong_tokens(client, monkeypatch, tmp_path):
    _configure_token_file(monkeypatch, tmp_path)
    query_response = client.get(f"/integration/v1/market-context?token={TOKEN}")
    assert query_response.status_code == 401
    assert query_response.headers["WWW-Authenticate"] == (
        'Bearer realm="quant-integration"'
    )

    wrong_response = client.get(
        "/integration/v1/market-context",
        headers={"Authorization": "Bearer definitely-wrong"},
    )
    assert wrong_response.status_code == 401


def test_auth_uses_constant_time_comparison(client, monkeypatch, tmp_path):
    _configure_token_file(monkeypatch, tmp_path)
    calls = []

    def compare_digest(left, right):
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(service.service_auth.hmac, "compare_digest", compare_digest)
    response = client.get(
        "/integration/v1/market-context",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert calls == [(TOKEN.encode(), TOKEN.encode())]


def test_market_context_is_header_only_and_whitelisted(client, monkeypatch, tmp_path):
    _configure_token_file(monkeypatch, tmp_path)
    response = client.get(
        "/integration/v1/market-context",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    payload = response.get_json()
    assert payload["schema_version"] == "quant.market-context.v1"
    assert payload["status"] == "degraded"
    assert payload["market"]["expected_as_of"] == "2026-07-20"
    assert payload["market"]["factors"][0]["value"] is None
    assert payload["signals"]["current"][0]["code"] == "510300"
    assert payload["signals"]["current"][0]["decision_date"] == "2026-07-20"
    assert payload["signals"]["current"][0]["data_date"] == "2026-07-17"
    assert payload["risk"]["vix"] == 15.2

    serialized = response.get_data(as_text=True)
    for forbidden in (
        "must-not-leak",
        "private-watchlist",
        "private_model_input",
        "internal collection details",
    ):
        assert forbidden not in serialized
    assert "NaN" not in serialized


def test_non_get_methods_are_not_exposed(client, monkeypatch, tmp_path):
    _configure_token_file(monkeypatch, tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert (
        client.post("/integration/v1/market-context", headers=headers).status_code
        == 405
    )
    assert (
        client.options("/integration/v1/market-context", headers=headers).status_code
        == 405
    )


def test_internal_read_failures_return_degraded_contract(client, monkeypatch, tmp_path):
    _configure_token_file(monkeypatch, tmp_path)

    def fail():
        raise RuntimeError("internal path must not leak")

    monkeypatch.setattr(service, "_load_market_overview", fail)
    monkeypatch.setattr(service, "_load_persisted_risk_state", fail)
    response = client.get(
        "/integration/v1/market-context",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.get_json()["quality"]["degraded_reasons"] == [
        "market_context_unavailable",
        "risk_snapshot_unavailable",
    ]
    assert "internal path" not in response.get_data(as_text=True)


def _create_swing_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE macro_slice (
            date TEXT PRIMARY KEY,
            total_amount REAL,
            margin_balance REAL,
            margin_growth_pct REAL,
            limit_up INTEGER,
            limit_down INTEGER,
            limit_ratio REAL,
            source TEXT,
            extra TEXT
        );
        CREATE TABLE trend_signal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            signal_type TEXT,
            period TEXT,
            data_date TEXT,
            title TEXT,
            confidence REAL
        );
        """
    )
    for day in range(1, 23):
        date_value = f"2026-06-{day:02d}"
        connection.execute(
            "INSERT INTO macro_slice VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date_value,
                10_000.0 + day,
                18_000.0 + day,
                0.01 * day,
                20 + day,
                day,
                (20 + day) / (day + 1),
                "fixture",
                json.dumps({"collect_status": {}}, ensure_ascii=False),
            ),
        )
    connection.execute(
        "INSERT INTO trend_signal "
        "(date, code, name, signal_type, period, data_date, title, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-06-22",
            "510300",
            "沪深300ETF",
            "volume_breakout_ma60",
            "D",
            "2026-06-22",
            "放量突破 MA60",
            0.71,
        ),
    )
    connection.commit()
    connection.close()


def test_market_reader_uses_readonly_sqlite_without_mutating_database(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "swing_macro.db"
    _create_swing_database(db_path)
    (tmp_path / "config.swing.json").write_text(
        json.dumps(
            {
                "swing": {
                    "store_file": "data/swing_macro.db",
                    "macro": {"min_history_days": 20},
                    "ma_periods": [20, 60],
                }
            }
        ),
        encoding="utf-8",
    )
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    monkeypatch.setattr(service, "BASE_DIR", tmp_path)
    original_load_factor = service._load_factor
    snapshot_checks = []

    def load_factor_in_snapshot(connection, *args, **kwargs):
        snapshot_checks.append(connection.in_transaction)
        return original_load_factor(connection, *args, **kwargs)

    monkeypatch.setattr(service, "_load_factor", load_factor_in_snapshot)

    overview = service._load_market_overview()

    assert overview["available"] is True
    assert overview["updated"] == "2026-06-22"
    assert overview["limit_up"] == 42
    assert overview["signals"][0]["code"] == "510300"
    assert overview["signals"][0]["decision_date"] == "2026-06-22"
    assert overview["signals"][0]["data_date"] == "2026-06-22"
    assert all(factor["history_ready"] for factor in overview["factors"])
    assert snapshot_checks and all(snapshot_checks)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    with closing(service._open_readonly_sqlite(db_path)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM macro_slice")

    _configure_token_file(monkeypatch, tmp_path)
    assert service._readiness_ok() is True
    assert service.app.test_client().get("/ready").status_code == 200

    writable = sqlite3.connect(db_path)
    writable.executescript(
        """
        DROP TABLE trend_signal;
        CREATE TABLE trend_signal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            signal_type TEXT,
            period TEXT,
            title TEXT,
            confidence REAL
        );
        """
    )
    writable.commit()
    writable.close()
    assert service._readiness_ok() is False
    assert service.app.test_client().get("/ready").status_code == 503


def test_swing_database_path_cannot_escape_readonly_data_mount(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "outside.db"
    sqlite3.connect(outside).close()
    monkeypatch.setattr(service, "BASE_DIR", tmp_path)
    assert service._resolve_swing_db({"swing": {"store_file": "outside.db"}}) is None


def test_missing_risk_file_is_unavailable_not_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "BASE_DIR", tmp_path)
    monkeypatch.delenv("RADAR_RISK_STATE_FILE", raising=False)
    assert service._load_persisted_risk_state() == {}
    risk = service._clean_risk({})
    assert risk["available"] is False
    assert risk["data_complete"] is False


def test_fresh_complete_context_is_ok(monkeypatch):
    now = time.time()
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    risk = {
        "level": "normal",
        "timestamp": now,
        "age_seconds": 5,
        "missing_fields": [],
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }
    payload = service._build_market_context(overview, risk)
    assert payload["status"] == "ok"
    assert payload["quality"]["degraded_reasons"] == []


def _daily_audit(*, status="success", outcome="no_signal", signal_count=0):
    return {
        "decision_date": "2026-07-20",
        "data_date": "2026-07-20",
        "run": {
            "run_id": "a" * 32,
            "status": status,
            "started_at": "2026-07-20T07:10:00Z",
            "completed_at": (
                None if status == "running" else "2026-07-20T07:11:00Z"
            ),
            "reason_codes": [],
        },
        "decision": {
            "outcome": outcome,
            "signal_count": signal_count,
            "allowed_signal_count": signal_count,
            "blocked_signal_count": 0,
        },
        "coverage": {
            "target_count": 10,
            "evaluated_count": 10 if status == "success" else 0,
            "failed_count": 0 if status == "success" else 10,
        },
    }


def test_market_context_adds_successful_no_signal_daily_audit(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    overview["signals"] = []
    overview["daily_audit"] = _daily_audit()
    risk = {
        "level": "normal",
        "timestamp": time.time(),
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }

    payload = service._build_market_context(overview, risk)

    assert payload["signals"]["daily_audit"]["run"]["status"] == "success"
    assert payload["signals"]["daily_audit"]["decision"] == {
        "outcome": "no_signal",
        "signal_count": 0,
        "allowed_signal_count": 0,
        "blocked_signal_count": 0,
    }
    assert payload["signals"]["latest_signal_date"] == "2026-07-20"


def test_run_before_decision_window_is_operational_only(monkeypatch):
    item = _daily_audit(status="partial", outcome="data_unavailable")
    item["run"]["started_at"] = "2026-07-20T01:59:01Z"
    item["run"]["completed_at"] = "2026-07-20T01:59:23Z"
    item["coverage"].update({"evaluated_count": 10, "failed_count": 0})

    cleaned = service._clean_daily_audit(
        {
            "run_id": item["run"]["run_id"],
            "decision_date": item["decision_date"],
            "data_date": item["data_date"],
            "status": item["run"]["status"],
            "started_at": item["run"]["started_at"],
            "completed_at": item["run"]["completed_at"],
            "target_count": item["coverage"]["target_count"],
            "evaluated_count": item["coverage"]["evaluated_count"],
            "failed_count": item["coverage"]["failed_count"],
            "signal_count": 0,
            "allowed_signal_count": 0,
            "blocked_signal_count": 0,
            "decision_outcome": item["decision"]["outcome"],
            "reason_codes": "[]",
        },
        decision_time="15:30 Asia/Shanghai",
    )

    assert cleaned is not None
    assert cleaned["run"]["decision_eligible"] is False
    assert cleaned["run"]["eligibility_reason"] == "before_decision_window"


def test_outside_window_audit_has_its_own_quality_reason(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    overview["daily_audit"] = _daily_audit(
        status="partial", outcome="data_unavailable"
    )
    overview["daily_audit"]["run"].update(
        {
            "decision_eligible": False,
            "eligibility_reason": "before_decision_window",
        }
    )
    risk = {
        "level": "normal",
        "timestamp": time.time(),
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }

    payload = service._build_market_context(overview, risk)

    reasons = payload["quality"]["degraded_reasons"]
    assert "swing_audit_outside_decision_window" in reasons
    assert "swing_audit_partial" not in reasons


def test_effectiveness_summary_is_additive_and_hides_premature_metrics(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    overview["effectiveness"] = {
        "state": "sample_insufficient",
        "sample_size": 0,
        "minimum_samples": 30,
        "primary_horizon_sessions": 20,
        "method": "live_oos_or_walk_forward_oos",
        "metrics_available": False,
        "horizon_samples": {"1": 0, "3": 0, "5": 0, "10": 0, "20": 0},
        "blocked_counterfactual_sample_size": 0,
    }

    payload = service._build_market_context(
        overview,
        {
            "level": "normal",
            "timestamp": time.time(),
            "vix": 15.2,
            "us_10y": 4.1,
            "crude_oil": 72.5,
        },
    )

    assert payload["signals"]["effectiveness"]["state"] == "sample_insufficient"
    assert payload["signals"]["effectiveness"]["metrics_available"] is False
    assert "metrics" not in payload["signals"]["effectiveness"]


def test_partial_or_failed_daily_audit_degrades_without_hiding_market(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    overview["daily_audit"] = _daily_audit(
        status="failed", outcome="data_unavailable"
    )
    risk = {
        "level": "normal",
        "timestamp": time.time(),
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }

    payload = service._build_market_context(overview, risk)

    assert payload["market"]["available"] is True
    assert "swing_audit_failed" in payload["quality"]["degraded_reasons"]


def test_load_daily_audit_rejects_inconsistent_no_signal_row():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
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
        )
        """
    )
    connection.execute(
        "INSERT INTO swing_eod_run VALUES "
        "(1, ?, '2026-07-20', '2026-07-20', 'success', ?, ?, 10, 10, 0, 1, 1, 0, "
        "'no_signal', '[]')",
        ("b" * 32, "2026-07-20T07:10:00Z", "2026-07-20T07:11:00Z"),
    )

    audit, invalid = service._load_daily_audit(connection)

    assert audit is None
    assert invalid is True
    connection.close()


def test_effectiveness_excludes_partial_and_pre_window_decisions():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE swing_eod_run (
            run_id TEXT PRIMARY KEY,
            decision_date TEXT,
            status TEXT,
            started_at TEXT
        );
        CREATE TABLE eod_target_decision (
            decision_id TEXT PRIMARY KEY,
            run_id TEXT,
            decision_date TEXT,
            decision_at TEXT,
            decision_state TEXT,
            raw_trigger_count INTEGER,
            validation_split TEXT,
            code TEXT
        );
        CREATE TABLE eod_decision_outcome (
            decision_id TEXT,
            horizon_sessions INTEGER,
            net_return REAL,
            excess_return REAL,
            mfe REAL,
            mae REAL,
            fill_status TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO swing_eod_run VALUES (?,?,?,?)",
        [
            ("early", "2026-07-20", "success", "2026-07-20T01:59:01Z"),
            (
                "formal-partial",
                "2026-07-20",
                "partial",
                "2026-07-20T07:30:01Z",
            ),
            ("formal", "2026-07-20", "success", "2026-07-20T07:30:01Z"),
        ],
    )
    connection.executemany(
        "INSERT INTO eod_target_decision VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "early-decision",
                "early",
                "2026-07-20",
                "2026-07-20T01:59:05Z",
                "allowed",
                1,
                "live_oos",
                "510300",
            ),
            (
                "formal-partial-decision",
                "formal-partial",
                "2026-07-20",
                "2026-07-20T07:30:05Z",
                "allowed",
                1,
                "live_oos",
                "510050",
            ),
            (
                "formal-decision",
                "formal",
                "2026-07-20",
                "2026-07-20T07:30:05Z",
                "allowed",
                1,
                "live_oos",
                "510500",
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO eod_decision_outcome VALUES (?,?,?,?,?,?,?)",
        [
            ("early-decision", 20, 0.9, 0.8, 0.9, -0.01, "filled"),
            (
                "formal-partial-decision",
                20,
                0.8,
                0.7,
                0.8,
                -0.01,
                "filled",
            ),
            ("formal-decision", 20, 0.02, 0.01, 0.03, -0.01, "filled"),
        ],
    )

    summary, invalid = service._load_effectiveness(
        connection,
        validity_config={"min_samples": 30, "primary_horizon_sessions": 20},
        decision_time="15:30 Asia/Shanghai",
    )

    assert invalid is False
    assert summary == {
        "state": "sample_insufficient",
        "sample_size": 1,
        "minimum_samples": 30,
        "primary_horizon_sessions": 20,
        "method": "live_oos_or_walk_forward_oos",
        "metrics_available": False,
        "horizon_samples": {"1": 0, "3": 0, "5": 0, "10": 0, "20": 1},
        "blocked_counterfactual_sample_size": 0,
    }
    connection.close()


def test_load_market_overview_keeps_failed_audit_without_macro_slice(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "audit-only.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
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
        )
        """
    )
    connection.execute(
        "INSERT INTO swing_eod_run VALUES "
        "(1, ?, '2026-07-20', '2026-07-20', 'failed', ?, ?, 10, 0, 10, 0, 0, 0, "
        "'data_unavailable', '[\"trend_data_unavailable\"]')",
        ("c" * 32, "2026-07-20T07:10:00Z", "2026-07-20T07:11:00Z"),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(service, "_read_json_file", lambda _path: {"swing": {}})
    monkeypatch.setattr(service, "_resolve_swing_db", lambda _config: db_path)

    overview = service._load_market_overview()

    assert overview["available"] is False
    assert overview["daily_audit"]["run"]["status"] == "failed"
    assert overview["daily_audit_invalid"] is False


def test_invalid_audit_marker_reaches_consumer_contract(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    overview["daily_audit_invalid"] = True
    risk = {
        "level": "normal",
        "timestamp": time.time(),
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }

    payload = service._build_market_context(overview, risk)

    assert payload["signals"]["daily_audit"] == {"invalid": True}
    assert "swing_audit_invalid" in payload["quality"]["degraded_reasons"]


def test_stale_market_and_insufficient_factor_history_are_degraded(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["updated"] = "2026-07-10"
    overview["factors"][0]["value"] = 10_000.0
    overview["factors"][0]["history_ready"] = False
    risk = {
        "level": "normal",
        "timestamp": time.time(),
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }
    payload = service._build_market_context(overview, risk)
    assert payload["market"]["is_stale"] is True
    assert payload["market"]["age_days"] == 10
    assert "market_context_stale" in payload["quality"]["degraded_reasons"]
    assert "factor_history_insufficient" in payload["quality"]["degraded_reasons"]


def test_future_risk_timestamp_is_degraded(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    now = 1_800_000_000.0
    monkeypatch.setattr(service.time, "time", lambda: now)
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    risk = {
        "level": "normal",
        "timestamp": now + service.RISK_FUTURE_SKEW_SECONDS + 1,
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }
    payload = service._build_market_context(overview, risk)
    assert payload["risk"]["is_future"] is True
    assert payload["status"] == "degraded"
    assert "risk_snapshot_from_future" in payload["quality"]["degraded_reasons"]


class _FixtureHolidayCalendar:
    HOLIDAYS_2026 = {
        "09-25",
        "09-26",
        "09-27",
        "10-01",
        "10-02",
        "10-03",
        "10-04",
        "10-05",
        "10-06",
        "10-07",
    }

    @classmethod
    def is_holiday(cls, value):
        return value.strftime("%m-%d") in cls.HOLIDAYS_2026


@pytest.mark.parametrize(
    ("now", "expected"),
    (
        (datetime(2026, 7, 20, 15, 29, tzinfo=service.SHANGHAI_TZ), "2026-07-17"),
        (datetime(2026, 7, 20, 15, 30, tzinfo=service.SHANGHAI_TZ), "2026-07-20"),
        (datetime(2026, 7, 25, 12, 0, tzinfo=service.SHANGHAI_TZ), "2026-07-24"),
        (datetime(2026, 10, 7, 12, 0, tzinfo=service.SHANGHAI_TZ), "2026-09-30"),
    ),
)
def test_expected_market_date_uses_close_grace_weekends_and_long_holidays(
    now, expected, monkeypatch
):
    monkeypatch.setattr(
        service, "_holiday_calendar_class", lambda: _FixtureHolidayCalendar
    )
    monkeypatch.delenv(service.MARKET_EOD_GRACE_MINUTES_ENV, raising=False)
    assert service._expected_market_as_of(now) == expected


def test_long_holiday_age_is_not_itself_stale(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 10, 7))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-09-30")
    freshness = service._market_freshness("2026-09-30")
    assert freshness == {
        "age_days": 7,
        "expected_as_of": "2026-09-30",
        "calendar_available": True,
        "is_stale": False,
    }


def test_expected_market_date_fails_closed_when_calendar_is_unavailable(monkeypatch):
    monkeypatch.setattr(service, "_holiday_calendar_class", lambda: None)
    assert (
        service._expected_market_as_of(
            datetime(2026, 7, 20, 16, 0, tzinfo=service.SHANGHAI_TZ)
        )
        is None
    )


def test_unavailable_market_calendar_fails_neutral(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: None)
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    risk = {
        "level": "normal",
        "timestamp": time.time(),
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }
    payload = service._build_market_context(overview, risk)
    assert payload["market"]["expected_as_of"] is None
    assert payload["market"]["is_stale"] is True
    assert "market_calendar_unavailable" in payload["quality"]["degraded_reasons"]


def test_risk_quality_reports_incomplete_and_future_independently(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(service.time, "time", lambda: now)
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    risk = {
        "level": "normal",
        "timestamp": now + service.RISK_FUTURE_SKEW_SECONDS + 1,
        "missing_fields": ["vix"],
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 72.5,
    }
    payload = service._build_market_context(overview, risk)
    reasons = payload["quality"]["degraded_reasons"]
    assert "risk_snapshot_incomplete" in reasons
    assert "risk_snapshot_from_future" in reasons


def test_builder_canonicalizes_signal_dates_at_contract_boundary(monkeypatch):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    monkeypatch.setattr(service, "_expected_market_as_of", lambda: "2026-07-20")
    overview = _overview()
    overview["factors"][0]["value"] = 10_000.0
    mismatched = dict(overview["signals"][0], decision_date="2026-07-19")
    overview["signals"] = [mismatched]
    overview["recent_signals"] = [mismatched]
    overview["latest_decision_date"] = "2099-01-01"
    payload = service._build_market_context(
        overview,
        {
            "level": "normal",
            "timestamp": time.time(),
            "vix": 15.2,
            "us_10y": 4.1,
            "crude_oil": 72.5,
        },
    )
    assert payload["signals"]["current"] == []
    assert payload["signals"]["latest_decision_date"] == "2026-07-19"
    assert "signal_contract_invalid" in payload["quality"]["degraded_reasons"]


@pytest.mark.parametrize(
    ("decision_date", "data_date"),
    [
        ("not-a-date", "2026-07-20"),
        ("2026-07-20", "not-a-date"),
        ("2026-07-19", "2026-07-20"),
        ("2099-01-01", "2099-01-01"),
    ],
)
def test_signal_dates_must_be_valid_and_causally_ordered(
    decision_date, data_date, monkeypatch
):
    monkeypatch.setattr(service, "_today_shanghai", lambda: date(2026, 7, 20))
    assert (
        service._clean_signal(
            {
                "decision_date": decision_date,
                "data_date": data_date,
                "code": "510300",
            }
        )
        is None
    )
