#!/usr/bin/env python3
"""Release-based fallback for the QLib nightly updater.

Use this only after the DoltHub SQL path has failed. It downloads the exact
GitHub release for the target trade date, verifies the release manifest and
archive, extracts it safely, then runs the proven append-and-publish path.

Evidence discipline:
* The fallback requires a matching same-target terminal SQL failure. It copies
  only fixed fields of that failure into ``update-status.attempts.jsonl``
  before any network access.
* A fallback failure never writes or rewrites a status file. The original
  ``failed``/``interrupted`` record remains the authoritative failure, and the
  fallback stage/error survive in the attempt journal.
* A successful fallback publishes the normal ``current`` status with
  ``sourceMode=release_fallback`` and ``sourceFailure`` (fixed fields only).
* No URL, response body, archive path or subprocess output is logged.

The download budget is separate from, and does not extend, the SQL run's
shared 84-second transport budget. Archive time is bounded by ``--archive-
timeout`` and the caller's own process timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import release_package  # noqa: E402


def _append_module():
    """Import the live append module only when a release is ready to apply."""
    import release_append
    return release_append

SHANGHAI = ZoneInfo("Asia/Shanghai")
SAFE_CODE = re.compile(r"[A-Za-z0-9_:.,+\-]{1,160}\Z")
TARGET_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
PROXY_ENV_NAMES = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


def _environment_proxy() -> str | None:
    """Promote a validated cron/PAM proxy when no explicit --proxy is given.

    release_package deliberately disables ambient curl proxies unless one is
    passed explicitly. Cron supplies the host proxy through pam_env, so the
    trigger must promote the standard HTTPS/ALL variables to an explicit
    --proxy. Invalid values fail closed instead of silently bypassing it.
    """
    for name in PROXY_ENV_NAMES:
        value = os.environ.get(name)
        if not value:
            continue
        if not release_package._valid_proxy(value):
            raise ValueError("invalid_proxy_environment")
        return value
    return None


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat()


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, (release_package.ReleaseError, ValueError)):
        text = str(exc)
        if SAFE_CODE.fullmatch(text):
            return text
    return type(exc).__name__


def _attempt_path(status_path: Path) -> Path:
    return status_path.with_name(status_path.stem + ".attempts.jsonl")


def append_attempt(status_path: Path, record: dict) -> None:
    """Append one bounded JSON line; never follow or replace the file."""
    path = _attempt_path(status_path)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe_attempt_record")
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_sql_failure(status_path: Path, *, target: str) -> dict | None:
    """Copy only fixed fields from a same-target terminal SQL failure."""
    try:
        if status_path.is_symlink() or not status_path.is_file():
            return None
        document = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("status") not in ("failed", "interrupted"):
        return None
    if document.get("targetDate") is not None:
        if document.get("targetDate") != target:
            return None
    elif document.get("plannedDate") != target:
        # A pre-target failure may omit targetDate. Its planned day must still
        # be the target, or a later day's failure could be misattributed.
        return None
    failure = {}
    for key in ("runId", "status", "failureStage", "error", "time", "plannedDate", "sourceCommit"):
        value = document.get(key)
        if isinstance(value, str) and 0 < len(value) <= 300:
            failure[key] = value
        elif value is None:
            failure[key] = None
    return failure


def _calendar(root: Path) -> list[str]:
    return (root / "calendars" / "day.txt").read_text(encoding="ascii").splitlines()


def _active_last(data_dir: Path) -> str | None:
    """Return the active tree's last calendar day, or None if unreadable."""
    try:
        days = _calendar(Path(data_dir) / "cn_data")
    except (OSError, UnicodeError):
        return None
    return days[-1] if days else None


def validate_release_tree(data_dir: Path, release_dir: Path, target: str) -> list[str]:
    """Prove the extracted tree is an append whose last day is ``target``."""
    active = _calendar(data_dir / "cn_data")
    release = _calendar(release_dir)
    if not active or not release:
        raise ValueError("release_calendar_empty")
    if active[-1] >= target:
        raise ValueError("active_tree_already_at_or_beyond_target")
    if release[:len(active)] != active:
        raise ValueError("release_calendar_is_not_an_append")
    added = release[len(active):]
    if not added or added[-1] != target:
        raise ValueError("release_target_mismatch")
    return added


def run(*, data_dir: Path, target: str, tag: str, work_dir: Path,
        health_file: Path, backup_dir: Path | None = None, publish: bool = False,
        status_path: Path, planned_date: str | None = None,
        proxy: str | None = None, metadata_timeout: int = 25,
        archive_timeout: int = 1200, wait_until: str | None = None,
        poll_seconds: int = 600, keep_work: bool = False) -> dict:
    if TARGET_RE.fullmatch(target) is None:
        raise ValueError("invalid_target_date")
    if release_package.TAG_RE.fullmatch(tag) is None:
        raise ValueError("invalid_release_tag")
    data_dir = Path(data_dir).resolve()
    status_path = Path(status_path)
    sql_failure = read_sql_failure(status_path, target=target)
    planned_date = (planned_date or (sql_failure or {}).get("plannedDate")
                    or datetime.now(SHANGHAI).date().isoformat())
    run_id = str(uuid.uuid4())
    last = _active_last(data_dir)
    if last is not None and last >= target:
        append_attempt(status_path, {
            "schemaVersion": 1, "event": "source_fallback_skipped",
            "status": "skipped", "time": _now(), "runId": run_id,
            "targetDate": target, "releaseTag": tag,
            "plannedDate": planned_date,
            "reason": "active_tree_already_at_or_beyond_target",
            "activeLastDate": last, "sourceFailure": sql_failure,
        })
        return {"status": "already_current", "target": target,
                "releaseTag": tag, "activeLastDate": last,
                "sourceCommit": None, "rows": 0, "calendarAdded": []}
    if publish and sql_failure is None:
        raise ValueError("no_matching_sql_failure")
    started = {"schemaVersion": 1, "event": "source_fallback_started",
               "status": "running", "time": _now(), "runId": run_id,
               "targetDate": target, "releaseTag": tag,
               "plannedDate": planned_date, "failureStage": "release_prepare",
               "sourceFailure": sql_failure}
    append_attempt(status_path, started)

    stage = "release_prepare"
    work: Path | None = None

    def mark(name: str) -> None:
        nonlocal stage
        stage = name

    try:
        work = Path(work_dir) / f"run-{datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S')}-{run_id[:8]}"
        stage = "release_manifest"
        if wait_until is not None:
            try:
                deadline = datetime.fromisoformat(wait_until)
            except ValueError:
                raise ValueError("invalid_wait_until") from None
            if deadline.tzinfo is None:
                raise ValueError("invalid_wait_until")
            release_package.wait_for_release(
                tag, deadline.timestamp(), poll_seconds=poll_seconds,
                proxy=proxy, metadata_timeout=metadata_timeout)
        prepared = release_package.prepare_release(
            tag, work, proxy=proxy, metadata_timeout=metadata_timeout,
            archive_timeout=archive_timeout, on_stage=mark)
        stage = "release_validate"
        added = validate_release_tree(data_dir, prepared["release_dir"], target)
        stage = "release_append"
        result = _append_module().publish_release(
            data_dir=data_dir, release_dir=prepared["release_dir"],
            source_commit=prepared["manifest"]["dolt_commit"], target=target,
            health_file=health_file, backup_dir=backup_dir, publish=publish,
            planned_date=planned_date, source_mode="release_fallback",
            source_failure=sql_failure)
        append_attempt(status_path, {
            "schemaVersion": 1, "event": "source_fallback_completed",
            "status": "completed" if publish else "validated", "time": _now(),
            "runId": run_id, "targetDate": target, "releaseTag": tag,
            "plannedDate": planned_date, "failureStage": None,
            "error": None, "calendarAdded": added,
            "sourceCommit": prepared["manifest"]["dolt_commit"],
            "sourceMode": "release_fallback", "sourceFailure": sql_failure,
        })
        if publish and not keep_work:
            # The publish already succeeded; a cleanup failure must not turn
            # it into a reported fallback failure.
            try:
                archive = prepared.get("archive")
                extract = Path(prepared["release_dir"]).parent
                if archive is not None and Path(archive).exists():
                    Path(archive).unlink()
                if extract.exists() and extract.name.startswith("extract-"):
                    import shutil
                    shutil.rmtree(extract, ignore_errors=True)
            except OSError:
                pass
        return {"status": result.get("status"), "target": target,
                "releaseTag": tag, "sourceCommit": prepared["manifest"]["dolt_commit"],
                "rows": result.get("rows"), "calendarAdded": added}
    except BaseException as exc:
        code = _safe_error(exc)
        try:
            append_attempt(status_path, {
                "schemaVersion": 1, "event": "source_fallback_failed",
                "status": "locked" if isinstance(exc, BlockingIOError) else "failed",
                "time": _now(), "runId": run_id, "targetDate": target,
                "releaseTag": tag, "plannedDate": planned_date,
                "failureStage": stage, "error": code,
                "sourceFailure": sql_failure,
            })
        except OSError:
            # The journal is evidence, not the fallback result; never replace
            # the original stage/error with a journal-write failure.
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/home/ubuntu/qlib/data"))
    parser.add_argument("--target", required=True)
    parser.add_argument("--tag", help="Release tag; defaults to --target")
    parser.add_argument("--work-dir", type=Path, default=Path("/home/ubuntu/qlib/work/release-fallback"))
    parser.add_argument("--health-file", type=Path, default=Path("/home/ubuntu/research-data/data_health.py"))
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--status-file", type=Path, default=Path("/home/ubuntu/qlib/logs/update-status.json"))
    parser.add_argument("--planned-date",
                        help="Run day for the status record; defaults to the matched failure's plannedDate")
    parser.add_argument("--proxy", help="Optional socks5h proxy URL for GitHub")
    parser.add_argument("--metadata-timeout", type=int, default=25)
    parser.add_argument("--archive-timeout", type=int, default=1200)
    parser.add_argument("--wait-until", help="ISO timestamp; poll only while the release is unpublished")
    parser.add_argument("--poll-seconds", type=int, default=600)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    tag = args.tag or args.target
    try:
        proxy = args.proxy if args.proxy is not None else _environment_proxy()
        result = run(data_dir=args.data_dir, target=args.target, tag=tag,
                     work_dir=args.work_dir, health_file=args.health_file,
                     backup_dir=args.backup_dir, publish=args.publish,
                     status_path=args.status_file, planned_date=args.planned_date,
                     proxy=proxy, metadata_timeout=args.metadata_timeout,
                     archive_timeout=args.archive_timeout,
                     wait_until=args.wait_until, poll_seconds=args.poll_seconds,
                     keep_work=args.keep_work)
    except BlockingIOError:
        print(json.dumps({"status": "locked", "target": args.target}, sort_keys=True))
        return 75
    except release_package.ReleaseError as exc:
        print(json.dumps({"status": "failed", "target": args.target,
                          "error": _safe_error(exc)}, sort_keys=True))
        return 3 if str(exc) == "release_not_published" else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "target": args.target,
                          "error": _safe_error(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
