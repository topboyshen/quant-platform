#!/usr/bin/env python3
"""Explicit, separately observed research appendix; never a full historical snapshot."""
import argparse
import datetime as dt
from pathlib import Path
import re
import sqlite3
import sys
import uuid

import core_market as core
import snapshot_runtime as runtime

SCHEMA = "research.appendix.v1"
APPENDIX_DB = Path.home() / "research-data/research-appendix.db"


def collectors(day, codes):
    """Creating this map performs no network calls; only requested entries execute."""
    import daily_snapshot as legacy  # load suppliers only for an explicit appendix request
    return {
        "akshareMarket": legacy.akshare_market,
        "lhb": lambda: legacy.akshare_lhb(day), "spot": legacy.spot_market,
        "fundFlow": lambda: legacy.fund_flow_sina(codes, days=5),
        "lhbOfficial": lambda: legacy.lhb_official(day),
        "mootdx": lambda: legacy.mootdx_quotes(codes), "tencent": lambda: legacy.tencent_quote(codes),
        "baostock": lambda: legacy.baostock_valuation(codes, days=30),
        "announcements": lambda: legacy.collect_cninfo_announcements(codes),
        "news": lambda: legacy.cls_telegraph(20), "northbound": legacy.northbound_flow,
        "concepts": lambda: legacy.collect_concepts(codes), "insiders": lambda: legacy.collect_shareholders(codes),
        "hotStocks": legacy.hot_stocks, "globalMarkets": legacy.global_markets,
    }


def build_appendix(core_record, requested, codes, *, source_collectors=None, clock=None, request_id=None):
    core.validate_core(core_record)
    if not requested or len(requested) != len(set(requested)) or set(requested) - set(core.APPENDIX_SOURCES):
        raise ValueError("explicit unique allowed sources required")
    if not codes or any(not re.fullmatch(r"[0-9]{6}", c) for c in codes):
        raise ValueError("explicit security codes required")
    clock = clock or (lambda: dt.datetime.now().astimezone().isoformat())
    started = clock()
    observed_day = core._instant(started).astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    if observed_day != core_record["date"]:
        raise ValueError("live appendix cannot represent historical market observations")
    available = source_collectors if source_collectors is not None else collectors(core_record["date"], codes)
    if set(requested) - set(available):
        raise ValueError("requested source collector unavailable")
    sources = {}
    for name in requested:
        observed_at = clock()
        try:
            payload = available[name]()
            core.encode(payload)  # reject non-finite JSON
            errors = runtime.source_error_paths(payload)
            sources[name] = {"status": "source_error" if errors else "observed_unreviewed",
                             "observedAt": observed_at, "completedAt": clock(), "payload": payload,
                             "errorPaths": errors}
        except Exception as exc:
            sources[name] = {"status": "failed", "observedAt": observed_at, "completedAt": clock(),
                             "errorType": type(exc).__name__}  # no raw response/credential details
    completed = clock()
    start, end = core._instant(started), core._instant(completed)
    if end < start or end.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat() != observed_day:
        raise ValueError("appendix collection crossed market observation day")
    result = {"schema": SCHEMA, "scope": "research_appendix", "fullSnapshot": False,
            "requestId": request_id or uuid.uuid4().hex, "coreMarketDate": core_record["date"],
            "corePayloadSha256": core.digest(core_record), "securityCodes": list(codes),
            "requestedSources": list(requested), "collectionWindow": {"startedAt": started, "completedAt": completed},
            "status": "partial" if any(s["status"] in ("failed", "source_error") for s in sources.values()) else "collected_unreviewed",
            "sources": sources, "limitation": "On-demand observations; no full-snapshot, historical-completeness, or research-review claim."}
    validate_appendix(result, core_record)
    return result


def validate_appendix(record, core_record):
    core.validate_core(core_record)
    if record.get("schema") != SCHEMA or record.get("scope") != "research_appendix" or record.get("fullSnapshot") is not False:
        raise ValueError("invalid appendix contract")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record.get("requestId", "")):
        raise ValueError("invalid appendix request identity")
    if record.get("coreMarketDate") != core_record["date"] or record.get("corePayloadSha256") != core.digest(core_record):
        raise ValueError("appendix/core identity mismatch")
    requested = record.get("requestedSources")
    if (not isinstance(requested, list) or not requested or len(set(requested)) != len(requested)
            or set(requested) - set(core.APPENDIX_SOURCES) or set(record.get("sources", {})) != set(requested)):
        raise ValueError("appendix requested/observed sources mismatch")
    window = record["collectionWindow"]
    start, end = core._instant(window["startedAt"]), core._instant(window["completedAt"])
    cst = dt.timezone(dt.timedelta(hours=8))
    if (not core._instant(core_record["generatedAt"]) <= start <= end
            or start.astimezone(cst).date().isoformat() != core_record["date"]
            or end.astimezone(cst).date().isoformat() != core_record["date"]):
        raise ValueError("invalid appendix/core observation time")
    for source in record["sources"].values():
        if (source.get("status") not in {"observed_unreviewed", "failed", "source_error"}
                or not start <= core._instant(source["observedAt"]) <= core._instant(source["completedAt"]) <= end):
            raise ValueError("invalid appendix source observation")
    expected_status = "partial" if any(s["status"] in ("failed", "source_error") for s in record["sources"].values()) else "collected_unreviewed"
    if record.get("status") != expected_status:
        raise ValueError("appendix result/source status mismatch")
    core.encode(record)
    return record


def store_appendix(record, core_record, db_path=APPENDIX_DB):
    validate_appendix(record, core_record)
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError("appendix database is symlinked")
    if path.resolve() in {Path(runtime.FULL_DB).resolve(), core.CORE_DB.resolve()}:
        raise ValueError("appendix database must be separate")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = core.encode(record)
    sha = core.digest(record)
    with sqlite3.connect(path, timeout=runtime.SQLITE_TIMEOUT) as conn:
        conn.execute("BEGIN IMMEDIATE")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names & {"snapshot", "core_market"}:
            raise ValueError("refusing snapshot/core database")
        conn.execute("CREATE TABLE IF NOT EXISTS research_appendix (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)")
        row = conn.execute("SELECT payload,payload_sha256 FROM research_appendix WHERE request_id=?", (record["requestId"],)).fetchone()
        if row is not None:
            if row != (payload, sha):
                raise ValueError("immutable appendix request conflict")
            return {"status": "idempotent", "payloadSha256": sha}
        conn.execute("INSERT INTO research_appendix VALUES (?,?,?)", (record["requestId"], payload, sha))
    return {"status": "inserted", "payloadSha256": sha}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--core-date", required=True)
    p.add_argument("--source", action="append", required=True, choices=core.APPENDIX_SOURCES)
    p.add_argument("--code", action="append", required=True)
    p.add_argument("--core-db", type=Path, default=core.CORE_DB)
    p.add_argument("--db", type=Path, default=APPENDIX_DB)
    args = p.parse_args(argv)
    record = core.read_core(args.core_date, args.core_db)
    if record is None:
        p.error("bound core archive missing")
    appendix = build_appendix(record, args.source, args.code)
    stored = store_appendix(appendix, record, args.db)
    print(core.encode({**stored, "schema": SCHEMA, "requestId": appendix["requestId"], "collectionStatus": appendix["status"]}))
    return 0 if appendix["status"] == "collected_unreviewed" else 1


if __name__ == "__main__":
    sys.exit(main())
