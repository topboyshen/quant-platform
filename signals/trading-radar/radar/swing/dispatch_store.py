"""Durable dispatch queue (schema v6): notification_dispatch + notification_attempt.

Rules enforced here (all inside ``BEGIN IMMEDIATE`` transactions, so threads and
processes serialise on the SQLite write lock):

* one row per *business event* (``business_key``); the body hash is only a consistency
  check - a different body for the same event stops the dispatch, it never overwrites
  the stored payload nor creates a new send entitlement;
* the retry budget (<=3 POSTs, ~40 s, same Shanghai day) lives in the row, is charged
  *before* the POST (a crash still counts) and therefore survives restarts;
* ``uncertain`` is terminal for automation; a ``sending`` row whose lease expired is
  turned into ``uncertain`` - never retried;
* the legacy ``notification_outbox`` is consulted but never modified.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from radar.notify.identity import business_id as derive_business_id
from radar.notify.identity import first_date
from radar.notify.result import (
    CONFIRMED,
    NOT_SENT_PERMANENT,
    NOT_SENT_RETRYABLE,
    UNCERTAIN,
    SendResult,
)
from runtime_contracts.notification_health import (  # single definition, stdlib only
    ALL_STATES,
    TERMINAL_UNCONFIRMED,
    compute_dispatch_health,
)

MAX_ATTEMPTS = 3
BUDGET_SECONDS = 40.0
LEASE_SECONDS = 90.0
BACKOFF_SECONDS = (0.5, 1.0)
_SHANGHAI = ZoneInfo("Asia/Shanghai")

class DispatchClaimLost(RuntimeError):
    """The attempt no longer owns the row (lease reaped / state changed)."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class NotificationDispatchMixin:
    """Mixed into ``SwingStore`` (needs ``_connect`` and ``_lock``)."""

    dispatch_clock = None  # optional zero-arg callable returning an aware datetime (tests)

    # ------------------------------------------------------------------ helpers
    def _dispatch_now(self) -> datetime:
        clock = self.dispatch_clock
        moment = clock() if clock else datetime.now(timezone.utc)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    @contextmanager
    def _dispatch_txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.rollback()
                    raise
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row(conn: sqlite3.Connection, business_key: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM notification_dispatch WHERE business_key=?", (business_key,)
        ).fetchone()
        return dict(row) if row else None

    def dispatch_ready(self) -> Tuple[bool, str]:
        """True only when the DB is at version 6 with a verified structure."""
        from radar.swing.migrations import NOTIFICATION_SCHEMA_VERSION, verify_notification_schema

        try:
            with self._lock:
                conn = self._connect()
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if version != NOTIFICATION_SCHEMA_VERSION:
                        return False, f"schema_version_{version}"
                    verify_notification_schema(conn)
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            return False, f"schema_check_failed:{type(exc).__name__}"
        return True, "ok"

    def dispatch_get(self, business_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                return self._row(conn, business_key)
            finally:
                conn.close()

    # ---------------------------------------------------------------- enqueue
    def _legacy_lookup(
        self,
        conn: sqlite3.Connection,
        *,
        operation: str,
        namespace: str,
        legacy_keys: List[str],
        payload_obj: Dict[str, Any],
        business: str,
        now: datetime,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Compare the new event with EVERY legacy outbox row of the same operation.

        Returns ``(kind, reason, ref)``; kind is one of

        * ``'sent'``     - the same event (same body) was already delivered: duplicate, send nothing;
        * ``'conflict'`` - the same event was already delivered with a DIFFERENT body: never
                           re-sent, recorded as a conflict for review;
        * ``'open'``     - a legacy failed/sending/pending row for the same event exists, or a row
                           of this operation/day cannot be mapped at all: true delivery status
                           unknown, so the new event is quarantined (paused);
        * ``'none'``     - no legacy record belongs to this event.

        An exact legacy key hit (same scope, same body) is conclusive.  Otherwise rows are
        matched on the stable business identity (operation + business id), whatever their
        status and whatever scope their old key was built from - e.g. ``eod:{audit_run_id}``
        before the switch to ``eod:{data date}``.  An identity that names no date (macro /
        datasource alerts) is only matched against legacy rows created on the event's own
        Shanghai day, so an old record cannot suppress every later alert of that kind.
        """
        if legacy_keys:
            marks = ",".join("?" for _ in legacy_keys)
            exact = conn.execute(
                f"SELECT id,status FROM notification_outbox WHERE idempotency_key IN ({marks}) "
                "ORDER BY id",
                legacy_keys,
            ).fetchall()
            for row in exact:
                if row["status"] == "sent":
                    return "sent", "legacy_sent_duplicate", f"outbox:{row['id']}"
            for row in exact:
                return "open", f"legacy_{row['status']}", f"outbox:{row['id']}"

        event_day = first_date(namespace) or now.astimezone(_SHANGHAI).date().isoformat()
        names_a_date = bool(first_date(business))
        new_payload = json.loads(json.dumps(payload_obj, default=str))
        sent_same: List[str] = []
        sent_changed: List[str] = []
        unfinished: List[Tuple[str, str]] = []
        unmappable: List[str] = []
        for row in conn.execute(
            "SELECT id,status,payload_json,created_at FROM notification_outbox "
            "WHERE operation=? ORDER BY id",
            (operation,),
        ).fetchall():
            created = _parse(row["created_at"])
            same_day = created is None or created.astimezone(_SHANGHAI).date().isoformat() == event_day
            ref = f"outbox:{row['id']}"
            try:
                legacy_payload = json.loads(row["payload_json"])
                if not isinstance(legacy_payload, dict):
                    raise ValueError("legacy payload is not an object")
                legacy_business = derive_business_id(
                    operation, legacy_payload.get("args", []), legacy_payload.get("kwargs", {})
                )
            except Exception:  # noqa: BLE001 - unknown shape: cannot be mapped to any event
                if same_day:
                    unmappable.append(ref)
                continue
            if legacy_business != business or not (names_a_date or same_day):
                continue
            if row["status"] == "sent":
                (sent_same if legacy_payload == new_payload else sent_changed).append(ref)
            else:
                unfinished.append((ref, f"legacy_{row['status']}"))
        if sent_same:
            return "sent", "legacy_sent_duplicate", sent_same[0]
        if sent_changed:
            return "conflict", "legacy_sent_payload_changed", sent_changed[0]
        if unfinished:
            return "open", unfinished[0][1], unfinished[0][0]
        if unmappable:
            return "open", "legacy_unmappable", unmappable[0]
        return "none", None, None

    def dispatch_enqueue(
        self,
        *,
        business_key: str,
        namespace: str,
        channel: str,
        operation: str,
        target_alias: str,
        target_digest: str,
        payload_json: str,
        payload_sha256: str,
        payload_obj: Dict[str, Any],
        business: str,
        legacy_keys: List[str],
    ) -> Dict[str, Any]:
        """Create the event row once; report body/target conflicts without altering it."""
        now = self._dispatch_now()
        stamp = _iso(now)
        with self._dispatch_txn() as conn:
            existing = self._row(conn, business_key)
            if existing:
                conflict = None
                conflict_sha = None
                if existing["payload_sha256"] != payload_sha256:
                    conflict, conflict_sha = "payload", payload_sha256
                elif existing["target_digest"] != target_digest:
                    conflict, conflict_sha = "target", target_digest
                if conflict:
                    conn.execute(
                        "UPDATE notification_dispatch SET conflict_count=conflict_count+1,"
                        "last_conflict_kind=?,last_conflict_sha256=?,reviewed_at=NULL,"
                        "updated_at=? WHERE business_key=?",
                        (conflict, conflict_sha, stamp, business_key),
                    )
                    existing = self._row(conn, business_key)
                return {**existing, "created": False, "conflict": conflict}
            kind, reason, ref = self._legacy_lookup(
                conn,
                operation=operation,
                namespace=namespace,
                legacy_keys=legacy_keys,
                payload_obj=payload_obj,
                business=business,
                now=now,
            )
            state = {
                "none": "pending", "sent": "confirmed",
                "open": "quarantined_legacy", "conflict": "quarantined_legacy",
            }[kind]
            completed = stamp if state != "pending" else None
            conn.execute(
                "INSERT INTO notification_dispatch (business_key,namespace,channel,operation,"
                "target_alias,target_digest,payload_json,payload_sha256,state,max_attempts,"
                "created_at,updated_at,completed_at,reason_code,legacy_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    business_key, namespace[:128], channel[:32], operation[:64],
                    target_alias[:64], target_digest, payload_json, payload_sha256, state,
                    MAX_ATTEMPTS, stamp, stamp, completed, reason, ref,
                ),
            )
            conflict = None
            if kind == "conflict":
                # The event was delivered before with another body: record the conflict (the
                # stored row is quarantined and can never be claimed), send nothing.
                conn.execute(
                    "UPDATE notification_dispatch SET conflict_count=1,last_conflict_kind='payload',"
                    "last_conflict_sha256=? WHERE business_key=?",
                    (payload_sha256, business_key),
                )
                conflict = "payload"
            return {**self._row(conn, business_key), "created": True, "conflict": conflict}

    # ------------------------------------------------------------------ claim
    def dispatch_claim(
        self, business_key: str, *, lease_seconds: float = LEASE_SECONDS
    ) -> Dict[str, Any]:
        """Atomically take the right to make ONE POST, or report why not.

        Returns ``{"status": ..., "row": ...}`` with status one of ``claimed`` (also
        ``attempt_no`` and ``claim_token``), ``in_progress``, ``not_due``, ``blocked_conflict``,
        ``terminal``.
        """
        now = self._dispatch_now()
        stamp = _iso(now)
        with self._dispatch_txn() as conn:
            row = self._row(conn, business_key)
            if row is None:
                raise RuntimeError("通知 dispatch 记录不存在")
            state = row["state"]
            if state == "confirmed" or state in TERMINAL_UNCONFIRMED:
                return {"status": "terminal", "row": row}
            if state == "sending":
                lease = _parse(row["lease_expires_at"])
                if lease is not None and lease > now:
                    return {"status": "in_progress", "row": row}
                conn.execute(
                    "UPDATE notification_dispatch SET state='uncertain',"
                    "reason_code='sending_lease_expired',completed_at=?,updated_at=?,"
                    "claim_token=NULL,lease_expires_at=NULL WHERE business_key=? AND state='sending'",
                    (stamp, stamp, business_key),
                )
                conn.execute(
                    "UPDATE notification_attempt SET outcome='uncertain',finished_at=?,"
                    "error_class='lease_expired' WHERE outcome='started' AND dispatch_id=?",
                    (stamp, row["id"]),
                )
                return {"status": "terminal", "row": self._row(conn, business_key)}
            if int(row["conflict_count"] or 0) > 0:
                # A recorded same-event conflict (other body / other target / delivered before
                # with other body) means this row must never obtain another POST.  Checked in the
                # same transaction that would hand out the claim, so a caller that was waiting
                # for its retry cannot resume.  Nothing is rewritten: an already issued request
                # is recorded as it happened by ``dispatch_finish``.
                return {"status": "blocked_conflict", "row": row}
            if state == "retry_wait":
                due = _parse(row["next_attempt_at"])
                if due is not None and due > now:
                    return {"status": "not_due", "row": row}
            first = _parse(row["first_attempt_at"])
            deadline = _parse(row["budget_deadline_at"])
            over_budget = deadline is not None and now > deadline
            overnight = first is not None and first.astimezone(_SHANGHAI).date() != now.astimezone(_SHANGHAI).date()
            if row["attempts_used"] >= row["max_attempts"] or over_budget or overnight:
                conn.execute(
                    "UPDATE notification_dispatch SET state='failed_exhausted',"
                    "reason_code=?,completed_at=?,updated_at=?,next_attempt_at=NULL "
                    "WHERE business_key=?",
                    (
                        "attempts_exhausted" if row["attempts_used"] >= row["max_attempts"]
                        else ("retry_overnight_forbidden" if overnight else "budget_exhausted"),
                        stamp, stamp, business_key,
                    ),
                )
                return {"status": "terminal", "row": self._row(conn, business_key)}
            attempt_no = int(row["attempts_used"]) + 1
            token = uuid.uuid4().hex
            first_stamp = row["first_attempt_at"] or stamp
            deadline_stamp = row["budget_deadline_at"] or _iso(now + timedelta(seconds=BUDGET_SECONDS))
            conn.execute(
                "UPDATE notification_dispatch SET state='sending',attempts_used=?,"
                "first_attempt_at=?,budget_deadline_at=?,next_attempt_at=NULL,"
                "lease_expires_at=?,claim_token=?,updated_at=?,reason_code=NULL "
                "WHERE business_key=?",
                (
                    attempt_no, first_stamp, deadline_stamp,
                    _iso(now + timedelta(seconds=lease_seconds)), token, stamp, business_key,
                ),
            )
            conn.execute(
                "INSERT INTO notification_attempt (dispatch_id,attempt_no,started_at,outcome) "
                "VALUES (?,?,?, 'started')",
                (row["id"], attempt_no, stamp),
            )
            return {
                "status": "claimed",
                "row": self._row(conn, business_key),
                "attempt_no": attempt_no,
                "claim_token": token,
            }

    # ----------------------------------------------------------------- finish
    def dispatch_finish(
        self,
        business_key: str,
        *,
        claim_token: str,
        attempt_no: int,
        result: SendResult,
    ) -> Dict[str, Any]:
        """Record the classified result of the claimed attempt and apply the transition."""
        now = self._dispatch_now()
        stamp = _iso(now)
        with self._dispatch_txn() as conn:
            row = self._row(conn, business_key)
            if row is None:
                raise RuntimeError("通知 dispatch 记录不存在")
            if (
                row["state"] == "confirmed"
                and result.outcome == CONFIRMED
                and row["message_id"] == result.message_id
                and row["claim_token"] is None
            ):
                return row  # idempotent receipt save
            if row["state"] != "sending" or row["claim_token"] != claim_token:
                raise DispatchClaimLost("发送尝试已不再持有该通知")
            updates: Dict[str, Any] = {
                "last_outcome": result.outcome,
                "last_stage": result.stage or None,
                "last_http_status": result.http_status,
                "last_error_class": result.error_class or None,
                "last_retry_after": result.retry_after,
                "claim_token": None,
                "lease_expires_at": None,
                "updated_at": stamp,
            }
            if result.outcome == CONFIRMED:
                updates.update(state="confirmed", message_id=result.message_id,
                               receipt_at=stamp, completed_at=stamp, reason_code=None)
            elif result.outcome == NOT_SENT_PERMANENT:
                updates.update(state="failed_permanent", completed_at=stamp,
                               reason_code=result.error_class or "permanent_rejection")
            elif result.outcome == NOT_SENT_RETRYABLE:
                attempts = int(row["attempts_used"])
                backoff = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
                wait = float(result.retry_after) if result.retry_after is not None else backoff
                wait = max(wait, 0.0)
                deadline = _parse(row["budget_deadline_at"]) or now
                if result.retry_after is not None and now + timedelta(seconds=wait) > deadline:
                    updates.update(state="rate_limited_over_budget", completed_at=stamp,
                                   reason_code="rate_limit_wait_exceeds_budget")
                elif attempts >= int(row["max_attempts"]):
                    updates.update(state="failed_exhausted", completed_at=stamp,
                                   reason_code="attempts_exhausted")
                elif now + timedelta(seconds=wait) > deadline:
                    updates.update(state="failed_exhausted", completed_at=stamp,
                                   reason_code="budget_exhausted")
                else:
                    updates.update(state="retry_wait", next_attempt_at=_iso(now + timedelta(seconds=wait)),
                                   reason_code=result.error_class or "not_sent_retryable")
            else:  # uncertain (and any unknown label fails closed)
                updates.update(state="uncertain", completed_at=stamp,
                               reason_code=result.error_class or "uncertain")
            assignments = ",".join(f"{column}=?" for column in updates)
            conn.execute(
                f"UPDATE notification_dispatch SET {assignments} WHERE business_key=?",
                [*updates.values(), business_key],
            )
            conn.execute(
                "UPDATE notification_attempt SET finished_at=?,outcome=?,stage=?,http_status=?,"
                "error_class=?,retry_after=?,message_id=? WHERE dispatch_id=? AND attempt_no=?",
                (
                    stamp, result.outcome if result.outcome in (CONFIRMED, NOT_SENT_RETRYABLE,
                                                                NOT_SENT_PERMANENT) else UNCERTAIN,
                    result.stage or None, result.http_status, result.error_class or None,
                    result.retry_after, result.message_id, row["id"], attempt_no,
                ),
            )
            return self._row(conn, business_key)

    # ------------------------------------------------------- review / metrics
    def dispatch_review(self, business_key: str, note: str = "") -> bool:
        """Human acknowledgement.  Never changes the delivery state, never re-sends."""
        stamp = _iso(self._dispatch_now())
        with self._dispatch_txn() as conn:
            cursor = conn.execute(
                "UPDATE notification_dispatch SET reviewed_at=?,review_note=?,updated_at=? "
                "WHERE business_key=? AND (state!='confirmed' OR conflict_count>0)",
                (stamp, str(note)[:200], stamp, business_key),
            )
            return cursor.rowcount == 1

    def dispatch_counts(self) -> Dict[str, int]:
        with self._lock:
            conn = self._connect()
            try:
                counts = {state: 0 for state in ALL_STATES}
                for row in conn.execute(
                    "SELECT state,COUNT(*) AS n FROM notification_dispatch GROUP BY state"
                ).fetchall():
                    counts[str(row["state"])] = int(row["n"])
                return counts
            finally:
                conn.close()

    def dispatch_health(self) -> Dict[str, Any]:
        """Read-only health summary (shared SQL: ``runtime_contracts.notification_health``).

        Every non-confirmed, non-reviewed row is review_needed.  Contains only counts/ages -
        never payloads, chat ids, tokens or error text.
        """
        with self._lock:
            conn = self._connect()
            try:
                return compute_dispatch_health(conn, self._dispatch_now())
            finally:
                conn.close()
