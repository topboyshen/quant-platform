"""Read the staged generation through the actual read-only consumer API."""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
from unittest.mock import patch

from atomic import MANIFEST, read_small


def consumer_check(health_file):
    health_file = Path(health_file).absolute()
    if health_file.is_symlink() or health_file.resolve() != health_file:
        raise ValueError("unsafe_consumer_module_path")
    # Capture a reviewed module identity before starting any source download.
    expected = read_small(health_file, 1024 * 1024)

    def check(root, manifest):
        if read_small(health_file, 1024 * 1024) != expected:
            raise ValueError("consumer_module_changed")
        manifest_bytes = read_small(root / MANIFEST)
        if json.loads(manifest_bytes) != manifest:
            raise ValueError("candidate_manifest_argument_disagrees")
        inode = root.stat().st_ino
        spec = importlib.util.spec_from_file_location("_qlib_candidate_readonly_consumer", health_file)
        health = importlib.util.module_from_spec(spec)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("database_forbidden")), \
                patch.object(subprocess, "run", side_effect=AssertionError("subprocess_forbidden")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess_forbidden")):
            # Execute the exact captured bytes, not a stale __pycache__ entry.
            exec(compile(expected, str(health_file), "exec"), health.__dict__)
            calendar, complete, generation = health.qlib_evidence(root)
        if (calendar[-1] != manifest["targetDate"] or complete != manifest["targetDate"]
                or generation[1] != manifest["baseReleaseTag"]
                or generation[2] != hashlib.sha256(manifest_bytes).hexdigest()
                or generation[3][0] != manifest["generationId"]):
            raise ValueError("candidate_consumer_disagrees")
        if (root.stat().st_ino != inode or read_small(root / MANIFEST) != manifest_bytes
                or read_small(health_file, 1024 * 1024) != expected):
            raise ValueError("candidate_or_consumer_changed_during_check")
        return {"status": "passed", "referenceCompleteDate": complete,
                "consumerSha256": hashlib.sha256(expected).hexdigest(),
                "manifestBytes": len(manifest_bytes), "networkOrDatabaseCalls": False}

    return check
