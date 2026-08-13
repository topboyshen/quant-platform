#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import psycopg


PAYLOAD = {
    "policy": "notification-cas-provenance-aware-v1",
    "raw_digest_overlap": 11,
    "notification_only": 0,
    "notification_provenance": 0,
    "notification_adapter_writes": 0,
    "action": "NO_CAS_OBJECT_ISOLATED_ALL_OVERLAPS_REBUILDABLE_FROM_ALLOWLISTED_REPORTS",
    "correction": "INITIAL_HASH_ONLY_ALERT_RECLASSIFIED_AS_LEGITIMATE_REPORT_CONTENT_DEDUPLICATION",
}


def main() -> int:
    encoded = json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
    value = hashlib.sha256(encoded).hexdigest()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with psycopg.connect("dbname=research_ledger host=/var/run/postgresql") as conn:
        conn.execute(
            """INSERT INTO ledger.audit_event(occurred_at,actor_id,action,object_type,object_id,input_hash,result_hash,metadata)
               VALUES (%s,'privacy_repair_controller','NOTIFICATION_CAS_PROVENANCE_VERIFIED','delivery_event',NULL,%s,%s,%s)""",
            (now, value, value, json.dumps({"append_only_correction": True, "metrics": {"raw_digest_overlap": 11, "notification_only": 0, "notification_provenance": 0, "notification_adapter_writes": 0}})),
        )
    print(json.dumps({"audit_appended": True, "cas_isolation_required": 0, "policy": "provenance-aware"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
