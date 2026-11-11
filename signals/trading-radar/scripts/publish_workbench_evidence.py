#!/usr/bin/env python3
"""Publish bounded, allowlisted workbench evidence without accessing business DBs.

The digest domain preserves report bytes and source mtime. The release domain
exposes only three fixed services and selected runtime fields. Operational sync
timestamps never replace a report's own generated_at.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/trading-radar")
STAGE = Path("data/hermes-workbench-allowlist/workbench-evidence")
DIGEST_SOURCE = Path("data/approved-signals/digests")
DIGEST_STAGE = STAGE / "digest-view/digests"
RELEASE_STAGE = STAGE / "release"
SERVICES = (
    "radar-quantdinger-backend",
    "radar-quantdinger-frontend",
    "radar-integration-api",
)
MAX_JSON_BYTES = 512 * 1024
MAX_MARKDOWN_BYTES = 256 * 1024
MAX_PIN_BYTES = 64 * 1024
_IMAGE_REF = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _aware_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _directory(root: Path, relative: Path, *, create: bool = False) -> int:
    """Pin every path component, including ancestors of the supplied root."""
    absolute = root.absolute()
    if relative.is_absolute() or ".." in absolute.parts or ".." in relative.parts:
        raise ValueError("invalid path")
    fd = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        root_parts = absolute.parts[1:]
        for index, part in enumerate((*root_parts, *relative.parts)):
            created = False
            if create and index >= len(root_parts):
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            if created:
                os.fchmod(child, 0o755)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(root: Path, relative: Path, limit: int) -> tuple[bytes, int, tuple]:
    directory = _directory(root, relative.parent)
    try:
        named = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1 or not 0 < named.st_size <= limit:
            raise ValueError("invalid source file")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(relative.name, flags, dir_fd=directory), "rb") as handle:
            before = os.fstat(handle.fileno())
            if _identity(before) != _identity(named) or not stat.S_ISREG(before.st_mode):
                raise OSError("source changed before read")
            content = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            current = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
            if (_identity(before) != _identity(after) or _identity(after) != _identity(current)
                    or not 0 < len(content) <= limit or len(content) != after.st_size):
                raise OSError("source changed during read")
        return content, after.st_mtime_ns, _identity(after)
    finally:
        os.close(directory)


def _atomic_write(root: Path, relative: Path, data: bytes, *, mtime_ns: int | None = None) -> None:
    directory = _directory(root, relative.parent, create=True)
    temporary = "." + relative.name + "." + uuid.uuid4().hex + ".tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(temporary, flags, 0o644, dir_fd=directory), "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(data)
            handle.flush()
            if mtime_ns is not None:
                os.utime(handle.fileno(), ns=(mtime_ns, mtime_ns))
            os.fsync(handle.fileno())
        os.replace(temporary, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _remove(root: Path, relative: Path) -> None:
    directory = _directory(root, relative.parent)
    try:
        os.unlink(relative.name, dir_fd=directory)
    finally:
        os.close(directory)


def _publish_pair(root: Path, reports: list[tuple[Path, bytes, int | None, int]]) -> None:
    # Validate and retain both prior files before changing either. Roll back a
    # completed first replacement if a later replacement fails. Hash-bound
    # health is written last, so readers also reject a pair during publication.
    previous = {}
    for relative, _, _, limit in reports:
        try:
            previous[relative] = _read(root, relative, limit)
        except FileNotFoundError:
            previous[relative] = None
    changed = []
    try:
        for relative, content, mtime_ns, _ in reports:
            _atomic_write(root, relative, content, mtime_ns=mtime_ns)
            changed.append(relative)
    except BaseException:
        for relative in reversed(changed):
            old = previous[relative]
            if old is None:
                _remove(root, relative)
            else:
                _atomic_write(root, relative, old[0], mtime_ns=old[1])
        raise


def _digest(root: Path, observed_at: str) -> bool:
    try:
        json_source = DIGEST_SOURCE / "latest.json"
        md_source = DIGEST_SOURCE / "latest.md"
        document = _read(root, json_source, MAX_JSON_BYTES)
        markdown = _read(root, md_source, MAX_MARKDOWN_BYTES)
        payload = json.loads(document[0].decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != "0.2" or payload.get("report_type") != "execution_digest":
            raise ValueError("invalid digest schema")
        _aware_time(payload.get("generated_at"))
        body = payload.get("markdown")
        if not isinstance(body, str) or body.encode("utf-8") != markdown[0]:
            raise ValueError("digest pair differs")
        if document != _read(root, json_source, MAX_JSON_BYTES) or markdown != _read(root, md_source, MAX_MARKDOWN_BYTES):
            raise OSError("digest source changed")
        health = {
            "schema_version": "quant.execution-digest-sync.v1", "sync_at": observed_at, "ok": True,
            "hashes": {"latest.json": hashlib.sha256(document[0]).hexdigest(), "latest.md": hashlib.sha256(markdown[0]).hexdigest()},
        }
        _publish_pair(root, [
            (DIGEST_STAGE / "latest.json", document[0], document[1], MAX_JSON_BYTES),
            (DIGEST_STAGE / "latest.md", markdown[0], markdown[1], MAX_MARKDOWN_BYTES),
            (DIGEST_STAGE / "sync_health.json", _json_bytes(health), None, MAX_PIN_BYTES),
        ])
        return True
    except Exception as error:
        health = {"schema_version": "quant.execution-digest-sync.v1", "sync_at": observed_at, "ok": False, "error": type(error).__name__}
    _atomic_write(root, DIGEST_STAGE / "sync_health.json", _json_bytes(health))
    return False


def _valid_text(value: object, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("invalid release field")
    return value


def _sanitize_pin(raw: bytes) -> bytes:
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("invalid pin schema")
    services = payload.get("services")
    if not isinstance(services, dict):
        raise ValueError("invalid pin services")
    sanitized = {}
    for name in SERVICES:
        service = services.get(name)
        if not isinstance(service, dict):
            raise ValueError("missing pin service")
        sanitized[name] = {"image": _valid_text(service.get("image"), _IMAGE_REF), "image_id": _valid_text(service.get("image_id"), _IMAGE_ID)}
    return _json_bytes({"schema_version": 1, "services": sanitized})


# Docker itself emits only the listed fields. Config.Env, mounts, labels and
# command arguments never enter this publisher's stdout capture.
_INSPECT_FORMAT = (
    '{"Config":{"Image":{{json .Config.Image}}},"Image":{{json .Image}},'
    '"State":{"Running":{{json .State.Running}},"Health":{"Status":'
    '{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}},'
    '"StartedAt":{{json .State.StartedAt}}},"Id":{{json .Id}}}'
)


def _inspect_services() -> dict:
    result = subprocess.run(
        ["docker", "inspect", "--format", _INSPECT_FORMAT, *SERVICES],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5,
    )
    lines = result.stdout.splitlines()
    if len(lines) != len(SERVICES) or len(result.stdout) > MAX_PIN_BYTES:
        raise ValueError("invalid inspect result")
    return {name: json.loads(line) for name, line in zip(SERVICES, lines)}


def _sanitize_actual(raw: object) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("Config"), dict) or not isinstance(raw.get("State"), dict):
        raise ValueError("invalid inspect record")
    state = raw["State"]
    if type(state.get("Running")) is not bool:
        raise ValueError("invalid running status")
    health = state.get("Health")
    status = health.get("Status") if isinstance(health, dict) else "none"
    if status not in ("healthy", "unhealthy", "starting", "none"):
        raise ValueError("invalid health status")
    _aware_time(state.get("StartedAt"))
    return {
        "image": _valid_text(raw["Config"].get("Image"), _IMAGE_REF),
        "image_id": _valid_text(raw.get("Image"), _IMAGE_ID),
        "running": state["Running"], "health": status,
        "container_id": _valid_text(raw.get("Id"), _CONTAINER_ID), "started_at": state["StartedAt"],
    }


def _release(root: Path, observed_at: str, inspect_fn) -> bool:
    try:
        pin_path = Path("deploy/production_pin.json")
        source = _read(root, pin_path, MAX_PIN_BYTES)
        sanitized = _sanitize_pin(source[0])
        records = _inspect_services() if inspect_fn is None else {name: inspect_fn(name) for name in SERVICES}
        services = {name: _sanitize_actual(records[name]) for name in SERVICES}
        if source[0] != _read(root, pin_path, MAX_PIN_BYTES)[0]:
            raise OSError("pin changed during inspect")
        _atomic_write(root, RELEASE_STAGE / "production_pin.json", sanitized, mtime_ns=source[1])
        actual = {
            "schema_version": "quant.workbench-release.v1", "observed_at": observed_at, "available": True,
            "pin_sha256": hashlib.sha256(sanitized).hexdigest(), "services": services,
        }
    except Exception as error:
        actual = {"schema_version": "quant.workbench-release.v1", "observed_at": observed_at, "available": False, "error": type(error).__name__}
    _atomic_write(root, RELEASE_STAGE / "actual.json", _json_bytes(actual))
    return actual["available"]


def publish(root: Path, now: datetime, inspect_fn=None) -> dict[str, bool]:
    """Publish each domain independently; injected inspect_fn takes one service.

    A test inspect_fn returns a Docker-shaped dict containing Config.Image,
    Image, State.Running, State.Health.Status, Id and State.StartedAt. Production
    collects all three services in one bounded five-second Docker invocation.
    """
    root = Path(root)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("publisher clock must be timezone-aware")
    observed_at = now.isoformat()
    outcomes = {}
    for name, action in (("digest", lambda: _digest(root, observed_at)), ("release", lambda: _release(root, observed_at, inspect_fn))):
        try:
            outcomes[name] = action()
        except Exception:
            # Unwritable health/evidence in one domain must not suppress the other.
            outcomes[name] = False
    return outcomes


def main() -> int:
    if len(sys.argv) != 1:
        return 2
    return 0 if all(publish(ROOT, datetime.now(timezone.utc)).values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
