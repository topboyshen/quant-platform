"""Database-only, read-only preflight for the explicit notification-v6 migration.

This does not initialize SwingStore, migrate, checkpoint, create a backup, inspect
message payloads, or certify the compatibility of other deployed processes.
Active WAL/journal databases are refused before SQLite is opened: ``mode=ro``
alone may create/write ``-shm``, while ``immutable=1`` can ignore committed WAL.
The operator must arrange a separate, authorized quiescent window; this module
never changes journal mode or removes sidecars to obtain a green result.
Filesystem checks are observations, not a lock against arbitrary concurrent
writers. Operator-established quiescence remains an external prerequisite.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from urllib.parse import quote

from runtime_contracts.notification_schema import (
    DISPATCH_STATES,
    NOTIFICATION_SCHEMA_VERSION,
    NOTIFICATION_TABLES,
    verify_notification_schema,
)


# A conservative, fixed floor for the two small new tables/indexes and rollback
# journal metadata, not a database-size multiplier, backup budget or reservation.
MIN_MIGRATION_FREE_BYTES = 4 * 1024 * 1024
MAX_PREFLIGHT_READ_SECONDS = 10

# Required legacy columns, not business data. Existing tables must survive the
# additive migration unchanged; a bare user_version=5 is not enough evidence.
BASELINE_COLUMNS = {
    "schema_migration": "version applied_at description",
    "macro_slice": "date total_amount margin_balance margin_growth_pct limit_up limit_down limit_ratio source extra created_at",
    "trend_signal": "id date code name signal_type period data_date title detail confidence notified_at created_at",
    "swing_eod_run": "id run_id decision_date data_date status started_at completed_at target_count evaluated_count failed_count signal_count allowed_signal_count blocked_signal_count decision_outcome reason_codes details created_at",
    "factor_import_batch": "batch_id factor_name source source_ref requested_start requested_end observed_at record_count checksum validation_status details",
    "factor_observation": "observation_id batch_id factor_name observation_date value source source_ref source_market_time available_at checksum components_json imported_at",
    "eod_risk_snapshot": "snapshot_id run_id decision_at level values_json source_timestamps_json available_at_json sources_json missing_fields_json created_at",
    "eod_target_decision": "decision_id run_id snapshot_id decision_date decision_at data_date code name sector data_status decision_state raw_trigger_count raw_signals_json risk_action risk_level reason_code reference_close validation_split created_at",
    "eod_decision_outcome": "outcome_id decision_id horizon_sessions evaluation_as_of entry_date entry_open entry_fill exit_date exit_close exit_fill benchmark_code benchmark_return gap_return gross_return net_return excess_return mfe mae fees_return slippage_return fill_status fill_reason created_at",
    "notification_outbox": "id idempotency_key channel operation payload_json status attempts created_at attempted_at sent_at last_error",
}
BASELINE_PRIMARY_KEYS = {
    "schema_migration": "version", "macro_slice": "date", "trend_signal": "id",
    "swing_eod_run": "id", "factor_import_batch": "batch_id",
    "factor_observation": "observation_id", "eod_risk_snapshot": "snapshot_id",
    "eod_target_decision": "decision_id", "eod_decision_outcome": "outcome_id",
    "notification_outbox": "id",
}
BASELINE_UNIQUE_KEYS = {
    "swing_eod_run": [("run_id",)],
    "factor_observation": [("batch_id", "factor_name", "observation_date")],
    "eod_risk_snapshot": [("run_id",)],
    "eod_target_decision": [("run_id", "code")],
    "eod_decision_outcome": [("decision_id", "horizon_sessions")],
    "notification_outbox": [("idempotency_key",)],
}
BASELINE_FOREIGN_KEYS = {
    "factor_observation": {("factor_import_batch", "batch_id", "batch_id")},
    "eod_risk_snapshot": {("swing_eod_run", "run_id", "run_id")},
    "eod_target_decision": {
        ("swing_eod_run", "run_id", "run_id"),
        ("eod_risk_snapshot", "snapshot_id", "snapshot_id"),
    },
    "eod_decision_outcome": {("eod_target_decision", "decision_id", "decision_id")},
}
INTEGER_COLUMNS = {
    "schema_migration": "version", "trend_signal": "id",
    "macro_slice": "limit_up limit_down",
    "swing_eod_run": "id target_count evaluated_count failed_count signal_count allowed_signal_count blocked_signal_count",
    "factor_import_batch": "record_count", "eod_target_decision": "raw_trigger_count",
    "eod_decision_outcome": "horizon_sessions", "notification_outbox": "id attempts",
}
REAL_COLUMNS = {
    "macro_slice": "total_amount margin_balance margin_growth_pct limit_ratio",
    "trend_signal": "confidence", "factor_observation": "value",
    "eod_target_decision": "reference_close",
    "eod_decision_outcome": "entry_open entry_fill exit_close exit_fill benchmark_return gap_return gross_return net_return excess_return mfe mae fees_return slippage_return",
}
CRITICAL_CHECKS = {
    "notification_outbox": (
        "CHECK(status IN ('pending','sending','sent','failed'))", "CHECK(attempts>=0)",
    ),
    "swing_eod_run": (
        "CHECK(status IN ('running','success','partial','failed','skipped'))",
        "CHECK(decision_outcome IN ('not_evaluated','emitted','no_signal','blocked','data_unavailable','skipped'))",
    ),
}
EVIDENCE_TABLES = (
    "factor_import_batch", "factor_observation", "eod_risk_snapshot",
    "eod_target_decision", "eod_decision_outcome",
)


class PreflightBlocked(RuntimeError):
    """A bounded, payload-free reason; raw SQLite/OS exception text is not logged."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sql_shape(sql: str) -> str:
    return re.sub(r"\s+", "", sql).casefold().replace("ifnotexists", "").rstrip(";")


def _unique_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    keys = set()
    for index in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not index[2] or index[4]:
            continue
        escaped = str(index[1]).replace('"', '""')
        keys.add(tuple(str(column[2]) for column in conn.execute(
            f'PRAGMA index_info("{escaped}")'
        ).fetchall()))
    return keys


def verify_baseline_schema(conn: sqlite3.Connection, version: int) -> None:
    """Read schema metadata only, without calling any migration entrypoint."""
    if version not in (5, NOTIFICATION_SCHEMA_VERSION):
        raise PreflightBlocked("unsupported_schema_version")
    for table, raw_columns in BASELINE_COLUMNS.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None or not isinstance(row[0], str) or "virtualtable" in _sql_shape(row[0]):
            raise PreflightBlocked(f"baseline_table_invalid:{table}")
        columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if set(raw_columns.split()) - {str(column[1]) for column in columns}:
            raise PreflightBlocked(f"baseline_columns_missing:{table}")
        primary_key = tuple(str(column[1]) for column in columns if column[5])
        if primary_key != (BASELINE_PRIMARY_KEYS[table],):
            raise PreflightBlocked(f"baseline_primary_key_invalid:{table}")
        integer_columns = set(INTEGER_COLUMNS.get(table, "").split())
        real_columns = set(REAL_COLUMNS.get(table, "").split())
        for column in columns:
            if str(column[1]) not in raw_columns.split():
                continue
            expected_type = ("INTEGER" if str(column[1]) in integer_columns else
                             "REAL" if str(column[1]) in real_columns else "TEXT")
            if str(column[2]).upper() != expected_type:
                raise PreflightBlocked(f"baseline_column_type_invalid:{table}:{column[1]}")
        shape = _sql_shape(row[0])
        if any(_sql_shape(check) not in shape for check in CRITICAL_CHECKS.get(table, ())):
            raise PreflightBlocked(f"baseline_check_constraint_invalid:{table}")
        unique_keys = _unique_keys(conn, table)
        if set(BASELINE_UNIQUE_KEYS.get(table, ())) - unique_keys:
            raise PreflightBlocked(f"baseline_unique_key_invalid:{table}")
        declared_foreign_keys = {
            (str(key[2]), str(key[3]), str(key[4]))
            for key in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            if key[5] == "NO ACTION" and key[6] == "NO ACTION"
        }
        if BASELINE_FOREIGN_KEYS.get(table, set()) - declared_foreign_keys:
            raise PreflightBlocked(f"baseline_foreign_key_invalid:{table}")

    for table in EVIDENCE_TABLES:
        for action in ("update", "delete"):
            name = f"trg_{table}_immutable_{action}"
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=? AND tbl_name=?",
                (name, table),
            ).fetchone()
            expected = (f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END")
            if row is None or _sql_shape(row[0]) != _sql_shape(expected):
                raise PreflightBlocked(f"baseline_immutability_guard_invalid:{table}:{action}")

    # INTEGER affinity is not a stored-value type guarantee (e.g. PRIMARY KEY
    # DESC / WITHOUT ROWID can still store TEXT or REAL). Do not coerce 5.5 to
    # 5, or expose a private value in int(value)'s ValueError traceback.
    history_versions = set()
    for row in conn.execute("SELECT version FROM schema_migration"):
        if type(row[0]) is not int:
            raise PreflightBlocked("baseline_migration_history_invalid")
        history_versions.add(row[0])
    if history_versions != set(range(1, version + 1)):
        raise PreflightBlocked("baseline_migration_history_invalid")
    if version == 5:
        for table in NOTIFICATION_TABLES:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                raise PreflightBlocked("version_5_contains_notification_v6_objects")
    else:
        try:
            verify_notification_schema(conn)
        except (sqlite3.Error, RuntimeError, TypeError):
            raise PreflightBlocked("notification_v6_structure_invalid") from None
        for table in NOTIFICATION_TABLES:
            columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            primary_keys = [column for column in columns if column[5]]
            if (len(primary_keys) != 1 or primary_keys[0][1] != "id"
                    or str(primary_keys[0][2]).upper() != "INTEGER"):
                raise PreflightBlocked("notification_v6_primary_key_invalid")
            expected_key = (("business_key",) if table == "notification_dispatch" else
                            ("dispatch_id", "attempt_no"))
            if expected_key not in _unique_keys(conn, table):
                raise PreflightBlocked("notification_v6_unique_key_invalid")
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            checks = (["CHECK(state IN (" + ",".join(f"'{state}'" for state in DISPATCH_STATES) + "))",
                       "CHECK(max_attempts BETWEEN 1 AND 3)",
                       "CHECK(attempts_used>=0 AND attempts_used<=max_attempts)",
                       "CHECK(conflict_count>=0)"] if table == "notification_dispatch" else
                      ["CHECK(attempt_no BETWEEN 1 AND 3)"])
            if any(_sql_shape(check) not in _sql_shape(sql) for check in checks):
                raise PreflightBlocked("notification_v6_check_constraint_invalid")
        foreign_keys = conn.execute('PRAGMA foreign_key_list("notification_attempt")').fetchall()
        if not any(key[2:7] == ("notification_dispatch", "dispatch_id", "id", "NO ACTION", "NO ACTION")
                   for key in foreign_keys):
            raise PreflightBlocked("notification_v6_foreign_key_invalid")


def _check_quiescent_file(path: Path) -> os.stat_result:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PreflightBlocked("database_not_regular_file")
        # Refuse before opening SQLite. This also preserves a hot rollback
        # journal rather than silently initiating recovery on a dry-run.
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                Path(str(path) + suffix).lstat()
            except FileNotFoundError:
                continue
            # Includes broken symlinks; never treat an unstatable sidecar as a
            # clean database. Other OS errors are fail-closed by the outer guard.
            raise PreflightBlocked("database_sidecars_require_quiescence")
        with path.open("rb") as handle:
            header = handle.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
            raise PreflightBlocked("database_header_invalid")
        if header[18] != 1 or header[19] != 1:
            raise PreflightBlocked("wal_database_requires_quiescence")
        return info
    except FileNotFoundError:
        raise PreflightBlocked("database_not_found") from None
    except OSError:
        raise PreflightBlocked("database_not_readable") from None


def _migration_capacity(path: Path, version: int) -> dict:
    if version == NOTIFICATION_SCHEMA_VERSION:
        return {"required": False, "status": "not_needed_already_v6"}
    try:
        directory = path.parent
        # No test file is created to check writability. These are necessary
        # conditions at check time, not a reservation or proof of future access.
        if not os.access(path, os.W_OK) or not os.access(directory, os.W_OK | os.X_OK):
            raise PreflightBlocked("migration_write_permission_denied")
        fs = os.statvfs(directory)
        if fs.f_flag & getattr(os, "ST_RDONLY", 1):
            raise PreflightBlocked("migration_filesystem_readonly")
        if getattr(fs, "f_favail", 1) == 0:
            raise PreflightBlocked("migration_free_inodes_insufficient")
        available = fs.f_bavail * fs.f_frsize
        if available < MIN_MIGRATION_FREE_BYTES:
            raise PreflightBlocked("migration_free_space_insufficient")
        return {"required": True, "status": "necessary_conditions_met",
                "available_bytes": available, "minimum_free_bytes": MIN_MIGRATION_FREE_BYTES,
                "space_reserved": False}
    except OSError:
        raise PreflightBlocked("migration_filesystem_unverified") from None


def preflight_notification_database(path: Path) -> dict:
    """Read-only checks of an operator-quiesced rollback-journal database."""
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        raise PreflightBlocked("database_path_unverified") from None
    before = _check_quiescent_file(path)
    uri = "file:" + quote(path.as_posix(), safe="/") + "?mode=ro"
    conn = None
    deadline = time.monotonic() + MAX_PREFLIGHT_READ_SECONDS
    read_budget_exceeded = False

    def stop_expired_read() -> int:
        nonlocal read_budget_exceeded
        read_budget_exceeded = time.monotonic() > deadline
        return int(read_budget_exceeded)

    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3, isolation_level=None)
        conn.set_progress_handler(stop_expired_read, 1000)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise PreflightBlocked("readonly_connection_unverified")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        verify_baseline_schema(conn, version)
        if conn.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise PreflightBlocked("database_quick_check_failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise PreflightBlocked("database_foreign_key_check_failed")
        capacity = _migration_capacity(path, version)
        after = _check_quiescent_file(path)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(after):
            raise PreflightBlocked("database_changed_during_preflight")
        return {"status": "passed", "user_version": version,
                "baseline_structure": "verified", "notification_structure": (
                    "not_present_v5" if version == 5 else "verified_v6"),
                "capacity": capacity, "connection": "mode_ro_query_only",
                "supported_journal_mode": "delete_without_sidecars",
                "writer_quiescence": "operator_must_establish_not_certified",
                "deployed_reader_compatibility": "not_checked",
                "deployment_authorization": "not_granted_by_this_check"}
    except (sqlite3.Error, ValueError, TypeError):
        if read_budget_exceeded:
            raise PreflightBlocked("preflight_read_budget_exceeded") from None
        raise PreflightBlocked("database_read_or_structure_invalid") from None
    finally:
        if conn is not None:
            conn.set_progress_handler(None, 0)
            if conn.in_transaction:
                conn.rollback()
            conn.close()
