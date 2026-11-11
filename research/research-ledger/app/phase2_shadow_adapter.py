#!/usr/bin/env python3
"""Read a P0 Phase2 SQLite backup and emit a hash-only shadow import manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def h(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_locator_hint(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "UNPARSEABLE_SOURCE"


def rows(conn: sqlite3.Connection, query: str):
    return [dict(row) for row in conn.execute(query).fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    source = Path(args.source_db).resolve()
    out = Path(args.out).resolve()
    if not source.is_file():
        raise SystemExit("source backup missing")
    uri = f"file:{source}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        protocols = []
        for row in rows(conn, "SELECT protocol_hash,protocol_id,version,protocol_json,frozen_at,created_at FROM protocol ORDER BY protocol_hash"):
            payload = json.loads(row["protocol_json"])
            protocols.append({
                "protocol_hash": row["protocol_hash"],
                "version": row["version"],
                "universe_rule_hash": h(payload.get("universe", {})),
                "target_definition_hash": h(payload.get("target", payload.get("target_definition", {}))),
                "source_policy_hash": h(payload.get("data_source", payload.get("source", {}))),
                "frozen_at": row["frozen_at"],
                "created_at": row["created_at"],
                "row_hash": h({"protocol_hash": row["protocol_hash"], "version": row["version"], "protocol_json_hash": h(payload), "frozen_at": row["frozen_at"]}),
            })
        batches = []
        for row in rows(conn, "SELECT batch_id,cohort_id,protocol_hash,data_cutoff,target_session,status,expected_n,nominal_n,qualified_n,independent_batch_count,block_reason,created_at,sealed_at FROM batch ORDER BY created_at,batch_id"):
            safe = {key: row[key] for key in row.keys()}
            safe["row_hash"] = h(safe)
            batches.append(safe)
        forecasts = []
        for row in rows(conn, "SELECT forecast_id,batch_id,cohort_id,symbol,target_session,data_cutoff,created_at,registered_at,sealed_at,probability,model_version,feature_snapshot_hash,source_hashes_json,correlation_group,status FROM forecast ORDER BY created_at,forecast_id"):
            source_hashes = json.loads(row["source_hashes_json"])
            safe = {key: row[key] for key in row.keys() if key != "source_hashes_json"}
            safe["source_hashes"] = source_hashes
            safe["row_hash"] = h(safe)
            forecasts.append(safe)
        outcomes = []
        for row in rows(conn, "SELECT outcome_id,forecast_id,outcome,source_url,source_as_of,content_hash,settled_at,status,settler,reviewed_at,reviewed_by,review_hash FROM outcome ORDER BY settled_at,outcome_id"):
            safe = {key: row[key] for key in row.keys() if key != "source_url"}
            safe["source_url"] = source_locator_hint(row["source_url"])
            safe["row_hash"] = h(safe)
            outcomes.append(safe)
        risk_vetoes = []
        for row in rows(conn, "SELECT veto_id,batch_id,reason,evidence_hash,created_at,reviewer,overridden_at,overridden_by,override_reason FROM risk_veto ORDER BY created_at,veto_id"):
            safe = {
                "veto_id": row["veto_id"],
                "batch_id": row["batch_id"],
                "reason_hash": h(row["reason"]),
                "evidence_hash": row["evidence_hash"],
                "created_at": row["created_at"],
                "reviewer": row["reviewer"],
                "overridden_at": row["overridden_at"],
                "overridden_by": row["overridden_by"],
                "override_reason_hash": h(row["override_reason"]) if row["override_reason"] else None,
            }
            safe["row_hash"] = h(safe)
            risk_vetoes.append(safe)
        payload = {
            "manifest_schema": "phase2-shadow-v1",
            "source_system": "RESEARCH_SYSTEM_PHASE2",
            "source_database_hash": source_hash,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "protocols": protocols,
            "batches": batches,
            "forecasts": forecasts,
            "outcomes": outcomes,
            "risk_vetoes": risk_vetoes,
        }
        payload["manifest_hash"] = h({key: value for key, value in payload.items() if key != "generated_at"})
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.chmod(0o640)
        temp.replace(out)
        print(json.dumps({"manifest": str(out), "protocols": len(protocols), "batches": len(batches), "forecasts": len(forecasts), "outcomes": len(outcomes), "risk_vetoes": len(risk_vetoes)}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
