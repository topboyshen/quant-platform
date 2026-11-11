"""Shared fakes for the notification-v2 tests (synthetic data, temp DBs, no network)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from radar.notify.outbox import DurableOutboxNotifier
from radar.notify.result import (
    CONFIRMED,
    NOT_SENT_PERMANENT,
    NOT_SENT_RETRYABLE,
    UNCERTAIN,
    SendResult,
)
from radar.swing.migrations import migrate_notification_v6
from radar.swing.store import SwingStore

TOKEN = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234"
CHAT_ID = "987654321"
BODY_MARK = "SYNTHETIC-BODY-MARKER-XYZ"

OK = SendResult(CONFIRMED, "response", 200, message_id=4242)
UNSENT = SendResult(NOT_SENT_RETRYABLE, "connect", error_class="NewConnectionError")
PERMANENT = SendResult(NOT_SENT_PERMANENT, "platform_reject", 403, "telegram_403")
UNSURE = SendResult(UNCERTAIN, "response", 503, "http_503")


def limited(seconds: float) -> SendResult:
    return SendResult(NOT_SENT_RETRYABLE, "rate_limit", 429, "telegram_429", retry_after=seconds)


class FakeClock:
    def __init__(self, start: Optional[datetime] = None):
        # 10:00 Asia/Shanghai = 02:00Z: far from midnight in both zones
        self.value = start or datetime(2026, 10, 12, 2, 0, 0, tzinfo=timezone.utc)
        self.sleeps: List[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeSender:
    """Scripted ``*_ex`` sender.  ``script`` items are SendResult, or an Exception to raise."""

    def __init__(self, script: List[Any], digest: str = "target-digest-a"):
        self.script = list(script)
        self.calls: List[tuple] = []
        self.digest = digest

    def target_digest(self) -> str:
        return self.digest

    def _next(self, name: str, args: tuple) -> SendResult:
        self.calls.append((name, args))
        item = self.script.pop(0) if self.script else OK
        if isinstance(item, BaseException):
            raise item
        return item

    def send_system_alert_ex(self, title, message):
        return self._next("send_system_alert", (title, message))

    def send_trend_alert_ex(self, name, code, signal, period, detail=None):
        return self._next("send_trend_alert", (name, code, signal, period, detail))

    def send_macro_alert_ex(self, fact, level="info", detail=None):
        return self._next("send_macro_alert", (fact, level, detail))

    def send_datasource_alert_ex(self, source_name, failure_count, status="abnormal"):
        return self._next("send_datasource_alert", (source_name, failure_count, status))


def make_v6_store(path, clock: Optional[FakeClock] = None) -> SwingStore:
    store = SwingStore(str(path))
    conn = sqlite3.connect(str(path))
    try:
        migrate_notification_v6(conn)
    finally:
        conn.close()
    if clock is not None:
        store.dispatch_clock = clock.now
    return store


def make_notifier(store, sender, clock: Optional[FakeClock] = None, scope="eod:2026-10-12"):
    clock = clock or FakeClock()
    store.dispatch_clock = clock.now
    return DurableOutboxNotifier(
        store, sender, logging.getLogger("test.notify.v2"), scope=scope, sleep=clock.sleep
    )


def dispatch_rows(path) -> List[dict]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM notification_dispatch ORDER BY id")]
    finally:
        conn.close()


def attempt_rows(path) -> List[dict]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM notification_attempt ORDER BY id")]
    finally:
        conn.close()
