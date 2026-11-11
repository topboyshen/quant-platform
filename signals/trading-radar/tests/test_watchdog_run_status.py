"""A crash in the watchdog / daily-report stage must leave an independent, observable failure state."""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from runtime_contracts.calendar import TZ
from runtime_contracts.watchdog_metrics import watchdog_metrics

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("watchdog_run_status_tests", ROOT / "scripts/check_swing_eod_terminal.py")
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

NOW = datetime(2026, 10, 8, 15, 50, tzinfo=TZ)  # Thursday, the incident slot
FAILED_RUN = {"status": "failed", "decision_outcome": "data_unavailable", "reason_codes": ["trend_data_unavailable"],
              "completed_at": "2026-10-08T15:31:13+08:00", "evaluated_count": 0, "failed_count": 10, "run_id": "r"}


def _patch(monkeypatch, tmp_path):
    class Store:
        def get_eod_runs_for_date(self, day):
            return [FAILED_RUN]
    monkeypatch.setattr(module, "_config", lambda path: {
        "swing": {"store_file": str(tmp_path / "absent.db")},
        "telegram": {"bot_token": "fixture-not-real", "chat_id": "fixture"}})
    monkeypatch.setattr(module, "SwingStore", lambda path: Store())
    monkeypatch.setattr(module.HolidayCalendar, "is_holiday", lambda now: False)


def _calendar(path: Path) -> Path:
    """Self-contained calendar: the production artifact is runtime data, not tracked."""
    days = []
    cursor = date(2024, 1, 1)
    end = date(2026, 12, 31)
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    path.write_text(json.dumps({
        "schema_version": 1,
        "source": "akshare.tool_trade_date_hist_sina",
        "generated_at": "2026-10-08T15:00:00+08:00",
        "count": len(days),
        "min_date": days[0],
        "max_date": days[-1],
        "trading_dates": days,
    }), encoding="utf-8")
    return path


def _metrics(tmp_path, now=NOW):
    text = watchdog_metrics(
        tmp_path / "swing_eod_watchdog.json", _calendar(tmp_path / "trading_calendar.json"), now
    )
    rows = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split(" ", 1)
            rows[name.removeprefix("radar_swing_eod_")] = value
    return rows


def test_report_build_crash_is_recorded_and_still_raises(monkeypatch, tmp_path):
    """Incident shape: IndexError inside the daily-report build at 15:50."""
    _patch(monkeypatch, tmp_path)

    class Notifier:
        def __init__(self, *a, **k): pass

    def boom(*args, **kwargs):
        return [][0]
    monkeypatch.setattr(module, "TelegramNotifier", Notifier)
    monkeypatch.setattr(module, "DurableOutboxNotifier", lambda *a, **k: Notifier())
    monkeypatch.setattr(module, "build_daily_report", boom)
    with pytest.raises(IndexError):
        module.run("unused.json", now=NOW)
    status = json.loads((tmp_path / "swing_eod_watchdog_run.json").read_text())
    assert status["status"] == "failed" and status["trade_date"] == "2026-10-08"
    assert status["error_type"] == "IndexError"
    assert status["error_location"].startswith("test_watchdog_run_status.py:") and " in boom" in status["error_location"]
    # business artifact is still published independently by persist_result
    assert (tmp_path / "swing_eod_watchdog.json").exists()
    rows = _metrics(tmp_path)
    assert rows["watchdog_run_failed"] == "1" and rows["watchdog_run_valid"] == "1"
    assert rows["watchdog_run_current"] == "1"


def test_failure_record_never_contains_exception_message(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    def leak(*args, **kwargs):
        raise RuntimeError("https://api.telegram.org/bot123456:SECRET-TOKEN/sendMessage failed")
    monkeypatch.setattr(module, "TelegramNotifier", leak)
    with pytest.raises(RuntimeError):
        module.run("unused.json", now=NOW)
    raw = (tmp_path / "swing_eod_watchdog_run.json").read_text()
    assert "SECRET" not in raw and "telegram" not in raw.lower()
    assert json.loads(raw)["error_type"] == "RuntimeError"


def test_successful_run_records_ok_and_clears_earlier_failure(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    module.persist_watchdog_run_status(str(tmp_path / "absent.db"), NOW, "failed", RuntimeError("x"))
    assert _metrics(tmp_path)["watchdog_run_failed"] == "1"
    result = module.run("unused.json", now=NOW, send_alert=False)
    assert result["ok"] is False  # business failure is not a crash
    assert json.loads((tmp_path / "swing_eod_watchdog_run.json").read_text())["status"] == "ok"
    assert _metrics(tmp_path)["watchdog_run_failed"] == "0"


def test_status_write_failure_does_not_mask_original_crash(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    def broken_status(*args, **kwargs):
        raise OSError("disk full fixture")
    monkeypatch.setattr(module, "persist_watchdog_run_status", broken_status)
    monkeypatch.setattr(module, "TelegramNotifier", lambda *a, **k: (_ for _ in ()).throw(ValueError("orig")))
    with pytest.raises(ValueError, match="orig"):
        module.run("unused.json", now=NOW)


def test_record_run_status_can_be_disabled(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    module.run("unused.json", now=NOW, send_alert=False, record_run_status=False)
    assert not (tmp_path / "swing_eod_watchdog_run.json").exists()


def test_config_unreadable_still_raises_and_records_default_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    def bad_config(path):
        raise FileNotFoundError("config fixture")
    monkeypatch.setattr(module, "_config", bad_config)
    with pytest.raises(FileNotFoundError):
        module.run("missing.json", now=NOW)
    assert json.loads((tmp_path / "data/swing_eod_watchdog_run.json").read_text())["error_type"] == "FileNotFoundError"


def test_metrics_unknown_when_file_missing_or_invalid(tmp_path):
    rows = _metrics(tmp_path)
    assert rows["watchdog_run_failed"] == "NaN" and rows["watchdog_run_valid"] == "0"
    run_file = tmp_path / "swing_eod_watchdog_run.json"
    good = {"schema_version": 1, "status": "failed", "trade_date": "2026-10-08", "checked_at": NOW.isoformat()}
    for bad in (
        "not json", "[]", json.dumps({**good, "schema_version": 2}), json.dumps({**good, "status": "weird"}),
        json.dumps({**good, "trade_date": "2026-10-07"}), json.dumps({**good, "checked_at": "2026-10-09T10:00:00+08:00"}),
        json.dumps({**good, "checked_at": "garbage"}), json.dumps({**good, "padding": "x" * 5000}),
    ):
        run_file.write_text(bad)
        rows = _metrics(tmp_path)
        assert rows["watchdog_run_failed"] == "NaN" and rows["watchdog_run_valid"] == "0", bad[:60]
    run_file.write_text(json.dumps(good))
    assert _metrics(tmp_path)["watchdog_run_failed"] == "1"


def test_stale_day_failure_is_not_current(tmp_path):
    run_file = tmp_path / "swing_eod_watchdog_run.json"
    run_file.write_text(json.dumps({"schema_version": 1, "status": "failed", "trade_date": "2026-10-08",
                                    "checked_at": NOW.isoformat()}))
    next_day = datetime(2026, 10, 9, 16, 0, tzinfo=TZ)
    rows = _metrics(tmp_path, next_day)
    assert rows["watchdog_run_failed"] == "1" and rows["watchdog_run_current"] == "0"


def test_existing_business_metrics_unchanged_by_run_status(tmp_path):
    rows = _metrics(tmp_path)
    for key in ("ok", "deferred", "recovery_status", "evidence_valid", "terminal_due"):
        assert key.replace("ok", "watchdog_ok") in rows or key in rows
