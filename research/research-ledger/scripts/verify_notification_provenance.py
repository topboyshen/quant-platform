#!/usr/bin/env python3
"""Provenance-aware notification/CAS privacy verifier.

It never emits content, paths, source identifiers, or digest values. A raw digest overlap is
accepted only when the same bytes are independently re-hashable from an allowlisted report file
and all Ledger CAS provenance remains REPORT-only.
"""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path("/home/ubuntu/AStock_AI_System").resolve()
REPORT_ROOT = (PROJECT_ROOT / "reports").resolve()
MANIFEST_DIR = Path("/var/lib/research-ledger/manifests/astock")
CAS_ROOT = Path("/var/lib/research-ledger/cas")
MAX_FILE_BYTES = 32 * 1024 * 1024


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_report(relative_path: str) -> Path | None:
    candidate = (PROJECT_ROOT / relative_path)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(REPORT_ROOT)
        mode = candidate.lstat().st_mode
        if candidate.is_symlink() or not stat.S_ISREG(mode) or candidate.stat().st_size > MAX_FILE_BYTES:
            return None
        return resolved
    except (OSError, ValueError):
        return None


def scalar_sql(query: str) -> int:
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-At", "-d", "research_ledger", "-c", query],
        check=True, capture_output=True, text=True,
    )
    return int(result.stdout.strip() or "0")


def main() -> int:
    reports: dict[str, int] = {}
    notification_hashes: set[str] = set()
    notification_entries = 0
    notification_adapter_writes = 0
    manifest_count = 0
    for manifest_path in sorted(MANIFEST_DIR.glob("astock-*.json")):
        manifest_count += 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("artifacts", []):
            kind = item.get("kind")
            if kind == "report" and item.get("entry_mode") == "CAS_ARCHIVED":
                source = safe_report(str(item.get("relative_path", "")))
                report_hash = item.get("sha256")
                if source and isinstance(report_hash, str) and digest(source) == report_hash:
                    reports[report_hash] = reports.get(report_hash, 0) + 1
            elif kind == "notification":
                notification_entries += 1
                digest_value = item.get("artifact_hash")
                if isinstance(digest_value, str) and len(digest_value) == 64:
                    notification_hashes.add(digest_value)
                if item.get("entry_mode") == "CAS_ARCHIVED" or "storage_uri" in item:
                    notification_adapter_writes += 1
    raw_names = {path.name for path in (CAS_ROOT / "sha256").rglob("*") if path.is_file()} if (CAS_ROOT / "sha256").exists() else set()
    raw_overlap = notification_hashes.intersection(raw_names)
    rebuildable_overlap = {value for value in raw_overlap if value in reports}
    notification_only = raw_overlap.difference(rebuildable_overlap)
    if notification_hashes:
        digest_list = ",".join("'" + item + "'" for item in sorted(notification_hashes))
        invalid_snapshot_provenance = scalar_sql(f"SELECT count(*) FROM ledger.research_snapshot WHERE content_hash IN ({digest_list}) AND snapshot_type <> 'ASTOCK_REPORT'")
        evidence_provenance = scalar_sql(f"SELECT count(*) FROM ledger.evidence_snapshot WHERE content_hash IN ({digest_list})")
        source_provenance = scalar_sql(f"SELECT count(*) FROM ledger.source_record WHERE content_hash IN ({digest_list})")
    else:
        invalid_snapshot_provenance = evidence_provenance = source_provenance = 0
    notification_provenance = invalid_snapshot_provenance + evidence_provenance + source_provenance
    report_cas_consistent = 0
    for report_hash in reports:
        domain_key = hashlib.sha256(("RESEARCH_LEDGER_REPORT_CAS_V2:" + report_hash).encode()).hexdigest()
        cas_path = CAS_ROOT / "v2" / "reports" / domain_key[:2] / domain_key
        if cas_path.is_file() and digest(cas_path) == report_hash:
            report_cas_consistent += 1
    body_scan_leakage = len(notification_only) + notification_adapter_writes + notification_provenance
    result = {
        "status": "pass" if not notification_only and notification_provenance == 0 and notification_adapter_writes == 0 else "fail",
        "manifest_count": manifest_count,
        "notification_metadata_entries": notification_entries,
        "raw_digest_overlap": len(raw_overlap),
        "legitimate_report_rebuildable_overlap": len(rebuildable_overlap),
        "notification_only": len(notification_only),
        "notification_provenance": notification_provenance,
        "notification_adapter_writes": notification_adapter_writes,
        "body_scan_leakage": body_scan_leakage,
        "verified_report_sources": len(reports),
        "report_cas_hash_consistent": report_cas_consistent,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
