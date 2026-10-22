#!/usr/bin/env python3
"""Versioned reduced-scope archive. Never writes the legacy full snapshot table."""
import argparse
import json
import contextlib
import datetime as dt
import os
import re
from pathlib import Path
import sys
import time
import uuid

import snapshot_runtime as runtime
from qlib_summary import qlib_market
from core_storage import CORE_DB, read_core, store_core
from core_contract import (SCHEMA, CORE_SOURCES, APPENDIX_SOURCES, encode, digest,
                           date as _date, instant as _instant, validate_core, validate_calendar_evidence)
import snapshot_contract
CORE_STATE = Path.home() / ".local/state/research-data/core-market"


def build_core(qlib, *, run_id, generated_at, calendar_evidence):
    # Deep-copy through canonical JSON: later collector mutations cannot alter the record.
    record = runtime._strict_json(encode({
        "schema": SCHEMA, "scope": "core_market", "fullSnapshot": False,
        "artifactKind": "qlib_market_summary", "includesCompletePriceArchive": False,
        "date": qlib["date"], "generatedAt": generated_at, "runId": run_id,
        "requiredSources": CORE_SOURCES,
        "appendix": {"status": "not_requested", "sources": APPENDIX_SOURCES},
        "qlib": qlib,
        "calendarEvidence": calendar_evidence,
    }))
    return validate_core(record)


def load_calendar_evidence(path, now):
    from calendar_evidence import load_calendar_evidence as read_calendar
    try:
        return read_calendar(path, now)
    except (ValueError, OSError, TypeError, KeyError):
        raise runtime.SnapshotDeferredError("independent trading calendar evidence invalid or stale") from None



PRODUCTION_QLIB_DATA = Path("/home/ubuntu/qlib/data")
PRODUCTION_QLIB_STATUS = Path("/home/ubuntu/qlib/logs/update-status.json")
_ZONED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def qlib_status_evidence(expected, now=None):
    """Distinguish a missing status file from an unreadable or unbound one.

    Only a production-mode record for this planned day, with a zoned time and
    run identity, can stop the snapshot. A test or incomplete record cannot.
    """
    path = PRODUCTION_QLIB_STATUS
    if not path.exists():
        return {"state": "missing"}
    try:
        if path.is_symlink() or not path.is_file():
            return {"state": "unreadable", "reason": "status_not_regular_file"}
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError):
        return {"state": "unreadable", "reason": "status_unreadable"}
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        return {"state": "unreadable", "reason": "status_schema_invalid"}
    if document.get("status") not in ("failed", "interrupted"):
        return {"state": "not_terminal", "status": document.get("status")}
    if document.get("mode") != "production":
        return {"state": "rejected", "reason": "status_not_production"}
    try:
        if Path(document.get("dataRoot", "")).resolve() != PRODUCTION_QLIB_DATA.resolve():
            return {"state": "rejected", "reason": "status_data_root_mismatch"}
    except (OSError, TypeError, ValueError):
        return {"state": "rejected", "reason": "status_data_root_mismatch"}
    if document.get("plannedDate") != expected:
        return {"state": "rejected", "reason": "status_planned_date_mismatch"}
    when = document.get("time")
    if not isinstance(when, str) or _ZONED.fullmatch(when) is None:
        return {"state": "rejected", "reason": "status_time_invalid"}
    parsed = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return {"state": "rejected", "reason": "status_time_invalid"}
    if parsed.astimezone(snapshot_contract.CST).date().isoformat() != expected:
        return {"state": "rejected", "reason": "status_time_day_mismatch"}
    current = now or dt.datetime.now(snapshot_contract.CST)
    if parsed > current.astimezone(parsed.tzinfo) + dt.timedelta(minutes=5):
        return {"state": "rejected", "reason": "status_time_in_future"}
    run_id = document.get("runId")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None or document.get("generation") != run_id:
        return {"state": "rejected", "reason": "status_generation_invalid"}
    if document.get("targetDate") not in (None, expected):
        return {"state": "rejected", "reason": "status_target_mismatch"}
    error = document.get("error") if isinstance(document.get("error"), str) and document.get("error") else document.get("status")
    if not isinstance(error, str) or len(error) > 300:
        error = "qlib_update_failed"
    return {"state": "failed", "error": "qlib_update_failed:" + error, "runId": run_id,
            "failureStage": document.get("failureStage"), "status": document.get("status")}


def run_core(*, db_path=CORE_DB, run_id=None, now=None, effective_trade_date=None,
             calendar_evidence=None):
    """Qlib-only summary, not a complete price archive; no live source calls."""
    run_id = run_id or uuid.uuid4().hex
    generated_at = now or dt.datetime.now().astimezone().isoformat()
    expected_run_day = os.environ.get("RESEARCH_DATA_EXPECTED_RUN_DAY")
    if (runtime._supervised_run_id() is not None and expected_run_day is not None
            and _instant(generated_at).astimezone(snapshot_contract.CST).date().isoformat() != expected_run_day):
        raise runtime.SnapshotDeferredError("core_supervised_day_rolled_over", target=expected_run_day)
    runtime._supervised_soft_deadline("core Qlib scan")
    try:
        decision = validate_calendar_evidence(calendar_evidence, _instant(generated_at))
    except (ValueError, TypeError, KeyError):
        raise runtime.SnapshotDeferredError("independent trading calendar evidence invalid or stale") from None
    expected = decision["targetDate"]
    retry_day = os.environ.get("RESEARCH_DATA_RETRY_CORE_DAY")
    retry_calendar = os.environ.get("RESEARCH_DATA_RETRY_CORE_CALENDAR_SHA256")
    if retry_day is not None or retry_calendar is not None:
        if retry_day != expected or retry_calendar != calendar_evidence["sha256"]:
            raise runtime.SnapshotDeferredError("core_retry_calendar_missing_or_changed", target=expected)
    if decision["status"] == "closed":
        return {"status": "skipped_non_trading", "target": decision["previousTradingDate"],
                "calendarEvidence": calendar_evidence}
    if effective_trade_date is not None and expected < _date(effective_trade_date):
        raise runtime.SnapshotDeferredError("core target predates effective date", target=expected)
    runtime.write_run_checkpoint(runtime._supervised_run_id(), target=expected,
                                 schema=SCHEMA, activePhase="qlib_readiness")
    evidence = qlib_status_evidence(expected, now=_instant(generated_at))
    if evidence.get("state") == "failed":
        exc = runtime.SnapshotDeferredError(evidence["error"], target=expected, source_failure={
            "provider": "qlib", "runId": evidence["runId"], "status": evidence["status"],
            "failureStage": evidence.get("failureStage")})
        exc.calendar_evidence = calendar_evidence
        raise exc
    q = qlib_market(None)
    if q["date"] != expected:
        exc = runtime.SnapshotDeferredError(
            "core_qlib_not_current_for_expected_trading_day" if q["date"] < expected else "core_qlib_future_target",
            target=expected, qlib_tag=q["generation"]["tag"])
        exc.calendar_evidence = calendar_evidence
        raise exc
    prior = read_core(q["date"], db_path)
    if prior is not None:
        if digest(prior["qlib"]) != digest(q):
            raise runtime.SnapshotDeferredError("same-day Qlib content differs from immutable core archive",
                                               target=q["date"], qlib_tag=q["generation"]["tag"])
        runtime.write_run_checkpoint(runtime._supervised_run_id(), target=q["date"],
                                     preexistingRowSha256=digest(prior), schema=SCHEMA)
        return {"status": "skipped_existing", "target": q["date"], "payloadSha256": digest(prior),
                "calendarEvidence": calendar_evidence}
    record = build_core(q, run_id=run_id, generated_at=generated_at, calendar_evidence=calendar_evidence)
    return {**store_core(record, db_path), "target": q["date"], "coverage": q["coverage"],
            "qlibTag": q["generation"]["tag"], "calendarEvidence": calendar_evidence}


def main(argv=None, *, _cli=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=CORE_DB)
    parser.add_argument("--state-dir", type=Path, default=CORE_STATE)
    parser.add_argument("--contract-policy", type=Path)
    parser.add_argument("--calendar-evidence", type=Path)
    args = parser.parse_args(argv)  # no historical-date or legacy-contract mode
    if _cli and args.contract_policy is None:
        parser.error("core CLI requires an explicit contract policy")
    supervised = runtime._supervised_run_id()
    full_state = os.environ.get("RESEARCH_DATA_FULL_STATE_DIR", runtime.FULL_STATE_DIR)
    if not full_state or args.state_dir.resolve() == Path(full_state).resolve():
        parser.error("core status must not overwrite full snapshot last-run")
    if _cli and runtime._supervised_run_id() is None:
        from snapshot_supervisor import run_supervised
        record, code = run_supervised(worker_path=str(Path(__file__).resolve()),
                                     worker_args=list(argv if argv is not None else sys.argv[1:]),
                                     state_dir=str(args.state_dir), db_path=str(args.db),
                                     contract=SCHEMA)
        print(encode(record))
        return code
    if supervised is not None:
        runtime._await_supervisor_gate()
        runtime._verify_supervisor_lock(str(args.state_dir))
    run_id = supervised or uuid.uuid4().hex
    started = time.monotonic()
    with contextlib.nullcontext() if supervised is not None else runtime.snapshot_run_lock(str(args.state_dir)):
        try:
            effective = None
            if args.contract_policy is not None:
                policy = snapshot_contract.read_policy(args.contract_policy)
                snapshot_contract.assert_core_history(policy, args.db)
                if snapshot_contract.select(policy, dt.datetime.now(snapshot_contract.CST)) != SCHEMA:
                    raise ValueError("core contract is not active")
                effective = snapshot_contract.first_core_date(policy)
            now = dt.datetime.now(snapshot_contract.CST)
            evidence = load_calendar_evidence(args.calendar_evidence, now)
            result = run_core(db_path=args.db, run_id=run_id, now=now.isoformat(),
                              effective_trade_date=effective, calendar_evidence=evidence)
            code = 0
        except runtime.SnapshotDeferredError as exc:
            result = {"status": "deferred", "target": exc.target, "sourceFailure": exc.source_failure, "errorType": type(exc).__name__,
                      "reason": str(exc), "qlibTag": exc.qlib_tag,
                      "calendarEvidence": getattr(exc, "calendar_evidence", None)}
            code = runtime.SNAPSHOT_DEFERRED_EXIT
        except Exception as exc:
            result = {"status": "rejected", "errorType": type(exc).__name__}
            code = 1
        result.update(schema=SCHEMA, scope="core_market", fullSnapshot=False, runId=run_id,
                      timestamp=dt.datetime.now().astimezone().isoformat(), durationSeconds=round(time.monotonic() - started, 3))
        runtime.write_last_run(result, str(args.state_dir))
        print(encode(result))
    return code


if __name__ == "__main__":
    sys.exit(main(_cli=True))
