"""Watchdog publication must not depend on a working Telegram transport."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from runtime_contracts.calendar import TZ
from runtime_contracts.eod import validate_watchdog

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("watchdog_publication_tests", ROOT / "scripts/check_swing_eod_terminal.py")
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_publish_failure_before_notification_and_keep_audit_read_only(monkeypatch, tmp_path):
    failed = {"status": "failed", "decision_outcome": "data_unavailable", "reason_codes": ["trend_data_unavailable"],
              "completed_at": "2026-09-11T15:30:20+08:00", "evaluated_count": 0, "failed_count": 10}
    class Store:
        def get_eod_runs_for_date(self, day): return [failed]
    monkeypatch.setattr(module, "_config", lambda path: {"swing": {"store_file": str(tmp_path / "absent.db")},
                                                       "telegram": {"bot_token": "fixture", "chat_id": "fixture"}})
    monkeypatch.setattr(module, "SwingStore", lambda path: Store())
    monkeypatch.setattr(module.HolidayCalendar, "is_holiday", lambda now: False)
    def broken(*args, **kwargs): raise RuntimeError("transport unavailable fixture")
    monkeypatch.setattr(module, "TelegramNotifier", broken)
    now = datetime(2026, 9, 11, 15, 50, tzinfo=TZ)
    with pytest.raises(RuntimeError): module.run("unused.json", now=now)
    data = json.loads((tmp_path / "swing_eod_watchdog.json").read_text())
    assert validate_watchdog(data, now)["recovery_status"] == 3
    assert not (tmp_path / "absent.db").exists()


def test_failed_replace_preserves_old_file_and_cleans_temporary(monkeypatch, tmp_path):
    path = tmp_path / "swing_eod_watchdog.json"; path.write_bytes(b"old evidence")
    def broken(*args): raise OSError("fixture")
    monkeypatch.setattr(module.os, "replace", broken)
    with pytest.raises(OSError):
        module.persist_watchdog_status(str(tmp_path / "unused.db"), {"ok": False})
    assert path.read_bytes() == b"old evidence"
    assert list(tmp_path.iterdir()) == [path]
