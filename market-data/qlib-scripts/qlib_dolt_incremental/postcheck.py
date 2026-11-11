#!/usr/bin/env python3
"""Read-only post-promotion checks; never starts a download or business task."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
from unittest.mock import patch

from atomic import read_small
from run import iso, read_calendar
from alignment import validate_index_market_alignment
from tree import validate_tree


def verify(data, target, health_file):
    root = data / "cn_data"
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("unsafe_active_path")
    before = root.stat().st_ino
    manifest_raw = read_small(root / ".qlib-dolt-incremental.json", 65536)
    manifest = json.loads(manifest_raw)
    calendar = read_calendar(root)
    if calendar[-1] != iso(target) or manifest["targetDate"] != target:
        raise ValueError("installed_target_disagrees")
    tags = [(data / ".tag").read_text().strip(), (root / ".qlib-release").read_text().strip()]
    if tags[0] != tags[1] or tags[0] != manifest["baseReleaseTag"]:
        raise ValueError("release_identity_disagrees")
    source_absence = manifest.get("sourceAbsence")
    source_commit = manifest["sourceCommit"]
    price = validate_tree(root, calendar, target, source_absence=source_absence,
                          expected_source_commit=source_commit)
    indices = validate_index_market_alignment(root, calendar, source_absence=source_absence,
                                              expected_source_commit=source_commit,
                                              legacy_unquoted=manifest.get("legacyUnquotedCSI"))
    spec = importlib.util.spec_from_file_location("_read_only_health_postcheck", health_file)
    health = importlib.util.module_from_spec(spec)
    with patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden")), \
            patch.object(sqlite3, "connect", side_effect=AssertionError("database_forbidden")), \
            patch.object(subprocess, "run", side_effect=AssertionError("subprocess_forbidden")), \
            patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess_forbidden")):
        spec.loader.exec_module(health)
        actual_calendar, actual_complete, generation = health.qlib_evidence(root)
    if actual_calendar != calendar or actual_complete != target or generation[1] != tags[0]:
        raise ValueError("consumer_evidence_disagrees")
    if root.stat().st_ino != before or read_small(root / ".qlib-dolt-incremental.json", 65536) != manifest_raw:
        raise ValueError("generation_changed_during_postcheck")
    if list(data.glob(".qlib-incremental.*")):
        raise ValueError("transaction_cleanup_incomplete")
    return {"status": "postcheck_passed", "targetDate": target, "sourceCommit": manifest["sourceCommit"],
            "generationId": manifest["generationId"], "inode": before, "baseReleaseTag": tags[0],
            "calendarRows": len(calendar), "calendarSha256": hashlib.sha256(read_small(root / "calendars/day.txt", 1048576)).hexdigest(),
            "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(), "priceValidation": price,
            "indexPriceValidation": indices, "consumerReferenceCompleteDate": actual_complete,
            "dataCompleteness": "partial_source_absence" if (source_absence and source_absence.get("dates")) or indices["status"] == "partial" else "no_proven_source_absence",
            "sourceAbsence": source_absence,
            "legacyUnquotedCSI": manifest.get("legacyUnquotedCSI"),
            "networkOrDatabaseCalls": False, "transactionResidue": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/home/ubuntu/qlib/data"))
    parser.add_argument("--through", required=True)
    parser.add_argument("--health-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.data_dir, args.through, args.health_file), sort_keys=True))
