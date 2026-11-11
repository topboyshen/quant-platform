"""Code-review round 2 of notification v2 (each test is a counter-example of the first candidate).

P1-1  a legacy *sent* record must still count when the scope/key changed (eod:{run id} -> eod:{date})
P1-2  a recorded conflict must really stop later claims (pending / retry_wait / concurrent)
P2-1  a redirect must never cause a second request (real requests client + loopback server)
P2-2  independent monitoring must not keep showing a stale green snapshot

All data synthetic, temporary databases only, loopback-only network (see the runner's guard).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

from radar.notify.health import HEALTH_FILE, write_notification_health
from radar.notify.outbox import FLAG_ENV, DurableOutboxNotifier
from radar.notify.result import CONFIRMED, UNCERTAIN
from radar.notify.telegram import TelegramNotifier
from runtime_contracts.watchdog_metrics import watchdog_metrics
from tests.notify_v2_helpers import (
    CHAT_ID,
    OK,
    TOKEN,
    UNSENT,
    UNSURE,
    FakeClock,
    FakeSender,
    attempt_rows,
    dispatch_rows,
    make_notifier,
    make_v6_store,
)

TITLE, MESSAGE = "EOD 日报 2026-10-12", "synthetic daily report text"
SAME_DAY = "2026-10-12T02:00:00Z"
OTHER_DAY = "2026-10-09T02:00:00Z"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    clock = FakeClock()
    db = tmp_path / "swing_macro.db"  # the exporter's default DB name next to the watchdog file
    store = make_v6_store(db, clock)
    return SimpleNamespace(db=db, clock=clock, store=store, tmp=tmp_path)


def _legacy(store, status, *, scope, title=TITLE, message=MESSAGE):
    payload = {"args": [title, message], "kwargs": {}}
    key = store.notification_key("send_system_alert", {"scope": scope, **payload})
    store.enqueue_notification(key, operation="send_system_alert", payload=payload)
    store.begin_notification_delivery(key)
    store.finish_notification_delivery(key, success=status == "sent", error="x")
    return key


def _raw_legacy(db, operation, payload, *, status="sent", created_at=SAME_DAY, key=None):
    """Insert a legacy outbox row directly (for shapes the old API can no longer produce)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    key = key or hashlib.sha256((operation + text + created_at).encode()).hexdigest()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO notification_outbox (idempotency_key,channel,operation,payload_json,status,"
            "attempts,created_at) VALUES (?,?,?,?,?,?,?)",
            (key, "telegram", operation, text, status, 1, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _legacy_snapshot(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT * FROM notification_outbox ORDER BY id").fetchall()
    finally:
        conn.close()


# ======================================================================= P1-1
def test_p11_old_run_id_key_sent_report_is_not_sent_again_after_a_restart(env):
    _legacy(env.store, "sent", scope="eod:audit-run-20261012-153000")
    for _ in range(2):  # a fresh notifier each time == a new process
        sender = FakeSender([OK, OK])
        result = make_notifier(env.store, sender, env.clock).send_system_alert_ex(TITLE, MESSAGE)
        assert result.outcome == CONFIRMED and result.duplicate and sender.calls == []
    assert [r["state"] for r in dispatch_rows(env.db)] == ["confirmed"]


def test_p11_old_success_with_changed_body_is_a_conflict_and_nothing_is_sent(env):
    key = _legacy(env.store, "sent", scope="eod:audit-run-1", message="the text that WAS delivered")
    before = _legacy_snapshot(env.db)
    sender = FakeSender([OK, OK])
    notifier = make_notifier(env.store, sender, env.clock)
    first = notifier.send_system_alert_ex(TITLE, "a rebuilt, different text")
    assert first.outcome == "blocked_conflict" and not first and sender.calls == []
    row = dispatch_rows(env.db)[0]
    assert row["conflict_count"] == 1 and row["last_conflict_kind"] == "payload"
    assert row["state"] == "quarantined_legacy" and row["reason_code"] == "legacy_sent_payload_changed"
    assert row["attempts_used"] == 0 and row["legacy_ref"].startswith("outbox:")
    # later calls (either body) never gain a send entitlement
    for body in ("a rebuilt, different text", "the text that WAS delivered", "yet another"):
        later = notifier.send_system_alert_ex(TITLE, body)
        assert not later and later.outcome in ("blocked_conflict", "quarantined_legacy", "confirmed")
    assert sender.calls == [] and attempt_rows(env.db) == []
    assert _legacy_snapshot(env.db) == before and key  # legacy history untouched
    assert env.store.notification_health()["review_needed"] >= 1  # visible, not silently healthy


def test_p11_unmappable_same_day_legacy_row_pauses_and_enters_review(env):
    _raw_legacy(env.db, "send_system_alert", "this is not json{", status="sent")
    sender = FakeSender([OK])
    result = make_notifier(env.store, sender, env.clock).send_system_alert_ex(TITLE, MESSAGE)
    assert result.outcome == "quarantined_legacy" and sender.calls == []
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "quarantined_legacy" and row["reason_code"] == "legacy_unmappable"
    assert env.store.notification_health()["quarantined_legacy"] == 1


def test_p11_unmappable_row_from_another_day_does_not_block_todays_event(env):
    _raw_legacy(env.db, "send_system_alert", "this is not json{", status="sent", created_at=OTHER_DAY)
    sender = FakeSender([OK])
    assert make_notifier(env.store, sender, env.clock).send_system_alert_ex(TITLE, MESSAGE).confirmed
    assert len(sender.calls) == 1


def test_p11_undated_identity_is_only_matched_within_the_same_day(env):
    payload = {"args": ["fact A"], "kwargs": {"level": "冰点", "detail": {"factor": "turnover"}}}
    _raw_legacy(env.db, "send_macro_alert", payload, created_at=OTHER_DAY, key="k-old-day")
    sender = FakeSender([OK, OK])
    n = make_notifier(env.store, sender, env.clock)
    assert n.send_macro_alert_ex("fact A", "冰点", {"factor": "turnover"}).confirmed  # other day: new event
    assert len(sender.calls) == 1
    _raw_legacy(env.db, "send_macro_alert", {**payload, "args": ["fact B"]}, created_at=SAME_DAY, key="k-today")
    sender2 = FakeSender([OK])
    clock_next = FakeClock(datetime(2026, 10, 13, 2, 0, 0, tzinfo=timezone.utc))  # next day, new namespace
    n3 = make_notifier(env.store, sender2, clock_next, scope="eod:2026-10-13")
    assert n3.send_macro_alert_ex("fact B", "冰点", {"factor": "turnover"}).confirmed
    assert len(sender2.calls) == 1


def test_p11_undated_same_day_success_with_same_body_is_a_duplicate_and_changed_body_a_conflict(env):
    payload = {"args": ["fact A"], "kwargs": {"level": "冰点", "detail": {"factor": "turnover"}}}
    _raw_legacy(env.db, "send_macro_alert", payload, created_at=SAME_DAY, key="k-same-day")
    sender = FakeSender([OK, OK])
    n = make_notifier(env.store, sender, env.clock)
    dup = n.send_macro_alert_ex("fact A", "冰点", {"factor": "turnover"})
    assert dup.outcome == CONFIRMED and dup.duplicate and sender.calls == []
    changed = n.send_macro_alert_ex("fact A (edited)", "冰点", {"factor": "turnover"})
    assert changed.outcome in ("blocked_conflict", "confirmed")  # same business key: stored row wins
    assert sender.calls == []


def test_p11_unrelated_legacy_sent_row_does_not_block_a_new_event(env):
    _legacy(env.store, "sent", scope="eod:audit-run-1", title="EOD 日报 2026-10-09", message="old")
    sender = FakeSender([OK])
    assert make_notifier(env.store, sender, env.clock).send_system_alert_ex(TITLE, MESSAGE).confirmed
    assert len(sender.calls) == 1


# ======================================================================= P1-2
def _enqueue_via_store(store, title=TITLE, message=MESSAGE, scope="eod:2026-10-12", digest="target-digest-a"):
    from radar.notify import identity

    business = identity.business_id("send_system_alert", [title, message], {})
    key = identity.business_key(scope, "telegram", "send_system_alert", business)
    obj = identity.legacy_payload([title, message], {})
    row = store.dispatch_enqueue(
        business_key=key, namespace=scope, channel="telegram", operation="send_system_alert",
        target_alias="a", target_digest=digest, payload_json=identity.canonical_json(obj),
        payload_sha256=identity.payload_sha256([title, message], {}), payload_obj=obj,
        business=business, legacy_keys=[],
    )
    return key, row


def test_p12_claim_refuses_a_pending_row_with_an_unresolved_conflict(env):
    key, _ = _enqueue_via_store(env.store)
    _, conflicting = _enqueue_via_store(env.store, message="a different body")
    assert conflicting["conflict"] == "payload"
    claim = env.store.dispatch_claim(key)
    assert claim["status"] == "blocked_conflict"
    assert dispatch_rows(env.db)[0]["attempts_used"] == 0 and attempt_rows(env.db) == []


def test_p12_notifier_stops_when_conflict_was_recorded_before_its_first_claim(env):
    _enqueue_via_store(env.store)
    _enqueue_via_store(env.store, message="a different body")
    sender = FakeSender([OK])
    result = make_notifier(env.store, sender, env.clock).send_system_alert_ex(TITLE, MESSAGE)
    assert result.outcome == "blocked_conflict" and sender.calls == []


def test_p12_a_waiting_retry_cannot_resume_after_another_caller_reports_a_conflict(env):
    sender_a = FakeSender([UNSENT, OK])
    sender_b = FakeSender([OK])
    b_results = []

    def sleep_a(seconds):  # while A waits for its retry, B finds the same event with another body
        env.clock.sleep(seconds)
        b_results.append(
            make_notifier(env.store, sender_b, env.clock).send_system_alert_ex(TITLE, "B says something else")
        )

    a = DurableOutboxNotifier(env.store, sender_a, logging.getLogger("test.a"), scope="eod:2026-10-12",
                              sleep=sleep_a)
    env.store.dispatch_clock = env.clock.now
    result_a = a.send_system_alert_ex(TITLE, MESSAGE)
    assert b_results[0].outcome == "blocked_conflict" and sender_b.calls == []
    assert result_a.outcome == "blocked_conflict"
    assert len(sender_a.calls) == 1  # the second POST (which would have succeeded) never happened
    row = dispatch_rows(env.db)[0]
    assert row["conflict_count"] == 1 and row["state"] == "retry_wait" and row["attempts_used"] == 1


def test_p12_changed_target_during_retry_wait_also_stops_the_waiting_attempt(env):
    sender_a = FakeSender([UNSENT, OK], digest="digest-a")
    sender_b = FakeSender([OK], digest="digest-OTHER")

    def sleep_a(seconds):
        env.clock.sleep(seconds)
        make_notifier(env.store, sender_b, env.clock).send_system_alert_ex(TITLE, MESSAGE)

    a = DurableOutboxNotifier(env.store, sender_a, logging.getLogger("test.a"), scope="eod:2026-10-12",
                              sleep=sleep_a)
    env.store.dispatch_clock = env.clock.now
    result = a.send_system_alert_ex(TITLE, MESSAGE)
    assert result.outcome == "blocked_target_changed" and len(sender_a.calls) == 1 and sender_b.calls == []


def test_p12_in_flight_request_is_recorded_truthfully_and_nothing_else_is_sent(env):
    started, release = threading.Event(), threading.Event()

    class SlowSender(FakeSender):
        def send_system_alert_ex(self, title, message):
            self.calls.append(("send_system_alert", (title, message)))
            started.set()
            assert release.wait(10)
            return OK

    slow = SlowSender([])
    out = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("a", make_notifier(env.store, slow, env.clock).send_system_alert_ex(TITLE, MESSAGE))
    )
    worker.start()
    assert started.wait(10)
    sender_b = FakeSender([OK])
    b = make_notifier(env.store, sender_b, env.clock).send_system_alert_ex(TITLE, "B different body")
    assert b.outcome == "blocked_conflict" and sender_b.calls == []
    release.set()
    worker.join(10)
    assert out["a"].outcome == CONFIRMED  # the POST already happened: record the truth
    row = dispatch_rows(env.db)[0]
    assert row["state"] == "confirmed" and row["message_id"] == 4242 and row["conflict_count"] == 1
    again = make_notifier(env.store, FakeSender([OK]), env.clock).send_system_alert_ex(TITLE, MESSAGE)
    assert again.duplicate


def test_p12_conflict_never_hands_out_a_new_claim_even_after_review(env):
    key, _ = _enqueue_via_store(env.store)
    _enqueue_via_store(env.store, message="different")
    assert env.store.dispatch_review(key, "looked at it")
    env.clock.advance(400)  # long past the "pending for too long" threshold
    assert env.store.dispatch_claim(key)["status"] == "blocked_conflict"
    health = env.store.notification_health()
    assert health["review_needed"] == 0  # acknowledged: no permanent false alarm ...
    assert dispatch_rows(env.db)[0]["attempts_used"] == 0  # ... but still never sendable


# ======================================================================= P2-1
class _Server:
    """Loopback HTTP server: a /bot*/sendMessage endpoint answering ``mode`` and a /landing target."""

    def __init__(self, mode):
        self.mode = mode
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, len(body)))

            def _answer(self):
                self._record()
                if self.path.startswith("/landing"):
                    payload = json.dumps({"ok": True, "result": {"message_id": 7, "chat": {"id": int(CHAT_ID)}}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if isinstance(outer.mode, int) and 300 <= outer.mode < 400:
                    self.send_response(outer.mode)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/landing")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({"ok": True, "result": {"message_id": 5, "chat": {"id": int(CHAT_ID)}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _answer
            do_GET = _answer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _loopback_notifier(server, session=None):
    for name in ("RADAR_HTTP_PROXY", "RADAR_HTTPS_PROXY", "RADAR_ALL_PROXY"):
        import os
        os.environ.pop(name, None)
    notifier = TelegramNotifier(TOKEN, CHAT_ID, logging.getLogger("test.loopback"), session=session)
    notifier.base_url = f"http://127.0.0.1:{server.port}/bot{TOKEN}"
    return notifier


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_p21_redirect_is_not_followed_and_makes_exactly_one_request(status):
    with _Server(status) as server:
        result = _loopback_notifier(server).send_once("title", "body")
    assert len(server.requests) == 1, server.requests  # no second POST (307/308) and no GET (301/302)
    assert server.requests[0][0] == "POST" and server.requests[0][1].endswith("/sendMessage")
    assert result.outcome == UNCERTAIN and result.stage == "redirect"
    assert result.http_status == status and result.error_class == f"redirect_{status}"
    assert not result


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_p21_an_injected_default_session_does_not_follow_redirects_either(status):
    with _Server(status) as server:
        result = _loopback_notifier(server, session=requests.Session()).send_once("title", "body")
    assert len(server.requests) == 1 and result.outcome == UNCERTAIN and result.stage == "redirect"


def test_p21_post_call_and_owned_session_both_disable_redirects():
    from unittest.mock import MagicMock

    session = MagicMock()
    session.post.return_value = MagicMock(status_code=307, headers={"Location": "http://x/"})
    TelegramNotifier(TOKEN, CHAT_ID, logging.getLogger("t"), session=session).send_once("t", "b")
    assert session.post.call_args.kwargs.get("allow_redirects") is False
    owned = TelegramNotifier(TOKEN, CHAT_ID, logging.getLogger("t"))
    assert owned._session.max_redirects == 0


def test_p21_normal_success_over_the_real_client_is_one_confirmed_request():
    with _Server(200) as server:
        result = _loopback_notifier(server).send_once("title", "body")
    assert result.confirmed and result.message_id == 5 and len(server.requests) == 1


def test_p21_through_the_outbox_a_redirect_is_uncertain_with_one_total_request(env):
    with _Server(307) as server:
        notifier = DurableOutboxNotifier(env.store, _loopback_notifier(server), logging.getLogger("t"),
                                         scope="eod:2026-10-12", sleep=env.clock.sleep)
        first = notifier.send_system_alert_ex(TITLE, MESSAGE)
        second = notifier.send_system_alert_ex(TITLE, MESSAGE)
    assert first.outcome == UNCERTAIN and second.outcome == UNCERTAIN
    assert len(server.requests) == 1


# ======================================================================= P2-2
def _metrics(env, now, **kwargs):
    text = watchdog_metrics(env.tmp / "swing_eod_watchdog.json", env.tmp / "no-calendar.json", now, **kwargs)
    return {l.split()[0]: l.split()[1] for l in text.splitlines() if l and not l.startswith("#")}


def test_p22_sender_crash_leaves_a_stale_green_snapshot_but_the_exporter_still_alarms(env):
    n = make_notifier(env.store, FakeSender([OK]), env.clock)
    n.send_system_alert_ex("EOD 日报 2026-10-09", "ok")
    write_notification_health(env.store, env.clock.now())  # last snapshot the sender wrote: all clear
    snapshot = json.loads((env.tmp / HEALTH_FILE).read_text())
    assert snapshot["review_needed"] == 0 and snapshot["uncertain"] == 0
    # a later run claims an event, POSTs and dies: no snapshot, no finish
    key, _ = _enqueue_via_store(env.store, title="EOD 日报 2026-10-12")
    assert env.store.dispatch_claim(key)["status"] == "claimed"
    env.clock.advance(120)
    metrics = _metrics(env, env.clock.now())
    assert metrics["radar_swing_eod_notification_health_valid"] == "1"
    assert metrics["radar_swing_eod_notification_sending_expired"] == "1"
    assert float(metrics["radar_swing_eod_notification_review_needed"]) >= 1
    assert metrics["radar_swing_eod_notification_health_live"] == "1"


def test_p22_uncertain_row_written_without_any_snapshot_is_seen_live(env):
    make_notifier(env.store, FakeSender([UNSURE]), env.clock).send_system_alert_ex(TITLE, MESSAGE)
    (env.tmp / HEALTH_FILE).unlink(missing_ok=True)  # the crashed sender never wrote one
    metrics = _metrics(env, env.clock.now())
    assert metrics["radar_swing_eod_notification_uncertain"] == "1"
    assert metrics["radar_swing_eod_notification_review_needed"] == "1"


def test_p22_snapshot_older_than_the_limit_is_unknown_when_the_db_cannot_be_read(env):
    write_notification_health(env.store, env.clock.now())
    later = env.clock.now() + timedelta(hours=20)
    metrics = _metrics(env, later, db_path=env.tmp / "does-not-exist.db")
    assert metrics["radar_swing_eod_notification_health_valid"] == "0"
    assert metrics["radar_swing_eod_notification_review_needed"] == "NaN"
    assert metrics["radar_swing_eod_notification_health_live"] == "0"


def test_p22_fresh_snapshot_is_used_only_as_a_short_lived_fallback(env):
    write_notification_health(env.store, env.clock.now())
    soon = env.clock.now() + timedelta(seconds=60)
    metrics = _metrics(env, soon, db_path=env.tmp / "does-not-exist.db")
    assert metrics["radar_swing_eod_notification_health_valid"] == "1"
    assert metrics["radar_swing_eod_notification_health_live"] == "0"
    assert metrics["radar_swing_eod_notification_review_needed"] == "0"


def test_p22_pre_migration_db_is_unknown_not_healthy_even_with_a_fresh_zero_snapshot(tmp_path):
    from radar.swing.store import SwingStore

    db = tmp_path / "swing_macro.db"
    SwingStore(str(db))  # schema v5: no dispatch tables
    now = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
    (tmp_path / HEALTH_FILE).write_text(json.dumps({
        "schema_version": 1, "checked_at": "2026-10-12T01:59:30Z", "available": True,
        **{k: 0 for k in ("uncertain", "failed_permanent", "failed_exhausted", "rate_limited_over_budget",
                          "quarantined_legacy", "sending_expired", "retry_wait_stuck", "pending_stale",
                          "conflicts", "review_needed", "in_flight", "oldest_review_age_seconds")},
    }))
    text = watchdog_metrics(tmp_path / "swing_eod_watchdog.json", tmp_path / "c.json", now)
    metrics = {l.split()[0]: l.split()[1] for l in text.splitlines() if l and not l.startswith("#")}
    assert metrics["radar_swing_eod_notification_health_valid"] == "0"
    assert metrics["radar_swing_eod_notification_review_needed"] == "NaN"


def test_p22_live_check_is_read_only_and_leaves_the_database_untouched(env):
    from runtime_contracts.notification_health import open_readonly

    make_notifier(env.store, FakeSender([UNSURE]), env.clock).send_system_alert_ex(TITLE, MESSAGE)
    env.db.chmod(0o444)  # even a read-only file must be readable by the check
    before = (hashlib.sha256(env.db.read_bytes()).hexdigest(), env.db.stat().st_mtime_ns)
    siblings = sorted(p.name for p in env.tmp.iterdir())
    metrics = _metrics(env, env.clock.now())
    assert metrics["radar_swing_eod_notification_uncertain"] == "1"
    assert (hashlib.sha256(env.db.read_bytes()).hexdigest(), env.db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in env.tmp.iterdir()) == siblings  # no -journal/-wal/-shm left behind
    conn = open_readonly(env.db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM notification_dispatch")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE notification_dispatch SET state='confirmed'")
    finally:
        conn.close()


@pytest.mark.parametrize("kind", [
    "uncertain", "failed_exhausted", "rate_limited_over_budget", "failed_permanent",
    "quarantined_legacy", "conflicts", "sending_expired", "retry_wait_stuck", "pending_stale",
])
def test_p22_live_numbers_equal_the_store_health_for_every_problem_state(env, kind):
    from tests.test_notify_dispatch_v2 import _make_problem

    _make_problem(kind, env)
    store_health = env.store.notification_health()
    metrics = _metrics(env, env.clock.now())
    assert store_health[kind] >= 1
    for field in ("review_needed", "uncertain", "failed_permanent", "failed_exhausted",
                  "rate_limited_over_budget", "quarantined_legacy", "sending_expired",
                  "retry_wait_stuck", "pending_stale", "conflicts"):
        assert metrics[f"radar_swing_eod_notification_{field}"] == str(store_health[field]), field
