"""Unit tests for 15:40 data_unavailable alert deferral until the 15:48 retry."""

from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_swing_eod_terminal_under_test",
    ROOT / "scripts" / "check_swing_eod_terminal.py",
)
# Importing the watchdog pulls radar notify/store; skip if that fails in isolation.
try:
    assert SPEC is not None and SPEC.loader is not None
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
except Exception as exc:  # pragma: no cover
    module = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

BEIJING = ZoneInfo("Asia/Shanghai")


def _result(**run_fields):
    return {
        "ok": False,
        "reason": "failed",
        "message": "EOD 终态为 failed",
        "run": {
            "status": "failed",
            "decision_outcome": "data_unavailable",
            "reason_codes": ["trend_data_unavailable"],
            **run_fields,
        },
    }


@unittest.skipIf(module is None, f"watchdog import failed: {_IMPORT_ERROR}")
class DeferUntilRetryTests(unittest.TestCase):
    def test_1540_data_unavailable_is_deferred(self):
        now = datetime(2026, 9, 11, 15, 40, tzinfo=BEIJING)
        self.assertTrue(module.defer_until_retry(_result(), now))

    def test_1550_data_unavailable_is_not_deferred(self):
        now = datetime(2026, 9, 11, 15, 50, tzinfo=BEIJING)
        self.assertFalse(module.defer_until_retry(_result(), now))

    def test_1540_publication_wait_is_deferred(self):
        now = datetime(2026, 9, 11, 15, 40, tzinfo=BEIJING)
        running = _result(
            status="running", decision_outcome=None, reason_codes=[]
        )
        running["reason"] = "running_timeout"
        self.assertTrue(module.defer_until_retry(running, now))

    def test_1550_publication_wait_is_not_deferred(self):
        now = datetime(2026, 9, 11, 15, 50, tzinfo=BEIJING)
        running = _result(
            status="running", decision_outcome=None, reason_codes=[]
        )
        running["reason"] = "running_timeout"
        self.assertFalse(module.defer_until_retry(running, now))

    def test_missing_run_is_not_deferred(self):
        now = datetime(2026, 9, 11, 15, 40, tzinfo=BEIJING)
        self.assertFalse(
            module.defer_until_retry(
                {"ok": False, "reason": "missing", "message": "当天没有任何 EOD 审计记录"},
                now,
            )
        )

    def test_unhandled_error_is_not_deferred(self):
        now = datetime(2026, 9, 11, 15, 40, tzinfo=BEIJING)
        self.assertFalse(
            module.defer_until_retry(
                _result(decision_outcome="data_unavailable", reason_codes=["unhandled_error"]),
                now,
            )
        )


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def get_eod_runs_for_date(self, decision_date):
        del decision_date
        return self.rows

    def get_run_evidence_counts(self, run_id):
        del run_id
        return {"target_decisions": 10, "risk_snapshots": 1}


def _failed_1530():
    return {
        "run_id": "fail-1530",
        "status": "failed",
        "decision_outcome": "data_unavailable",
        "reason_codes": ["trend_data_unavailable"],
        "completed_at": "2026-09-10T07:30:27.657101Z",
        "evaluated_count": 0,
        "failed_count": 10,
        "target_count": 10,
    }


def _success(completed_at):
    return {
        "run_id": "ok-retry",
        "status": "success",
        "decision_outcome": "no_signal",
        "reason_codes": [],
        "completed_at": completed_at,
        "evaluated_count": 10,
        "failed_count": 0,
        "target_count": 10,
    }


@unittest.skipIf(module is None, f"watchdog import failed: {_IMPORT_ERROR}")
class FirstVersusTerminalTests(unittest.TestCase):
    def test_1548_recovery_is_success_at_1550_and_keeps_first_failure(self):
        now = datetime(2026, 9, 11, 15, 50, tzinfo=BEIJING)
        rows = [_success("2026-09-11T07:48:20.825478Z"), _failed_1530()]
        result = module.inspect_terminal_audit(
            FakeStore(rows), decision_date="2026-09-11", now=now
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["attempts"]["recovered_after_data_gap"])
        self.assertEqual(result["attempts"]["first"]["status"], "failed")
        self.assertEqual(result["attempts"]["latest"]["status"], "success")
        self.assertFalse(module.defer_until_retry(result, now))

    def test_1606_recovery_still_alerts_at_1550(self):
        now = datetime(2026, 9, 10, 15, 50, tzinfo=BEIJING)
        rows = [_failed_1530()]
        result = module.inspect_terminal_audit(
            FakeStore(rows), decision_date="2026-09-10", now=now
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["attempts"]["recovered_after_data_gap"])
        self.assertFalse(module.defer_until_retry(result, now))
        summary = module.format_attempt_summary(result["attempts"])
        self.assertIn("first=failed/data_unavailable", summary)
        self.assertIn("latest=failed/data_unavailable", summary)

    def test_day_status_keeps_first_failure_and_terminal_recovery(self):
        recovered = {
            "ok": True,
            "reason": "success",
            "attempts": {
                "attempts": 2,
                "recovered_after_data_gap": True,
                "first": {"status": "failed"},
                "latest": {"status": "success"},
            },
        }
        day = module.classify_day_status(recovered)
        self.assertEqual(day["recovery_status"], "recovered_after_data_gap")
        self.assertEqual(day["business_terminal_status"], "success")
        self.assertTrue(day["systemd_first_failed"])

        pending = {
            "ok": True,
            "reason": "awaiting_same_day_retry",
            "deferred": True,
            "attempts": {
                "attempts": 1,
                "first": {"status": "failed"},
                "latest": {"status": "failed"},
            },
        }
        self.assertEqual(module.classify_day_status(pending)["recovery_status"], "awaiting_retry")
        failed = {
            "ok": False,
            "reason": "failed",
            "attempts": {
                "attempts": 1,
                "first": {"status": "failed"},
                "latest": {"status": "failed"},
            },
        }
        self.assertEqual(module.classify_day_status(failed)["recovery_status"], "failed_final")

    def test_persist_watchdog_status_is_atomic(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            store_file = str(Path(raw) / "swing_macro.db")
            Path(store_file).write_bytes(b"")
            module.persist_watchdog_status(
                store_file,
                {"ok": True, "reason": "success", "attempts": {"attempts": 2}},
            )
            path = Path(raw) / "swing_eod_watchdog.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


@unittest.skipIf(module is None, f"watchdog import failed: {_IMPORT_ERROR}")
class DailyReportSlotTests(unittest.TestCase):
    def test_1550_sends_daily_report_instead_of_a_second_failure_page(self):
        now = datetime(2026, 9, 15, 15, 50, tzinfo=BEIJING)
        self.assertEqual(module.notification_plan({"ok": False}, now), "daily")
        self.assertEqual(module.notification_plan({"ok": True}, now), "daily")

    def test_1540_success_waits_for_the_daily_report(self):
        now = datetime(2026, 9, 15, 15, 40, tzinfo=BEIJING)
        self.assertEqual(module.notification_plan({"ok": True}, now), "none")
        self.assertEqual(module.notification_plan({"ok": False}, now), "failure")


if __name__ == "__main__":
    unittest.main()
