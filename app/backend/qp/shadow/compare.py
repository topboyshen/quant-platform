"""M4 shadow comparator: legacy export vs qp, matrix C1-C10.

Pure read-only on both sides.  The legacy side is the per-date export written by
``qp.collect.signals`` (manifest checked before any comparison); the qp side reads
the ``signals``/``notify`` schemas through a SQLAlchemy session.  No writes except
the optional JSON/Markdown report under ``--out``.

Verdict rules from M4-SHADOW-CONTRACT §5/§6.2:

* any hard C1-C6 mismatch, C7 missing/contradictory notification, C8/C9 mismatch or
  C10 violation -> ``FAIL``;
* missing/corrupt export or a qp side that has not run yet -> ``BLOCKED``;
* source/text-only divergence -> ``WARN`` (does not block acceptance);
* ``gate_ok`` is true for ``PASS`` and ``WARN``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.collect.signals import EXPORTER, FILE_NAMES
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.signals import (
    ApprovedIntake,
    DecisionOutcome,
    EodRun,
    ExecutionDigest,
    MacroSlice,
    RiskSnapshot,
    TargetDecision,
    TrendSignal,
)

UTC = dt.UTC
STATUS_ORDER = {"PASS": 0, "WARN": 1, "BLOCKED": 2, "FAIL": 3}
CST = dt.timezone(dt.timedelta(hours=8))


# --------------------------------------------------------------------------- helpers
def _check(
    cid: str, status: str, summary: str, *, diffs: Iterable[str] = (), details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": cid,
        "status": status,
        "summary": summary,
        "diffs": [str(item) for item in diffs],
        "details": details or {},
    }


def _blocked(cid: str, reason: str) -> dict[str, Any]:
    return _check(cid, "BLOCKED", reason)


def _worst(checks: list[dict[str, Any]]) -> str:
    return max((check["status"] for check in checks), key=lambda status: STATUS_ORDER.get(status, 3), default="PASS")


def _date_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)[:10]


def _parse_dt(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num_eq(a: Any, b: Any, tol: float) -> bool:
    left, right = _num(a), _num(b)
    if left is None or right is None:
        return left is None and right is None
    return abs(left - right) <= tol


def _cmp_num(path: str, legacy: Any, qp: Any, tol: float, diffs: list[str]) -> None:
    left, right = _num(legacy), _num(qp)
    if left is None and right is None:
        return
    if left is None or right is None:
        diffs.append(f"{path}: legacy={legacy!r} qp={qp!r}")
        return
    if abs(left - right) > tol:
        diffs.append(f"{path}: legacy={left!r} qp={right!r} tol={tol}")


def _cmp_exact(path: str, legacy: Any, qp: Any, diffs: list[str]) -> None:
    if legacy != qp:
        diffs.append(f"{path}: legacy={legacy!r} qp={qp!r}")


def _norm_level(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _signal_types(rows: list[Any]) -> set[str]:
    kinds: set[str] = set()
    for row in rows or []:
        if isinstance(row, dict) and row.get("signal_type") is not None:
            kinds.add(str(row["signal_type"]))
    return kinds


def _source_tokens(value: Any) -> set[str]:
    return {token for token in str(value or "").split("+") if token and token != "none"}


def _load_export(day: str, legacy: str | os.PathLike[str]) -> dict[str, Any]:
    base = Path(legacy)
    date_dir = base / day if (base / day).is_dir() else base
    out: dict[str, Any] = {"dir": str(date_dir), "valid": False, "errors": [], "manifest": None, "files": {}}
    manifest_path = date_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["errors"].append(f"manifest_unreadable:{type(exc).__name__}")
        return out
    out["manifest"] = manifest
    if str(manifest.get("decision_date")) != day:
        out["errors"].append(f"manifest_date:{manifest.get('decision_date')!r}")
    if manifest.get("exporter") != EXPORTER:
        out["errors"].append(f"exporter:{manifest.get('exporter')!r}")
    if manifest.get("ok") is not True:
        out["errors"].append(f"manifest_not_ok:{manifest.get('missing')!r}")
    declared = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    files: dict[str, Any] = {}
    for name in FILE_NAMES:
        meta = declared.get(name)
        path = date_dir / name
        if not isinstance(meta, dict):
            out["errors"].append(f"missing_declaration:{name}")
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            out["errors"].append(f"missing_file:{name}")
            continue
        if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
            out["errors"].append(f"sha256:{name}")
            continue
        if len(raw) != meta.get("bytes"):
            out["errors"].append(f"bytes:{name}")
            continue
        try:
            files[name] = json.loads(raw)
        except ValueError:
            out["errors"].append(f"json:{name}")
    out["files"] = files
    out["valid"] = not out["errors"]
    return out


def _qp_run(session: Session, day: dt.date) -> EodRun | None:
    return (
        session.execute(
            select(EodRun).where(EodRun.decision_date == day).order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())
        )
        .scalars()
        .first()
    )


def _latest_legacy_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not runs:
        return None
    return max(runs, key=lambda row: (int(row.get("id") or 0), str(row.get("run_id") or "")))


# --------------------------------------------------------------------------- C1
def _compare_run(legacy_run: dict[str, Any] | None, qp_run: EodRun | None, attempts_count: int = 1) -> dict[str, Any]:
    if legacy_run is None and qp_run is None:
        return _blocked("C1", "both sides have no EOD run")
    if legacy_run is None or qp_run is None:
        return _blocked("C1", f"run missing: legacy={legacy_run is not None} qp={qp_run is not None}")
    diffs: list[str] = []
    _cmp_exact("status", legacy_run.get("status"), qp_run.status, diffs)
    _cmp_exact("outcome", legacy_run.get("decision_outcome"), qp_run.decision_outcome, diffs)
    for key, attr in (
        ("target_count", "target_count"),
        ("evaluated_count", "evaluated_count"),
        ("failed_count", "failed_count"),
        ("signal_count", "signal_count"),
        ("allowed_signal_count", "allowed_signal_count"),
        ("blocked_signal_count", "blocked_signal_count"),
    ):
        _cmp_exact(key, legacy_run.get(key), getattr(qp_run, attr), diffs)
    legacy_reasons = set(str(item) for item in (legacy_run.get("reason_codes") or []))
    qp_reasons = set(str(item) for item in (qp_run.reason_codes or []))
    if legacy_reasons != qp_reasons:
        diffs.append(f"reason_codes: legacy={sorted(legacy_reasons)} qp={sorted(qp_reasons)}")
    details = {
        "legacy_run_id": legacy_run.get("run_id"),
        "qp_run_id": qp_run.run_id,
        "legacy_attempts": attempts_count,
        "qp_attempt": qp_run.attempt_no,
        "counts": {
            key: {"legacy": legacy_run.get(key), "qp": getattr(qp_run, attr)}
            for key, attr in (
                ("target_count", "target_count"),
                ("evaluated_count", "evaluated_count"),
                ("failed_count", "failed_count"),
                ("signal_count", "signal_count"),
                ("allowed_signal_count", "allowed_signal_count"),
                ("blocked_signal_count", "blocked_signal_count"),
            )
        },
    }
    return _check(
        "C1",
        "FAIL" if diffs else "PASS",
        "latest attempt terminal fields" + ("" if not diffs else " differ"),
        diffs=diffs,
        details=details,
    )


# --------------------------------------------------------------------------- C2
def _compare_decisions(
    session: Session, day: dt.date, qp_run: EodRun | None, legacy_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if qp_run is None:
        return _blocked("C2", "qp EOD run missing")
    qp_rows = (
        session.execute(
            select(TargetDecision).where(TargetDecision.run_id == qp_run.run_id).order_by(TargetDecision.code)
        )
        .scalars()
        .all()
    )
    legacy_by_code = {str(row.get("code")): row for row in legacy_rows}
    qp_by_code = {row.code: row for row in qp_rows}
    diffs: list[str] = []
    for code in sorted(set(legacy_by_code) - set(qp_by_code)):
        diffs.append(f"missing_in_qp:{code}")
    for code in sorted(set(qp_by_code) - set(legacy_by_code)):
        diffs.append(f"missing_in_legacy:{code}")
    for code in sorted(set(legacy_by_code) & set(qp_by_code)):
        left, right = legacy_by_code[code], qp_by_code[code]
        for field in ("data_status", "decision_state", "risk_action", "reason_code"):
            _cmp_exact(f"{code}.{field}", left.get(field), getattr(right, field), diffs)
        _cmp_exact(f"{code}.risk_level", _norm_level(left.get("risk_level")), _norm_level(right.risk_level), diffs)
        _cmp_num(f"{code}.reference_close", left.get("reference_close"), right.reference_close, 1e-4, diffs)
        legacy_types = _signal_types(left.get("raw_signals") or [])
        qp_types = _signal_types(right.raw_signals or [])
        if legacy_types != qp_types:
            diffs.append(f"{code}.raw_signal_types: legacy={sorted(legacy_types)} qp={sorted(qp_types)}")
        if right.validation_split != "live_oos":
            diffs.append(f"{code}.validation_split: qp={right.validation_split!r}")
        if left.get("validation_split") not in (None, "live_oos"):
            diffs.append(f"{code}.validation_split: legacy={left.get('validation_split')!r}")
    details = {"legacy_rows": len(legacy_rows), "qp_rows": len(qp_rows)}
    return _check(
        "C2",
        "FAIL" if diffs else "PASS",
        f"{len(legacy_by_code)} target decisions" + ("" if not diffs else " differ"),
        diffs=diffs,
        details=details,
    )


# --------------------------------------------------------------------------- C3
def _compare_risk(
    legacy_snapshots: list[dict[str, Any]], legacy_run_id: Any, session: Session, qp_run: EodRun | None
) -> dict[str, Any]:
    if qp_run is None:
        return _blocked("C3", "qp EOD run missing")
    legacy_snap = next((row for row in legacy_snapshots if str(row.get("run_id")) == str(legacy_run_id)), None)
    if legacy_snap is None and legacy_snapshots:
        legacy_snap = legacy_snapshots[-1]
    qp_snap = session.execute(select(RiskSnapshot).where(RiskSnapshot.run_id == qp_run.run_id)).scalars().first()
    if legacy_snap is None and qp_snap is None:
        return _check("C3", "PASS", "no risk snapshot on either side")
    if legacy_snap is None or qp_snap is None:
        return _check(
            "C3",
            "FAIL",
            "risk snapshot missing on one side",
            diffs=[f"legacy={legacy_snap is not None} qp={qp_snap is not None}"],
        )
    diffs: list[str] = []
    warnings: list[str] = []
    _cmp_exact("level", legacy_snap.get("level"), qp_snap.level, diffs)
    legacy_missing = set(str(item) for item in (legacy_snap.get("missing_fields") or []))
    qp_missing = set(str(item) for item in (qp_snap.missing_fields or []))
    if legacy_missing != qp_missing:
        diffs.append(f"missing_fields: legacy={sorted(legacy_missing)} qp={sorted(qp_missing)}")
    legacy_values = legacy_snap.get("values") or {}
    qp_values = qp_snap.values or {}
    legacy_sources = legacy_snap.get("sources") or {}
    qp_sources = qp_snap.sources or {}
    for key in ("vix", "us_10y", "crude_oil"):
        _cmp_num(f"values.{key}", legacy_values.get(key), qp_values.get(key), 0.01, diffs)
        if str(legacy_sources.get(key) or "") != str(qp_sources.get(key) or ""):
            warnings.append(f"source_divergence.{key}: legacy={legacy_sources.get(key)!r} qp={qp_sources.get(key)!r}")
    details = {
        "legacy_values": {key: legacy_values.get(key) for key in ("vix", "us_10y", "crude_oil")},
        "qp_values": {key: qp_values.get(key) for key in ("vix", "us_10y", "crude_oil")},
        "source_divergence": warnings,
    }
    status = "FAIL" if diffs else ("WARN" if warnings else "PASS")
    return _check(
        "C3", status, "risk snapshot" + ("" if not diffs else " differs"), diffs=diffs + warnings, details=details
    )


# --------------------------------------------------------------------------- C4
def _compare_signals(session: Session, day: dt.date, legacy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    qp_rows = (
        session.execute(
            select(TrendSignal)
            .where(TrendSignal.decision_date == day)
            .order_by(TrendSignal.code, TrendSignal.signal_type)
        )
        .scalars()
        .all()
    )
    legacy = {(str(row.get("code")), str(row.get("signal_type"))): row for row in legacy_rows}
    qp = {(row.code, row.signal_type): row for row in qp_rows}
    diffs: list[str] = []
    for key in sorted(set(legacy) - set(qp)):
        diffs.append(f"missing_in_qp:{key[0]}/{key[1]}")
    for key in sorted(set(qp) - set(legacy)):
        diffs.append(f"missing_in_legacy:{key[0]}/{key[1]}")
    for key in sorted(set(legacy) & set(qp)):
        left, right = legacy[key], qp[key]
        code = f"{key[0]}/{key[1]}"
        _cmp_exact(f"{code}.period", left.get("period"), right.period, diffs)
        _cmp_exact(f"{code}.data_date", _date_str(left.get("data_date")), _date_str(right.data_date), diffs)
        legacy_detail = left.get("detail") if isinstance(left.get("detail"), dict) else {}
        qp_detail = right.detail if isinstance(right.detail, dict) else {}
        for field in ("dif", "dea", "weekly_close"):
            if field not in legacy_detail and field not in qp_detail:
                continue
            _cmp_num(f"{code}.{field}", legacy_detail.get(field), qp_detail.get(field), 1e-4, diffs)
    details = {"legacy_rows": len(legacy_rows), "qp_rows": len(qp_rows)}
    return _check(
        "C4",
        "FAIL" if diffs else "PASS",
        f"{len(legacy)} trend signals" + ("" if not diffs else " differ"),
        diffs=diffs,
        details=details,
    )


# --------------------------------------------------------------------------- C5
def _compare_macro(legacy_macro: dict[str, Any] | None, session: Session, day: dt.date) -> dict[str, Any]:
    qp_macro = session.get(MacroSlice, day)
    if legacy_macro is None and qp_macro is None:
        return _check("C5", "PASS", "no macro slice on either side")
    if legacy_macro is None or qp_macro is None:
        return _check(
            "C5",
            "FAIL",
            "macro slice missing on one side",
            diffs=[f"legacy={legacy_macro is not None} qp={qp_macro is not None}"],
        )
    diffs: list[str] = []
    warnings: list[str] = []
    _cmp_num("total_amount", legacy_macro.get("total_amount"), qp_macro.total_amount, 0.01, diffs)
    _cmp_num("margin_balance", legacy_macro.get("margin_balance"), qp_macro.margin_balance, 0.01, diffs)
    _cmp_num("margin_growth_pct", legacy_macro.get("margin_growth_pct"), qp_macro.margin_growth_pct, 0.01, diffs)
    _cmp_exact("limit_up", legacy_macro.get("limit_up"), qp_macro.limit_up, diffs)
    _cmp_exact("limit_down", legacy_macro.get("limit_down"), qp_macro.limit_down, diffs)
    _cmp_num("limit_ratio", legacy_macro.get("limit_ratio"), qp_macro.limit_ratio, 0.01, diffs)
    legacy_sources = _source_tokens(legacy_macro.get("source"))
    qp_sources = _source_tokens(qp_macro.source)
    if legacy_sources != qp_sources:
        warnings.append(f"source_divergence: legacy={sorted(legacy_sources)} qp={sorted(qp_sources)}")
    details = {"legacy_source": legacy_macro.get("source"), "qp_source": qp_macro.source}
    status = "FAIL" if diffs else ("WARN" if warnings else "PASS")
    return _check(
        "C5", status, "macro slice" + ("" if not diffs else " differs"), diffs=diffs + warnings, details=details
    )


# --------------------------------------------------------------------------- C6
def _compare_outcomes(session: Session, qp_run: EodRun | None, legacy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if qp_run is None:
        return _blocked("C6", "qp EOD run missing")
    rows = session.execute(
        select(DecisionOutcome, TargetDecision.code)
        .join(TargetDecision, DecisionOutcome.decision_id == TargetDecision.id)
        .where(TargetDecision.run_id == qp_run.run_id)
        .order_by(TargetDecision.code, DecisionOutcome.horizon_sessions)
    ).all()
    qp = {(code, int(outcome.horizon_sessions)): outcome for outcome, code in rows}
    legacy = {(str(row.get("code")), int(row.get("horizon_sessions"))): row for row in legacy_rows}
    diffs: list[str] = []
    for key in sorted(set(legacy) - set(qp)):
        diffs.append(f"missing_in_qp:{key[0]}/{key[1]}")
    for key in sorted(set(qp) - set(legacy)):
        diffs.append(f"missing_in_legacy:{key[0]}/{key[1]}")
    for key in sorted(set(legacy) & set(qp)):
        left, right = legacy[key], qp[key]
        name = f"{key[0]}/{key[1]}"
        _cmp_exact(f"{name}.entry_date", _date_str(left.get("entry_date")), _date_str(right.entry_date), diffs)
        _cmp_exact(f"{name}.exit_date", _date_str(left.get("exit_date")), _date_str(right.exit_date), diffs)
        _cmp_exact(f"{name}.fill_status", left.get("fill_status"), right.fill_status, diffs)
        _cmp_num(f"{name}.entry_price", left.get("entry_fill"), right.entry_price, 1e-4, diffs)
        _cmp_num(f"{name}.exit_price", left.get("exit_fill"), right.exit_price, 1e-4, diffs)
        for field in ("net_return", "benchmark_return", "excess_return"):
            _cmp_num(f"{name}.{field}", left.get(field), getattr(right, field), 1e-6, diffs)
    details = {"legacy_rows": len(legacy_rows), "qp_rows": len(qp)}
    return _check(
        "C6",
        "FAIL" if diffs else "PASS",
        f"{len(legacy)} outcome rows" + ("" if not diffs else " differ"),
        diffs=diffs,
        details=details,
    )


# --------------------------------------------------------------------------- C7
_WATCHDOG_KINDS = ("failure", "daily")
_ATTEMPTS_RE = re.compile(r"attempts=(\d+)")


def _legacy_notification(notifications: dict[str, Any], namespace: str) -> dict[str, Any] | None:
    dispatch = [row for row in notifications.get("dispatch") or [] if str(row.get("namespace")) == namespace]
    if dispatch:
        return dispatch[-1]
    outbox = [
        row for row in notifications.get("outbox") or [] if str(row.get("idempotency_key") or "").startswith(namespace)
    ]
    return outbox[-1] if outbox else None


def _compare_notifications(session: Session, day: dt.date, export: dict[str, Any]) -> dict[str, Any]:
    notifications = export["files"].get("notifications.json") or {}
    diffs: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    seen = 0
    for kind in _WATCHDOG_KINDS:
        namespace = f"watchdog:{day.isoformat()}:{kind}"
        legacy_row = _legacy_notification(notifications, namespace)
        qp_row = session.execute(select(Outbox).where(Outbox.dedup_key == namespace)).scalars().first()
        details[kind] = {"legacy": legacy_row is not None, "qp": qp_row is not None, "namespace": namespace}
        if legacy_row is None and qp_row is None:
            continue
        seen += 1
        if legacy_row is None or qp_row is None:
            diffs.append(f"{kind}: existence legacy={legacy_row is not None} qp={qp_row is not None}")
            continue
        try:
            qp_payload = json.loads(qp_row.body)
        except ValueError:
            diffs.append(f"{kind}: qp outbox body is not JSON")
            continue
        legacy_payload = legacy_row.get("payload") if isinstance(legacy_row.get("payload"), dict) else {}
        text = str(legacy_payload.get("text") or legacy_payload.get("message") or legacy_payload.get("body") or "")
        if "report_type" in legacy_payload:
            _cmp_exact(f"{kind}.report_type", legacy_payload.get("report_type"), qp_payload.get("report_type"), diffs)
        if "terminal_ok" in legacy_payload:
            _cmp_exact(f"{kind}.terminal_ok", legacy_payload.get("terminal_ok"), qp_payload.get("terminal_ok"), diffs)
        elif kind == "failure":
            inferred = "终态检查失败" not in text if text else None
            if inferred is not None and bool(qp_payload.get("terminal_ok")) != inferred:
                diffs.append(f"{kind}.terminal_ok: inferred_legacy={inferred} qp={qp_payload.get('terminal_ok')!r}")
            elif inferred is None:
                warnings.append(f"{kind}: legacy payload has no structured terminal_ok/text")
        else:
            warnings.append(f"{kind}: legacy payload is text-only; terminal_ok not compared")
        match = _ATTEMPTS_RE.search(text)
        attempts = (
            (qp_payload.get("attempts") or {}).get("attempts") if isinstance(qp_payload.get("attempts"), dict) else None
        )
        if match and attempts is not None:
            if int(match.group(1)) != int(attempts):
                warnings.append(f"{kind}.attempts: legacy_text={match.group(1)} qp={attempts}")
        elif attempts is not None:
            warnings.append(f"{kind}: legacy text has no attempts= marker")
    if diffs:
        status = "FAIL"
    elif warnings:
        status = "WARN"
    else:
        status = "PASS"
    return _check(
        "C7",
        status,
        f"watchdog notifications: {seen} compared" + ("" if not diffs else " with contradictions"),
        diffs=diffs + warnings,
        details=details,
    )


# --------------------------------------------------------------------------- C8
def _compare_intake(session: Session, day: dt.date, legacy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC)
    end = start + dt.timedelta(days=1)
    qp_rows = (
        session.execute(
            select(ApprovedIntake)
            .where(ApprovedIntake.received_at >= start, ApprovedIntake.received_at < end)
            .order_by(ApprovedIntake.id)
        )
        .scalars()
        .all()
    )
    legacy = {str(row.get("idempotency_key")): row for row in legacy_rows}
    qp = {row.idempotency_key: row for row in qp_rows}
    diffs: list[str] = []
    for key in sorted(set(legacy) - set(qp)):
        diffs.append(f"missing_in_qp:{key}")
    for key in sorted(set(qp) - set(legacy)):
        diffs.append(f"missing_in_legacy:{key}")
    for key in sorted(set(legacy) & set(qp)):
        left, right = legacy[key], qp[key]
        _cmp_exact(f"{key}.status", left.get("status"), right.status, diffs)
        left_risk = left.get("risk_check") if isinstance(left.get("risk_check"), dict) else {}
        qp_risk = right.risk_check if isinstance(right.risk_check, dict) else {}
        _cmp_exact(f"{key}.risk_check.passed", left_risk.get("passed"), qp_risk.get("passed"), diffs)
        _cmp_exact(f"{key}.detail", left.get("detail"), right.detail, diffs)
        _cmp_exact(f"{key}.payload_sha256", left.get("payload_sha256"), right.payload_sha256, diffs)
        legacy_at = _parse_dt(left.get("received_at"))
        qp_at = _parse_dt(right.received_at)
        if legacy_at is None or qp_at is None:
            diffs.append(f"{key}.received_at: unparsable legacy={left.get('received_at')!r} qp={right.received_at!r}")
        elif abs((legacy_at - qp_at).total_seconds()) > 5:
            diffs.append(f"{key}.received_at: legacy={legacy_at.isoformat()} qp={qp_at.isoformat()} (>5s)")
    details = {"legacy_rows": len(legacy), "qp_rows": len(qp)}
    return _check(
        "C8",
        "FAIL" if diffs else "PASS",
        f"{len(legacy)} intake rows" + ("" if not diffs else " differ"),
        diffs=diffs,
        details=details,
    )


# --------------------------------------------------------------------------- C9
def _normalized_digest(body: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(body, ensure_ascii=False))
    normalized.pop("generated_at", None)
    return normalized


def _hmac_sorted(body: dict[str, Any], secret: bytes) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256=" + hmac.new(secret, b"0." + raw, hashlib.sha256).hexdigest()


def _compare_digest(
    session: Session, day: dt.date, legacy: dict[str, Any] | None, *, hmac_secret: bytes | None
) -> dict[str, Any]:
    qp_row = session.get(ExecutionDigest, day)
    qp_body = qp_row.body if qp_row is not None and isinstance(qp_row.body, dict) else None
    if legacy is None and qp_body is None:
        return _check("C9", "WARN", "no execution digest on either side")
    if legacy is None or qp_body is None:
        return _blocked("C9", f"digest missing: legacy={legacy is not None} qp={qp_body is not None}")
    diffs: list[str] = []
    warnings: list[str] = []
    _cmp_exact("digest_date", legacy.get("as_of_day"), _date_str(qp_row.digest_date), diffs)
    legacy_items = legacy.get("items") if isinstance(legacy.get("items"), list) else []
    qp_items = qp_body.get("items") if isinstance(qp_body.get("items"), list) else []
    legacy_keys = {str(item.get("idempotency_key")) for item in legacy_items if isinstance(item, dict)}
    qp_keys = {str(item.get("idempotency_key")) for item in qp_items if isinstance(item, dict)}
    if legacy_keys != qp_keys:
        diffs.append(f"items: legacy={sorted(legacy_keys)} qp={sorted(qp_keys)}")
    _cmp_exact("item_count", len(legacy_items), qp_row.item_count, diffs)
    legacy_summary = legacy.get("summary") if isinstance(legacy.get("summary"), dict) else {}
    _cmp_exact("accepted", legacy_summary.get("accepted"), qp_row.accepted, diffs)
    _cmp_exact("rejected", legacy_summary.get("rejected"), qp_row.rejected, diffs)
    legacy_norm = _normalized_digest(legacy)
    qp_norm = _normalized_digest(qp_body)
    body_diff = legacy_norm != qp_norm
    if body_diff:
        keys = sorted(set(legacy_norm) | set(qp_norm))
        changed = [key for key in keys if legacy_norm.get(key) != qp_norm.get(key)]
        diffs.append(f"body_normalized_diff:{changed}")
    elif legacy != qp_body:
        warnings.append("generated_at differs; all other body fields equal")
    if hmac_secret is None:
        warnings.append("hmac_secret_not_provided")
    elif not body_diff:
        # JSONB does not preserve insertion order, so use a sorted canonical form
        # for the cross-side HMAC check (the production signer keeps legacy order).
        legacy_sig = _hmac_sorted(legacy_norm, hmac_secret)
        qp_sig = _hmac_sorted(qp_norm, hmac_secret)
        if legacy_sig != qp_sig:
            diffs.append("hmac_normalized_body_mismatch")
    status = "FAIL" if diffs else ("WARN" if warnings else "PASS")
    return _check(
        "C9",
        status,
        "execution digest" + ("" if not diffs else " differs"),
        diffs=diffs + warnings,
        details={"legacy_items": len(legacy_items), "qp_items": len(qp_items)},
    )


# --------------------------------------------------------------------------- C10
def _compare_safety(session: Session, day: dt.date, export: dict[str, Any]) -> dict[str, Any]:
    pattern = f"%{day.isoformat()}%"
    outbox_rows = session.execute(select(Outbox).where(Outbox.dedup_key.like(pattern))).scalars().all()
    violations: list[str] = []
    for row in outbox_rows:
        if row.mode != "shadow":
            violations.append(f"outbox_mode:{row.dedup_key}={row.mode}")
        if row.status == "sent":
            violations.append(f"outbox_status_sent:{row.dedup_key}")
    attempts = (
        session.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .join(Outbox, DeliveryAttempt.outbox_id == Outbox.id)
            .where(Outbox.dedup_key.like(pattern))
        )
        or 0
    )
    if attempts:
        violations.append(f"delivery_attempt_rows:{attempts}")
    digest_row = session.get(ExecutionDigest, day)
    if digest_row is not None:
        if digest_row.push_state != "shadowed":
            violations.append(f"digest_push_state:{digest_row.push_state}")
        if digest_row.pushed_at is not None:
            violations.append("digest_pushed_at_not_null")
    manifest = export.get("manifest") if isinstance(export.get("manifest"), dict) else {}
    exporter = str(manifest.get("exporter") or "")
    if not exporter.startswith("qp.collect.signals/"):
        violations.append(f"legacy_exporter:{exporter or 'missing'}")
    details = {
        "outbox_rows": len(outbox_rows),
        "delivery_attempt_rows": attempts,
        "digest_push_state": getattr(digest_row, "push_state", None),
        "digest_pushed_at": (
            digest_row.pushed_at.isoformat() if digest_row is not None and digest_row.pushed_at else None
        ),
        "legacy_exporter": exporter,
        "telegram_or_dedirock_outbound": False,
    }
    return _check(
        "C10",
        "FAIL" if violations else "PASS",
        "shadow-only safety invariants" + ("" if not violations else " violated"),
        diffs=violations,
        details=details,
    )


# --------------------------------------------------------------------------- orchestration
def compare_day(
    session: Session,
    day: dt.date,
    legacy: str | os.PathLike[str],
    *,
    hmac_secret: bytes | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    day_text = day.isoformat()
    export = _load_export(day_text, legacy)
    checks: list[dict[str, Any]] = []
    if export["valid"]:
        checks.append(
            _check(
                "EXPORT",
                "PASS",
                "manifest and file hashes verified",
                details={"dir": export["dir"], "exported_at": (export["manifest"] or {}).get("exported_at")},
            )
        )
    else:
        checks.append(
            _check(
                "EXPORT",
                "BLOCKED",
                "legacy export missing or corrupt",
                diffs=export["errors"],
                details={"dir": export["dir"]},
            )
        )
    run = _qp_run(session, day)
    if export["valid"]:
        files = export["files"]
        runs = files["eod_run.json"]
        legacy_run = _latest_legacy_run(runs)
        checks.append(_compare_run(legacy_run, run, len(runs)))
        if run is None:
            for cid in ("C2", "C3", "C4", "C5", "C6"):
                checks.append(_blocked(cid, "qp EOD run missing"))
        else:
            checks.append(_compare_decisions(session, day, run, files["target_decisions.json"]))
            checks.append(
                _compare_risk(
                    files["risk_snapshot.json"], legacy_run.get("run_id") if legacy_run else None, session, run
                )
            )
            checks.append(_compare_signals(session, day, files["trend_signals.json"]))
            checks.append(_compare_macro(files["macro_slice.json"], session, day))
            checks.append(_compare_outcomes(session, run, files["outcomes.json"]))
        checks.append(_compare_notifications(session, day, export))
        checks.append(_compare_intake(session, day, files["intake.json"]))
        checks.append(_compare_digest(session, day, files["digest.json"], hmac_secret=hmac_secret))
    checks.append(_compare_safety(session, day, export))
    overall = _worst(checks)
    counts = {
        status: sum(1 for check in checks if check["status"] == status)
        for status in ("PASS", "WARN", "BLOCKED", "FAIL")
    }
    return {
        "schema_version": 1,
        "date": day_text,
        "generated_at": (now or dt.datetime.now(UTC)).isoformat(timespec="seconds"),
        "overall": overall,
        "gate_ok": overall in ("PASS", "WARN"),
        "counts": counts,
        "legacy_dir": export["dir"],
        "checks": checks,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# M4 shadow compare {result['date']}",
        "",
        f"- Overall: **{result['overall']}**",
        f"- Gate ok: {result['gate_ok']}",
        f"- Legacy: `{result['legacy_dir']}`",
        "- Checks: " + ", ".join(f"{key}={value}" for key, value in result["counts"].items()),
        "",
    ]
    for check in result["checks"]:
        lines.append(f"## {check['id']} {check['status']} — {check['summary']}")
        for diff in check.get("diffs") or []:
            lines.append(f"- {diff}")
        lines.append("")
    return "\n".join(lines)


def write_report(result: dict[str, Any], out_dir: str | os.PathLike[str]) -> list[str]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    stem = result["date"]
    targets = {
        f"{stem}.compare.json": json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        f"{stem}.compare.md": render_markdown(result),
    }
    for name, text in targets.items():
        path = root / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return sorted(targets)
