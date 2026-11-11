"""Subprocess helper for crash / multi-process tests (``python -m tests.notify_v2_worker``).

Synthetic data only; no network."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time

from radar.notify.outbox import DurableOutboxNotifier
from radar.notify.result import CONFIRMED, SendResult
from radar.swing.migrations import migrate_notification_v6
from radar.swing.store import SwingStore


def _append(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (text + "\n").encode())
    finally:
        os.close(fd)


class _Sender:
    def __init__(self, post_log: str, mode: str):
        self.post_log, self.mode = post_log, mode

    def target_digest(self) -> str:
        return "target-digest-a"

    def send_system_alert_ex(self, title, message):
        _append(self.post_log, f"POST pid={os.getpid()}")
        if self.mode == "crash_after_post":
            os._exit(9)  # the "platform" accepted it; the process dies before saving anything
        if self.mode == "slow_ok":
            time.sleep(0.4)
        return SendResult(CONFIRMED, "response", 200, message_id=1)


def main() -> None:
    mode = sys.argv[1]
    db = sys.argv[2]
    if mode == "migrate_fault":
        step = sys.argv[3]

        def fault(name: str) -> None:
            if name == step:
                os._exit(9)

        conn = sqlite3.connect(db)
        migrate_notification_v6(conn, _fault=fault)
        return
    post_log, sender_mode, scope = sys.argv[3], sys.argv[4], sys.argv[5]
    store = SwingStore(db)
    notifier = DurableOutboxNotifier(
        store, _Sender(post_log, sender_mode), logging.getLogger("worker"), scope=scope
    )
    result = notifier.send_system_alert_ex("EOD report 2026-10-12", "synthetic body")
    sys.stdout.write(json.dumps(result.summary()) + "\n")


if __name__ == "__main__":
    main()
