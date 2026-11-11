#!/usr/bin/env python3
"""Build a non-invasive Qlib/MLflow/Kronos model-artifact manifest.

No experiment is run, no model/data file is copied, and no portfolio/watchlist is read.
Only fixed paths, hashes, Git commits, sizes, and mtimes are registered.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(path: Path) -> str:
    try:
        return subprocess.check_output(["git", "-c", f"safe.directory={path}", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "UNAVAILABLE"


def cutoff(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()


def manifest() -> dict:
    qlib = Path("/home/ubuntu/qlib")
    kronos = Path("/home/ubuntu/Kronos")
    qlib_source = qlib / "source"
    qlib_config = qlib / "scripts" / "workflow_lightgbm_alpha158.yaml"
    qlib_artifact = qlib / "mlflow.db"
    kronos_code = kronos
    kronos_artifact = Path("/home/ubuntu/kronos-models") / "mini"
    artifacts = []
    if qlib_artifact.is_file() and qlib_config.is_file():
        artifacts.append({
            "source_system": "QLIB",
            "artifact_uri": f"file://{qlib_artifact}",
            "artifact_hash": digest(qlib_artifact),
            "data_cutoff": cutoff(qlib / "data"),
            "universe_hash": hashlib.sha256(b"UNDECLARED_PUBLIC_EXPERIMENT_UNIVERSE").hexdigest(),
            "feature_hash": digest(qlib_config),
            "label_hash": hashlib.sha256(b"UNDECLARED_LABEL_DEFINITION").hexdigest(),
            "code_hash": git_head(qlib_source),
            "config_hash": digest(qlib_config),
            "random_seed": "UNDECLARED",
            "split_hash": hashlib.sha256(b"UNDECLARED_SPLIT_REQUIRES_REPLAY_APPROVAL").hexdigest(),
        })
    if kronos_artifact.exists():
        artifacts.append({
            "source_system": "KRONOS",
            "artifact_uri": f"file://{kronos_artifact}",
            "artifact_hash": hashlib.sha256((str(kronos_artifact) + str(kronos_artifact.stat().st_mtime_ns)).encode()).hexdigest(),
            "data_cutoff": cutoff(kronos_artifact),
            "universe_hash": hashlib.sha256(b"UNDECLARED_SCENARIO_ONLY").hexdigest(),
            "feature_hash": hashlib.sha256(b"UNDECLARED_SCENARIO_ONLY").hexdigest(),
            "label_hash": hashlib.sha256(b"UNDECLARED_SCENARIO_ONLY").hexdigest(),
            "code_hash": git_head(kronos_code),
            "config_hash": hashlib.sha256(b"UNDECLARED_SCENARIO_ONLY").hexdigest(),
            "random_seed": "UNDECLARED",
            "split_hash": hashlib.sha256(b"UNDECLARED_SCENARIO_ONLY").hexdigest(),
        })
    payload = {"manifest_schema": "model-shadow-v1", "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "artifacts": artifacts}
    payload["manifest_hash"] = hashlib.sha256(json.dumps({"artifacts": artifacts}, sort_keys=True).encode()).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = manifest()
    output.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    output.chmod(0o640)
    print(json.dumps({"manifest": str(output), "artifacts": len(data["artifacts"]), "kronos_scenario_only": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
