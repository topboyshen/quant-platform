#!/usr/bin/env python3
"""Create a privacy-preserving legacy SQLite manifest from a backup copy.

Rows are read only to derive source identifiers, source-row hashes, and original timestamps.
No business field values are written to the manifest. All legacy records are retrospective.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_row_hash(row: sqlite3.Row) -> str:
    normalized = {key: row[key] for key in row.keys()}
    return digest_bytes(json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode())


def first_time(row: sqlite3.Row, candidates: tuple[str, ...]) -> str | None:
    keys = set(row.keys())
    for field in candidates:
        if field in keys and row[field] is not None:
            return str(row[field])
    return None


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--source-system", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    source = Path(args.source_db).resolve()
    output = Path(args.out).resolve()
    if not source.is_file():
        raise SystemExit("source backup missing")
    db_hash = digest_bytes(source.read_bytes())
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        records = []
        for table_row in table_rows:
            table = table_row["name"]
            for row in conn.execute(f"SELECT rowid AS __ledger_rowid__, * FROM {quote(table)} ORDER BY rowid"):
                records.append({
                    "source_table": table,
                    "source_record_id": str(row["__ledger_rowid__"]),
                    "source_record_hash": stable_row_hash(row),
                    "original_recorded_at": first_time(row, ("created_at", "registered_at", "occurred_at", "recorded_at", "settled_at", "updated_at")),
                    "original_sealed_at": first_time(row, ("sealed_at", "reviewed_at")),
                    "retrospective": True,
                    "reason_code": "LEGACY_SHADOW_IMPORT_REQUIRES_PROSPECTIVE_REQUALIFICATION",
                })
        payload = {
            "manifest_schema": "legacy-shadow-v1",
            "source_system": args.source_system,
            "source_database_hash": db_hash,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "records": records,
        }
        payload["manifest_hash"] = digest_bytes(json.dumps({key: value for key, value in payload.items() if key != "generated_at"}, ensure_ascii=False, sort_keys=True).encode())
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.chmod(0o640)
        temp.replace(output)
        print(json.dumps({"manifest": str(output), "source_system": args.source_system, "records": len(records), "all_retrospective": True}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
