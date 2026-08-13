#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ALLOWED = {"kind", "entry_mode", "event_id", "status", "occurred_at", "artifact_hash", "bytes", "relative_path_hash"}


def main() -> int:
    manifest_dir = Path("/var/lib/research-ledger/manifests/astock")
    cas_root = Path("/var/lib/research-ledger/cas/sha256")
    manifests = sorted(manifest_dir.glob("astock-*.json"))
    if not manifests:
        raise SystemExit("no AStock manifest")
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
    notifications = [item for item in manifest["artifacts"] if item["kind"] == "notification"]
    cas_names = {path.name for path in cas_root.rglob("*") if path.is_file()}
    for item in notifications:
        if item.get("entry_mode") != "METADATA_ONLY":
            raise SystemExit("notification not metadata-only")
        if "storage_uri" in item or item["artifact_hash"] in cas_names:
            raise SystemExit("notification content hash found in CAS")
        if not set(item).issubset(ALLOWED):
            raise SystemExit("notification manifest contains non-allowlisted field")
    print(json.dumps({"status": "ok", "notification_metadata_entries": len(notifications), "cas_notification_blobs": 0, "manifest": str(manifests[-1])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
