"""M9 decommission inventory (read-only).

Collects the legacy units, timers, containers and data paths that will be
stopped/archived at decommission time.  The tool never stops, disables or
deletes anything; it only records what exists with a stable manifest hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

LEGACY_PREFIXES = ("trading-radar", "astock", "quant-", "research-", "signal-bus", "qd-", "radar-", "nng8-")

DEFAULT_PATHS = (
    "/home/ubuntu/trading-radar",
    "/home/ubuntu/AStock_AI_System",
    "/home/ubuntu/signal-bus",
    "/home/ubuntu/QuantDinger",
    "/opt/research-ledger",
    "/opt/research-system-phase2",
    "/opt/nng8-dashboard",
)


def is_legacy_unit(name: str) -> bool:
    return any(str(name).startswith(prefix) for prefix in LEGACY_PREFIXES)


def _timer(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit": row.get("unit"),
        "activates": row.get("activates"),
        "next": row.get("next"),
        "last": row.get("last"),
        "persistent": row.get("persistent"),
    }


def parse_timers(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_timer(row) for row in rows if is_legacy_unit(row.get("unit", ""))]


def parse_containers(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        name = str(row.get("Names") or row.get("name") or "")
        if not name.startswith("radar-"):
            continue
        out.append(
            {
                "name": name,
                "image": row.get("Image") or row.get("image"),
                "status": row.get("Status") or row.get("status"),
            }
        )
    return sorted(out, key=lambda item: item["name"])


def parse_du(output: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].isdigit():
            sizes[parts[1].strip()] = int(parts[0])
    return sizes


def build_manifest(
    *,
    timers: Iterable[dict[str, Any]],
    user_timers: Iterable[dict[str, Any]],
    containers: Iterable[dict[str, Any]],
    paths: dict[str, int],
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    now = generated_at or dt.datetime.now(dt.UTC)
    timers_list = sorted(parse_timers(timers), key=lambda item: item["unit"] or "")
    user_list = sorted(parse_timers(user_timers), key=lambda item: item["unit"] or "")
    path_list = [{"path": path, "bytes": int(size)} for path, size in sorted(paths.items())]
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "timers": timers_list,
        "user_timers": user_list,
        "containers": parse_containers(containers),
        "paths": path_list,
        "totals": {
            "timers": len(timers_list) + len(user_list),
            "containers": len(parse_containers(containers)),
            "path_bytes": sum(item["bytes"] for item in path_list),
        },
    }


def manifest_sha256(manifest: dict[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k != "sha256"}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_paths(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything is archive-only; deletion requires separate consent and is never planned here."""
    return [
        {"path": item["path"], "bytes": item["bytes"], "action": "archive_only"} for item in manifest.get("paths", [])
    ]


def _run(cmd: list[str], runner: Callable[..., subprocess.CompletedProcess]) -> str:
    try:
        result = runner(cmd, capture_output=True, text=True, check=False)
    except OSError:  # e.g. systemctl/docker absent on a non-systemd host
        return ""
    return result.stdout if result.returncode == 0 else ""


def _json_rows(text: str) -> list[dict[str, Any]]:
    """systemctl --output=json returns one JSON array; docker --format returns NDJSON."""
    text = text.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except ValueError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def collect(
    *,
    paths: Iterable[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read-only collection via systemctl/docker/du; never mutates state."""
    timers_text = _run(["systemctl", "list-timers", "--all", "--no-pager", "--output=json"], runner)
    user_text = _run(["systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"], runner)
    containers_text = _run(["docker", "ps", "--format", "{{json .}}"], runner)
    sizes: dict[str, int] = {}
    for path in DEFAULT_PATHS if paths is None else paths:
        sizes.update(parse_du(_run(["du", "-sb", str(path)], runner)))
    return build_manifest(
        timers=_json_rows(timers_text),
        user_timers=_json_rows(user_text),
        containers=_json_rows(containers_text),
        paths=sizes,
        generated_at=generated_at,
    )
