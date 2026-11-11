"""Read-only notification v6 schema contract; standard-library dependencies only."""
from __future__ import annotations

import re
import sqlite3

NOTIFICATION_SCHEMA_VERSION = 6
DISPATCH_STATES = (
    "pending", "sending", "retry_wait", "confirmed", "failed_permanent",
    "failed_exhausted", "rate_limited_over_budget", "uncertain", "quarantined_legacy",
)
NOTIFICATION_TABLES = ("notification_dispatch", "notification_attempt")
REQUIRED_COLUMNS = {
    "notification_dispatch": {
        "business_key", "namespace", "channel", "operation", "target_alias", "target_digest",
        "payload_json", "payload_sha256", "state", "attempts_used", "max_attempts",
        "first_attempt_at", "budget_deadline_at", "next_attempt_at", "lease_expires_at",
        "claim_token", "created_at", "updated_at", "completed_at", "last_outcome",
        "last_stage", "last_http_status", "last_error_class", "last_retry_after",
        "reason_code", "message_id", "receipt_at", "conflict_count", "last_conflict_kind",
        "last_conflict_sha256", "legacy_ref", "reviewed_at", "review_note",
    },
    "notification_attempt": {
        "dispatch_id", "attempt_no", "started_at", "finished_at", "outcome", "stage",
        "http_status", "error_class", "retry_after", "message_id",
    },
}


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).casefold()


def verify_notification_schema(conn: sqlite3.Connection) -> None:
    """Verify columns, accepted states and the actual append-only guard bodies.

    This function performs SELECT/PRAGMA reads only. It can be used by migration
    code and the read-only host exporter without importing SwingStore.
    """
    for table in NOTIFICATION_TABLES:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            raise RuntimeError(f"数据库版本 6 缺少表 {table}")
        columns = {str(column[1]) for column in conn.execute(f"PRAGMA table_info({table})")}
        missing = REQUIRED_COLUMNS[table] - columns
        if missing:
            raise RuntimeError(f"表 {table} 缺少列: {sorted(missing)}")
        if table == "notification_dispatch":
            for state in DISPATCH_STATES:
                if f"'{state}'" not in row[0]:
                    raise RuntimeError(f"notification_dispatch 状态约束缺少 {state}")
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=? AND tbl_name=?",
                               (f"trg_{table}_no_delete", table)).fetchone()
        expected = (f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} history is append-only'); END")
        if trigger is None or _normalized(trigger[0]) != _normalized(expected):
            raise RuntimeError(f"表 {table} 防删除触发器缺失或定义不符")
