"""Versioned, transactional schema migrations for the Swing decision store.

The application used to patch tables opportunistically during startup while
leaving ``PRAGMA user_version`` at zero.  That made the effective schema depend
on which process happened to open the database first.  All schema changes now
live here and are applied in one ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable

CURRENT_SCHEMA_VERSION = 5
# Version 6 only ADDS the notification_dispatch/notification_attempt tables.  It is applied
# exclusively by ``migrate_notification_v6`` (explicit, operator-run); ``migrate`` never
# upgrades 5 -> 6 implicitly.  Anything above 6 is rejected.
NOTIFICATION_SCHEMA_VERSION = 6
SUPPORTED_SCHEMA_VERSIONS = (CURRENT_SCHEMA_VERSION, NOTIFICATION_SCHEMA_VERSION)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _execute_all(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def _migration_1(conn: sqlite3.Connection) -> None:
    _execute_all(
        conn,
        (
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS macro_slice (
                date TEXT PRIMARY KEY,
                total_amount REAL,
                margin_balance REAL,
                margin_growth_pct REAL,
                limit_up INTEGER,
                limit_down INTEGER,
                limit_ratio REAL,
                source TEXT,
                extra TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trend_signal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                signal_type TEXT,
                period TEXT,
                data_date TEXT,
                title TEXT,
                detail TEXT,
                confidence REAL,
                notified_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """,
        ),
    )
    macro_columns = _columns(conn, "macro_slice")
    if "extra" not in macro_columns:
        conn.execute("ALTER TABLE macro_slice ADD COLUMN extra TEXT")
    signal_columns = _columns(conn, "trend_signal")
    if "data_date" not in signal_columns:
        conn.execute("ALTER TABLE trend_signal ADD COLUMN data_date TEXT")
    if "notified_at" not in signal_columns:
        conn.execute("ALTER TABLE trend_signal ADD COLUMN notified_at TEXT")
    _execute_all(
        conn,
        (
            "CREATE INDEX IF NOT EXISTS idx_trend_signal_date ON trend_signal(date)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_trend_signal_data_bar
            ON trend_signal(code, signal_type, period, data_date)
            WHERE data_date IS NOT NULL AND data_date <> ''
            """,
        ),
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS swing_eod_run (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            decision_date TEXT NOT NULL,
            data_date TEXT,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            target_count INTEGER NOT NULL DEFAULT 0,
            evaluated_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            signal_count INTEGER NOT NULL DEFAULT 0,
            allowed_signal_count INTEGER NOT NULL DEFAULT 0,
            blocked_signal_count INTEGER NOT NULL DEFAULT 0,
            decision_outcome TEXT NOT NULL DEFAULT 'not_evaluated',
            reason_codes TEXT NOT NULL DEFAULT '[]',
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
            CHECK (decision_outcome IN (
                'not_evaluated', 'emitted', 'no_signal', 'blocked',
                'data_unavailable', 'skipped'
            )),
            CHECK (
                target_count >= 0 AND evaluated_count >= 0
                AND failed_count >= 0 AND signal_count >= 0
                AND allowed_signal_count >= 0 AND blocked_signal_count >= 0
            ),
            CHECK (evaluated_count + failed_count <= target_count),
            CHECK (allowed_signal_count + blocked_signal_count = signal_count)
        )
        """
    )
    columns = _columns(conn, "swing_eod_run")
    if "allowed_signal_count" not in columns:
        conn.execute(
            "ALTER TABLE swing_eod_run ADD COLUMN "
            "allowed_signal_count INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE swing_eod_run SET allowed_signal_count=signal_count "
            "WHERE decision_outcome='emitted'"
        )
    if "blocked_signal_count" not in columns:
        conn.execute(
            "ALTER TABLE swing_eod_run ADD COLUMN "
            "blocked_signal_count INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE swing_eod_run SET blocked_signal_count=signal_count "
            "WHERE decision_outcome='blocked'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swing_eod_run_decision_date "
        "ON swing_eod_run(decision_date DESC, id DESC)"
    )


def _migration_3(conn: sqlite3.Connection) -> None:
    _execute_all(
        conn,
        (
            """
            CREATE TABLE IF NOT EXISTS factor_import_batch (
                batch_id TEXT PRIMARY KEY,
                factor_name TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                requested_start TEXT,
                requested_end TEXT,
                observed_at TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                CHECK (record_count >= 0),
                CHECK (validation_status IN ('accepted', 'rejected'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS factor_observation (
                observation_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES factor_import_batch(batch_id),
                factor_name TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                value REAL NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_market_time TEXT NOT NULL,
                available_at TEXT NOT NULL,
                checksum TEXT NOT NULL,
                components_json TEXT NOT NULL DEFAULT '{}',
                imported_at TEXT NOT NULL,
                UNIQUE (batch_id, factor_name, observation_date),
                CHECK (value > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS eod_risk_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE REFERENCES swing_eod_run(run_id),
                decision_at TEXT NOT NULL,
                level TEXT NOT NULL,
                values_json TEXT NOT NULL,
                source_timestamps_json TEXT NOT NULL,
                available_at_json TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                missing_fields_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS eod_target_decision (
                decision_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES swing_eod_run(run_id),
                snapshot_id TEXT REFERENCES eod_risk_snapshot(snapshot_id),
                decision_date TEXT NOT NULL,
                decision_at TEXT NOT NULL,
                data_date TEXT,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                sector TEXT NOT NULL,
                data_status TEXT NOT NULL,
                decision_state TEXT NOT NULL,
                raw_trigger_count INTEGER NOT NULL,
                raw_signals_json TEXT NOT NULL,
                risk_action TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                reference_close REAL,
                validation_split TEXT NOT NULL DEFAULT 'live_oos',
                created_at TEXT NOT NULL,
                UNIQUE (run_id, code),
                CHECK (data_status IN ('ok', 'failed')),
                CHECK (decision_state IN ('no_trigger', 'allowed', 'blocked', 'data_failed')),
                CHECK (risk_action IN ('not_applicable', 'allowed', 'blocked')),
                CHECK (raw_trigger_count >= 0),
                CHECK (validation_split IN ('live_oos', 'walk_forward_oos'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS eod_decision_outcome (
                outcome_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL REFERENCES eod_target_decision(decision_id),
                horizon_sessions INTEGER NOT NULL,
                evaluation_as_of TEXT NOT NULL,
                entry_date TEXT,
                entry_open REAL,
                entry_fill REAL,
                exit_date TEXT,
                exit_close REAL,
                exit_fill REAL,
                benchmark_code TEXT NOT NULL,
                benchmark_return REAL,
                gap_return REAL,
                gross_return REAL,
                net_return REAL,
                excess_return REAL,
                mfe REAL,
                mae REAL,
                fees_return REAL,
                slippage_return REAL,
                fill_status TEXT NOT NULL,
                fill_reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (decision_id, horizon_sessions),
                CHECK (horizon_sessions IN (1, 3, 5, 10, 20)),
                CHECK (fill_status IN ('filled', 'unfilled'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_factor_observation_lookup "
            "ON factor_observation(factor_name, observation_date)",
            "CREATE INDEX IF NOT EXISTS idx_target_decision_date "
            "ON eod_target_decision(decision_date DESC, code)",
            "CREATE INDEX IF NOT EXISTS idx_target_decision_state "
            "ON eod_target_decision(decision_state, decision_date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_outcome_horizon "
            "ON eod_decision_outcome(horizon_sessions, fill_status)",
        ),
    )


def _migration_4(conn: sqlite3.Connection) -> None:
    _execute_all(
        conn,
        (
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                attempted_at TEXT,
                sent_at TEXT,
                last_error TEXT,
                CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
                CHECK (attempts >= 0)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_notification_outbox_status "
            "ON notification_outbox(status, id)",
        ),
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    immutable_tables = (
        "factor_import_batch",
        "factor_observation",
        "eod_risk_snapshot",
        "eod_target_decision",
        "eod_decision_outcome",
    )
    for table in immutable_tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is immutable');
            END
            """
        )


_MIGRATIONS: Dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {
    1: ("legacy macro slices and trend signals", _migration_1),
    2: ("append-only EOD run audit", _migration_2),
    3: ("factor provenance, decisions, risk snapshots and outcomes", _migration_3),
    4: ("durable idempotent notification outbox", _migration_4),
    5: ("immutability guards for evidence tables", _migration_5),
}


DISPATCH_STATES = (
    "pending",
    "sending",
    "retry_wait",
    "confirmed",
    "failed_permanent",
    "failed_exhausted",
    "rate_limited_over_budget",
    "uncertain",
    "quarantined_legacy",
)

_DISPATCH_DDL = f"""
CREATE TABLE notification_dispatch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_key TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    channel TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_alias TEXT NOT NULL,
    target_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts_used INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    first_attempt_at TEXT,
    budget_deadline_at TEXT,
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    claim_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_outcome TEXT,
    last_stage TEXT,
    last_http_status INTEGER,
    last_error_class TEXT,
    last_retry_after REAL,
    reason_code TEXT,
    message_id INTEGER,
    receipt_at TEXT,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    last_conflict_kind TEXT,
    last_conflict_sha256 TEXT,
    legacy_ref TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    CHECK (state IN ({", ".join("'" + state + "'" for state in DISPATCH_STATES)})),
    CHECK (max_attempts BETWEEN 1 AND 3),
    CHECK (attempts_used >= 0 AND attempts_used <= max_attempts),
    CHECK (conflict_count >= 0)
)
"""

_ATTEMPT_DDL = """
CREATE TABLE notification_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id INTEGER NOT NULL REFERENCES notification_dispatch(id),
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL DEFAULT 'started',
    stage TEXT,
    http_status INTEGER,
    error_class TEXT,
    retry_after REAL,
    message_id INTEGER,
    UNIQUE (dispatch_id, attempt_no),
    CHECK (attempt_no BETWEEN 1 AND 3)
)
"""

_V6_INDEXES = (
    "CREATE INDEX idx_notification_dispatch_state ON notification_dispatch(state, id)",
    "CREATE INDEX idx_notification_attempt_dispatch ON notification_attempt(dispatch_id, attempt_no)",
)
_V6_GUARDS = tuple(
    f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} history is append-only'); END"
    for table in ("notification_dispatch", "notification_attempt")
)
_V6_TABLES = ("notification_dispatch", "notification_attempt")
_V6_REQUIRED_COLUMNS = {
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

# Steps of the v6 migration, in order.  ``_fault(step)`` is a test-only injection hook
# called after each step inside the single transaction.
MIGRATION_6_STEPS = (
    "begin",
    "create_dispatch",
    "create_attempt",
    "create_indexes",
    "create_guards",
    "record_schema_row",
    "set_user_version",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def verify_notification_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless the v6 tables exactly carry the structure this code relies on."""
    for table in _V6_TABLES:
        if not _table_exists(conn, table):
            raise RuntimeError(f"数据库版本 6 缺少表 {table}")
        missing = _V6_REQUIRED_COLUMNS[table] - _columns(conn, table)
        if missing:
            raise RuntimeError(f"表 {table} 缺少列: {sorted(missing)}")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification_dispatch'"
    ).fetchone()[0]
    for state in DISPATCH_STATES:
        if f"'{state}'" not in sql:
            raise RuntimeError(f"notification_dispatch 状态约束缺少 {state}")
    triggers = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    for table in _V6_TABLES:
        if f"trg_{table}_no_delete" not in triggers:
            raise RuntimeError(f"表 {table} 缺少防删除触发器")


def _verify_baseline_without_notification_tables(conn: sqlite3.Connection) -> None:
    present = [table for table in _V6_TABLES if _table_exists(conn, table)]
    if present:
        raise RuntimeError(f"数据库版本 5 却存在版本 6 的表 {present}，结构不可信，拒绝使用")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending baseline migration atomically and return the schema version.

    Only versions 5 and 6 are accepted as already-current; a database at 6 has its
    table structure verified, a database above 6 is rejected.  The 5 -> 6 step is NOT
    performed here (see ``migrate_notification_v6``).
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > NOTIFICATION_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current} 高于应用支持版本 {NOTIFICATION_SCHEMA_VERSION}"
        )
    if current == NOTIFICATION_SCHEMA_VERSION:
        verify_notification_schema(conn)
        return current
    if current == CURRENT_SCHEMA_VERSION:
        _verify_baseline_without_notification_tables(conn)
        return current

    conn.execute("BEGIN IMMEDIATE")
    try:
        # A version-zero production database can already contain some newer
        # tables.  Every migration is deliberately idempotent so it can adopt
        # that database without rewriting historical rows.
        for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            description, migration = _MIGRATIONS[version]
            migration(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migration(version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (version, _utc_now_iso(), description),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return CURRENT_SCHEMA_VERSION


def migrate_notification_v6(
    conn: sqlite3.Connection, *, _fault: Callable[[str], None] | None = None
) -> int:
    """Explicit, additive 5 -> 6 migration: two new tables; legacy rows/tables untouched.

    Everything happens inside one ``BEGIN IMMEDIATE`` transaction (SQLite DDL and
    ``PRAGMA user_version`` are transactional), so an interruption at any step leaves
    the database exactly at version 5 and a rerun succeeds.  Idempotent at version 6.
    """
    fault = _fault or (lambda step: None)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > NOTIFICATION_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current} 高于应用支持版本 {NOTIFICATION_SCHEMA_VERSION}"
        )
    if current == NOTIFICATION_SCHEMA_VERSION:
        verify_notification_schema(conn)
        return current
    if current != CURRENT_SCHEMA_VERSION:
        raise RuntimeError(f"数据库版本 {current} 必须先升级到 {CURRENT_SCHEMA_VERSION}")
    _verify_baseline_without_notification_tables(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        fault("begin")
        conn.execute(_DISPATCH_DDL)
        fault("create_dispatch")
        conn.execute(_ATTEMPT_DDL)
        fault("create_attempt")
        for statement in _V6_INDEXES:
            conn.execute(statement)
        fault("create_indexes")
        for statement in _V6_GUARDS:
            conn.execute(statement)
        fault("create_guards")
        conn.execute(
            "INSERT OR REPLACE INTO schema_migration(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (
                NOTIFICATION_SCHEMA_VERSION,
                _utc_now_iso(),
                "additive notification_dispatch/notification_attempt (legacy outbox untouched)",
            ),
        )
        fault("record_schema_row")
        conn.execute(f"PRAGMA user_version={NOTIFICATION_SCHEMA_VERSION}")
        fault("set_user_version")
        verify_notification_schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return NOTIFICATION_SCHEMA_VERSION
