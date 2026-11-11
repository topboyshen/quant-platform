#!/usr/bin/env python3
"""Diagnose notification-to-CAS hash matches without emitting bodies, paths, or hash values."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


MANIFEST_DIR = Path("/var/lib/research-ledger/manifests/astock")
CAS_ROOT = Path("/var/lib/research-ledger/cas")


def main() -> int:
    notification_hashes: set[str] = set()
    notification_entries = 0
    manifests = sorted(MANIFEST_DIR.glob("astock-*.json"))
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("artifacts", []):
            if item.get("kind") == "notification":
                notification_entries += 1
                value = item.get("artifact_hash", item.get("sha256"))
                if isinstance(value, str) and len(value) == 64:
                    notification_hashes.add(value)
    raw_root = CAS_ROOT / "sha256"
    raw_matches = []
    if raw_root.exists():
        raw_matches = [path for path in raw_root.rglob("*") if path.is_file() and path.name in notification_hashes]
    with psycopg.connect("dbname=research_ledger host=/var/run/postgresql", row_factory=dict_row) as conn:
        values = list(notification_hashes)
        if values:
            report_refs = conn.execute("SELECT count(*) AS n FROM ledger.research_snapshot WHERE content_hash = ANY(%s)", (values,)).fetchone()["n"]
            evidence_refs = conn.execute("SELECT count(*) AS n FROM ledger.evidence_snapshot WHERE content_hash = ANY(%s)", (values,)).fetchone()["n"]
            source_refs = conn.execute("SELECT count(*) AS n FROM ledger.source_record WHERE content_hash = ANY(%s)", (values,)).fetchone()["n"]
            delivery_refs = conn.execute("SELECT count(*) AS n FROM ledger.delivery_event WHERE payload_hash = ANY(%s)", (values,)).fetchone()["n"]
        else:
            report_refs = evidence_refs = source_refs = delivery_refs = 0
    result = {
        "status": "diagnosed",
        "manifest_count": len(manifests),
        "notification_metadata_entries": notification_entries,
        "distinct_notification_hashes": len(notification_hashes),
        "raw_cas_notification_matches": len(raw_matches),
        "matched_objects_with_report_refs": int(report_refs),
        "matched_objects_with_evidence_refs": int(evidence_refs),
        "matched_objects_with_source_refs": int(source_refs),
        "delivery_metadata_refs": int(delivery_refs),
        "all_matches_in_pre_domain_separation_raw_namespace": bool(raw_matches),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
