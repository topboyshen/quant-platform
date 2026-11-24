"""C7 watchdog notification comparison, isolated from the legacy export pipeline."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

from qp.shadow.compare import _compare_notifications

DAY = dt.date(2026, 11, 24)


class Result:
    def __init__(self, row):
        self.row = row

    def scalars(self):
        return self

    def first(self):
        return self.row


class Session:
    def __init__(self, rows):
        self.rows = iter(rows)

    def execute(self, _statement):
        return Result(next(self.rows))


def outbox(body: dict):
    return SimpleNamespace(body=json.dumps(body))


def test_c7_matches_structured_and_inferred_notifications():
    export = {
        "files": {
            "notifications.json": {
                "dispatch": [
                    {
                        "namespace": f"watchdog:{DAY.isoformat()}:failure",
                        "payload": {"text": "终态检查失败 attempts=2"},
                    },
                    {
                        "namespace": f"watchdog:{DAY.isoformat()}:daily",
                        "payload": {"report_type": "eod_daily_report", "terminal_ok": True, "text": "attempts=1"},
                    },
                ]
            }
        }
    }
    session = Session(
        [
            outbox({"terminal_ok": False, "attempts": {"attempts": 2}}),
            outbox({"report_type": "eod_daily_report", "terminal_ok": True, "attempts": {"attempts": 1}}),
        ]
    )
    check = _compare_notifications(session, DAY, export)
    assert check["id"] == "C7"
    assert check["status"] == "PASS"
    assert check["details"]["failure"]["legacy"] is True
    assert check["details"]["daily"]["qp"] is True


def test_c7_reports_existence_json_and_attempt_diffs():
    export = {
        "files": {
            "notifications.json": {
                "outbox": [
                    {"idempotency_key": f"watchdog:{DAY.isoformat()}:failure", "payload": {"text": "no marker"}},
                    {"idempotency_key": f"watchdog:{DAY.isoformat()}:daily", "payload": {"text": "daily"}},
                ]
            }
        }
    }
    session = Session(
        [
            outbox({"terminal_ok": False, "attempts": {"attempts": 1}}),
            SimpleNamespace(body="not json"),
        ]
    )
    check = _compare_notifications(session, DAY, export)
    assert check["id"] == "C7"
    assert check["status"] == "FAIL"
    assert any("not JSON" in item for item in check["diffs"])
    assert any("attempts" in item for item in check["diffs"])
