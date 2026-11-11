#!/usr/bin/env python3
"""Bounded trigger for the QLib GitHub release fallback.

The nightly phase runs only after the SQL updater exits non-zero. It accepts a
fresh terminal status record from that exact run, resolves the target from the
failure's own plan (``targetDate`` or ``plannedDate``), persists it, and makes
one immediate fallback attempt. The morning phase reuses that persisted target
only while the same failure ``runId`` is still the current terminal record.

The fallback itself re-checks the status file and requires a matching terminal
SQL failure before it may publish. This module never edits the SQL failure
status: only a successful fallback does, via ``release_append``.

No URL, response body or subprocess stderr is logged. Failures stay in fixed
machine codes and are recorded in the trigger state and the fallback attempt
journal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SHANGHAI = ZoneInfo("Asia/Shanghai")
PRODUCTION_STATUS = Path("/home/ubuntu/qlib/logs/update-status.json")
PRODUCTION_STATE = Path("/home/ubuntu/qlib/logs/release-fallback-state.json")
PRODUCTION_DATA = Path("/home/ubuntu/qlib/data")
CALENDAR_ROOT = Path("/home/ubuntu/AStock_AI_System")
RESEARCH_DATA = Path("/home/ubuntu/research-data")
BACKUP_ROOT = Path("/home/ubuntu/qlib/backups/release-fallback")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
RUN_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
SAFE_CODE = re.compile(r"[A-Za-z0-9_:.,+\-]{1,160}\Z")
FALLBACK_TIMEOUT = 1500
MAX_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _parse_instant(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value


def _safe_error(exc: BaseException) -> str:
    text = str(exc)
    if SAFE_CODE.fullmatch(text):
        return text
    return type(exc).__name__


def load_terminal_status(path: Path) -> dict | None:
    """Return a production terminal failure bound to run identity, or None."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        return None
    if document.get("status") not in ("failed", "interrupted"):
        return None
    if document.get("mode") != "production":
        return None
    try:
        if Path(document.get("dataRoot", "")).resolve() != PRODUCTION_DATA.resolve():
            return None
    except (OSError, TypeError, ValueError):
        return None
    planned = document.get("plannedDate")
    if not isinstance(planned, str) or DATE_RE.fullmatch(planned) is None:
        return None
    run_id = document.get("runId")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        return None
    if document.get("generation") != run_id:
        return None
    when = _parse_instant(document.get("time"))
    if when is None:
        return None
    if when.astimezone(SHANGHAI).date().isoformat() != planned:
        return None
    result = dict(document)
    result["_when"] = when
    return result


def target_from_status(document: dict) -> str | None:
    """Use targetDate when a future run.py records it; otherwise the run day."""
    target = document.get("targetDate")
    if target is None:
        target = document.get("plannedDate")
    if not isinstance(target, str) or DATE_RE.fullmatch(target) is None:
        return None
    if target > document["plannedDate"]:
        return None
    return target


def is_fresh(document: dict, started_at: datetime | None) -> bool:
    if started_at is None:
        return False
    return document["_when"] >= started_at - timedelta(seconds=5)


def production_calendar(now: datetime):
    """Best-effort open/closed check; never a precondition for a write."""
    try:
        if str(RESEARCH_DATA) not in sys.path:
            sys.path.insert(0, str(RESEARCH_DATA))
        import calendar_evidence
        evidence = calendar_evidence.load_calendar_evidence(str(CALENDAR_ROOT), now)
        document = evidence["document"]
        return document.get("status"), document.get("targetDate"), document.get("previousTradingDate")
    except Exception:
        return None, None, None


def load_state(path: Path) -> dict | None:
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        return None
    return document


def write_state(path: Path, document: dict) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ValueError("state_path_symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _attempt(document: dict, phase: str, result: str, rc: int | None,
             error: str | None, now: datetime) -> dict:
    attempts = list(document.get("attempts") or [])[-MAX_ATTEMPTS + 1:]
    attempts.append({"phase": phase, "time": now.isoformat(), "result": result,
                     "exitCode": rc, "error": error})
    document["attempts"] = attempts
    document["updatedAt"] = now.isoformat()
    return document


def _classify(rc: int, result: dict) -> str:
    if rc == 0:
        status = result.get("status")
        if status == "published":
            return "completed"
        if status == "already_current":
            return "already_current"
        return "failed"
    if rc == 75:
        return "locked"
    if rc == 3 and result.get("error") == "release_not_published":
        return "not_published"
    return "failed"


def _invoke(target: str) -> tuple[int, dict]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / f"{target}-{stamp}"
    command = [
        sys.executable, "-B", str(HERE / "release_update.py"),
        "--target", target, "--publish",
        "--backup-dir", str(backup),
        "--metadata-timeout", "25", "--archive-timeout", "1200",
    ]
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=FALLBACK_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 124, {"status": "failed", "error": "fallback_process_timeout"}
    except OSError:
        return 127, {"status": "failed", "error": "fallback_process_failed"}
    lines = [line for line in completed.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return completed.returncode, {"status": "failed", "error": "fallback_output_missing"}
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return completed.returncode, {"status": "failed", "error": "fallback_output_invalid"}
    if not isinstance(result, dict):
        return completed.returncode, {"status": "failed", "error": "fallback_output_invalid"}
    return completed.returncode, result


def nightly(*, now: datetime, run_started_at: datetime | None, run_rc: int,
            status_path: Path = PRODUCTION_STATUS, state_path: Path = PRODUCTION_STATE,
            calendar_lookup=production_calendar, invoke=_invoke) -> dict:
    """One immediate fallback attempt after the nightly SQL run failed."""
    base = {"event": "release_fallback_trigger", "phase": "nightly", "time": now.isoformat()}
    if run_rc == 0:
        return {**base, "result": "skipped", "reason": "sql_run_succeeded"}
    if run_rc == 75:
        return {**base, "result": "skipped", "reason": "update_lock_busy"}
    document = load_terminal_status(status_path)
    if document is None:
        return {**base, "result": "skipped", "reason": "no_terminal_failure"}
    if not is_fresh(document, run_started_at):
        return {**base, "result": "skipped", "reason": "stale_terminal_failure"}
    target = target_from_status(document)
    if target is None:
        return {**base, "result": "skipped", "reason": "target_invalid"}
    if target != document["plannedDate"]:
        # run.py records failures before targetDate exists; without a calendar
        # mapping we cannot prove a non-trading-day target. Stay fail-closed.
        return {**base, "result": "skipped", "reason": "target_requires_calendar_mapping"}
    calendar_status, calendar_target, _ = calendar_lookup(now)
    if calendar_status == "closed":
        return {**base, "result": "skipped", "reason": "non_trading_day"}
    if calendar_status == "open" and calendar_target != target:
        return {**base, "result": "skipped", "reason": "calendar_target_mismatch"}

    state = {
        "schemaVersion": 1, "phase": "nightly", "status": "pending",
        "target": target, "plannedDate": document["plannedDate"],
        "sourceFailureRunId": document["runId"],
        "sourceFailureStatus": document["status"],
        "sourceFailureTime": document.get("time"),
        "createdAt": now.isoformat(), "updatedAt": now.isoformat(), "attempts": [],
    }
    write_state(state_path, state)
    try:
        rc, result = invoke(target)
        outcome = _classify(rc, result)
    except Exception as exc:  # trigger-side failure, never a status write
        rc, result, outcome = None, {}, "failed"
        state["error"] = _safe_error(exc)

    state["status"] = {
        "completed": "completed", "already_current": "completed",
        "not_published": "pending_retry", "locked": "pending_retry",
    }.get(outcome, "failed")
    _attempt(state, "nightly", outcome, rc, result.get("error") if isinstance(result.get("error"), str) else None, now)
    write_state(state_path, state)
    return {**base, "result": outcome, "target": target,
            "exitCode": rc, "error": result.get("error") if outcome == "failed" else None,
            "state": state["status"]}


def morning(*, now: datetime, status_path: Path = PRODUCTION_STATUS,
            state_path: Path = PRODUCTION_STATE, invoke=_invoke) -> dict:
    """One retry for a target persisted by the nightly trigger."""
    base = {"event": "release_fallback_trigger", "phase": "morning", "time": now.isoformat()}
    state = load_state(state_path)
    if state is None:
        return {**base, "result": "skipped", "reason": "no_state"}
    if state.get("status") != "pending_retry":
        return {**base, "result": "skipped", "reason": "state_not_pending",
                "state": state.get("status")}
    document = load_terminal_status(status_path)
    if document is None:
        state["status"] = "stale"
        _attempt(state, "morning", "stale", None, "status_not_terminal", now)
        write_state(state_path, state)
        return {**base, "result": "skipped", "reason": "status_not_terminal"}
    target = state.get("target")
    if (document["runId"] != state.get("sourceFailureRunId")
            or document["plannedDate"] != state.get("plannedDate")
            or target_from_status(document) != target):
        state["status"] = "stale"
        _attempt(state, "morning", "stale", None, "failure_identity_changed", now)
        write_state(state_path, state)
        return {**base, "result": "skipped", "reason": "failure_identity_changed"}

    try:
        rc, result = invoke(target)
        outcome = _classify(rc, result)
    except Exception as exc:
        rc, result, outcome = None, {}, "failed"
        state["error"] = _safe_error(exc)

    if outcome == "not_published":
        state["status"] = "expired"
    elif outcome in ("completed", "already_current"):
        state["status"] = "completed"
    else:
        state["status"] = "failed"
    _attempt(state, "morning", outcome, rc, result.get("error") if isinstance(result.get("error"), str) else None, now)
    write_state(state_path, state)
    return {**base, "result": outcome, "target": target,
            "exitCode": rc, "error": result.get("error") if outcome == "failed" else None,
            "state": state["status"]}


def inspect(*, status_path: Path = PRODUCTION_STATUS,
            state_path: Path = PRODUCTION_STATE) -> dict:
    """Read-only self-check used by package verification."""
    document = load_terminal_status(status_path)
    state = load_state(state_path)
    return {
        "event": "release_fallback_inspect",
        "status": "terminal" if document else "not_terminal_or_invalid",
        "runId": document.get("runId") if document else None,
        "plannedDate": document.get("plannedDate") if document else None,
        "targetDate": document.get("targetDate") if document else None,
        "stateStatus": state.get("status") if state else None,
        "stateTarget": state.get("target") if state else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("nightly", "morning", "inspect"), required=True)
    parser.add_argument("--run-rc", type=int, default=0)
    parser.add_argument("--run-started-at")
    parser.add_argument("--status-file", type=Path, default=PRODUCTION_STATUS)
    parser.add_argument("--state-file", type=Path, default=PRODUCTION_STATE)
    args = parser.parse_args()
    now = _now()
    if args.phase == "inspect":
        result = inspect(status_path=args.status_file, state_path=args.state_file)
    elif args.phase == "nightly":
        started = _parse_instant(args.run_started_at)
        result = nightly(now=now, run_started_at=started, run_rc=args.run_rc,
                         status_path=args.status_file, state_path=args.state_file)
    else:
        result = morning(now=now, status_path=args.status_file, state_path=args.state_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
