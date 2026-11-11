"""Append-only core storage. No supplier imports and no full-history writes."""
import datetime as dt
import hashlib
from pathlib import Path
import sqlite3
import snapshot_runtime as runtime
from core_contract import SCHEMA, encode, digest, date as _date, validate_core
from snapshot_contract import CST
CORE_DB = Path.home() / "research-data/core-market.db"


def _write_gate(record):
    runtime._supervised_soft_deadline("core SQLite write")
    if runtime._supervised_run_id() is not None and record["date"] != dt.datetime.now(CST).date().isoformat():
        raise ValueError("historical_core_write_forbidden")


def _guard_db(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if names & {"snapshot", "research_appendix"}:
        raise ValueError("refusing legacy/appendix database")

def read_core(day, db_path=CORE_DB):
    _date(day)
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError("core database is symlinked")
    if not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        _guard_db(conn)
        row = conn.execute("SELECT payload,payload_sha256,schema FROM core_market WHERE date=?", (day,)).fetchone()
    if row is None:
        return None
    if row[2] != SCHEMA:
        raise ValueError("core stored schema mismatch")
    if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
        raise ValueError("core stored payload hash mismatch")
    record = validate_core(runtime._strict_json(row[0]))
    if record["date"] != day:
        raise ValueError("core row date mismatch")
    return record

def store_core(record, db_path=CORE_DB):
    validate_core(record)
    _write_gate(record)
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError("core database is symlinked")
    if path.resolve() == Path(runtime.FULL_DB).resolve():
        raise ValueError("refusing legacy full snapshot database path")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encode(record)
    sha = digest(record)
    runtime.write_run_checkpoint(runtime._supervised_run_id(), target=record["date"],
                                 snapshotCandidateSha256=sha, schema=SCHEMA)
    with sqlite3.connect(path, timeout=runtime.SQLITE_TIMEOUT) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _write_gate(record)
        _guard_db(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS core_market (date TEXT PRIMARY KEY, schema TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)")
        row = conn.execute("SELECT payload,payload_sha256,schema FROM core_market WHERE date=?", (record["date"],)).fetchone()
        if row is not None:
            if row != (payload, sha, SCHEMA):
                raise ValueError("immutable core archive conflict")
            _write_gate(record)
            return {"status": "idempotent", "payloadSha256": sha}
        conn.execute("INSERT INTO core_market VALUES (?,?,?,?)", (record["date"], SCHEMA, payload, sha))
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("core database quick_check failed")
        _write_gate(record)
    return {"status": "inserted", "payloadSha256": sha}
