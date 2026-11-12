"""Read-only legacy swing/signals exporter (M4.6).

Runs as the legacy owner (``ubuntu``) with ``ProtectHome=read-only``; opens both
SQLite databases with ``mode=ro``, never copies a database and never writes outside
``--out-root``.  One directory per decision date, manifest last:

    <out-root>/<decision_date>/
      manifest.json  eod_run.json  target_decisions.json  risk_snapshot.json
      trend_signals.json  outcomes.json  macro_slice.json  notifications.json
      risk_state.json  intake.json  digest.json

The file list follows M4-SHADOW-CONTRACT §2.4 plus ``notifications.json`` for C7.
``digest.json`` is ``execution_digest_<decision_date>.json`` (written at 08:35 the
next morning); ``intake.json`` holds the rows whose UTC ``received_at`` date equals
``decision_date``, which is exactly the set that digest consumes.  The 08:45 export
is therefore the complete one.

``--emit-json`` prints the same file objects to stdout instead of writing; that is
used once to build deterministic fixtures without writing on the legacy host.
``--date`` also accepts ``today``/``yesterday`` (Asia/Shanghai), which the 15:55/16:20
and next-day 08:45 systemd timers use.  Stdlib only: this module must also run when
piped to ``python3 -`` on Ubuntu-nnG8.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "1.0"
CST = ZoneInfo("Asia/Shanghai")
EXPORTER = "qp.collect.signals/1"
FILE_NAMES = (
    "eod_run.json", "target_decisions.json", "risk_snapshot.json", "trend_signals.json",
    "outcomes.json", "macro_slice.json", "notifications.json", "risk_state.json",
    "intake.json", "digest.json",
)
SENSITIVE_RE = re.compile(
    r"(token|chat[_-]?id|secret|authorization|cookie|password|api[_-]?key)", re.I)


def resolve_date(value: str, *, now: dt.datetime | None = None) -> dt.date:
    """Resolve ``YYYY-MM-DD``, ``today`` or ``yesterday`` in Asia/Shanghai."""
    if value in {"today", "yesterday"}:
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.timezone.utc)
        day = current.astimezone(CST).date()
        return day - dt.timedelta(days=1) if value == "yesterday" else day
    return dt.date.fromisoformat(value)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).isoformat(timespec="seconds")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def sanitize(value: Any) -> Any:
    """Remove credential-shaped values; names stay visible for schema comparison."""
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if SENSITIVE_RE.search(str(k)) else sanitize(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextlib.contextmanager
def _connect_ro(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = "file:" + quote(str(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect_raw(legacy_root: Path, decision_date: str) -> dict[str, Any]:
    """Read the legacy stores and return un-rendered rows; no writes."""
    db = legacy_root / "data/swing_macro.db"
    intake_db = legacy_root / "data/approved-signals/intake.sqlite3"
    risk_file = legacy_root / "data/radar_risk_state.json"
    override_file = legacy_root / "data/radar_risk_override.json"
    digest_file = legacy_root / "data/approved-signals/digests" / f"execution_digest_{decision_date}.json"
    risk_ts = [stamp for stamp in (_mtime(risk_file), _mtime(override_file)) if stamp is not None]
    raw: dict[str, Any] = {
        "decision_date": decision_date,
        "source_mtime": {
            "eod_run.json": _iso(_mtime(db)),
            "target_decisions.json": _iso(_mtime(db)),
            "risk_snapshot.json": _iso(_mtime(db)),
            "trend_signals.json": _iso(_mtime(db)),
            "outcomes.json": _iso(_mtime(db)),
            "macro_slice.json": _iso(_mtime(db)),
            "notifications.json": _iso(_mtime(db)),
            "risk_state.json": _iso(max(risk_ts) if risk_ts else None),
            "intake.json": _iso(_mtime(intake_db)),
            "digest.json": _iso(_mtime(digest_file)),
        },
        "runs": [], "decisions": [], "snapshots": [], "signals": [], "outcomes": [],
        "macro": None, "outbox": [], "dispatch": [], "attempts": [],
        "intake": [], "risk_state": None, "risk_override": None, "digest": None,
        "missing": [],
    }
    if not db.is_file():
        raw["missing"].append(str(db))
        return raw
    with _connect_ro(db) as conn:
        runs = _rows(conn, "SELECT * FROM swing_eod_run WHERE decision_date=? ORDER BY id",
                     (decision_date,))
        raw["runs"] = runs
        if runs:
            # ``eod_target_decision.run_id`` references the text ``swing_eod_run.run_id``.
            latest_run_id = runs[-1]["run_id"]
            raw["decisions"] = _rows(
                conn, "SELECT * FROM eod_target_decision WHERE run_id=? ORDER BY code",
                (latest_run_id,))
            raw["outcomes"] = _rows(
                conn,
                "SELECT o.*, d.decision_date AS decision_date, d.code AS code, d.run_id AS run_id "
                "FROM eod_decision_outcome o JOIN eod_target_decision d "
                "ON d.decision_id=o.decision_id WHERE d.run_id=? "
                "ORDER BY d.code, o.horizon_sessions", (latest_run_id,))
            run_ids = [row["run_id"] for row in runs]
            raw["snapshots"] = _rows(
                conn, "SELECT * FROM eod_risk_snapshot WHERE run_id IN (%s) ORDER BY rowid"
                % ",".join("?" * len(run_ids)), tuple(run_ids))
        raw["signals"] = _rows(conn, "SELECT * FROM trend_signal WHERE date=? ORDER BY id",
                               (decision_date,))
        macro = _rows(conn, "SELECT * FROM macro_slice WHERE date=? LIMIT 1", (decision_date,))
        raw["macro"] = macro[0] if macro else None
        # C7 only cares about the current shadow window, whose business keys carry
        # the ISO date.  Old hash-only keys without a date are intentionally omitted.
        raw["outbox"] = _rows(
            conn, "SELECT * FROM notification_outbox WHERE idempotency_key LIKE ? ORDER BY id",
            (f"%{decision_date}%",))
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "notification_dispatch" in tables:
            raw["dispatch"] = _rows(
                conn, "SELECT * FROM notification_dispatch "
                      "WHERE business_key LIKE ? OR namespace LIKE ? ORDER BY id",
                (f"%{decision_date}%", f"%{decision_date}%"))
            dispatch_ids = [row["id"] for row in raw["dispatch"]]
            if dispatch_ids:
                raw["attempts"] = _rows(
                    conn, "SELECT * FROM notification_attempt WHERE dispatch_id IN (%s) "
                          "ORDER BY dispatch_id, attempt_no"
                    % ",".join("?" * len(dispatch_ids)), tuple(dispatch_ids))
    if intake_db.is_file():
        with _connect_ro(intake_db) as conn:
            raw["intake"] = _rows(
                conn, "SELECT * FROM intakes WHERE substr(received_at,1,10)=? ORDER BY rowid",
                (decision_date,))
    state = _read_json_file(risk_file)
    override = _read_json_file(override_file)
    raw["risk_state"] = state if isinstance(state, dict) else None
    raw["risk_override"] = override if isinstance(override, dict) else None
    digest = _read_json_file(digest_file)
    raw["digest"] = digest if isinstance(digest, dict) else None
    return raw


def _parse_run(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "reason_codes": _json_loads(row.get("reason_codes"), []),
            "details": _json_loads(row.get("details"), {})}


def _parse_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {**row,
            "values": _json_loads(row.get("values_json"), {}),
            "source_timestamps": _json_loads(row.get("source_timestamps_json"), {}),
            "available_at": _json_loads(row.get("available_at_json"), {}),
            "sources": _json_loads(row.get("sources_json"), {}),
            "missing_fields": _json_loads(row.get("missing_fields_json"), [])}


def _parse_decision(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "raw_signals": _json_loads(row.get("raw_signals_json"), [])}


def _parse_signal(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "detail": _json_loads(row.get("detail"), {})}


def _parse_payload(row: dict[str, Any], field: str = "payload_json") -> dict[str, Any]:
    """Parse the payload and drop the raw string so no credential can leak through."""
    parsed = {key: value for key, value in row.items() if key != field}
    return {**parsed, "payload": _json_loads(row.get(field), {})}


def _parse_intake(row: dict[str, Any]) -> dict[str, Any]:
    payload = _json_loads(row.get("payload_json"), {})
    payload = payload if isinstance(payload, dict) else {}
    return {
        "idempotency_key": row.get("idempotency_key"),
        "received_at": row.get("received_at"),
        "status": row.get("status"),
        "detail": row.get("detail"),
        "risk_check": {"passed": row.get("status") == "accepted", "detail": row.get("detail")},
        "payload_sha256": canonical_payload_sha256(payload),
        "payload": sanitize(payload),
    }


def build_export(raw: dict[str, Any]) -> dict[str, Any]:
    """Render the legacy rows into the per-date files (deterministic order)."""
    macro = raw.get("macro")
    macro_out = None
    if isinstance(macro, dict):
        macro_out = {**macro, "extra": _json_loads(macro.get("extra"), {})}
    risk = None
    if raw.get("risk_state") is not None or raw.get("risk_override") is not None:
        risk = {"state": sanitize(raw.get("risk_state")),
                "override": sanitize(raw.get("risk_override"))}
    notifications = {
        "outbox": [_parse_payload(row) for row in raw.get("outbox") or []],
        "dispatch": [_parse_payload(row) for row in raw.get("dispatch") or []],
        "attempts": list(raw.get("attempts") or []),
    }
    return sanitize({
        "eod_run.json": [_parse_run(row) for row in raw.get("runs") or []],
        "target_decisions.json": [_parse_decision(row) for row in raw.get("decisions") or []],
        "risk_snapshot.json": [_parse_snapshot(row) for row in raw.get("snapshots") or []],
        "trend_signals.json": [_parse_signal(row) for row in raw.get("signals") or []],
        "outcomes.json": list(raw.get("outcomes") or []),
        "macro_slice.json": macro_out,
        "notifications.json": notifications,
        "risk_state.json": risk,
        "intake.json": [_parse_intake(row) for row in raw.get("intake") or []],
        "digest.json": raw.get("digest"),
    })


def render_file(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def build_manifest(decision_date: str, exported_at: str, raw: dict[str, Any],
                   files: dict[str, Any]) -> dict[str, Any]:
    rendered = {name: render_file(value) for name, value in files.items()}
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not raw.get("missing"),
        "exporter": EXPORTER,
        "decision_date": decision_date,
        "exported_at": exported_at,
        "missing": raw.get("missing") or [],
        "files": {name: {"sha256": hashlib.sha256(rendered[name]).hexdigest(),
                         "bytes": len(rendered[name])} for name in sorted(rendered)},
        "source_mtime": raw.get("source_mtime") or {},
    }


def write_export(out_root: Path, decision_date: str, raw: dict[str, Any],
                 exported_at: str) -> dict[str, Any]:
    files = build_export(raw)
    manifest = build_manifest(decision_date, exported_at, raw, files)
    target = out_root / decision_date
    target.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        _atomic_write(target / name, render_file(value))
    for name in FILE_NAMES:
        if name not in files:
            (target / name).unlink(missing_ok=True)
    _atomic_write(target / "manifest.json", render_file(manifest))
    return manifest


def export_day(legacy_root: str | os.PathLike[str], decision_date: str,
               out_root: str | os.PathLike[str], *,
               now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    raw = collect_raw(Path(legacy_root), decision_date)
    return write_export(Path(out_root), decision_date, raw, now.isoformat(timespec="seconds"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="read-only legacy swing signals exporter")
    ap.add_argument("--date", required=True,
                    help="YYYY-MM-DD, or 'today'/'yesterday' in Asia/Shanghai")
    ap.add_argument("--legacy-root", default="/home/ubuntu/trading-radar")
    ap.add_argument("--out-root", default="/var/lib/qp-inbox/signals-legacy")
    ap.add_argument("--emit-json", action="store_true",
                    help="print file objects instead of writing (fixture generation)")
    a = ap.parse_args(argv)
    try:
        decision_date = resolve_date(a.date).isoformat()
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad_date"}))
        return 2
    if a.emit_json:
        raw = collect_raw(Path(a.legacy_root), decision_date)
        files = build_export(raw)
        print(json.dumps({"ok": not raw.get("missing"), "decision_date": decision_date,
                          "missing": raw.get("missing") or [],
                          "source_mtime": raw.get("source_mtime") or {},
                          "files": {name: json.loads(render_file(value).decode("utf-8"))
                                    for name, value in files.items()}},
                         ensure_ascii=False))
        return 0 if not raw.get("missing") else 2
    manifest = export_day(a.legacy_root, decision_date, a.out_root)
    print(json.dumps({"ok": manifest["ok"], "decision_date": decision_date,
                      "files": len(manifest["files"]), "missing": manifest["missing"]},
                     ensure_ascii=False))
    return 0 if manifest["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
