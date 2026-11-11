from __future__ import annotations

import sqlite3

import pytest

from radar.swing.store import SwingStore


def test_eod_run_records_successful_no_signal_decision(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    run_id = store.begin_eod_run("2026-07-28", target_count=10)

    store.finish_eod_run(
        run_id,
        status="success",
        decision_outcome="no_signal",
        data_date="2026-07-28",
        evaluated_count=10,
        failed_count=0,
        signal_count=0,
    )

    audit = store.get_latest_eod_run()
    assert audit is not None
    assert audit["decision_date"] == "2026-07-28"
    assert audit["status"] == "success"
    assert audit["decision_outcome"] == "no_signal"
    assert audit["evaluated_count"] == 10
    assert audit["completed_at"].endswith("Z")


def test_eod_run_is_append_only_across_same_day_retries(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    first = store.begin_eod_run("2026-07-28", target_count=10)
    store.finish_eod_run(
        first,
        status="failed",
        decision_outcome="data_unavailable",
        evaluated_count=0,
        failed_count=10,
        reason_codes=["trend_data_unavailable"],
    )
    second = store.begin_eod_run("2026-07-28", target_count=10)
    store.finish_eod_run(
        second,
        status="success",
        decision_outcome="emitted",
        data_date="2026-07-28",
        evaluated_count=10,
        signal_count=2,
        allowed_signal_count=2,
    )

    latest = store.get_latest_eod_run()
    assert latest is not None
    assert latest["run_id"] == second
    assert latest["signal_count"] == 2
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM swing_eod_run").fetchone()[0] == 2


def test_completed_eod_run_cannot_be_rewritten(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    run_id = store.begin_eod_run("2026-07-28", target_count=1)
    store.finish_eod_run(
        run_id,
        status="success",
        decision_outcome="no_signal",
        evaluated_count=1,
    )

    with pytest.raises(RuntimeError, match="已完成"):
        store.finish_eod_run(
            run_id,
            status="failed",
            decision_outcome="data_unavailable",
            failed_count=1,
        )


@pytest.mark.parametrize(
    ("decision_date", "target_count"),
    [("2026-7-28", 10), ("2026-07-28", -1), ("2026-07-28", True)],
)
def test_eod_run_rejects_invalid_contract(tmp_path, decision_date, target_count):
    store = SwingStore(str(tmp_path / "swing.db"))
    with pytest.raises(ValueError):
        store.begin_eod_run(decision_date, target_count=target_count)


def test_eod_run_rejects_false_success_and_inconsistent_signal_counts(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    incomplete = store.begin_eod_run("2026-07-28", target_count=10)
    with pytest.raises(ValueError, match="完整检查"):
        store.finish_eod_run(
            incomplete,
            status="success",
            decision_outcome="no_signal",
            evaluated_count=9,
            failed_count=1,
        )

    inconsistent = store.begin_eod_run("2026-07-28", target_count=10)
    with pytest.raises(ValueError, match="放行/拦截"):
        store.finish_eod_run(
            inconsistent,
            status="success",
            decision_outcome="emitted",
            evaluated_count=10,
            signal_count=2,
            allowed_signal_count=1,
            blocked_signal_count=0,
        )
