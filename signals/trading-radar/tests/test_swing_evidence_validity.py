from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest

from radar.notify.outbox import DurableOutboxNotifier
from radar.risk.macro_risk_control import MacroRiskControl
from radar.swing.migrations import CURRENT_SCHEMA_VERSION
from radar.swing.outcome_evaluator import SwingOutcomeEvaluator
from radar.swing.store import MacroSlice, SwingStore
from radar.swing.validity import build_validity_summary, walk_forward_windows
from scripts.check_swing_eod_terminal import inspect_terminal_audit
from scripts.check_swing_eod_window import within_eod_catchup_window
from scripts.import_swing_factor_history import (
    SOURCE,
    SOURCE_REF,
    _observation,
    _sha256,
    _verify_cached_observation,
)


def _batch_observations():
    rows = []
    for day, value in (("2026-07-24", 20_000.0), ("2026-07-25", 21_000.0)):
        rows.append(
            _observation(
                day,
                {
                    "sse_stock_amount_yi": value / 2,
                    "szse_stock_amount_yuan": value / 2 * 100_000_000,
                    "szse_stock_amount_yi": value / 2,
                    "total_amount_yi": value,
                },
            )
        )
    batch = {
        "batch_id": "trusted-history-1",
        "factor_name": "total_amount",
        "source": SOURCE,
        "source_ref": SOURCE_REF,
        "requested_start": "2026-07-24",
        "requested_end": "2026-07-25",
        "observed_at": "2026-07-28T08:00:00+08:00",
        "record_count": 2,
        "checksum": _sha256(rows),
        "validation_status": "accepted",
    }
    return batch, rows


def _decision(code: str, state: str, *, raw_count: int = 0):
    return {
        "code": code,
        "name": code,
        "sector": "测试",
        "data_status": "failed" if state == "data_failed" else "ok",
        "decision_state": state,
        "raw_trigger_count": raw_count,
        "raw_signals": [
            {"signal_type": "price", "period": "daily", "title": "test"}
            for _ in range(raw_count)
        ],
        "risk_action": (
            "blocked"
            if state == "blocked"
            else "allowed"
            if state == "allowed"
            else "not_applicable"
        ),
        "risk_level": "critical" if state == "blocked" else "ok",
        "reason_code": state,
        "data_date": "2026-07-28",
        "reference_close": 10.0,
        "validation_split": "live_oos",
    }


def test_schema_migration_factor_provenance_and_immutability(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    store.upsert(MacroSlice(date="2026-07-24", total_amount=19_000.0, source="live"))
    store.upsert(MacroSlice(date="2026-07-25", margin_balance=18_000.0, source="live"))

    batch, observations = _batch_observations()
    result = store.record_factor_import(batch=batch, observations=observations)

    assert result == {"inserted": 2, "filled": 1, "conflicts": 1}
    imported_status = store.get_for_date("2026-07-25").extra["collect_status"][
        "total_amount"
    ]
    assert imported_status["factor_import"] is True
    assert imported_status["historical_import"] is True
    assert imported_status["import_kind"] == "historical"
    with sqlite3.connect(store.db_path) as connection:
        existing, filled = connection.execute(
            "SELECT total_amount FROM macro_slice ORDER BY date"
        ).fetchall()
        assert existing[0] == 19_000.0
        assert filled[0] == 21_000.0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE factor_observation SET value=1 WHERE observation_date='2026-07-24'"
            )


def test_every_target_gets_one_immutable_decision_and_one_risk_snapshot(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    run_id = store.begin_eod_run("2026-07-28", target_count=4)
    snapshot_id = store.record_risk_snapshot(
        run_id,
        decision_at="2026-07-28T07:30:00Z",
        risk_state={
            "level": "critical",
            "vix": 21.0,
            "source_timestamps": {"vix": 1_000.0},
            "available_at": {"vix": 1_001.0},
            "sources": {"vix": "test"},
        },
    )
    identities = store.record_target_decisions(
        run_id,
        decision_at="2026-07-28T07:30:00Z",
        snapshot_id=snapshot_id,
        decisions=[
            _decision("510300", "no_trigger"),
            _decision("510500", "allowed", raw_count=1),
            _decision("159915", "blocked", raw_count=1),
            _decision("588000", "data_failed"),
        ],
    )

    assert len(identities) == 4
    assert store.get_run_evidence_counts(run_id) == {
        "target_decisions": 4,
        "risk_snapshots": 1,
    }
    assert store.decision_state_counts("2026-07-28") == {
        "no_trigger": 1,
        "allowed": 1,
        "blocked": 1,
        "data_failed": 1,
    }


class _Notifier:
    def __init__(self):
        self.calls = []

    def send_system_alert(self, title, message):
        self.calls.append((title, message))
        return True


def test_notification_outbox_is_idempotent(tmp_path):
    from tests.notify_v2_helpers import make_v6_store

    store = make_v6_store(tmp_path / "swing.db")
    base = _Notifier()
    notifier = DurableOutboxNotifier(store, base, logging.getLogger("test.outbox"))

    assert notifier.send_system_alert("audit", "missing") is True
    assert notifier.send_system_alert("audit", "missing") is True

    assert base.calls == [("audit", "missing")]
    counts = store.outbox_counts()
    # legacy queue untouched (new sends go to the v6 dispatch queue); every dispatch state
    # key is always present so a missing key can never read as "healthy".
    assert counts == {
        "pending": 0, "sending": 0, "sent": 0, "failed": 0,
        "dispatch_pending": 0, "dispatch_sending": 0, "dispatch_retry_wait": 0,
        "dispatch_confirmed": 1, "dispatch_failed_permanent": 0,
        "dispatch_failed_exhausted": 0, "dispatch_rate_limited_over_budget": 0,
        "dispatch_uncertain": 0, "dispatch_quarantined_legacy": 0,
    }

    next_run = DurableOutboxNotifier(
        store,
        base,
        logging.getLogger("test.outbox.next"),
        scope="eod:next-run",
    )
    assert next_run.send_system_alert("audit", "missing") is True
    assert base.calls == [("audit", "missing"), ("audit", "missing")]


def test_pre_v6_database_pauses_sending_instead_of_using_the_legacy_sender(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))  # schema 5: no dispatch tables
    base = _Notifier()
    notifier = DurableOutboxNotifier(store, base, logging.getLogger("test.outbox.v5"))

    assert notifier.send_system_alert("audit", "missing") is False
    assert base.calls == []
    assert store.outbox_counts() == {"pending": 0, "sending": 0, "sent": 0, "failed": 0}


class _OutcomeStore:
    def __init__(self, decision):
        self.decision = decision
        self.rows = []

    def list_decisions_for_outcome(self, *, limit):
        return [self.decision]

    def record_decision_outcome(self, outcome):
        self.rows.append(outcome)
        return True


class _Fetcher:
    def __init__(self, frames):
        self.frames = frames

    def get_historical_data(self, code, days):
        return self.frames[code]


def _daily_frame(dates, *, first_open, first_low=None):
    count = len(dates)
    opens = [first_open] + [first_open + index * 0.02 for index in range(1, count)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": [value * 1.02 for value in opens],
            "low": [first_low if first_low is not None else first_open * 0.98]
            + [value * 0.98 for value in opens[1:]],
            "close": [value * 1.01 for value in opens],
            "volume": 1_000_000,
        }
    )


def test_outcome_uses_next_session_open_and_includes_costs_gap_mfe_mae():
    sessions = pd.bdate_range("2026-07-29", periods=20)
    benchmark = _daily_frame(sessions, first_open=4.0)
    target = _daily_frame(sessions, first_open=10.2)
    store = _OutcomeStore(
        {
            "decision_id": "decision-1",
            "decision_date": "2026-07-28",
            "code": "510500",
            "reference_close": 10.0,
            "completed_horizons": set(),
        }
    )
    evaluator = SwingOutcomeEvaluator(
        store,
        _Fetcher({"510300": benchmark, "510500": target}),
    )

    summary = evaluator.evaluate_pending(as_of=sessions[-1].date().isoformat())

    assert summary["inserted"] == 5
    first = next(row for row in store.rows if row["horizon_sessions"] == 1)
    assert first["entry_date"] == "2026-07-29"
    assert first["entry_open"] == pytest.approx(10.2)
    assert first["entry_fill"] > first["entry_open"]
    assert first["gap_return"] == pytest.approx(0.02)
    assert first["net_return"] < first["gross_return"]
    assert first["fees_return"] > 0
    assert first["slippage_return"] > 0
    assert first["mfe"] > 0
    assert first["mae"] < 0


def test_outcome_marks_limit_up_without_liquidity_unfilled():
    sessions = pd.bdate_range("2026-07-29", periods=20)
    store = _OutcomeStore(
        {
            "decision_id": "decision-2",
            "decision_date": "2026-07-28",
            "code": "159915",
            "reference_close": 10.0,
            "completed_horizons": set(),
        }
    )
    evaluator = SwingOutcomeEvaluator(
        store,
        _Fetcher(
            {
                "510300": _daily_frame(sessions, first_open=4.0),
                "159915": _daily_frame(sessions, first_open=11.0, first_low=11.0),
            }
        ),
    )

    evaluator.evaluate_pending(as_of=sessions[-1].date().isoformat())

    assert len(store.rows) == 5
    assert {row["fill_status"] for row in store.rows} == {"unfilled"}
    assert {row["fill_reason"] for row in store.rows} == {
        "limit_up_no_executable_liquidity"
    }


def test_validity_hides_early_win_rate_and_reports_risk_counterfactual():
    rows = [
        {
            "validation_split": "live_oos",
            "raw_trigger_count": 1,
            "decision_state": "allowed",
            "net_return": 0.03,
            "excess_return": 0.01,
            "mfe": 0.05,
            "mae": -0.01,
        },
        {
            "validation_split": "live_oos",
            "raw_trigger_count": 1,
            "decision_state": "blocked",
            "net_return": -0.04,
            "excess_return": -0.03,
            "mfe": 0.01,
            "mae": -0.06,
        },
        {
            "validation_split": "live_oos",
            "raw_trigger_count": 1,
            "decision_state": "blocked",
            "net_return": 0.02,
            "excess_return": 0.01,
            "mfe": 0.03,
            "mae": -0.01,
        },
    ]

    summary = build_validity_summary(rows)

    assert summary["label"] == "样本不足"
    assert summary["metrics"] is None
    assert summary["risk_counterfactual"]["prevented_loss"] == pytest.approx(0.04)
    assert summary["risk_counterfactual"]["missed_upside"] == pytest.approx(0.02)


def test_walk_forward_windows_never_mix_train_and_test_dates():
    dates = pd.bdate_range("2025-01-01", periods=170).strftime("%Y-%m-%d").tolist()
    windows = list(walk_forward_windows(dates, train_sessions=120, test_sessions=20))

    assert windows
    for train, test in windows:
        assert set(train).isdisjoint(test)
        assert max(train) < min(test)


def test_future_macro_quote_is_rejected_and_valid_source_times_are_kept(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", tmp_path / "override.json")
    monkeypatch.setattr("radar.risk.macro_risk_control.time.time", lambda: 1_000.0)
    control = MacroRiskControl(logger_=logging.getLogger("test.future"))

    state = control.check(
        vix=18.0,
        us_10y=4.0,
        crude_oil=70.0,
        observation_metadata={
            "vix": {
                "source_timestamp": 2_000.0,
                "available_at": 2_000.0,
                "source": "future",
            },
            "us_10y": {
                "source_timestamp": 900.0,
                "available_at": 950.0,
                "source": "yahoo",
            },
            "crude_oil": {
                "source_timestamp": 901.0,
                "available_at": 951.0,
                "source": "yahoo",
            },
        },
    )

    assert state.level == "warning"
    assert state.vix is None
    assert state.missing_fields == ["vix"]
    assert state.source_timestamps == {"us_10y": 900.0, "crude_oil": 901.0}
    assert state.available_at == {"us_10y": 950.0, "crude_oil": 951.0}


def test_terminal_watchdog_requires_complete_immutable_evidence(tmp_path):
    now = datetime(2026, 7, 28, 7, 40, tzinfo=timezone.utc)
    missing_store = SwingStore(str(tmp_path / "missing.db"))
    assert inspect_terminal_audit(
        missing_store, decision_date="2026-07-28", now=now
    )["reason"] == "missing"

    store = SwingStore(str(tmp_path / "complete.db"))
    run_id = store.begin_eod_run("2026-07-28", target_count=2)
    store.finish_eod_run(
        run_id,
        status="success",
        decision_outcome="no_signal",
        evaluated_count=2,
    )
    assert inspect_terminal_audit(
        store, decision_date="2026-07-28", now=now
    )["reason"] == "evidence_incomplete"


def test_persistent_eod_catchup_never_runs_during_next_morning():
    assert within_eod_catchup_window(
        datetime(2026, 7, 28, 1, 59, tzinfo=timezone.utc)
    ) is False
    assert within_eod_catchup_window(
        datetime(2026, 7, 28, 7, 30, tzinfo=timezone.utc)
    ) is True
    assert within_eod_catchup_window(
        datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    ) is False


def test_official_history_observation_is_not_available_to_same_day_replay():
    observation = _observation(
        "2026-07-27",
        {
            "sse_stock_amount_yi": 10_331.08,
            "szse_stock_amount_yuan": 1_046_386_156_558.69,
            "szse_stock_amount_yi": 10_463.861566,
            "total_amount_yi": 20_794.94,
        },
    )

    assert observation["observation_date"] == "2026-07-27"
    assert observation["source_market_time"] == "2026-07-27T15:00:00+08:00"
    assert observation["available_at"] == "2026-07-28T00:00:00+08:00"
    assert len(observation["checksum"]) == 64


def test_official_history_rejects_tampered_checkpoint_observation():
    observation = _observation(
        "2026-07-27",
        {
            "sse_stock_amount_yi": 10_331.08,
            "szse_stock_amount_yuan": 1_046_386_156_558.69,
            "szse_stock_amount_yi": 10_463.861566,
            "total_amount_yi": 20_794.94,
        },
    )
    tampered = {**observation, "value": 99_999.0}

    with pytest.raises(ValueError, match="SHA-256"):
        _verify_cached_observation("2026-07-27", tampered)
