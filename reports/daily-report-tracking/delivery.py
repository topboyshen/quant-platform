"""Conservative, single-attempt delivery using a dedicated SQLite outbox.

The identity is account/channel/business date/report type/revision, never the
body hash. A committed ``sending`` claim precedes the external call. Ambiguous
outcomes remain blocked across restarts; there is deliberately no lease expiry
or automatic retry. This prevents duplicate attempts, not guaranteed delivery.
Keep this ledger durable: restoring an older copy can lose deduplication proof.

``sender(text)`` must perform at most one provider request and return a JSON
object. Success requires ``ok is True``, ``message_id`` and a nonempty
``targetFingerprint``. The adapter must verify that fingerprint against its
configured destination. Explicit negative proof uses
``{"ok": False, "definitely_not_sent": True}``; it is terminal too.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timezone


NOTIFICATION_TYPE = "daily_research_summary"
APPLICATION_ID = 0x44525331
SCHEMA_VERSION = 1
TABLE = "daily_summary_outbox"


class OutboxError(RuntimeError):
    """The independent notification ledger cannot safely be used."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _identity(account_id, channel, business_date, revision):
    for name, value in (("account_id", account_id), ("channel", channel)):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(name + " must be a nonempty, unpadded string")
    if not isinstance(business_date, str):
        raise ValueError("business_date must be YYYY-MM-DD")
    if date.fromisoformat(business_date).isoformat() != business_date:
        raise ValueError("business_date must be YYYY-MM-DD")
    if isinstance(revision, bool) or not isinstance(revision, (str, int)):
        raise ValueError("revision must be a nonempty string or integer")
    revision = str(revision)
    if not revision or revision != revision.strip():
        raise ValueError("revision must be nonempty and unpadded")
    identity = [account_id, channel, business_date, NOTIFICATION_TYPE, revision]
    key = "drs:v1:" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    return identity, key


def _connect(db_path):
    path = os.fspath(db_path)
    if not isinstance(path, str) or not path or path == ":memory:" or path.startswith("file:"):
        raise ValueError("db_path must name a persistent, dedicated SQLite file")
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # FULL persists the claim before sender is invoked. Use SQLite's default
    # rollback journal: no background worker or process-local lock is needed.
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _initialize(conn):
    """Called within the same immediate transaction as the first claim."""
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    objects = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE substr(name, 1, 7) != 'sqlite_'"
    ).fetchall()
    if application_id == 0 and not objects and version == 0:
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute(f"""
            CREATE TABLE {TABLE} (
                idempotency_key TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                business_date TEXT NOT NULL,
                notification_type TEXT NOT NULL,
                revision TEXT NOT NULL,
                body_text TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('sending','sent','unknown','not_sent')),
                claim_token TEXT NOT NULL,
                attempt_count INTEGER NOT NULL CHECK(attempt_count = 1),
                created_at TEXT NOT NULL,
                sending_at TEXT NOT NULL,
                completed_at TEXT,
                message_id TEXT,
                target_fingerprint TEXT,
                receipt_json TEXT,
                reason TEXT,
                UNIQUE(account_id, channel, business_date, notification_type, revision)
            )
        """)
    elif (application_id != APPLICATION_ID or version != SCHEMA_VERSION
          or [(r["type"], r["name"]) for r in objects] != [("table", TABLE)]):
        raise OutboxError("Refusing a database that is not the dedicated daily-summary outbox")


def _view(row, *, attempted=False, reused=False):
    return {
        "status": row["status"],
        "idempotency_key": row["idempotency_key"],
        "account_id": row["account_id"],
        "channel": row["channel"],
        "business_date": row["business_date"],
        "notification_type": row["notification_type"],
        "revision": row["revision"],
        "content_sha256": row["content_sha256"],
        "message_id": row["message_id"],
        "targetFingerprint": row["target_fingerprint"],
        "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
        "reason": row["reason"],
        "attempt_count": row["attempt_count"],
        "attempted": attempted,
        "reused": reused,
        "persisted": True,
        "requires_reconciliation": row["status"] in ("sending", "unknown"),
    }


def _has_message_id(value):
    return (not isinstance(value, bool) and isinstance(value, (str, int))
            and bool(str(value).strip()))


def _outcome(response):
    """Normalize and freeze provider proof before persisting it."""
    try:
        receipt_json = _json(response)
        response = json.loads(receipt_json)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "unknown", _json({"invalid_receipt": "not_json_serializable"}), None, None, "invalid_receipt"
    if not isinstance(response, dict):
        return "unknown", receipt_json, None, None, "invalid_receipt"
    message_id = response.get("message_id")
    fingerprint = response.get("targetFingerprint")
    message_id = str(message_id) if _has_message_id(message_id) else None
    fingerprint = fingerprint if isinstance(fingerprint, str) and fingerprint.strip() else None
    for field in ("http_status", "status_code", "error_code"):
        value = response.get(field)
        if isinstance(value, (str, int)) and str(value).isdigit() and 500 <= int(value) < 600:
            return "unknown", receipt_json, message_id, fingerprint, "provider_5xx"
    if response.get("ok") is True and message_id and fingerprint:
        return "sent", receipt_json, message_id, fingerprint, None
    if (response.get("ok") is False and response.get("definitely_not_sent") is True
            and message_id is None):
        return "not_sent", receipt_json, None, fingerprint, "explicit_non_delivery_proof"
    if response.get("ok") is True and message_id is None:
        reason = "missing_message_id"
    elif response.get("ok") is True and fingerprint is None:
        reason = "missing_target_fingerprint"
    else:
        reason = "unconfirmed_provider_response"
    return "unknown", receipt_json, message_id, fingerprint, reason


def _record_outcome(conn, key, claim_token, outcome):
    status, receipt_json, message_id, fingerprint, reason = outcome
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(f"""
            UPDATE {TABLE}
            SET status=?, receipt_json=?, message_id=?, target_fingerprint=?,
                reason=?, completed_at=?
            WHERE idempotency_key=? AND claim_token=? AND status='sending'
        """, (status, receipt_json, message_id, fingerprint, reason, _now(), key, claim_token))
        if cursor.rowcount != 1:
            raise OutboxError("The persisted sending claim has changed; reconciliation required")
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE idempotency_key=?", (key,)).fetchone()
        conn.commit()
        return _view(row, attempted=True)
    except BaseException:
        conn.rollback()
        raise


def deliver(db_path, account_id, channel, business_date, revision, text, sender):
    """Attempt at most one send for this persisted immutable identity.

    Existing records (including ``sending``, ``unknown`` and ``not_sent``) never
    invoke sender again. Different text for an existing identity returns
    ``conflict``. A genuinely intended supplemental report needs a new revision;
    do not advance the revision as a transport retry. Infrastructure failures
    before the send raise; receipt-write failure after the send returns unknown
    with ``persisted=False`` while leaving the durable sending claim blocked.
    """
    identity, key = _identity(account_id, channel, business_date, revision)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a nonempty string")
    if not callable(sender):
        raise TypeError("sender must be callable")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    claim_token = uuid.uuid4().hex
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _initialize(conn)
            row = conn.execute(f"SELECT * FROM {TABLE} WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None:
                result = _view(row, reused=True)
                if row["body_text"] != text or row["content_sha256"] != content_hash:
                    result.update(status="conflict", persisted_status=row["status"],
                                  requested_content_sha256=content_hash,
                                  reason="same_identity_different_text")
                conn.commit()
                return result
            timestamp = _now()
            conn.execute(f"""
                INSERT INTO {TABLE} (
                    idempotency_key, account_id, channel, business_date,
                    notification_type, revision, body_text, content_sha256,
                    status, claim_token, attempt_count, created_at, sending_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sending', ?, 1, ?, ?)
            """, (key, *identity, text, content_hash, claim_token, timestamp, timestamp))
            row = conn.execute(f"SELECT * FROM {TABLE} WHERE idempotency_key=?", (key,)).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        # The external side effect is strictly after the durable claim, outside
        # a database transaction. A hard crash here leaves a blocked claim.
        try:
            outcome = _outcome(sender(row["body_text"]))
        except Exception as exc:
            # Exception text can contain credential-bearing URLs. Keep the
            # exception class, not a potentially secret request/response dump.
            outcome = ("unknown", _json({"transport_error": type(exc).__name__}),
                       None, None, "sender_exception")
        try:
            return _record_outcome(conn, key, claim_token, outcome)
        except Exception as exc:
            result = _view(row, attempted=True)
            result.update(status="unknown", persisted=False, persisted_status="sending",
                          reason="receipt_persistence_failed", requires_reconciliation=True,
                          persistence_error=type(exc).__name__,
                          receipt=json.loads(outcome[1]), message_id=outcome[2],
                          targetFingerprint=outcome[3])
            return result
    finally:
        conn.close()
