#!/usr/bin/env python3
"""Versioned reduced-scope archive. Never writes the legacy full snapshot table."""
import argparse
import contextlib
import datetime as dt
import hashlib
import os
import re
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid

import daily_snapshot as legacy
from core_contract import (SCHEMA, CORE_SOURCES, APPENDIX_SOURCES, encode, digest,
                           date as _date, instant as _instant, validate_core, validate_calendar_evidence)
import snapshot_contract
CORE_DB = Path.home() / "research-data/core-market.db"
CORE_STATE = Path.home() / ".local/state/research-data/core-market"


def build_core(qlib, *, run_id, generated_at, calendar_evidence):
    # Deep-copy through canonical JSON: later collector mutations cannot alter the record.
    record = legacy._strict_json(encode({
        "schema": SCHEMA, "scope": "core_market", "fullSnapshot": False,
        "artifactKind": "qlib_market_summary", "includesCompletePriceArchive": False,
        "date": qlib["date"], "generatedAt": generated_at, "runId": run_id,
        "requiredSources": CORE_SOURCES,
        "appendix": {"status": "not_requested", "sources": APPENDIX_SOURCES},
        "qlib": qlib,
        "calendarEvidence": calendar_evidence,
    }))
    return validate_core(record)


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
        row = conn.execute("SELECT payload,payload_sha256 FROM core_market WHERE date=?", (day,)).fetchone()
    if row is None:
        return None
    if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
        raise ValueError("core stored payload hash mismatch")
    record = validate_core(legacy._strict_json(row[0]))
    if record["date"] != day:
        raise ValueError("core row date mismatch")
    return record


def store_core(record, db_path=CORE_DB):
    validate_core(record)
    legacy._supervised_soft_deadline("core SQLite transaction")
    if legacy._supervised_run_id() is not None and record["date"] != dt.datetime.now(snapshot_contract.CST).date().isoformat():
        raise ValueError("historical_core_write_forbidden")
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError("core database is symlinked")
    if path.resolve() == Path(legacy.DB).resolve():
        raise ValueError("refusing legacy full snapshot database path")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encode(record)
    sha = digest(record)
    with sqlite3.connect(path, timeout=legacy.SQLITE_TIMEOUT) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _guard_db(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS core_market (date TEXT PRIMARY KEY, schema TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)")
        row = conn.execute("SELECT payload,payload_sha256 FROM core_market WHERE date=?", (record["date"],)).fetchone()
        if row is not None:
            if row != (payload, sha):
                raise ValueError("immutable core archive conflict")
            return {"status": "idempotent", "payloadSha256": sha}
        conn.execute("INSERT INTO core_market VALUES (?,?,?,?)", (record["date"], SCHEMA, payload, sha))
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("core database quick_check failed")
    return {"status": "inserted", "payloadSha256": sha}











def load_calendar_evidence(path, now):
    from calendar_evidence import load_calendar_evidence as read_calendar
    try:
        return read_calendar(path, now)
    except (ValueError, OSError, TypeError, KeyError):
        raise legacy.SnapshotDeferredError("independent trading calendar evidence invalid or stale") from None


def run_core(*, db_path=CORE_DB, run_id=None, now=None, effective_trade_date=None,
             calendar_evidence=None):
    """Qlib-only summary, not a complete price archive; no live source calls."""
    run_id = run_id or uuid.uuid4().hex
    generated_at = now or dt.datetime.now().astimezone().isoformat()
    try:
        decision = validate_calendar_evidence(calendar_evidence, _instant(generated_at))
    except (ValueError, TypeError, KeyError):
        raise legacy.SnapshotDeferredError("independent trading calendar evidence invalid or stale") from None
    if decision["status"] == "closed":
        return {"status": "skipped_non_trading", "target": decision["previousTradingDate"],
                "calendarEvidence": calendar_evidence}
    q = legacy.qlib_market(None)  # generation consistency and coverage are checked here
    if effective_trade_date is not None and q["date"] < _date(effective_trade_date):
        # Before reading/creating the core DB or collecting a market stage.
        raise legacy.SnapshotDeferredError("Qlib target predates core contract effective date",
                                           target=q["date"], qlib_tag=q["generation"]["tag"])
    if q["date"] != decision["targetDate"]:
        raise legacy.SnapshotDeferredError("Qlib target missing or inconsistent with expected trading date",
                                           target=q["date"], qlib_tag=q["generation"]["tag"])
    prior = read_core(q["date"], db_path)
    if prior is not None:
        if digest(prior["qlib"]) != digest(q):
            raise legacy.SnapshotDeferredError("same-day Qlib content differs from immutable core archive",
                                               target=q["date"], qlib_tag=q["generation"]["tag"])
        return {"status": "skipped_existing", "target": q["date"], "payloadSha256": digest(prior),
                "calendarEvidence": calendar_evidence}
    record = build_core(q, run_id=run_id, generated_at=generated_at, calendar_evidence=calendar_evidence)
    return {**store_core(record, db_path), "target": q["date"], "coverage": q["coverage"],
            "qlibTag": q["generation"]["tag"], "calendarEvidence": calendar_evidence}


def main(argv=None, *, _cli=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=CORE_DB)
    parser.add_argument("--state-dir", type=Path, default=CORE_STATE)
    parser.add_argument("--contract-policy", type=Path)
    parser.add_argument("--calendar-evidence", type=Path)
    args = parser.parse_args(argv)  # no historical-date or legacy-contract mode
    supervised = legacy._supervised_run_id()
    full_state = os.environ.get("RESEARCH_DATA_FULL_STATE_DIR") if supervised else legacy.STATE_DIR
    if not full_state or args.state_dir.resolve() == Path(full_state).resolve():
        parser.error("core status must not overwrite full snapshot last-run")
    if _cli and legacy._supervised_run_id() is None:
        from snapshot_supervisor import run_supervised
        record, code = run_supervised(worker_path=str(Path(__file__).resolve()),
                                     worker_args=list(argv if argv is not None else sys.argv[1:]),
                                     state_dir=str(args.state_dir))
        print(encode(record))
        return code
    if supervised is not None:
        legacy._await_supervisor_gate()
        legacy._verify_supervisor_lock()
    run_id = supervised or uuid.uuid4().hex
    started = time.monotonic()
    with contextlib.nullcontext() if supervised is not None else legacy.snapshot_run_lock(str(args.state_dir)):
        try:
            effective = None
            if args.contract_policy is not None:
                policy = snapshot_contract.read_policy(args.contract_policy)
                if snapshot_contract.select(policy, dt.datetime.now(snapshot_contract.CST)) != SCHEMA:
                    raise ValueError("core contract is not active")
                effective = policy["effectiveTradeDate"]
            now = dt.datetime.now(snapshot_contract.CST)
            evidence = load_calendar_evidence(args.calendar_evidence, now)
            result = run_core(db_path=args.db, run_id=run_id, now=now.isoformat(),
                              effective_trade_date=effective, calendar_evidence=evidence)
            code = 0
        except legacy.SnapshotDeferredError as exc:
            result = {"status": "deferred", "target": exc.target, "sourceFailure": exc.source_failure, "errorType": type(exc).__name__}
            code = legacy.SNAPSHOT_DEFERRED_EXIT
        except Exception as exc:
            result = {"status": "rejected", "errorType": type(exc).__name__}
            code = 1
        result.update(schema=SCHEMA, scope="core_market", fullSnapshot=False, runId=run_id,
                      timestamp=dt.datetime.now().astimezone().isoformat(), durationSeconds=round(time.monotonic() - started, 3))
        legacy.write_last_run(result, str(args.state_dir))
        print(encode(result))
    return code


if __name__ == "__main__":
    sys.exit(main(_cli=True))
