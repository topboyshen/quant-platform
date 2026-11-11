#!/usr/bin/env python3
"""Root-run, read-only AStock shadow adapter with notification data minimization.

CAS scope is strictly limited to regular, bounded files below the reports root. Notification
and orchestrator work files are *never* copied. They are only hashed in place and represented
by a strict metadata allowlist: derived event ID, derived status, mtime, byte count, and hash.
The adapter never parses notification JSON/text and never includes recipients, message bodies,
chat IDs, user IDs, credentials, or arbitrary file fields in its manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_FILE_BYTES = 32 * 1024 * 1024
PROJECT_ROOT = Path("/home/ubuntu/AStock_AI_System")
CAS_ROOTS = {"report": PROJECT_ROOT / "reports"}
METADATA_ONLY_ROOTS = {
    "orchestrator": PROJECT_ROOT / "work" / "orchestrator",
    "notification": PROJECT_ROOT / "work" / "notifications",
}
DENY_TOKENS = (".env", "token", "secret", "credential", ".pem", ".key", "id_rsa", "private")
SAFE_STATUS = {"started", "terminal", "pending", "sent", "success", "failed", "error", "recovery", "confirmed", "sending"}
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_safe(root: Path, path: Path) -> bool:
    """Accept only non-symlink regular files physically contained in a non-symlink root."""
    if root.is_symlink() or not root.is_dir() or path.is_symlink():
        return False
    try:
        root_real = root.resolve(strict=True)
        path_real = path.resolve(strict=True)
        path_real.relative_to(root_real)
    except (OSError, ValueError):
        return False
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)


def allowed_name(path: Path) -> bool:
    lowered = path.name.lower()
    return not any(token in lowered for token in DENY_TOKENS)


def bounded_regular_files(root: Path):
    if root.is_symlink() or not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not resolve_safe(root, path) or not allowed_name(path):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if 0 <= size <= MAX_FILE_BYTES:
            yield path


def cas_copy(source: Path, cas_root: Path, sha: str) -> Path:
    # Domain-separate report CAS keys from all metadata-only notification hashes.
    cas_key = hashlib.sha256(("RESEARCH_LEDGER_REPORT_CAS_V2:" + sha).encode()).hexdigest()
    target = cas_root / "v2" / "reports" / cas_key[:2] / cas_key
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or target.is_symlink() or digest(target) != sha:
            raise RuntimeError("CAS hash collision or corruption")
        return target
    fd, temporary = tempfile.mkstemp(prefix=".cas-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        temp_path = Path(temporary)
        if digest(temp_path) != sha:
            raise RuntimeError("CAS copy hash mismatch")
        os.chmod(temporary, 0o440)
        os.replace(temporary, target)
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def metadata_allowlist(kind: str, root: Path, path: Path, file_hash: str) -> dict[str, object]:
    """Return only derived metadata; never parse or retain work-file content."""
    rel = str(path.relative_to(PROJECT_ROOT))
    filename = path.name.lower()
    found = UUID_PATTERN.search(path.name)
    event_id = found.group(0).lower() if found else hashlib.sha256(rel.encode()).hexdigest()[:32]
    statuses = sorted(SAFE_STATUS.intersection(re.split(r"[^a-z]+", filename)))
    status = statuses[0] if statuses else "UNCLASSIFIED"
    details = path.stat()
    return {
        "kind": kind,
        "entry_mode": "METADATA_ONLY",
        "event_id": event_id,
        "status": status,
        "occurred_at": datetime.fromtimestamp(details.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
        "artifact_hash": file_hash,
        "bytes": details.st_size,
        "relative_path_hash": hashlib.sha256(rel.encode()).hexdigest(),
    }


def build_manifest(cas_root: Path, manifest_dir: Path) -> Path:
    artifacts: list[dict[str, object]] = []
    report_version = 0
    for kind, root in CAS_ROOTS.items():
        for path in bounded_regular_files(root):
            file_hash = digest(path)
            cas_path = cas_copy(path, cas_root, file_hash)
            report_version += 1
            metadata = path.stat()
            artifacts.append(
                {
                    "kind": kind,
                    "entry_mode": "CAS_ARCHIVED",
                    "relative_path": str(path.relative_to(PROJECT_ROOT)),
                    "storage_uri": f"file://{cas_path}",
                    "sha256": file_hash,
                    "bytes": metadata.st_size,
                    "mtime": datetime.fromtimestamp(metadata.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
                    "version_no": report_version,
                }
            )
    for kind, root in METADATA_ONLY_ROOTS.items():
        for path in bounded_regular_files(root):
            # Hashing is performed in place. The body is never copied, parsed, logged, or emitted.
            artifacts.append(metadata_allowlist(kind, root, path, digest(path)))
    stable_payload = {
        "manifest_schema": "astock-shadow-v2-data-minimized",
        "source_root": str(PROJECT_ROOT),
        "artifacts": artifacts,
    }
    manifest_hash = hashlib.sha256(json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload = {**stable_payload, "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "manifest_hash": manifest_hash}
    manifest_dir.mkdir(parents=True, exist_ok=True)
    out = manifest_dir / f"astock-{manifest_hash}.json"
    if out.exists():
        return out
    temporary = out.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cas-root", default="/var/lib/research-ledger/cas")
    parser.add_argument("--manifest-dir", default="/var/lib/research-ledger/manifests/astock")
    args = parser.parse_args()
    path = build_manifest(Path(args.cas_root), Path(args.manifest_dir))
    print(json.dumps({"manifest": str(path), "status": "created_or_reused"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
