from __future__ import annotations

import json
import sqlite3
import stat
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import radar_swing_eod
from radar.swing.store import MacroSlice, SwingStore
from scripts import repair_swing_total_amount as repair

BEIJING = ZoneInfo("Asia/Shanghai")
TRADING_AFTER_REPAIR = datetime(2026, 8, 28, 15, 46, tzinfo=BEIJING)


def _config(tmp_path: Path) -> tuple[Path, SwingStore]:
    database = tmp_path / "swing.db"
    config = tmp_path / "config.swing.json"
    config.write_text(
        json.dumps({"swing": {"store_file": str(database)}}), encoding="utf-8"
    )
    return config, SwingStore(str(database))


def _finish_run(
    store: SwingStore,
    *,
    day: str = "2026-08-28",
    status: str = "partial",
    reasons: list[str] | None = None,
) -> str:
    run_id = store.begin_eod_run(day, target_count=1)
    decision_at = f"{day}T15:30:00+08:00"
    snapshot_id = store.record_risk_snapshot(
        run_id,
        decision_at=decision_at,
        risk_state={"level": "unavailable", "missing_fields": ["total_amount"]},
    )
    store.record_target_decisions(
        run_id,
        decision_at=decision_at,
        snapshot_id=snapshot_id,
        decisions=[
            {
                "code": "510300",
                "name": "沪深300ETF",
                "sector": "宽基",
                "data_status": "ok",
                "decision_state": "no_trigger",
                "raw_trigger_count": 0,
                "raw_signals": [],
                "risk_action": "not_applicable",
                "risk_level": "unavailable",
                "reason_code": "no_trigger",
                "data_date": day,
                "reference_close": 4.2,
                "validation_split": "live_oos",
            }
        ],
    )
    store.finish_eod_run(
        run_id,
        status=status,
        decision_outcome=("no_signal" if status == "success" else "data_unavailable"),
        data_date=day,
        evaluated_count=1,
        reason_codes=reasons if reasons is not None else ["macro_data_partial"],
    )
    return run_id


def _eligible_store(tmp_path: Path) -> tuple[Path, SwingStore, str]:
    config, store = _config(tmp_path)
    store.upsert(
        MacroSlice(
            date="2026-08-28",
            total_amount=None,
            margin_balance=19_000.0,
            source="akshare",
        )
    )
    run_id = _finish_run(store)
    return config, store, run_id


def _usable_fetch(calls: list[str]):
    def fetch(decision_date: str):
        calls.append(decision_date)
        return {
            "usable": True,
            "value": 21_017.15,
            "source": "tencent",
            "status": {
                "available": True,
                "source": "tencent",
                "observed_date": decision_date,
                "date_evidence": repair.TENCENT_DATE_EVIDENCE,
                "stale": False,
                "anomalous": False,
            },
        }

    return fetch


@pytest.fixture(autouse=True)
def _trading_day(monkeypatch):
    monkeypatch.setattr(
        repair.HolidayCalendar, "is_holiday", classmethod(lambda cls, value: False)
    )


def _eod_evidence(database: str) -> dict[str, list[tuple]]:
    with sqlite3.connect(database) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "swing_eod_run",
                "eod_risk_snapshot",
                "eod_target_decision",
            )
        }


def _factor_counts(database: str) -> tuple[int, int]:
    with sqlite3.connect(database) as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM factor_import_batch").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM factor_observation").fetchone()[0],
        )


def test_eligible_repair_appends_factor_evidence_and_never_rewrites_eod(tmp_path):
    config, store, run_id = _eligible_store(tmp_path)
    before = _eod_evidence(store.db_path)
    calls: list[str] = []

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=_usable_fetch(calls),
    )

    assert result == {
        "ok": True,
        "action": "repaired",
        "reason": "eligible_partial_macro_data",
        "decision_date": "2026-08-28",
        "run_id": run_id,
        "batch_id": result["batch_id"],
        "value": 21_017.15,
        "source": "tencent",
        "dry_run": False,
        "inserted": 1,
        "filled": 1,
        "conflicts": 0,
    }
    assert calls == ["2026-08-28"]
    repaired_slice = store.get_for_date("2026-08-28")
    assert repaired_slice.total_amount == 21_017.15
    repaired_status = repaired_slice.extra["collect_status"]["total_amount"]
    assert repaired_status["factor_import"] is True
    assert repaired_status["historical_import"] is False
    assert repaired_status["import_kind"] == "same_day_stale_only_repair"
    assert _eod_evidence(store.db_path) == before
    with sqlite3.connect(store.db_path) as connection:
        batch = connection.execute(
            "SELECT factor_name,source,validation_status,details "
            "FROM factor_import_batch"
        ).fetchone()
        observation = connection.execute(
            "SELECT factor_name,observation_date,value,source,checksum "
            "FROM factor_observation"
        ).fetchone()
    assert batch[:3] == ("total_amount", "tencent", "accepted")
    details = json.loads(batch[3])
    assert details["import_kind"] == "same_day_stale_only_repair"
    assert details["qualifying_run_id"] == run_id
    assert details["recomputes_strategy"] is False
    assert details["sends_notification"] is False
    assert observation[:4] == (
        "total_amount",
        "2026-08-28",
        21_017.15,
        "tencent",
    )
    assert len(observation[4]) == 64


def test_second_execution_is_no_op_and_does_not_fetch(tmp_path):
    config, store, _ = _eligible_store(tmp_path)
    calls: list[str] = []
    fetch = _usable_fetch(calls)
    first = repair.run(
        str(config), now=TRADING_AFTER_REPAIR, fetch_total_amount=fetch
    )
    second = repair.run(
        str(config), now=TRADING_AFTER_REPAIR, fetch_total_amount=fetch
    )

    assert first["action"] == "repaired"
    assert second["action"] == "no_op"
    assert second["reason"] == "total_amount_present"
    assert calls == ["2026-08-28"]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM factor_import_batch"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM factor_observation"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("setup", "reason"),
    (
        ("missing_run", "eod_run_missing"),
        ("success", "latest_eod_not_partial"),
        ("wrong_reason", "macro_data_partial_absent"),
        ("missing_slice", "macro_slice_missing"),
        ("already_present", "total_amount_present"),
    ),
)
def test_preconditions_no_op_before_fetch(tmp_path, setup, reason):
    config, store = _config(tmp_path)
    if setup != "missing_slice":
        store.upsert(
            MacroSlice(
                date="2026-08-28",
                total_amount=18_888.0 if setup == "already_present" else None,
            )
        )
    if setup == "success":
        _finish_run(store, status="success", reasons=[])
    elif setup == "wrong_reason":
        _finish_run(store, reasons=["target_data_partial"])
    elif setup not in {"missing_run"}:
        _finish_run(store)
    calls: list[str] = []

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=_usable_fetch(calls),
    )

    assert result["action"] == "no_op"
    assert result["reason"] == reason
    assert calls == []


def test_latest_run_must_qualify_even_if_older_partial_exists(tmp_path):
    config, store, _ = _eligible_store(tmp_path)
    _finish_run(store, status="success", reasons=[])
    calls: list[str] = []

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=_usable_fetch(calls),
    )

    assert result["reason"] == "latest_eod_not_partial"
    assert calls == []


@pytest.mark.parametrize(
    ("now", "reason"),
    (
        (datetime(2026, 8, 29, 15, 55, tzinfo=BEIJING), "non_trading_day"),
        (datetime(2026, 8, 28, 15, 44, 59, tzinfo=BEIJING), "too_early"),
        (datetime(2026, 8, 28, 16, 0, 0, tzinfo=BEIJING), "too_late"),
    ),
)
def test_outside_initial_window_is_no_op_before_database_or_fetch(
    tmp_path, now, reason
):
    config = tmp_path / "missing-config.json"
    calls: list[str] = []

    result = repair.run(
        str(config), now=now, fetch_total_amount=_usable_fetch(calls)
    )

    assert result["reason"] == reason
    assert calls == []


def test_holiday_is_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(
        repair.HolidayCalendar, "is_holiday", classmethod(lambda cls, value: True)
    )
    calls: list[str] = []

    result = repair.run(
        str(tmp_path / "missing.json"),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=_usable_fetch(calls),
    )

    assert result["reason"] == "non_trading_day"
    assert calls == []


def test_dry_run_uses_read_only_db_and_never_initializes_store(
    monkeypatch, tmp_path
):
    config, store, _ = _eligible_store(tmp_path)
    before = _eod_evidence(store.db_path)
    calls: list[str] = []
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA user_version=4")
    database = Path(store.db_path)
    database.chmod(0o600)

    def forbidden_store(*_args, **_kwargs):
        raise AssertionError("dry-run must not initialize SwingStore")

    monkeypatch.setattr(repair, "SwingStore", forbidden_store)

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        dry_run=True,
        fetch_total_amount=_usable_fetch(calls),
    )

    assert result["action"] == "would_repair"
    assert result["dry_run"] is True
    assert calls == ["2026-08-28"]
    assert store.get_for_date("2026-08-28").total_amount is None
    assert _eod_evidence(store.db_path) == before
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM factor_import_batch"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM factor_observation"
        ).fetchone()[0] == 0
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_no_op_qualification_never_initializes_store(monkeypatch, tmp_path):
    config, _store = _config(tmp_path)

    def forbidden_store(*_args, **_kwargs):
        raise AssertionError("no-op must not initialize SwingStore")

    monkeypatch.setattr(repair, "SwingStore", forbidden_store)

    result = repair.run(str(config), now=TRADING_AFTER_REPAIR)

    assert result["action"] == "no_op"
    assert result["reason"] == "eod_run_missing"


def test_missing_database_is_error_and_is_not_created(tmp_path):
    database = tmp_path / "does-not-exist.db"
    config = tmp_path / "config.swing.json"
    config.write_text(
        json.dumps({"swing": {"store_file": str(database)}}), encoding="utf-8"
    )

    result = repair.run(str(config), now=TRADING_AFTER_REPAIR)

    assert result["ok"] is False
    assert result["reason"] == "database_missing"
    assert not database.exists()


@pytest.mark.parametrize(
    ("after_fetch", "reason"),
    (
        (
            datetime(2026, 8, 29, 0, 0, 1, tzinfo=BEIJING),
            "date_changed_after_fetch",
        ),
        (
            datetime(2026, 8, 28, 16, 0, 0, tzinfo=BEIJING),
            "window_closed_after_fetch",
        ),
    ),
)
def test_post_fetch_window_revalidation_blocks_write(
    tmp_path, after_fetch, reason
):
    config, store, _ = _eligible_store(tmp_path)
    before = _eod_evidence(store.db_path)
    calls: list[str] = []

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        after_fetch_now=after_fetch,
        fetch_total_amount=_usable_fetch(calls),
    )

    assert result["ok"] is False
    assert result["reason"] == reason
    assert calls == ["2026-08-28"]
    assert _factor_counts(store.db_path) == (0, 0)
    assert _eod_evidence(store.db_path) == before


def test_unusable_or_stale_source_is_auditable_error(tmp_path):
    config, store, _ = _eligible_store(tmp_path)

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=lambda _date: {
            "usable": False,
            "value": None,
            "source": "tencent",
            "status": {
                "available": True,
                "observed_date": "2026-08-27",
                "stale": True,
            },
        },
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["action"] == "error"
    assert result["reason"] == "source_unusable"
    assert store.get_for_date("2026-08-28").total_amount is None
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM factor_import_batch"
        ).fetchone()[0] == 0


def test_atomic_gate_rejects_macro_slice_race_without_factor_or_eod_mutation(
    tmp_path,
):
    config, store, _ = _eligible_store(tmp_path)
    before_eod = _eod_evidence(store.db_path)

    def competing_fetch(decision_date: str):
        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                "UPDATE macro_slice SET total_amount=? WHERE date=?",
                (20_999.0, decision_date),
            )
        return _usable_fetch([])(decision_date)

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=competing_fetch,
    )

    assert result["action"] == "no_op"
    assert result["reason"] == "precondition_changed"
    assert result["precondition_reason"] == "total_amount_present"
    assert _factor_counts(store.db_path) == (0, 0)
    assert _eod_evidence(store.db_path) == before_eod
    assert store.get_for_date("2026-08-28").total_amount == 20_999.0


def test_atomic_gate_rejects_changed_latest_run_before_factor_insert(tmp_path):
    config, store, _ = _eligible_store(tmp_path)
    evidence_after_competitor: list[dict[str, list[tuple]]] = []

    def competing_fetch(decision_date: str):
        competitor = SwingStore(store.db_path)
        _finish_run(competitor, day=decision_date, status="success", reasons=[])
        evidence_after_competitor.append(_eod_evidence(store.db_path))
        return _usable_fetch([])(decision_date)

    result = repair.run(
        str(config),
        now=TRADING_AFTER_REPAIR,
        fetch_total_amount=competing_fetch,
    )

    assert result["action"] == "no_op"
    assert result["precondition_reason"] == "latest_run_changed"
    assert _factor_counts(store.db_path) == (0, 0)
    assert _eod_evidence(store.db_path) == evidence_after_competitor[0]


def test_same_day_import_requires_apply_and_single_same_day_row(tmp_path):
    _config_path, store, run_id = _eligible_store(tmp_path)
    collected = _usable_fetch([])("2026-08-28")
    batch, observations = repair._build_import(
        decision_date="2026-08-28",
        run_id=run_id,
        collected_at=TRADING_AFTER_REPAIR,
        result=collected,
    )

    with pytest.raises(ValueError, match="必须填充"):
        store.record_factor_import(
            batch=batch,
            observations=observations,
            apply_to_macro_slice=False,
        )

    second = {**observations[0], "observation_date": "2026-08-27"}
    two_rows = [observations[0], second]
    two_row_batch = {
        **batch,
        "record_count": 2,
        "checksum": repair._sha256(two_rows),
    }
    with pytest.raises(ValueError, match="只能包含一条"):
        store.record_factor_import(
            batch=two_row_batch,
            observations=two_rows,
            apply_to_macro_slice=True,
        )
    assert _factor_counts(store.db_path) == (0, 0)


def test_weak_snapshot_date_source_cannot_be_accepted():
    result = {
        "usable": True,
        "value": 21_017.15,
        "source": "akshare",
        "status": {
            "available": True,
            "source": "akshare",
            "observed_date": "2026-08-28",
            "stale": False,
            "anomalous": False,
        },
    }

    assert repair._result_is_usable(result, "2026-08-28") is False


def test_claimed_tencent_date_without_qfqday_evidence_cannot_be_accepted():
    result = {
        "usable": True,
        "value": 21_017.15,
        "source": "tencent",
        "status": {
            "available": True,
            "source": "tencent",
            "observed_date": "2026-08-28",
            "stale": False,
            "anomalous": False,
        },
    }

    assert repair._result_is_usable(result, "2026-08-28") is False


def test_production_collector_rejects_stale_same_value(monkeypatch, tmp_path):
    def stale_collect(collector):
        collector._total_amount_observed_date = "2026-08-27"
        collector._total_amount_date_evidence = (
            collector.TENCENT_TOTAL_AMOUNT_DATE_EVIDENCE
        )
        return 21_017.15

    monkeypatch.setattr(
        repair.MacroSnapshotCollector, "_tencent_total_amount", stale_collect
    )

    result = repair._collect_live_total_amount("2026-08-28")

    assert result["usable"] is False
    assert result["value"] is None
    assert result["status"]["stale"] is True
    assert result["status"]["date_evidence"] == repair.TENCENT_DATE_EVIDENCE


def test_get_for_date_never_falls_back_to_another_day(tmp_path):
    store = SwingStore(str(tmp_path / "swing.db"))
    store.upsert(MacroSlice(date="2026-08-27", total_amount=20_000.0))

    assert store.get_for_date("2026-08-28") is None
    assert store.get_for_date("2026-08-27").total_amount == 20_000.0


def test_cli_exits_two_for_qualified_source_failure(monkeypatch):
    class Lock:
        def __init__(self, _name):
            pass

        def acquire(self):
            return self

        def release(self):
            pass

    monkeypatch.setattr(repair, "SingleInstanceLock", Lock)
    monkeypatch.setattr(
        repair,
        "run",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "error",
            "action": "error",
            "reason": "source_unusable",
        },
    )
    monkeypatch.setattr(sys, "argv", ["repair_swing_total_amount.py"])

    with pytest.raises(SystemExit) as exc_info:
        repair.main()

    assert exc_info.value.code == 2


@pytest.mark.parametrize("module", (repair, radar_swing_eod))
def test_swing_cli_lock_conflict_is_auditable_failure(monkeypatch, module):
    class BusyLock:
        def __init__(self, _name):
            pass

        def acquire(self):
            raise module.SingleInstanceLockError("busy")

    monkeypatch.setattr(module, "SingleInstanceLock", BusyLock)
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py"])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 75
