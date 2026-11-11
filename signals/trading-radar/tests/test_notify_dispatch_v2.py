"""Notification v2: dispatch queue, retry budget, migration and the five hard boundaries.

Boundary map (each has its own tests below):
  B1 version compatibility not widened / flag off pauses     -> test_b1_*
  B2 two queues must not each deduplicate                     -> test_b2_*
  B3 same business key + different body stops                 -> test_b3_*
  B4 rate-limit wait never truncated by the 40 s budget       -> test_b4_*
  B5 platform success + local save failure never re-sends     -> test_b5_*
All data synthetic, temp databases only, loopback-only network guard in the runner.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from dashboard.swing_metrics import render_swing_metrics
from radar.notify.health import HEALTH_FILE, write_notification_health
from radar.notify.outbox import FLAG_ENV, DurableOutboxNotifier
from radar.notify.result import CONFIRMED, UNCERTAIN, DeliveryResult, SendResult
from radar.notify.telegram import TelegramNotifier
from radar.swing.dispatch_store import DispatchClaimLost
from radar.swing.migrations import (
    MIGRATION_6_STEPS,
    migrate,
    migrate_notification_v6,
    verify_notification_schema,
)
from radar.swing.reporter import SwingReporter
from radar.swing.store import SwingStore
from runtime_contracts.watchdog_metrics import watchdog_metrics
from scripts import check_quant_monday_acceptance as acceptance
from scripts.check_swing_eod_terminal import _alert_fields
from tests.notify_v2_helpers import (
    BODY_MARK,
    CHAT_ID,
    OK,
    PERMANENT,
    TOKEN,
    UNSENT,
    UNSURE,
    FakeClock,
    FakeSender,
    attempt_rows,
    dispatch_rows,
    limited,
    make_notifier,
    make_v6_store,
)

ROOT = Path(__file__).resolve().parents[1]
TITLE, MESSAGE = "EOD 日报 2026-10-12", "synthetic daily report text"
WORKER_TITLE, WORKER_MESSAGE = "EOD report 2026-10-12", "synthetic body"  # what tests/notify_v2_worker.py sends


def _deliver(notifier, title=TITLE, message=MESSAGE):
    return notifier.send_system_alert_ex(title, message)


def _legacy_row(store, status, *, scope="eod:2026-10-12", title=TITLE, message=MESSAGE, error="x"):
    """Create a row in the OLD notification_outbox through the old API (sent/failed/sending/pending)."""
    payload = {"args": [title, message], "kwargs": {}}
    key = store.notification_key("send_system_alert", {"scope": scope, **payload})
    store.enqueue_notification(key, operation="send_system_alert", payload=payload)
    if status != "pending":
        store.begin_notification_delivery(key)
        if status in ("sent", "failed"):
            store.finish_notification_delivery(key, success=status == "sent", error=error)
    return key


def _legacy_snapshot(path):
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT * FROM notification_outbox ORDER BY id").fetchall()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='notification_outbox'").fetchone()[0]
        return rows, sql
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    clock = FakeClock()
    db = tmp_path / "swing.db"
    store = make_v6_store(db, clock)
    return SimpleNamespace(db=db, clock=clock, store=store, tmp=tmp_path)


# =============================================================== happy path / states
def test_confirmed_delivery_is_recorded_once_and_replays_as_duplicate(env):
    sender = FakeSender([OK])
    notifier = make_notifier(env.store, sender, env.clock)
    first = _deliver(notifier)
    again = _deliver(notifier)
    assert first.outcome == CONFIRMED and first.attempts == 1 and not first.duplicate
    assert again.outcome == CONFIRMED and again.duplicate
    assert len(sender.calls) == 1
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "confirmed" and row["message_id"] == 4242 and row["claim_token"] is None
    assert [a["outcome"] for a in attempt_rows(env.db)] == [CONFIRMED]


def test_not_sent_then_success_uses_two_posts_with_backoff(env):
    sender = FakeSender([UNSENT, OK])
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert result.outcome == CONFIRMED and result.attempts == 2
    assert len(sender.calls) == 2 and env.clock.sleeps == [0.5]


def test_three_not_sent_attempts_exhaust_the_budget_without_a_fourth_post(env):
    sender = FakeSender([UNSENT, UNSENT, UNSENT, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    result = _deliver(notifier)
    assert result.outcome == "failed_exhausted" and result.attempts == 3
    assert env.clock.sleeps == [0.5, 1.0] and len(sender.calls) == 3
    assert _deliver(notifier).outcome == "failed_exhausted"  # a re-call cannot restart it
    assert len(sender.calls) == 3


def test_permanent_rejection_stops_after_one_post(env):
    sender = FakeSender([PERMANENT, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    assert _deliver(notifier).outcome == "not_sent_permanent"
    assert _deliver(notifier).outcome == "not_sent_permanent"
    assert len(sender.calls) == 1 and env.clock.sleeps == []


@pytest.mark.parametrize("unsure", [UNSURE, SendResult(UNCERTAIN, "unknown", error_class="ReadTimeout")])
def test_uncertain_never_auto_resends_even_when_called_again_much_later(env, unsure):
    sender = FakeSender([unsure, OK, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    assert _deliver(notifier).outcome == UNCERTAIN
    env.clock.advance(3 * 86400)
    assert _deliver(notifier).outcome == UNCERTAIN
    assert len(sender.calls) == 1
    assert dispatch_rows(env.db)[0]["state"] == "uncertain"


def test_unexpected_exception_after_claim_is_uncertain_and_not_retried(env):
    sender = FakeSender([RuntimeError("boom with " + TOKEN), OK])
    notifier = make_notifier(env.store, sender, env.clock)
    assert _deliver(notifier).outcome == UNCERTAIN
    assert len(sender.calls) == 1
    assert TOKEN not in json.dumps(dispatch_rows(env.db), default=str)


def test_bool_only_notifier_without_evidence_is_never_retried(env):
    class BoolOnly:
        calls = 0

        def send_system_alert(self, title, message):
            BoolOnly.calls += 1
            return False

    notifier = make_notifier(env.store, BoolOnly(), env.clock)
    assert _deliver(notifier).outcome == UNCERTAIN and BoolOnly.calls == 1


# ====================================================================== retry budget
def test_budget_persists_across_restart_and_cannot_be_reset(env):
    """Two attempts consumed, 'process restart' (new store+notifier objects): only ONE more POST."""
    sender1 = FakeSender([UNSENT, UNSENT])
    make_notifier(env.store, sender1, env.clock)
    store, claim = env.store, None
    # run two attempts by hand so that the first process "dies" while waiting for the 3rd
    from radar.notify import identity
    business = identity.business_id("send_system_alert", [TITLE, MESSAGE], {})
    key = identity.business_key("eod:2026-10-12", "telegram", "send_system_alert", business)
    obj = identity.legacy_payload([TITLE, MESSAGE], {})
    store.dispatch_enqueue(
        business_key=key, namespace="eod:2026-10-12", channel="telegram", operation="send_system_alert",
        target_alias="a", target_digest="target-digest-a", payload_json=identity.canonical_json(obj),
        payload_sha256=identity.payload_sha256([TITLE, MESSAGE], {}), payload_obj=obj,
        business=business, legacy_keys=[])
    for _ in range(2):
        claim = store.dispatch_claim(key)
        assert claim["status"] == "claimed"
        store.dispatch_finish(key, claim_token=claim["claim_token"], attempt_no=claim["attempt_no"], result=UNSENT)
        env.clock.advance(2)
    assert dispatch_rows(env.db)[0]["attempts_used"] == 2

    store2 = SwingStore(str(env.db))  # restart
    sender2 = FakeSender([UNSENT, OK])
    n2 = make_notifier(store2, sender2, env.clock)
    result = _deliver(n2)
    assert len(sender2.calls) == 1          # not a fresh 3
    assert result.outcome == "failed_exhausted" and result.attempts == 3


def test_retry_wait_row_is_not_taken_over_before_due_and_expires_with_the_budget(env):
    sender = FakeSender([UNSENT])
    notifier = make_notifier(env.store, sender, env.clock)
    notifier._sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())  # first process dies in the wait
    with pytest.raises(KeyboardInterrupt):
        _deliver(notifier)
    assert dispatch_rows(env.db)[0]["state"] == "retry_wait"
    other = FakeSender([OK])
    n2 = make_notifier(SwingStore(str(env.db)), other, env.clock)
    assert _deliver(n2).outcome == "retry_wait_pending" and other.calls == []
    env.clock.advance(41)  # beyond the persisted 40 s deadline
    assert _deliver(n2).outcome == "failed_exhausted" and other.calls == []


def test_retry_never_crosses_midnight(env):
    start = datetime(2026, 10, 12, 15, 59, 50, tzinfo=timezone.utc)  # 23:59:50 Asia/Shanghai
    clock = FakeClock(start)
    store = make_v6_store(env.tmp / "night.db", clock)
    sender = FakeSender([SendResult("not_sent_retryable", "connect", error_class="NewConnectionError", retry_after=None), OK])
    notifier = make_notifier(store, sender, clock)
    notifier._sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _deliver(notifier)
    clock.value = datetime(2026, 10, 12, 16, 0, 5, tzinfo=timezone.utc)  # 00:00:05 next day, still < 40 s
    n2 = make_notifier(SwingStore(str(env.tmp / "night.db")), FakeSender([OK]), clock)
    result = _deliver(n2)
    assert result.outcome == "failed_exhausted" and result.reason == "retry_overnight_forbidden"


# ============================================================== leases / concurrency
def test_live_sending_lease_blocks_a_second_caller_and_expired_lease_becomes_uncertain(env):
    from radar.notify import identity
    sender = FakeSender([OK])
    notifier = make_notifier(env.store, sender, env.clock)
    obj = identity.legacy_payload([TITLE, MESSAGE], {})
    business = identity.business_id("send_system_alert", [TITLE, MESSAGE], {})
    key = identity.business_key("eod:2026-10-12", "telegram", "send_system_alert", business)
    env.store.dispatch_enqueue(
        business_key=key, namespace="eod:2026-10-12", channel="telegram", operation="send_system_alert",
        target_alias="a", target_digest="target-digest-a", payload_json=identity.canonical_json(obj),
        payload_sha256=identity.payload_sha256([TITLE, MESSAGE], {}), payload_obj=obj, business=business, legacy_keys=[])
    assert env.store.dispatch_claim(key)["status"] == "claimed"
    assert _deliver(notifier).outcome == "in_progress" and sender.calls == []
    env.clock.advance(91)
    assert _deliver(notifier).outcome == UNCERTAIN and sender.calls == []
    assert dispatch_rows(env.db)[0]["reason_code"] == "sending_lease_expired"
    assert attempt_rows(env.db)[0]["error_class"] == "lease_expired"


def test_threads_with_the_same_key_produce_exactly_one_post(tmp_path, monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    db = tmp_path / "swing.db"
    make_v6_store(db)
    posts, lock = [], threading.Lock()

    class Slow(FakeSender):
        def send_system_alert_ex(self, title, message):
            with lock:
                posts.append(threading.get_ident())
            import time
            time.sleep(0.3)
            return OK

    outcomes = []

    def worker():
        notifier = DurableOutboxNotifier(SwingStore(str(db)), Slow([]), logging.getLogger("t"), scope="eod:2026-10-12")
        outcomes.append(notifier.send_system_alert_ex(TITLE, MESSAGE).outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(posts) == 1
    assert outcomes.count(CONFIRMED) >= 1 and set(outcomes) <= {CONFIRMED, "in_progress"}
    assert dispatch_rows(db)[0]["state"] == "confirmed" and dispatch_rows(db)[0]["attempts_used"] == 1


def _worker(db, post_log, mode, scope="eod:2026-10-12"):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]).strip(os.pathsep)}
    env.pop(FLAG_ENV, None)
    return subprocess.Popen(
        [sys.executable, "-m", "tests.notify_v2_worker", "deliver", str(db), str(post_log), mode, scope],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_processes_with_the_same_key_produce_exactly_one_post(tmp_path):
    db, log = tmp_path / "swing.db", tmp_path / "posts.log"
    make_v6_store(db)
    procs = [_worker(db, log, "slow_ok") for _ in range(5)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    assert len(log.read_text().splitlines()) == 1
    assert dispatch_rows(db)[0]["state"] == "confirmed"


# ============================================================================ B5 crash
def test_b5_crash_after_post_leaves_uncertain_and_never_posts_again(tmp_path):
    db, log = tmp_path / "swing.db", tmp_path / "posts.log"
    make_v6_store(db)
    proc = _worker(db, log, "crash_after_post")
    proc.communicate(timeout=60)
    assert proc.returncode == 9
    assert len(log.read_text().splitlines()) == 1
    row = dispatch_rows(db)[0]
    assert row["state"] == "sending" and row["attempts_used"] == 1 and row["message_id"] is None
    # a later process (after the lease expired) must not POST: it can only record 'uncertain'
    future = FakeClock(datetime.now(timezone.utc) + timedelta(seconds=200))
    store = SwingStore(str(db))
    sender = FakeSender([OK])
    result = _deliver(make_notifier(store, sender, future), WORKER_TITLE, WORKER_MESSAGE)
    assert result.outcome == UNCERTAIN and sender.calls == []
    # and a live lease (immediately after the crash) also blocks a re-send
    assert dispatch_rows(db)[0]["state"] == "uncertain"


def test_b5_crash_leaves_live_lease_which_blocks_resend_until_it_expires(tmp_path):
    db, log = tmp_path / "swing.db", tmp_path / "posts.log"
    make_v6_store(db)
    _worker(db, log, "crash_after_post").communicate(timeout=60)
    sender = FakeSender([OK])
    result = _deliver(make_notifier(SwingStore(str(db)), sender, FakeClock(datetime.now(timezone.utc))),
                      WORKER_TITLE, WORKER_MESSAGE)
    assert result.outcome == "in_progress" and sender.calls == []


def test_b5_receipt_save_failure_retries_only_the_save(env, monkeypatch):
    sender = FakeSender([OK])
    notifier = make_notifier(env.store, sender, env.clock)
    real = env.store.dispatch_finish
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(env.store, "dispatch_finish", flaky)
    result = _deliver(notifier)
    assert result.outcome == CONFIRMED and result.receipt_saved
    assert len(sender.calls) == 1 and state["n"] == 3
    assert dispatch_rows(env.db)[0]["state"] == "confirmed"


def test_b5_save_that_never_succeeds_still_never_posts_twice(env, monkeypatch):
    sender = FakeSender([OK, OK, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    monkeypatch.setattr(env.store, "dispatch_finish",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    result = _deliver(notifier)
    assert result.outcome == CONFIRMED and not result.receipt_saved  # it WAS delivered
    assert len(sender.calls) == 1
    monkeypatch.undo()
    assert dispatch_rows(env.db)[0]["state"] == "sending"
    assert _deliver(notifier).outcome == "in_progress" and len(sender.calls) == 1
    env.clock.advance(200)
    assert _deliver(notifier).outcome == UNCERTAIN and len(sender.calls) == 1


def test_b5_stale_token_cannot_overwrite_a_reaped_row(env):
    notifier = make_notifier(env.store, FakeSender([OK]), env.clock)
    _deliver(notifier)
    key = dispatch_rows(env.db)[0]["business_key"]
    with pytest.raises(DispatchClaimLost):
        env.store.dispatch_finish(key, claim_token="stale", attempt_no=1, result=UNSENT)


# ==================================================================== B4 rate limit
def test_b4_retry_after_is_honoured_in_full_not_capped_to_five_seconds(env):
    sender = FakeSender([limited(12.0), OK])
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert result.outcome == CONFIRMED and result.attempts == 2
    assert env.clock.sleeps == [12.0]  # the pre-v2 sender capped this at 5 s
    assert (env.clock.value - FakeClock().value).total_seconds() >= 12.0


def test_b4_retry_after_beyond_remaining_budget_stops_explicitly_and_does_not_retry_early(env):
    sender = FakeSender([limited(60.0), OK])
    notifier = make_notifier(env.store, sender, env.clock)
    result = _deliver(notifier)
    assert result.outcome == "rate_limit_over_budget" and result.reason == "rate_limit_wait_exceeds_budget"
    assert len(sender.calls) == 1 and env.clock.sleeps == []
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "rate_limited_over_budget" and row["state"] != "uncertain"
    assert row["last_retry_after"] == 60.0
    env.clock.advance(120)  # even after the advertised wait, automation does not resend
    assert _deliver(notifier).outcome == "rate_limit_over_budget" and len(sender.calls) == 1


def test_b4_wait_that_fits_the_remaining_budget_is_taken_but_second_429_over_budget_stops(env):
    sender = FakeSender([limited(30.0), limited(30.0), OK])
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert env.clock.sleeps == [30.0]
    assert result.outcome == "rate_limit_over_budget" and len(sender.calls) == 2


def test_b4_bool_interface_has_no_internal_retry_even_for_429(monkeypatch, null_logger):
    sleeper = MagicMock()
    monkeypatch.setattr("time.sleep", sleeper)
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=429)
    session.post.return_value.json.return_value = {"ok": False, "parameters": {"retry_after": 1}}
    n = TelegramNotifier(TOKEN, CHAT_ID, null_logger, session=session)
    assert n.send_system_alert("a", "b") is False and n.send("a", "b") is False
    assert session.post.call_count == 2
    sleeper.assert_not_called()


# =============================================================== B3 same key, other body
def test_b3_same_business_key_with_different_body_stops_and_preserves_original(env):
    sender = FakeSender([OK, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    assert _deliver(notifier, message="original text").outcome == CONFIRMED
    stopped = _deliver(notifier, message="CORRECTED text")
    assert stopped.outcome == "blocked_conflict" and not stopped
    assert len(sender.calls) == 1
    row = dispatch_rows(env.db)[0]
    assert "original text" in row["payload_json"] and "CORRECTED" not in row["payload_json"]
    assert row["conflict_count"] == 1 and row["last_conflict_kind"] == "payload"
    assert len(dispatch_rows(env.db)) == 1  # no new send entitlement


def test_b3_conflict_on_a_not_yet_sent_event_does_not_adopt_the_new_body(env):
    sender = FakeSender([UNSURE, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    _deliver(notifier, message="first")
    assert _deliver(notifier, message="second").outcome == "blocked_conflict"
    assert len(sender.calls) == 1 and "second" not in dispatch_rows(env.db)[0]["payload_json"]


def test_b3_changed_target_conversation_stops_and_does_not_move_the_old_message(env):
    a = FakeSender([OK, OK], digest="target-digest-a")
    assert _deliver(make_notifier(env.store, a, env.clock)).outcome == CONFIRMED
    b = FakeSender([OK, OK], digest="target-digest-OTHER-CHAT")
    result = _deliver(make_notifier(env.store, b, env.clock))
    assert result.outcome == "blocked_target_changed" and b.calls == []
    row = dispatch_rows(env.db)[0]
    assert row["target_digest"] == "target-digest-a" and row["last_conflict_kind"] == "target"


def test_b3_changed_target_before_first_send_also_stops(env):
    first = FakeSender([UNSURE], digest="target-digest-a")
    _deliver(make_notifier(env.store, first, env.clock))
    second = FakeSender([OK], digest="other")
    assert _deliver(make_notifier(env.store, second, env.clock)).outcome == "blocked_target_changed"
    assert second.calls == []


def test_b3_different_namespace_is_a_different_business_event(env):
    sender = FakeSender([OK, OK])
    assert _deliver(make_notifier(env.store, sender, env.clock, scope="eod:2026-10-12")).confirmed
    assert _deliver(make_notifier(env.store, sender, env.clock, scope="eod:2026-10-13")).confirmed
    assert len(sender.calls) == 2


def test_b3_trend_identity_ignores_body_details_but_not_the_data_date(env):
    sender = FakeSender([OK, OK, OK])
    n = make_notifier(env.store, sender, env.clock)
    assert n.send_trend_alert_ex("N", "510300", "金叉", "W", {"data_date": "2026-10-09", "ma": 1}).confirmed
    assert n.send_trend_alert_ex("N", "510300", "金叉", "W", {"data_date": "2026-10-09", "ma": 2}).outcome == "blocked_conflict"
    assert n.send_trend_alert_ex("N", "510300", "金叉", "W", {"data_date": "2026-10-12", "ma": 1}).confirmed
    assert len(sender.calls) == 2


# =================================================================== B2 two queues
def test_b2_legacy_sent_blocks_a_duplicate_even_though_the_new_table_is_empty(env):
    _legacy_row(env.store, "sent")
    assert dispatch_rows(env.db) == []  # new table empty != "never sent"
    sender = FakeSender([OK])
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert result.outcome == CONFIRMED and result.duplicate and sender.calls == []
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "confirmed" and row["legacy_ref"].startswith("outbox:") and row["attempts_used"] == 0


def test_b2_legacy_sent_in_another_scope_is_the_same_business_event(env):
    """P1-1 (replaces the earlier wrong assertion that this must be sent again).

    The old EOD key was ``eod:{audit_run_id}``; after the switch to ``eod:{data date}`` the
    exact-key match fails.  The already *sent* daily report is still the same business
    event (same operation + same dated title) and must NOT get a new send entitlement."""
    _legacy_row(env.store, "sent", scope="eod:some-old-run-id")
    sender = FakeSender([OK])
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert result.outcome == CONFIRMED and result.duplicate and sender.calls == []
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "confirmed" and row["attempts_used"] == 0
    assert row["legacy_ref"].startswith("outbox:")


@pytest.mark.parametrize("legacy_status", ["failed", "sending", "pending"])
def test_b2_legacy_unfinished_rows_are_quarantined_not_retried(env, legacy_status):
    _legacy_row(env.store, legacy_status)
    sender = FakeSender([OK])
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert result.outcome == "quarantined_legacy" and not result and sender.calls == []
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "quarantined_legacy" and row["reason_code"] == f"legacy_{legacy_status}"
    assert row["attempts_used"] == 0


def test_b2_legacy_failed_row_with_other_scope_and_rebuilt_body_for_same_event_is_quarantined(env):
    """The 10/09 situation: legacy failed daily report; a rebuilt text under a new scope must
    not slip through as a 'new' event."""
    _legacy_row(env.store, "failed", scope="watchdog:2026-10-09:daily", title="EOD 日报 2026-10-09", message="old text")
    sender = FakeSender([OK])
    n = make_notifier(env.store, sender, env.clock, scope="watchdog:2026-10-09:daily-rerun")
    result = n.send_system_alert_ex("EOD 日报 2026-10-09", "rebuilt text")
    assert result.outcome == "quarantined_legacy" and sender.calls == []


def test_b2_unrelated_legacy_failed_row_does_not_block_a_new_event(env):
    _legacy_row(env.store, "failed", title="EOD 日报 2026-09-01", message="old")
    sender = FakeSender([OK])
    assert _deliver(make_notifier(env.store, sender, env.clock)).confirmed
    assert len(sender.calls) == 1


def test_b2_legacy_table_and_rows_are_never_modified_by_v2(env):
    _legacy_row(env.store, "sent", title="a")
    _legacy_row(env.store, "failed", title="b")
    _legacy_row(env.store, "sending", title="c")
    before = _legacy_snapshot(env.db)
    sender = FakeSender([OK, OK, OK])
    n = make_notifier(env.store, sender, env.clock)
    for title in ("a", "b", "c", "d"):
        _deliver(n, title=title)
    assert _legacy_snapshot(env.db) == before


def test_historical_failed_and_sending_are_not_woken_by_upgrade(tmp_path):
    """Build a v5 DB with legacy sent/failed/sending/pending rows, migrate, open with v2: nothing is sent."""
    db = tmp_path / "swing.db"
    store = SwingStore(str(db))
    for status in ("sent", "failed", "sending", "pending"):
        _legacy_row(store, status, title=f"hist-{status}")
    before = _legacy_snapshot(db)
    conn = sqlite3.connect(str(db))
    migrate_notification_v6(conn)
    conn.close()
    sender = FakeSender([OK] * 5)
    store2 = SwingStore(str(db))
    n = make_notifier(store2, sender, FakeClock())
    # constructing / health / metrics paths never POST
    store2.outbox_counts(); store2.notification_health(); write_notification_health(store2)
    assert sender.calls == [] and dispatch_rows(db) == []
    assert _legacy_snapshot(db) == before
    # re-delivering each historical event: sent -> duplicate, others -> quarantined, never a POST
    outcomes = {s: _deliver(n, title=f"hist-{s}", message=MESSAGE, ).outcome for s in ("sent", "failed", "sending", "pending")}
    assert outcomes == {"sent": CONFIRMED, "failed": "quarantined_legacy",
                        "sending": "quarantined_legacy", "pending": "quarantined_legacy"}
    assert sender.calls == []


# =================================================================== B1 versions / flag
def test_b1_v5_and_v6_are_accepted_and_v7_is_rejected(tmp_path):
    db = tmp_path / "swing.db"
    SwingStore(str(db))
    assert SwingStore(str(db)).schema_version() == 5
    conn = sqlite3.connect(str(db)); migrate_notification_v6(conn); conn.close()
    assert SwingStore(str(db)).schema_version() == 6
    conn = sqlite3.connect(str(db)); conn.execute("PRAGMA user_version=7"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match="高于应用支持版本"):
        SwingStore(str(db))
    conn = sqlite3.connect(str(db))
    with pytest.raises(RuntimeError, match="高于应用支持版本"):
        migrate(conn)
    with pytest.raises(RuntimeError, match="高于应用支持版本"):
        migrate_notification_v6(conn)
    conn.close()


def test_b1_v7_database_never_reaches_the_sender(tmp_path):
    db = tmp_path / "swing.db"
    make_v6_store(db)
    conn = sqlite3.connect(str(db)); conn.execute("PRAGMA user_version=7"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError):
        SwingStore(str(db))


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE notification_attempt",
        "DROP TRIGGER trg_notification_dispatch_no_delete",
        "ALTER TABLE notification_dispatch RENAME COLUMN state TO state_old",
    ],
)
def test_b1_v6_with_damaged_structure_is_rejected(tmp_path, damage):
    db = tmp_path / "swing.db"
    make_v6_store(db)
    conn = sqlite3.connect(str(db)); conn.execute(damage); conn.commit(); conn.close()
    with pytest.raises(RuntimeError):
        SwingStore(str(db))


def test_b1_v5_with_stray_dispatch_tables_is_rejected(tmp_path):
    db = tmp_path / "swing.db"
    make_v6_store(db)
    conn = sqlite3.connect(str(db)); conn.execute("PRAGMA user_version=5"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match="版本 6 的表"):
        SwingStore(str(db))


def test_b1_plain_swing_store_never_upgrades_5_to_6_implicitly(tmp_path):
    db = tmp_path / "swing.db"
    SwingStore(str(db)); SwingStore(str(db))
    conn = sqlite3.connect(str(db))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "notification_dispatch" not in names and conn.execute("PRAGMA user_version").fetchone()[0] == 5


@pytest.mark.parametrize("flag", ["off", "0", "false", "paused"])
def test_b1_flag_off_pauses_sending_and_does_not_fall_back_to_any_sender(env, monkeypatch, flag):
    monkeypatch.setenv(FLAG_ENV, flag)
    sender = FakeSender([OK])
    legacy_before = _legacy_snapshot(env.db)
    result = _deliver(make_notifier(env.store, sender, env.clock))
    assert result.outcome == "paused" and not result and result.reason == "feature_flag_off"
    assert sender.calls == [] and dispatch_rows(env.db) == [] and _legacy_snapshot(env.db) == legacy_before


def test_b1_flag_off_does_not_resend_already_attempted_events_when_turned_back_on(env, monkeypatch):
    sender = FakeSender([UNSURE, OK])
    n = make_notifier(env.store, sender, env.clock)
    assert _deliver(n).outcome == UNCERTAIN
    monkeypatch.setenv(FLAG_ENV, "off")
    assert _deliver(n).outcome == "paused"
    monkeypatch.setenv(FLAG_ENV, "on")
    assert _deliver(n).outcome == UNCERTAIN and len(sender.calls) == 1


def test_b1_schema_v5_database_pauses_instead_of_using_the_legacy_queue(tmp_path, monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    store = SwingStore(str(tmp_path / "swing.db"))
    sender = FakeSender([OK])
    result = _deliver(make_notifier(store, sender, FakeClock()))
    assert result.outcome == "paused" and result.reason == "schema_version_5" and sender.calls == []
    assert store.outbox_counts()["sent"] == 0


# ==================================================================== migration
def _table_names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')")}
    finally:
        conn.close()


def test_migration_adds_only_new_objects_and_leaves_legacy_untouched(tmp_path):
    db = tmp_path / "swing.db"
    store = SwingStore(str(db))
    _legacy_row(store, "sent", title="a"); _legacy_row(store, "failed", title="b")
    before_names, before_legacy = _table_names(db), _legacy_snapshot(db)
    conn = sqlite3.connect(str(db)); migrate_notification_v6(conn); verify_notification_schema(conn); conn.close()
    added = _table_names(db) - before_names
    assert {"notification_dispatch", "notification_attempt", "trg_notification_dispatch_no_delete",
            "trg_notification_attempt_no_delete"} <= added
    assert not (before_names - _table_names(db))
    assert _legacy_snapshot(db) == before_legacy


def test_migration_is_idempotent_at_v6(tmp_path):
    db = tmp_path / "swing.db"
    make_v6_store(db)
    conn = sqlite3.connect(str(db))
    assert migrate_notification_v6(conn) == 6 and migrate_notification_v6(conn) == 6
    conn.close()


@pytest.mark.parametrize("step", MIGRATION_6_STEPS)
def test_migration_interrupted_by_exception_rolls_back_and_rerun_succeeds(tmp_path, step):
    db = tmp_path / "swing.db"
    SwingStore(str(db))
    before = _table_names(db)

    def fault(name):
        if name == step:
            raise RuntimeError("injected")

    conn = sqlite3.connect(str(db))
    with pytest.raises(RuntimeError, match="injected"):
        migrate_notification_v6(conn, _fault=fault)
    conn.close()
    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    conn.close()
    assert _table_names(db) == before  # nothing half-created
    SwingStore(str(db))  # still opens as v5
    conn = sqlite3.connect(str(db))
    assert migrate_notification_v6(conn) == 6
    conn.close()
    assert SwingStore(str(db)).dispatch_ready() == (True, "ok")


@pytest.mark.parametrize("step", MIGRATION_6_STEPS)
def test_migration_killed_mid_transaction_leaves_a_clean_v5_and_rerun_succeeds(tmp_path, step):
    db = tmp_path / "swing.db"
    store = SwingStore(str(db))
    _legacy_row(store, "failed", title="kept")
    before_legacy, before_names = _legacy_snapshot(db), _table_names(db)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]).strip(os.pathsep)}
    proc = subprocess.run(
        [sys.executable, "-m", "tests.notify_v2_worker", "migrate_fault", str(db), step],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 9, proc.stderr
    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    conn.close()
    assert _table_names(db) == before_names and _legacy_snapshot(db) == before_legacy
    conn = sqlite3.connect(str(db))
    assert migrate_notification_v6(conn) == 6
    conn.close()
    assert _legacy_snapshot(db) == before_legacy


def test_dispatch_history_cannot_be_deleted(env):
    _deliver(make_notifier(env.store, FakeSender([OK]), env.clock))
    conn = sqlite3.connect(str(env.db))
    for table in ("notification_attempt", "notification_dispatch"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
    conn.close()


def test_rollback_by_flag_keeps_history_and_schema(env, monkeypatch):
    _deliver(make_notifier(env.store, FakeSender([UNSURE]), env.clock))
    monkeypatch.setenv(FLAG_ENV, "off")
    _deliver(make_notifier(env.store, FakeSender([OK]), env.clock), title="another")
    assert env.store.schema_version() == 6 and len(dispatch_rows(env.db)) == 1


# ================================================= returned object misuse / callers
def test_structured_results_are_falsy_unless_confirmed_so_old_callers_cannot_misread_them(env):
    assert not SendResult(UNCERTAIN) and not SendResult("not_sent_retryable") and SendResult(CONFIRMED)
    for outcome in ("uncertain", "paused", "failed_exhausted", "blocked_conflict", "quarantined_legacy",
                    "rate_limit_over_budget", "in_progress", "retry_wait_pending", "not_sent_permanent"):
        assert not DeliveryResult(outcome)
    n = make_notifier(env.store, FakeSender([UNSURE]), env.clock)
    assert n.send_system_alert(TITLE, MESSAGE) is False
    d = n.send_system_alert_ex(TITLE, MESSAGE)
    assert not d
    fields = _alert_fields(d)
    assert fields["alert_confirmed"] is False and fields["alert_sent"]["confirmed"] is False
    assert set(fields["alert_sent"]) == {"outcome", "attempts", "confirmed"}
    ok = _alert_fields(DeliveryResult(CONFIRMED, attempts=1))
    assert ok["alert_confirmed"] is True and ok["alert_sent"] == {"outcome": CONFIRMED, "attempts": 1, "confirmed": True}


def _signal():
    return SimpleNamespace(name="N", code="510300", title="周线金叉", period="W", confidence=0.8,
                           signal_type="weekly_cross", detail={"data_date": "2026-10-09"})


def test_reporter_only_marks_a_signal_notified_when_delivery_is_confirmed(env):
    for outcomes, expected_keys, unconfirmed in (
        ([OK], [("510300", "weekly_cross", "W", "2026-10-09")], 0),
        ([UNSURE], [], 1),
        ([PERMANENT], [], 1),
    ):
        store = make_v6_store(env.tmp / f"r{len(expected_keys)}{unconfirmed}{outcomes[0].outcome}.db", env.clock)
        notifier = make_notifier(store, FakeSender(outcomes), env.clock)
        out = SwingReporter(notifier).report([], [_signal()])
        assert out["sent_signal_keys"] == expected_keys and out["unconfirmed"] == unconfirmed
        assert out["sent"] == (1 if expected_keys else 0)
        assert all(set(d) >= {"outcome", "attempts", "confirmed"} for d in out["deliveries"])


def test_reporter_paused_notifier_never_marks_notified(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "off")
    store = make_v6_store(tmp_path / "p.db", FakeClock())
    out = SwingReporter(make_notifier(store, FakeSender([OK]))).report([], [_signal()])
    assert out["sent_signal_keys"] == [] and out["unconfirmed"] == 1


def test_reporter_keeps_working_with_bool_only_notifiers():
    notifier = MagicMock()
    notifier.send_trend_alert.return_value = True
    out = SwingReporter(notifier).report([], [_signal()])
    assert out["sent"] == 1 and len(out["sent_signal_keys"]) == 1


# ================================================ health: metrics / exporter / acceptance
def _make_problem(kind, env):
    clock, store = env.clock, env.store
    from radar.notify import identity
    make_notifier(store, FakeSender([]), clock)
    if kind == "uncertain":
        make_notifier(store, FakeSender([UNSURE]), clock).send_system_alert_ex("t-uncertain", "m")
    elif kind == "failed_exhausted":
        make_notifier(store, FakeSender([UNSENT] * 3), clock).send_system_alert_ex("t-exhausted", "m")
    elif kind == "rate_limited_over_budget":
        make_notifier(store, FakeSender([limited(90)]), clock).send_system_alert_ex("t-limited", "m")
    elif kind == "failed_permanent":
        make_notifier(store, FakeSender([PERMANENT]), clock).send_system_alert_ex("t-perm", "m")
    elif kind == "quarantined_legacy":
        _legacy_row(store, "failed", title="t-quar", message="m")
        make_notifier(store, FakeSender([OK]), clock).send_system_alert_ex("t-quar", "m")
    elif kind == "conflicts":
        s = FakeSender([OK, OK]); nn = make_notifier(store, s, clock)
        nn.send_system_alert_ex("t-conf", "one"); nn.send_system_alert_ex("t-conf", "two")
    elif kind in ("sending_expired", "retry_wait_stuck", "pending_stale"):
        business = identity.business_id("send_system_alert", ["t-x", "m"], {})
        key = identity.business_key("eod:2026-10-12", "telegram", "send_system_alert", business)
        obj = identity.legacy_payload(["t-x", "m"], {})
        store.dispatch_enqueue(
            business_key=key, namespace="eod:2026-10-12", channel="telegram", operation="send_system_alert",
            target_alias="a", target_digest="d", payload_json=identity.canonical_json(obj),
            payload_sha256=identity.payload_sha256(["t-x", "m"], {}), payload_obj=obj, business=business, legacy_keys=[])
        if kind == "sending_expired":
            store.dispatch_claim(key)
        elif kind == "retry_wait_stuck":
            claim = store.dispatch_claim(key)
            store.dispatch_finish(key, claim_token=claim["claim_token"], attempt_no=1, result=UNSENT)
        clock.advance(400)


PROBLEMS = ["uncertain", "failed_exhausted", "rate_limited_over_budget", "failed_permanent",
            "quarantined_legacy", "conflicts", "sending_expired", "retry_wait_stuck", "pending_stale"]


def _backlog(text):
    return next(float(l.split()[1]) for l in text.splitlines() if l.startswith("radar_swing_notification_backlog "))


def _overview(store):
    return {"available": False, "outbox_counts": store.outbox_counts(),
            "notification_health": store.notification_health()}


def _exporter(db, now):
    return {l.split()[0]: l.split()[1] for l in watchdog_metrics(
        Path(db).parent / "swing_eod_watchdog.json", Path(db).parent / "no-calendar.json", now,
        db_path=Path(db)).splitlines()
        if l and not l.startswith("#")}


def _acceptance(db):
    conn = acceptance._open_readonly_sqlite(Path(db))
    try:
        return acceptance.inspect_outbox(conn, decision_date="2026-10-12")
    finally:
        conn.close()


def test_healthy_state_reads_healthy_everywhere(env):
    _deliver(make_notifier(env.store, FakeSender([OK]), env.clock))
    text = render_swing_metrics(_overview(env.store))
    assert _backlog(text) == 0 and "radar_swing_notification_review_needed 0" in text
    write_notification_health(env.store, env.clock.now())
    metrics = _exporter(env.db, env.clock.now())
    assert metrics["radar_swing_eod_notification_health_valid"] == "1"
    assert metrics["radar_swing_eod_notification_review_needed"] == "0"
    assert _acceptance(env.db)["status"] == "pass"


@pytest.mark.parametrize("kind", PROBLEMS)
def test_every_problem_state_is_unhealthy_in_backlog_exporter_and_acceptance(env, kind):
    _make_problem(kind, env)
    health = env.store.notification_health()
    assert health["available"] and health["review_needed"] >= 1 and health[kind] >= 1
    text = render_swing_metrics(_overview(env.store))
    assert _backlog(text) >= 1
    assert f'radar_swing_notification_problem{{reason="{kind}"}} ' in text
    assert float(next(l for l in text.splitlines() if l.startswith(
        f'radar_swing_notification_problem{{reason="{kind}"}}')).split()[1]) >= 1
    write_notification_health(env.store, env.clock.now())
    metrics = _exporter(env.db, env.clock.now())
    assert metrics["radar_swing_eod_notification_health_valid"] == "1"
    assert float(metrics["radar_swing_eod_notification_review_needed"]) >= 1
    assert float(metrics[f"radar_swing_eod_notification_{kind if kind != 'conflicts' else 'conflicts'}"]) >= 1
    outcome = _acceptance(env.db)
    assert outcome["status"] == "fail", outcome


def test_exporter_never_reports_unknown_notification_state_as_zero(tmp_path):
    metrics = _exporter(tmp_path / "swing.db", datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert metrics["radar_swing_eod_notification_health_valid"] == "0"
    assert metrics["radar_swing_eod_notification_review_needed"] == "NaN"
    store = SwingStore(str(tmp_path / "swing.db"))  # v5: tables unavailable -> still unknown, not 0
    write_notification_health(store)
    metrics = _exporter(tmp_path / "swing.db", datetime.now(timezone.utc))
    assert metrics["radar_swing_eod_notification_health_valid"] == "0"
    assert metrics["radar_swing_eod_notification_review_needed"] == "NaN"
    (tmp_path / HEALTH_FILE).write_text('{"schema_version":1,"available":true,"checked_at":"2026-10-12T00:00:00Z","review_needed":"0"}')
    assert _exporter(tmp_path / "swing.db", datetime.now(timezone.utc))["radar_swing_eod_notification_health_valid"] == "0"


def test_human_review_clears_the_alarm_but_never_changes_state_or_resends(env):
    sender = FakeSender([UNSURE, OK])
    n = make_notifier(env.store, sender, env.clock)
    _deliver(n)
    key = dispatch_rows(env.db)[0]["business_key"]
    assert env.store.dispatch_review(key, "checked Telegram: not received") is True
    health = env.store.notification_health()
    assert health["uncertain"] == 0 and health["review_needed"] == 0
    assert dispatch_rows(env.db)[0]["state"] == "uncertain"
    assert _deliver(n).outcome == UNCERTAIN and len(sender.calls) == 1


def test_acceptance_marks_pre_migration_db_as_not_migrated_but_not_unhealthy(tmp_path):
    db = tmp_path / "swing.db"
    SwingStore(str(db))
    result = _acceptance(db)
    assert result["status"] == "pass" and result["evidence"]["dispatch"]["status"] == "not_migrated"


# ============================================================================ redaction
def test_no_token_chat_id_or_body_leaks_anywhere_except_the_stored_payload(env, caplog):
    body = f"report {BODY_MARK}"
    session = MagicMock()
    bodies = [
        MagicMock(status_code=503), MagicMock(status_code=429),
    ]
    bodies[0].json.return_value = {"ok": False, "description": f"{CHAT_ID} {TOKEN} {BODY_MARK}"}
    bodies[1].json.return_value = {"ok": False, "description": f"{CHAT_ID} {TOKEN}", "parameters": {"retry_after": 1}}
    session.post.side_effect = [
        requests.ConnectionError(MaxRetryError(None, f"/bot{TOKEN}/sendMessage {CHAT_ID} {BODY_MARK}",
                                               NewConnectionError(None, f"{TOKEN} {CHAT_ID}"))),
        bodies[1],
        bodies[0],
    ]
    log = logging.getLogger("test.redaction")
    log.propagate = True
    caplog.set_level(logging.DEBUG)
    base = TelegramNotifier(TOKEN, CHAT_ID, log, session=session)
    notifier = DurableOutboxNotifier(env.store, base, log, scope="eod:2026-10-12", sleep=env.clock.sleep)
    result = notifier.send_system_alert_ex("redaction title", body)
    assert result.outcome == UNCERTAIN
    write_notification_health(env.store, env.clock.now())
    texts = {
        "logs": "\n".join(r.getMessage() + repr(r.args) for r in caplog.records),
        "result": repr(result) + json.dumps(result.summary()),
        "health_file": (env.tmp / HEALTH_FILE).read_text(),
        "metrics": render_swing_metrics(_overview(env.store)),
        "exporter": "\n".join(_exporter(env.db, env.clock.now()).keys()),
        "acceptance": json.dumps(_acceptance(env.db), default=str),
    }
    # every DB column except the deliberately stored payload
    conn = sqlite3.connect(str(env.db)); conn.row_factory = sqlite3.Row
    cols = []
    for row in conn.execute("SELECT * FROM notification_dispatch"):
        cols += [str(v) for k, v in dict(row).items() if k != "payload_json"]
    for row in conn.execute("SELECT * FROM notification_attempt"):
        cols += [str(v) for v in dict(row).values()]
    conn.close()
    texts["db_non_payload"] = "\n".join(cols)
    for name, text in texts.items():
        for secret in (TOKEN, TOKEN.split(":")[1], CHAT_ID, BODY_MARK):
            assert secret not in text, f"{secret[:8]}… leaked in {name}"
    assert len(session.post.call_args_list) == 3  # 1 uncertain-free path: 2 not-sent retries + final uncertain


# ==================================== end-to-end through the real TelegramNotifier (fake HTTP)
def _http(status, body):
    response = MagicMock(status_code=status)
    response.json.return_value = body
    return response


def _receipt(message_id=11):
    return {"ok": True, "result": {"message_id": message_id, "chat": {"id": int(CHAT_ID)}}}


def _e2e(env, side_effects):
    session = MagicMock()
    session.post.side_effect = side_effects
    log = logging.getLogger("test.e2e")
    base = TelegramNotifier(TOKEN, CHAT_ID, log, session=session)
    return session, DurableOutboxNotifier(env.store, base, log, scope="eod:2026-10-12", sleep=env.clock.sleep)


def test_e2e_confirmed_receipt_is_stored_and_second_call_posts_nothing(env):
    session, n = _e2e(env, [_http(200, _receipt(11))])
    assert n.send_system_alert_ex(TITLE, MESSAGE).confirmed
    assert n.send_system_alert(TITLE, MESSAGE) is True
    assert session.post.call_count == 1 and dispatch_rows(env.db)[0]["message_id"] == 11


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_e2e_5xx_is_one_post_then_uncertain_never_retried(env, status):
    session, n = _e2e(env, [_http(status, {"ok": False}), _http(200, _receipt())])
    assert n.send_system_alert_ex(TITLE, MESSAGE).outcome == UNCERTAIN
    assert n.send_system_alert_ex(TITLE, MESSAGE).outcome == UNCERTAIN
    assert session.post.call_count == 1


def test_e2e_dns_failure_is_retried_within_budget_then_confirmed(env):
    dns = requests.ConnectionError(MaxRetryError(None, "/sendMessage", NewConnectionError(None, "name resolution")))
    session, n = _e2e(env, [dns, dns, _http(200, _receipt())])
    result = n.send_system_alert_ex(TITLE, MESSAGE)
    assert result.confirmed and result.attempts == 3 and session.post.call_count == 3
    assert env.clock.sleeps == [0.5, 1.0]


def test_e2e_ssl_eof_without_stage_evidence_is_uncertain_and_not_retried(env):
    session, n = _e2e(env, [requests.exceptions.SSLError("EOF occurred in violation of protocol (SSLEOFError)"),
                            _http(200, _receipt())])
    assert n.send_system_alert_ex(TITLE, MESSAGE).outcome == UNCERTAIN and session.post.call_count == 1


def test_e2e_429_waits_the_full_retry_after_then_succeeds(env):
    limited_response = _http(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 9}})
    session, n = _e2e(env, [limited_response, _http(200, _receipt())])
    assert n.send_system_alert_ex(TITLE, MESSAGE).confirmed
    assert env.clock.sleeps == [9.0] and session.post.call_count == 2


def test_e2e_permanent_403_is_not_retried(env):
    session, n = _e2e(env, [_http(403, {"ok": False, "error_code": 403}), _http(200, _receipt())])
    assert n.send_system_alert_ex(TITLE, MESSAGE).outcome == "not_sent_permanent" and session.post.call_count == 1


# ============================================================ online callers are migrated
def test_online_callers_use_the_structured_api_and_stable_namespace():
    eod = (ROOT / "radar_swing_eod.py").read_text(encoding="utf-8")
    watchdog = (ROOT / "scripts" / "check_swing_eod_terminal.py").read_text(encoding="utf-8")
    reporter = (ROOT / "radar" / "swing" / "reporter.py").read_text(encoding="utf-8")
    assert 'scope=f"eod:{macro_slice.date}"' in eod and "audit_run_id or macro_slice.date" not in eod
    assert "mark_signals_notified(result.get(\"sent_signal_keys\"" in eod
    assert watchdog.count("send_system_alert_ex(") == 2 and "result[\"alert_sent\"] =" not in watchdog
    assert "_deliver(" in reporter and "if confirmed:" in reporter and "notifier.send_trend_alert(" not in reporter
