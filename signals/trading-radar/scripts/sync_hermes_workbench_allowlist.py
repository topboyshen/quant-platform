#!/usr/bin/env python3
"""Refresh QuantDinger workbench allowlist staging dir.

Docker file bind-mounts freeze on unlink+recreate. This copies only allowlisted
sources into a staging tree directory-mounted into integration-api.

Freshness rules:
- Business timestamps come from SOURCE CONTENT (heartbeat JSON / report body
  parse / source file mtime), never from the sync clock.
- Staging files preserve source mtime via utime after write.
- A sync health sidecar records sync_ok / errors for failure visibility;
  it must not be treated as report generated_at or market cutoff.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", "/home/ubuntu/.hermes"))
STAGE = Path(
    os.environ.get(
        "HERMES_WORKBENCH_ALLOWLIST_DIR",
        "/home/ubuntu/trading-radar/data/hermes-workbench-allowlist",
    )
)
TZ = timezone(timedelta(hours=8))
REPORT_JOBS = ("026a1083d9e0", "ec6c86fd856c")
REPORT_NAME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}\.md")
MAX_REPORT_BYTES = 256 * 1024
MAX_REPORT_FUTURE_SKEW_SECONDS = 60

COPIES = [
    (HERMES / "state" / "gateway.heartbeat", Path("hermes-state/state/gateway.heartbeat")),
    (HERMES / "cron" / "ticker_heartbeat", Path("hermes-state/cron/ticker_heartbeat")),
    (HERMES / "cron" / "ticker_last_success", Path("hermes-state/cron/ticker_last_success")),
]


def _file_identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_latest_report(job: str, *, now: datetime | None = None) -> tuple[Path, bytes, int]:
    """Read the newest completed, dated output; never trust the legacy latest link.

    Hermes publishes dated .md files with atomic_replace. Their filename is the
    completion ordering key, not a replacement for the report's generated_at.
    Fail closed on an unreadable newest output, rather than serving an older
    success as if it were the newest attempt. Failure reports are valid outputs.
    """
    if job not in REPORT_JOBS:
        raise ValueError("report job is not allowlisted")
    current = now if now is not None else datetime.now(TZ)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("report selection clock must be timezone-aware")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(HERMES, dir_flags)
    try:
        # Pin each directory component; never follow a substituted parent link.
        for component in ("cron", "output", job):
            child_fd = os.open(component, dir_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        candidates = []
        for name in os.listdir(directory_fd):
            if not REPORT_NAME.fullmatch(name):
                continue
            try:
                stamp = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S.md").replace(tzinfo=TZ)
            except ValueError:
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                candidates.append((stamp, name, info))
        if not candidates:
            raise FileNotFoundError("no completed dated report in allowlisted job directory")
        stamp, name, selected = max(candidates, key=lambda row: row[0])
        if stamp > current.astimezone(TZ) + timedelta(seconds=MAX_REPORT_FUTURE_SKEW_SECONDS):
            raise ValueError("newest report filename is in the future")
        if not 0 < selected.st_size <= MAX_REPORT_BYTES:
            raise ValueError("newest report is empty or exceeds size limit")
        # O_NONBLOCK avoids hanging if a regular file is replaced by a FIFO.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(name, flags, dir_fd=directory_fd), "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or _file_identity(before) != _file_identity(selected):
                raise OSError("newest report changed before reading")
            data = handle.read(MAX_REPORT_BYTES + 1)
            after = os.fstat(handle.fileno())
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                _file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(named)
                or len(data) != after.st_size
                or not 0 < len(data) <= MAX_REPORT_BYTES
            ):
                raise OSError("newest report changed while reading")
        return HERMES / "cron" / "output" / job / name, data, after.st_mtime_ns
    finally:
        os.close(directory_fd)


def atomic_write(dst: Path, data: bytes, mode: int = 0o644, mtime_ns: int | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    if mtime_ns is not None:
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
    tmp.replace(dst)


def refresh_host_unit_status() -> None:
    helper = Path(
        "/home/ubuntu/.local/libexec/qd/write_hermes_unit_status.sh"
    )
    if not helper.exists():
        print(f"ERROR unit_status helper missing: {helper}", file=sys.stderr); return
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    subprocess.run(
        ["bash", str(helper), str(HERMES / "state" / "hermes-gateway.unit_status")],
        check=False,
        timeout=5,
        env=env,
    )
    try:
        os.chmod(HERMES / "state" / "hermes-gateway.unit_status", 0o644)
    except OSError:
        pass


def main() -> int:
    errors: list[dict] = []
    copied: list[dict] = []
    refresh_host_unit_status()

    unit_src = HERMES / "state" / "hermes-gateway.unit_status"
    all_copies = list(COPIES) + [
        (unit_src, Path("hermes-state/state/hermes-gateway.unit_status")),
    ] + [
        (HERMES / "cron" / "output" / job, Path("hermes-reports") / job / "latest.md")
        for job in REPORT_JOBS
    ]

    for src, rel in all_copies:
        dst = STAGE / rel
        try:
            if rel.parts[0] == "hermes-reports":
                real, data, mtime_ns = read_latest_report(src.name)
            else:
                if not src.exists():
                    errors.append({"src": str(src), "error": "missing_src"})
                    continue
                real = src.resolve() if src.is_symlink() else src
                data = real.read_bytes()
                mtime_ns = real.stat().st_mtime_ns
            atomic_write(dst, data, mtime_ns=mtime_ns)
            copied.append(
                {
                    "src": str(src),
                    "resolved": str(real),
                    "dst": str(dst),
                    "bytes": len(data),
                    "source_mtime": datetime.fromtimestamp(mtime_ns / 1e9, TZ).isoformat(),
                }
            )
        except Exception as e:
            errors.append({"src": str(src), "error": str(e), "trace": traceback.format_exc()[-400:]})

    health = {
        "schema": "hermes-workbench-allowlist-sync.v1",
        "sync_at": datetime.now(TZ).isoformat(),
        "ok": not errors,
        "copied": len(copied),
        "errors": errors,
        "note": "sync_at is operational only; never use as report generated_at, market_cutoff, or heartbeat updated_at",
    }
    # health sidecar mtime = now (ops); app must not treat this as business time
    atomic_write(STAGE / "hermes-state" / "sync_health.json", (json.dumps(health, ensure_ascii=False, indent=2) + "\n").encode())
    refresh_workbench_evidence()
    if errors:
        print(json.dumps(health, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


def refresh_workbench_evidence() -> None:
    """Independent evidence domain: do not turn its failures into Hermes failures."""
    helper = Path(__file__).with_name("publish_workbench_evidence.py")
    try:
        subprocess.run([sys.executable, str(helper)], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        # Per-domain health timestamps expire after 90s if the helper cannot run.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
