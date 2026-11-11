#!/usr/bin/env python3
"""Explicit, additive schema 5 -> 6 migration (notification_dispatch / notification_attempt).

NOT run automatically by anything. One transaction adds two tables, indexes/guards and
updates schema_migration/user_version metadata; business tables and rows are untouched.
No database copy is made (repository policy NO_BACKUP).
Rollback = turn the feature flag off (sending pauses); the new tables and their history stay.

Default is a read-only report.  ``--apply`` is required to change anything.
Run from the repository root:  ``python3 -m scripts.migrate_notification_v6 --db <path> [--apply]``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from radar.swing.migrations import (
    NOTIFICATION_SCHEMA_VERSION,
    migrate_notification_v6,
    verify_notification_schema,
)
from runtime_contracts.notification_preflight import (
    PreflightBlocked,
    preflight_notification_database,
    verify_baseline_schema,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="swing sqlite database path")
    parser.add_argument("--apply", action="store_true", help="perform the 5 -> 6 migration")
    parser.add_argument("--json", action="store_true", help="emit bounded preflight evidence as JSON")
    args = parser.parse_args(argv)
    path = Path(args.db)
    conn = None
    try:
        evidence = preflight_notification_database(path)
        path = path.resolve()
        if not args.apply:
            if args.json:
                sys.stdout.write(json.dumps({"dry_run": True, **evidence}, sort_keys=True) + "\n")
            else:
                sys.stdout.write(f"user_version={evidence['user_version']}\n")
                sys.stdout.write("database-only preflight: passed; no change made\n")
                sys.stdout.write("deployed readers: not checked; this is not deployment approval\n")
            return 0
        # mode=rw refuses to create a database if the selected file disappears.
        # The actual schema write remains solely in the single-transaction
        # migrate_notification_v6 entrypoint, never in the read-only preflight.
        uri = "file:" + quote(path.as_posix(), safe="/") + "?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        verify_baseline_schema(conn, version)
        result = migrate_notification_v6(conn)
        verify_notification_schema(conn)
        if args.json:
            status = "already_migrated" if evidence["user_version"] == 6 else "applied"
            sys.stdout.write(json.dumps({"dry_run": False, "status": status,
                "user_version": result, "deployed_reader_compatibility": "not_checked"}, sort_keys=True) + "\n")
        else:
            sys.stdout.write(f"schema now {result} (target {NOTIFICATION_SCHEMA_VERSION}); structure verified\n")
        return 0
    except (PreflightBlocked, sqlite3.Error, RuntimeError, OSError, ValueError, TypeError) as exc:
        reason = exc.code if isinstance(exc, PreflightBlocked) else "migration_failed"
        if args.json:
            sys.stdout.write(json.dumps({"status": "blocked", "reason": reason}, sort_keys=True) + "\n")
        else:
            sys.stderr.write(f"database preflight/migration blocked: {reason}\n")
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
