#!/usr/bin/env python3
"""Research Ledger: local-only, append-only investment research control plane.

No credential is stored in this application. PostgreSQL uses local peer authentication.
All importer payloads are manifests created by root-owned, read-only source adapters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import uuid
from collections import defaultdict
from decimal import Decimal
from datetime import date, datetime, time, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

APP_VERSION = "0.1.0-shadow"
MIGRATION_VERSION = "002_snapshot_cas_dedup"
UTC = timezone.utc
CN_TZ = ZoneInfo("Asia/Shanghai")
FORMAL_MIN_FORECASTS = 300
FORMAL_MIN_BATCHES = 10
FORMAL_MIN_BIN = 20


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime | None = None) -> str:
    return (value or now_utc()).astimezone(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def stable_uuid(namespace: str, *parts: str) -> uuid.UUID:
    return uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL, namespace), "|".join(parts))


def parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(UTC)


def db_dsn(name: str | None = None) -> str:
    return os.environ.get("LEDGER_DSN", f"dbname={name or os.environ.get('LEDGER_DB_NAME', 'research_ledger')} host=/var/run/postgresql")


def connect(name: str | None = None) -> psycopg.Connection:
    return psycopg.connect(db_dsn(name), row_factory=dict_row)


def migration_paths() -> list[Path]:
    return sorted((Path(__file__).resolve().parents[1] / "migrations").glob("[0-9][0-9][0-9]_*.sql"))


def apply_migration(db_name: str | None = None) -> None:
    with connect(db_name) as conn:
        for path in migration_paths():
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            artifact_hash = sha256_text(sql)
            exists = conn.execute("SELECT to_regclass('ledger.schema_migration') AS rel").fetchone()["rel"]
            if exists:
                row = conn.execute("SELECT artifact_hash FROM ledger.schema_migration WHERE version=%s", (version,)).fetchone()
                if row:
                    if row["artifact_hash"] != artifact_hash:
                        raise RuntimeError(f"migration artifact hash mismatch: {version}")
                    continue
            rendered = sql.replace(":'migration_hash'", f"'{artifact_hash}'")
            conn.execute(rendered)


def add_audit(conn: psycopg.Connection, actor: str, action: str, object_type: str, object_id: uuid.UUID | None, payload: Any, correlation_id: uuid.UUID | None = None) -> None:
    input_hash = sha256_json(payload)
    conn.execute(
        """INSERT INTO ledger.audit_event(occurred_at,actor_id,action,object_type,object_id,input_hash,result_hash,correlation_id,metadata)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (now_utc(), actor, action, object_type, object_id, input_hash, input_hash, correlation_id, json.dumps({"shadow": True})),
    )


def capability_hash(token: str) -> str:
    return sha256_text(token)


def require_capability(conn: psycopg.Connection, token: str | None, role: str, scope_id: uuid.UUID | None = None) -> dict[str, Any]:
    if not token:
        raise PermissionError("missing capability")
    row = conn.execute(
        """SELECT c.* FROM ledger.capability c
           LEFT JOIN ledger.capability_revocation r ON r.capability_id=c.capability_id
           WHERE c.capability_hash=%s AND c.role_name=%s AND r.capability_id IS NULL
             AND (c.expires_at IS NULL OR c.expires_at > now())""",
        (capability_hash(token), role),
    ).fetchone()
    if not row:
        raise PermissionError("invalid capability")
    if scope_id is not None and row["scope_id"] not in (None, scope_id):
        raise PermissionError("capability scope mismatch")
    return row


def require_admin(conn: psycopg.Connection, token: str | None) -> dict[str, Any]:
    return require_capability(conn, token, "ADMIN")


def create_case(conn: psycopg.Connection, title: str, classification: str, actor: str, data_cutoff: str | None = None) -> uuid.UUID:
    if classification not in {"FACT", "INFERENCE", "SCENARIO", "MIXED", "RETROSPECTIVE_EXTRACTED"}:
        raise ValueError("invalid classification")
    case_id = uuid.uuid4()
    payload = {"title": title, "classification": classification, "data_cutoff": data_cutoff}
    conn.execute(
        """INSERT INTO ledger.research_case(case_id,title,status,classification,created_at,data_cutoff,created_by,content_hash)
           VALUES (%s,%s,'DRAFT',%s,%s,%s,%s,%s)""",
        (case_id, title, classification, now_utc(), parse_dt(data_cutoff), actor, sha256_json(payload)),
    )
    add_audit(conn, actor, "CASE_CREATE", "research_case", case_id, payload)
    return case_id


def create_review_round(conn: psycopg.Connection, case_id: uuid.UUID, deadline_at: str, actor: str) -> uuid.UUID:
    deadline = parse_dt(deadline_at)
    if deadline is None or deadline <= now_utc():
        raise ValueError("review deadline must be in the future")
    round_id = uuid.uuid4()
    payload = {"case_id": str(case_id), "deadline_at": iso(deadline)}
    conn.execute(
        """INSERT INTO ledger.review_round(round_id,case_id,status,deadline_at,created_at,created_by,round_hash)
           VALUES (%s,%s,'BLIND',%s,%s,%s,%s)""",
        (round_id, case_id, deadline, now_utc(), actor, sha256_json(payload)),
    )
    add_audit(conn, actor, "REVIEW_ROUND_CREATE", "review_round", round_id, payload)
    return round_id


def submit_review(conn: psycopg.Connection, token: str, round_id: uuid.UUID, role: str, content_hash: str, actor: str, classification: str = "MIXED") -> uuid.UUID:
    if role not in {"BULL", "BEAR", "FORENSIC_FINANCE", "INDUSTRY", "SOURCE_AUDIT", "RISK_OFFICER"}:
        raise ValueError("invalid review role")
    require_capability(conn, token, role, round_id)
    round_row = conn.execute("SELECT status,deadline_at FROM ledger.review_round WHERE round_id=%s", (round_id,)).fetchone()
    if not round_row or round_row["status"] != "BLIND" or round_row["deadline_at"] <= now_utc():
        raise ValueError("review round is not accepting blinded submissions")
    submission_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.review_submission(submission_id,round_id,role_name,actor_id,classification,content_hash,submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (submission_id, round_id, role, actor, classification, content_hash, now_utc()),
    )
    add_audit(conn, actor, "REVIEW_SUBMIT", "review_submission", submission_id, {"round_id": str(round_id), "role": role, "content_hash": content_hash})
    return submission_id


def list_visible_reviews(conn: psycopg.Connection, token: str, round_id: uuid.UUID) -> list[dict[str, Any]]:
    cap = None
    for role in ("ADMIN", "BULL", "BEAR", "FORENSIC_FINANCE", "INDUSTRY", "SOURCE_AUDIT", "RISK_OFFICER"):
        try:
            cap = require_capability(conn, token, role, round_id)
            break
        except PermissionError:
            continue
    if cap is None:
        raise PermissionError("invalid capability")
    row = conn.execute("SELECT status FROM ledger.review_round WHERE round_id=%s", (round_id,)).fetchone()
    if not row:
        raise ValueError("review round missing")
    if row["status"] == "BLIND" and cap["role_name"] != "ADMIN":
        return list(conn.execute("SELECT submission_id,round_id,role_name,actor_id,classification,content_hash,submitted_at FROM ledger.review_submission WHERE round_id=%s AND role_name=%s", (round_id, cap["role_name"])).fetchall())
    return list(conn.execute("SELECT submission_id,round_id,role_name,actor_id,classification,content_hash,submitted_at FROM ledger.review_submission WHERE round_id=%s ORDER BY role_name", (round_id,)).fetchall())


def risk_veto(conn: psycopg.Connection, token: str, round_id: uuid.UUID, reason_hash: str, actor: str, evidence_gap_code: str = "EVIDENCE_INSUFFICIENT") -> uuid.UUID:
    require_capability(conn, token, "RISK_OFFICER", round_id)
    row = conn.execute("SELECT case_id,status FROM ledger.review_round WHERE round_id=%s", (round_id,)).fetchone()
    if not row or row["status"] != "BLIND":
        raise ValueError("risk veto requires active blinded round")
    veto_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.risk_decision(risk_decision_id,round_id,case_id,decision,reason_hash,evidence_gap_code,decided_at,actor_id)
           VALUES (%s,%s,%s,'VETO',%s,%s,%s,%s)""",
        (veto_id, round_id, row["case_id"], reason_hash, evidence_gap_code, now_utc(), actor),
    )
    add_audit(conn, actor, "RISK_VETO", "risk_decision", veto_id, {"round_id": str(round_id), "reason_hash": reason_hash, "evidence_gap_code": evidence_gap_code})
    return veto_id


def register_forecast(conn: psycopg.Connection, payload: dict[str, Any], actor: str) -> uuid.UUID:
    forecast_type = payload["forecast_type"]
    window_start = parse_dt(payload["window_start"])
    window_end = parse_dt(payload["window_end"])
    created_at = parse_dt(payload.get("created_at")) or now_utc()
    sealed_at = parse_dt(payload.get("sealed_at"))
    if window_start is None or window_end is None or window_end <= window_start:
        raise ValueError("invalid forecast window")
    if forecast_type == "BINARY_EVENT":
        probability = payload.get("probability")
        if probability is None or not 0 <= float(probability) <= 1:
            raise ValueError("binary probability must be in [0,1]")
        confidence_level = lower_bound = upper_bound = None
    elif forecast_type == "NUMERIC_RANGE":
        probability = None
        confidence_level = payload.get("confidence_level")
        lower_bound = payload.get("lower_bound")
        upper_bound = payload.get("upper_bound")
        if confidence_level is None or not 0 < float(confidence_level) < 1 or lower_bound is None or upper_bound is None or float(lower_bound) > float(upper_bound):
            raise ValueError("numeric range requires valid interval confidence")
    else:
        raise ValueError("unsupported forecast type")
    if sealed_at is not None and sealed_at >= window_start:
        prospective_eligible = False
        retrospective_reason = "SEALED_AFTER_WINDOW_START"
    elif sealed_at is None:
        prospective_eligible = False
        retrospective_reason = "UNSEALED"
    elif not payload.get("source_hashes") or not payload.get("feature_snapshot_hash"):
        prospective_eligible = False
        retrospective_reason = "MISSING_TRACEABILITY"
    else:
        prospective_eligible = True
        retrospective_reason = None
    forecast_id = uuid.UUID(payload.get("forecast_id")) if payload.get("forecast_id") else uuid.uuid4()
    status = payload.get("status", "DRAFT")
    if status == "SEALED" and sealed_at is None:
        raise ValueError("sealed forecast requires sealed_at")
    content_hash = sha256_json(payload)
    conn.execute(
        """INSERT INTO ledger.forecast(forecast_id,case_id,batch_id,protocol_id,parent_forecast_id,forecast_type,status,target_key,metric_definition_hash,target_session,window_start,window_end,probability,confidence_level,lower_bound,upper_bound,baseline_hash,invalidation_hash,settlement_rule_hash,model_version,feature_snapshot_hash,source_hashes,data_cutoff,created_at,sealed_at,prospective_eligible,retrospective_reason,content_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            forecast_id, payload.get("case_id"), payload.get("batch_id"), payload.get("protocol_id"), payload.get("parent_forecast_id"), forecast_type, status,
            payload["target_key"], payload["metric_definition_hash"], payload.get("target_session"), window_start, window_end, probability, confidence_level,
            lower_bound, upper_bound, payload["baseline_hash"], payload["invalidation_hash"], payload["settlement_rule_hash"], payload["model_version"],
            payload["feature_snapshot_hash"], json.dumps(payload.get("source_hashes", [])), parse_dt(payload["data_cutoff"]), created_at, sealed_at,
            prospective_eligible, retrospective_reason, content_hash,
        ),
    )
    add_audit(conn, actor, "FORECAST_REGISTER", "forecast", forecast_id, {"content_hash": content_hash, "prospective_eligible": prospective_eligible})
    return forecast_id


def research_correctness(forecast: dict[str, Any], observed_value: float) -> tuple[float, dict[str, Any]]:
    if forecast["forecast_type"] == "BINARY_EVENT":
        predicted = 1 if float(forecast["probability"]) >= 0.5 else 0
        outcome = int(observed_value)
        score = 1.0 if predicted == outcome else 0.0
        return score, {"method": "threshold_accuracy", "threshold": 0.5, "probability": float(forecast["probability"]), "outcome": outcome, "rule_version": "ledger-v1"}
    hit = float(forecast["lower_bound"]) <= float(observed_value) <= float(forecast["upper_bound"])
    return (1.0 if hit else 0.0), {"method": "interval_hit", "lower_bound": float(forecast["lower_bound"]), "upper_bound": float(forecast["upper_bound"]), "observed_value": float(observed_value), "rule_version": "ledger-v1"}


def settle_forecast(conn: psycopg.Connection, forecast_id: uuid.UUID, observed_value: float, source_id: uuid.UUID, input_hash: str, actor: str, reviewed: bool = False, observed_at: datetime | None = None) -> uuid.UUID:
    previous = conn.execute("SELECT settlement_id FROM ledger.settlement WHERE forecast_id=%s ORDER BY revision_no DESC LIMIT 1", (forecast_id,)).fetchone()
    if previous:
        raise ValueError("existing settlement cannot be overwritten; create revision via explicit correction workflow")
    forecast = conn.execute("SELECT * FROM ledger.forecast WHERE forecast_id=%s", (forecast_id,)).fetchone()
    if not forecast:
        raise ValueError("forecast not found")
    outcome_id = uuid.uuid4()
    outcome_status = "REVIEWED" if reviewed else "PENDING_REVIEW"
    now = now_utc()
    conn.execute(
        """INSERT INTO ledger.outcome_observation(outcome_id,forecast_id,revision_no,outcome_status,observed_value,observed_at,source_id,input_hash,rule_version,recorded_at,recorded_by)
           VALUES (%s,%s,1,%s,%s,%s,%s,%s,'ledger-v1',%s,%s)""",
        (outcome_id, forecast_id, outcome_status, observed_value, observed_at or now, source_id, input_hash, now, actor),
    )
    score, rationale = research_correctness(forecast, observed_value)
    settlement_id = uuid.uuid4()
    settlement_status = "REVIEWED" if reviewed else "PENDING_REVIEW"
    settlement_payload = {"forecast_id": str(forecast_id), "outcome_id": str(outcome_id), "score": score, "status": settlement_status}
    conn.execute(
        """INSERT INTO ledger.settlement(settlement_id,forecast_id,outcome_id,revision_no,settlement_status,research_correctness_score,investment_return_status,settlement_hash,settled_at,reviewed_at,reviewed_by)
           VALUES (%s,%s,%s,1,%s,%s,'NOT_APPLICABLE',%s,%s,%s,%s)""",
        (settlement_id, forecast_id, outcome_id, settlement_status, score, sha256_json(settlement_payload), now, now if reviewed else None, actor if reviewed else None),
    )
    conn.execute(
        """INSERT INTO ledger.evaluation(evaluation_id,settlement_id,evaluation_type,score,rationale_hash,rule_version,evaluated_at,evaluated_by)
           VALUES (%s,%s,'RESEARCH_CORRECTNESS',%s,%s,'ledger-v1',%s,%s)""",
        (uuid.uuid4(), settlement_id, score, sha256_json(rationale), now, actor),
    )
    add_audit(conn, actor, "SETTLEMENT_CREATE", "settlement", settlement_id, settlement_payload)
    return settlement_id


def calibration_status(conn: psycopg.Connection) -> dict[str, Any]:
    rows = list(conn.execute(
        """SELECT f.forecast_id,f.probability,f.batch_id,o.observed_value,s.reviewed_at
           FROM ledger.forecast f
           JOIN LATERAL (
             SELECT * FROM ledger.outcome_observation o WHERE o.forecast_id=f.forecast_id ORDER BY o.revision_no DESC LIMIT 1
           ) o ON o.outcome_status='REVIEWED'
           JOIN LATERAL (
             SELECT * FROM ledger.settlement s WHERE s.forecast_id=f.forecast_id ORDER BY s.revision_no DESC LIMIT 1
           ) s ON s.settlement_status='REVIEWED' AND s.outcome_id=o.outcome_id
           LEFT JOIN ledger.forecast_batch b ON b.batch_id=f.batch_id
           WHERE f.forecast_type='BINARY_EVENT' AND f.status='SEALED' AND f.prospective_eligible=true
             AND (b.batch_status IS NULL OR b.batch_status <> 'VETOED')"""

    ).fetchall())
    batches = {str(row["batch_id"]) for row in rows if row["batch_id"]}
    bins: list[dict[str, Any]] = []
    ece = 0.0
    brier = None
    if rows:
        brier = sum((float(r["probability"]) - float(r["observed_value"])) ** 2 for r in rows) / len(rows)
        for bucket in range(10):
            members = [r for r in rows if min(9, int(float(r["probability"]) * 10)) == bucket]
            if not members:
                continue
            mean_probability = sum(float(r["probability"]) for r in members) / len(members)
            outcome_rate = sum(float(r["observed_value"]) for r in members) / len(members)
            gap = mean_probability - outcome_rate
            ece += abs(gap) * len(members) / len(rows)
            bins.append({"bucket": bucket, "n": len(members), "mean_probability": mean_probability, "outcome_rate": outcome_rate, "gap": gap, "stable_eligible": len(members) >= FORMAL_MIN_BIN})
    stable = len(rows) >= FORMAL_MIN_FORECASTS and len(batches) >= FORMAL_MIN_BATCHES and bool(bins) and all(x["stable_eligible"] for x in bins)
    return {
        "status": "STABLE" if stable else "INSUFFICIENT_PROSPECTIVE_SAMPLES",
        "eligible_forecast_count": len(rows),
        "independent_batch_count": len(batches),
        "brier": brier if stable else None,
        "ece": ece if stable else None,
        "bins": bins if stable else [],
        "reason": None if stable else f"requires_{FORMAL_MIN_BATCHES}_batches_{FORMAL_MIN_FORECASTS}_forecasts_and_{FORMAL_MIN_BIN}_per_displayed_bin",
    }


def write_calibration_snapshot(conn: psycopg.Connection, actor: str = "calibration_worker") -> uuid.UUID:
    payload = calibration_status(conn)
    snapshot_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.calibration_snapshot(calibration_snapshot_id,metric_type,status,eligible_forecast_count,independent_batch_count,excluded_count,input_filter_hash,metric_payload,computed_at,computed_by,content_hash)
           VALUES (%s,'BINARY_BRIER',%s,%s,%s,0,%s,%s,%s,%s,%s)""",
        (snapshot_id, payload["status"], payload["eligible_forecast_count"], payload["independent_batch_count"], sha256_text("sealed+prospective+reviewed+binary"), json.dumps(payload), now_utc(), actor, sha256_json(payload)),
    )
    add_audit(conn, actor, "CALIBRATION_SNAPSHOT", "calibration_snapshot", snapshot_id, payload)
    return snapshot_id


def import_legacy_manifest(conn: psycopg.Connection, manifest: dict[str, Any], actor: str = "legacy_shadow_importer") -> dict[str, int]:
    batch_id = stable_uuid("ledger-import-batch", manifest["source_system"], manifest["source_database_hash"])
    imported = 0
    excluded = 0
    for item in manifest.get("records", []):
        status = "RETROSPECTIVE_EXTRACTED" if item.get("retrospective", True) else "IMPORTED_SHADOW"
        if item.get("excluded", False):
            status = "EXCLUDED"
            excluded += 1
        import_id = stable_uuid("ledger-import", manifest["source_system"], manifest["source_database_hash"], item["source_table"], str(item["source_record_id"]), item["source_record_hash"])
        conn.execute(
            """INSERT INTO ledger.import_ledger(import_id,source_system,source_database_hash,source_table,source_record_id,source_record_hash,original_recorded_at,original_sealed_at,import_batch_id,import_status,target_object_type,target_object_id,imported_at,imported_by,reason_code)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s,%s,%s)
               ON CONFLICT (source_system,source_database_hash,source_table,source_record_id,source_record_hash) DO NOTHING""",
            (import_id, manifest["source_system"], manifest["source_database_hash"], item["source_table"], str(item["source_record_id"]), item["source_record_hash"], parse_dt(item.get("original_recorded_at")), parse_dt(item.get("original_sealed_at")), batch_id, status, now_utc(), actor, item.get("reason_code")),
        )
        imported += 1
    add_audit(conn, actor, "LEGACY_SHADOW_IMPORT", "import_ledger", batch_id, {"source_system": manifest["source_system"], "imported": imported, "excluded": excluded})
    return {"imported": imported, "excluded": excluded, "batch_id": str(batch_id)}


def import_astock_manifest(conn: psycopg.Connection, manifest: dict[str, Any], actor: str = "astock_shadow_importer") -> dict[str, int]:
    imported = 0
    evidence = 0
    batch_id = stable_uuid("astock-import-batch", manifest["manifest_hash"])
    case_id = stable_uuid("astock-shadow-case", manifest["manifest_hash"])
    conn.execute(
        """INSERT INTO ledger.research_case(case_id,external_key,title,status,classification,created_at,created_by,content_hash)
           VALUES (%s,%s,%s,'IMPORTED_SHADOW','RETROSPECTIVE_EXTRACTED',%s,%s,%s)
           ON CONFLICT (case_id) DO NOTHING""",
        (case_id, f"astock-shadow:{manifest['manifest_hash']}", "AStock shadow import manifest", parse_dt(manifest["generated_at"]), actor, manifest["manifest_hash"]),
    )
    for item in manifest.get("artifacts", []):
        import_id = stable_uuid("astock-import", manifest["manifest_hash"], str(item.get("relative_path", item.get("relative_path_hash"))), str(item.get("sha256", item.get("artifact_hash"))))
        inserted = conn.execute(
            """INSERT INTO ledger.import_ledger(import_id,source_system,source_database_hash,source_table,source_record_id,source_record_hash,import_batch_id,import_status,target_object_type,target_object_id,imported_at,imported_by,reason_code)
               VALUES (%s,'ASTOCK_FILE_MANIFEST',%s,'file',%s,%s,%s,'RETROSPECTIVE_EXTRACTED',%s,%s,%s,%s,'SHADOW_IMPORT_NO_FORMAL_CALIBRATION')
               ON CONFLICT (import_id) DO NOTHING RETURNING import_id""",
            (import_id, manifest["manifest_hash"], item.get("relative_path", item.get("relative_path_hash")), item.get("sha256", item.get("artifact_hash")), batch_id, "research_snapshot" if item["kind"] == "report" else "delivery_event", case_id, now_utc(), actor),
        ).fetchone()
        if not inserted:
            continue
        if item["kind"] == "report":
            snapshot_id = stable_uuid("astock-snapshot", manifest["manifest_hash"], item["relative_path"], item["sha256"])
            conn.execute(
                """INSERT INTO ledger.research_snapshot(snapshot_id,case_id,version_no,snapshot_type,classification,observed_at,sealed_at,storage_uri,content_hash,created_at,created_by)
                   VALUES (%s,%s,%s,'ASTOCK_REPORT','RETROSPECTIVE_EXTRACTED',%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (snapshot_id) DO NOTHING""",
                (snapshot_id, case_id, item["version_no"], parse_dt(item["mtime"]), parse_dt(item["mtime"]), item["storage_uri"], item["sha256"], now_utc(), actor),
            )
            imported += 1
        elif item["kind"] == "notification":
            # The adapter provides no message body, recipient, chat ID, username, or storage URI for notifications.
            delivery_id = stable_uuid("astock-delivery-metadata", manifest["manifest_hash"], str(item["event_id"]), str(item["artifact_hash"]))
            conn.execute(
                """INSERT INTO ledger.delivery_event(delivery_id,case_id,channel,direction,status,payload_hash,external_reference_hash,occurred_at,recorded_by)
                   VALUES (%s,%s,'HERMES','IMPORTED_METADATA_ONLY',%s,%s,%s,%s,%s)
                   ON CONFLICT (delivery_id) DO NOTHING""",
                (delivery_id, case_id, item["status"], item["artifact_hash"], item["relative_path_hash"], parse_dt(item["occurred_at"]), actor),
            )
            evidence += 1
        else:
            # Orchestrator entries are retained only as an append-only audit reference, not CAS evidence.
            add_audit(conn, actor, "ASTOCK_ORCHESTRATOR_METADATA", "research_case", case_id, {"artifact_hash": item["artifact_hash"], "status": item["status"], "occurred_at": item["occurred_at"]})
            evidence += 1
    if imported or evidence:
        add_audit(conn, actor, "ASTOCK_SHADOW_IMPORT", "research_case", case_id, {"manifest_hash": manifest["manifest_hash"], "reports": imported, "metadata": evidence})
    return {"reports": imported, "metadata": evidence, "case_id": str(case_id), "batch_id": str(batch_id)}


def record_import(conn: psycopg.Connection, source_system: str, source_hash: str, source_table: str, source_record_id: str, source_record_hash: str, target_type: str, target_id: uuid.UUID, original_recorded_at: str | None, original_sealed_at: str | None, status: str, actor: str, reason_code: str | None = None) -> None:
    import_id = stable_uuid("ledger-source-import", source_system, source_hash, source_table, source_record_id, source_record_hash)
    conn.execute(
        """INSERT INTO ledger.import_ledger(import_id,source_system,source_database_hash,source_table,source_record_id,source_record_hash,original_recorded_at,original_sealed_at,import_batch_id,import_status,target_object_type,target_object_id,imported_at,imported_by,reason_code)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (source_system,source_database_hash,source_table,source_record_id,source_record_hash) DO NOTHING""",
        (import_id, source_system, source_hash, source_table, source_record_id, source_record_hash, parse_dt(original_recorded_at), parse_dt(original_sealed_at), stable_uuid("ledger-import-batch", source_system, source_hash), status, target_type, target_id, now_utc(), actor, reason_code),
    )


def assert_phase2_outcome_matches(existing: dict[str, Any], item: dict[str, Any], source_id: uuid.UUID) -> None:
    revision_no = int(existing["revision_no"])
    expected_ids = {stable_uuid("phase2-outcome", f"{item['outcome_id']}#r{revision_no}")}
    if revision_no == 1:
        # Compatibility with rev1 rows created before revision-aware IDs existed.
        expected_ids.add(stable_uuid("phase2-outcome", item["outcome_id"]))
    expected = {
        "observed_value": Decimal(str(item["outcome"])),
        "observed_at": parse_dt(item["source_as_of"]),
        "source_id": source_id,
        "input_hash": item["content_hash"],
        "rule_version": "phase2-v1",
        "recorded_at": parse_dt(item["settled_at"]),
        "recorded_by": item["settler"],
    }
    mismatches = [key for key, value in expected.items() if existing[key] != value]
    if existing["outcome_id"] not in expected_ids:
        mismatches.append("outcome_id")
    if mismatches:
        raise ValueError(
            f"phase2 outcome material drift for forecast {item['forecast_id']}: {','.join(sorted(mismatches))}"
        )


def assert_phase2_source_matches(existing: dict[str, Any], item: dict[str, Any]) -> None:
    source_time = parse_dt(item["source_as_of"])
    expected = {
        "source_type": "PHASE2_PUBLIC_MARKET",
        "source_locator": item["source_url"],
        "published_at": source_time,
        "observed_at": source_time,
        "data_cutoff": source_time,
        "content_hash": item["content_hash"],
    }
    mismatches = [key for key, value in expected.items() if existing[key] != value]
    if mismatches:
        raise ValueError(
            f"phase2 source material drift for forecast {item['forecast_id']}: {','.join(sorted(mismatches))}"
        )


def assert_phase2_settlement_matches(existing: dict[str, Any], outcome_id: uuid.UUID, score: float, rationale_hash: str, item: dict[str, Any]) -> None:
    expected = {
        "outcome_id": outcome_id,
        "research_correctness_score": Decimal(str(score)),
        "investment_return_status": "NOT_APPLICABLE",
        "settlement_hash": rationale_hash,
        "settled_at": parse_dt(item["settled_at"]),
        "reviewed_at": parse_dt(item.get("reviewed_at")),
        "reviewed_by": item.get("reviewed_by"),
    }
    mismatches = [key for key, value in expected.items() if existing[key] != value]
    if mismatches:
        raise ValueError(
            f"phase2 settlement material drift for forecast {item['forecast_id']}: {','.join(sorted(mismatches))}"
        )


def import_phase2_manifest(conn: psycopg.Connection, manifest: dict[str, Any], actor: str = "phase2_shadow_adapter") -> dict[str, int]:
    source_hash = manifest["source_database_hash"]
    ids: dict[str, uuid.UUID] = {}
    counts: defaultdict[str, int] = defaultdict(int)
    for proto in manifest.get("protocols", []):
        protocol_id = stable_uuid("phase2-protocol", proto["protocol_hash"])
        conn.execute(
            """INSERT INTO ledger.forecast_protocol(protocol_id,protocol_version,protocol_hash,universe_rule_hash,target_definition_hash,source_policy_hash,effective_at,created_at,created_by,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'FROZEN') ON CONFLICT (protocol_hash) DO NOTHING""",
            (protocol_id, proto["version"], proto["protocol_hash"], proto["universe_rule_hash"], proto["target_definition_hash"], proto["source_policy_hash"], parse_dt(proto["frozen_at"]), parse_dt(proto["created_at"]), actor),
        )
        stored_protocol = conn.execute(
            "SELECT protocol_id FROM ledger.forecast_protocol WHERE protocol_hash=%s",
            (proto["protocol_hash"],),
        ).fetchone()
        if not stored_protocol:
            raise ValueError(f"phase2 protocol could not be resolved: {proto['protocol_hash']}")
        protocol_id = stored_protocol["protocol_id"]
        ids[f"protocol:{proto['protocol_hash']}"] = protocol_id
        record_import(conn, "RESEARCH_SYSTEM_PHASE2", source_hash, "protocol", proto["protocol_hash"], proto["row_hash"], "forecast_protocol", protocol_id, proto.get("created_at"), proto.get("frozen_at"), "IMPORTED_SHADOW", actor)
        counts["protocol"] += 1
    batch_protocol_ids: dict[str, uuid.UUID | None] = {}
    for batch in manifest.get("batches", []):
        batch_id = stable_uuid("phase2-batch", batch["batch_id"])
        protocol_id = ids.get(f"protocol:{batch['protocol_hash']}")
        if protocol_id is None:
            stored_protocol = conn.execute(
                "SELECT protocol_id FROM ledger.forecast_protocol WHERE protocol_hash=%s",
                (batch["protocol_hash"],),
            ).fetchone()
            if not stored_protocol:
                raise ValueError(f"phase2 batch references unknown protocol: {batch['protocol_hash']}")
            protocol_id = stored_protocol["protocol_id"]
            ids[f"protocol:{batch['protocol_hash']}"] = protocol_id
        mapped_status = batch["status"] if batch["status"] in {"PENDING", "SEALED", "BLOCKED", "VETOED", "SETTLING", "SETTLED"} else "IMPORTED_SHADOW"
        conn.execute(
            """INSERT INTO ledger.forecast_batch(batch_id,protocol_id,external_batch_key,target_session,correlation_group,expected_universe_size,qualified_coverage,batch_status,created_at,sealed_at,content_hash,imported_from)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (external_batch_key) DO NOTHING""",
            (batch_id, protocol_id, f"phase2:{batch['batch_id']}", date.fromisoformat(batch["target_session"]), batch["cohort_id"], batch["expected_n"], batch["qualified_n"], mapped_status, parse_dt(batch["created_at"]), parse_dt(batch.get("sealed_at")), batch["row_hash"], f"phase2_status:{batch['status']}"),
        )
        stored_batch = conn.execute(
            "SELECT batch_id,protocol_id FROM ledger.forecast_batch WHERE external_batch_key=%s",
            (f"phase2:{batch['batch_id']}",),
        ).fetchone()
        if not stored_batch:
            raise ValueError(f"phase2 batch could not be resolved: {batch['batch_id']}")
        batch_id = stored_batch["batch_id"]
        ids[f"batch:{batch['batch_id']}"] = batch_id
        batch_protocol_ids[batch["batch_id"]] = stored_batch["protocol_id"]
        record_import(conn, "RESEARCH_SYSTEM_PHASE2", source_hash, "batch", batch["batch_id"], batch["row_hash"], "forecast_batch", batch_id, batch.get("created_at"), batch.get("sealed_at"), "IMPORTED_SHADOW", actor)
        counts["batch"] += 1
    for item in manifest.get("forecasts", []):
        forecast_id = stable_uuid("phase2-forecast", item["forecast_id"])
        ids[f"forecast:{item['forecast_id']}"] = forecast_id
        sealed_at = parse_dt(item["sealed_at"])
        target_day = date.fromisoformat(item["target_session"])
        start = datetime.combine(target_day, time(9, 30), CN_TZ).astimezone(UTC)
        end = datetime.combine(target_day, time(15, 0), CN_TZ).astimezone(UTC)
        eligible = bool(sealed_at and sealed_at < start and item["status"] == "SEALED")
        reason = None if eligible else "PHASE2_IMPORT_NOT_FORMAL_ELIGIBLE"
        batch_id = ids.get(f"batch:{item['batch_id']}") or stable_uuid("phase2-batch", item["batch_id"])
        protocol_id = batch_protocol_ids.get(item["batch_id"])
        if protocol_id is None:
            stored_batch = conn.execute(
                "SELECT batch_id,protocol_id FROM ledger.forecast_batch WHERE external_batch_key=%s",
                (f"phase2:{item['batch_id']}",),
            ).fetchone()
            if not stored_batch:
                raise ValueError(f"phase2 forecast references unknown batch: {item['batch_id']}")
            batch_id = stored_batch["batch_id"]
            ids[f"batch:{item['batch_id']}"] = batch_id
            protocol_id = stored_batch["protocol_id"]
            batch_protocol_ids[item["batch_id"]] = protocol_id
        conn.execute(
            """INSERT INTO ledger.forecast(forecast_id,batch_id,protocol_id,forecast_type,status,target_key,metric_definition_hash,target_session,window_start,window_end,probability,baseline_hash,invalidation_hash,settlement_rule_hash,model_version,feature_snapshot_hash,source_hashes,data_cutoff,created_at,sealed_at,prospective_eligible,retrospective_reason,content_hash)
               VALUES (%s,%s,%s,'BINARY_EVENT',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (forecast_id) DO NOTHING""",
            (forecast_id, batch_id, protocol_id, "SEALED" if item["status"] == "SEALED" else "IMPORTED_SHADOW", item["symbol"], sha256_text("target_session_close_gt_prior_close"), target_day, start, end, item["probability"], sha256_text("phase2_heuristic_baseline"), sha256_text("phase2_protocol_invalidations"), sha256_text("phase2_binary_close_rule"), item["model_version"], item["feature_snapshot_hash"], json.dumps(item["source_hashes"]), parse_dt(item["data_cutoff"]), parse_dt(item["created_at"]), sealed_at, eligible, reason, item["row_hash"]),
        )
        record_import(conn, "RESEARCH_SYSTEM_PHASE2", source_hash, "forecast", item["forecast_id"], item["row_hash"], "forecast", forecast_id, item.get("created_at"), item.get("sealed_at"), "IMPORTED_SHADOW", actor, reason)
        counts["forecast"] += 1
    for item in manifest.get("outcomes", []):
        forecast_id = ids.get(f"forecast:{item['forecast_id']}") or stable_uuid("phase2-forecast", item["forecast_id"])
        forecast = conn.execute(
            "SELECT * FROM ledger.forecast WHERE forecast_id=%s FOR UPDATE",
            (forecast_id,),
        ).fetchone()
        if not forecast:
            raise ValueError(f"phase2 outcome references unknown forecast: {item['forecast_id']}")
        source_id = stable_uuid("phase2-outcome-source", item["content_hash"])
        source_status = item["status"]
        if source_status not in {"PENDING_REVIEW", "APPROVED"}:
            raise ValueError(f"invalid phase2 outcome status for forecast {item['forecast_id']}: {source_status}")
        if source_status == "APPROVED" and (not item.get("reviewed_at") or not item.get("reviewed_by")):
            raise ValueError(f"approved phase2 outcome lacks review metadata: {item['forecast_id']}")
        if source_status == "PENDING_REVIEW" and (item.get("reviewed_at") or item.get("reviewed_by")):
            raise ValueError(f"pending phase2 outcome contains review metadata: {item['forecast_id']}")
        if item["outcome"] not in {0, 1}:
            raise ValueError(f"invalid phase2 binary outcome for forecast {item['forecast_id']}: {item['outcome']}")
        existing_source = conn.execute(
            "SELECT * FROM ledger.source_record WHERE source_id=%s",
            (source_id,),
        ).fetchone()
        if existing_source:
            assert_phase2_source_matches(existing_source, item)
        else:
            conn.execute(
                """INSERT INTO ledger.source_record(source_id,source_type,source_locator,published_at,observed_at,data_cutoff,content_hash,captured_at,captured_by,metadata)
                   VALUES (%s,'PHASE2_PUBLIC_MARKET',%s,%s,%s,%s,%s,%s,%s,%s)""",
                (source_id, item["source_url"], parse_dt(item["source_as_of"]), parse_dt(item["source_as_of"]), parse_dt(item["source_as_of"]), item["content_hash"], now_utc(), actor, json.dumps({"imported": "phase2"})),
            )
        review_status = "REVIEWED" if source_status == "APPROVED" else "PENDING_REVIEW"
        existing_outcome = conn.execute(
            "SELECT * FROM ledger.outcome_observation WHERE forecast_id=%s ORDER BY revision_no DESC LIMIT 1",
            (forecast_id,),
        ).fetchone()
        if existing_outcome:
            assert_phase2_outcome_matches(existing_outcome, item, source_id)
            existing_status = existing_outcome["outcome_status"]
            if existing_status == review_status:
                outcome_id = existing_outcome["outcome_id"]
                outcome_rev = existing_outcome["revision_no"]
            elif existing_status == "PENDING_REVIEW" and review_status == "REVIEWED":
                outcome_rev = existing_outcome["revision_no"] + 1
                outcome_id = stable_uuid("phase2-outcome", f"{item['outcome_id']}#r{outcome_rev}")
            else:
                raise ValueError(
                    f"invalid phase2 outcome transition for forecast {item['forecast_id']}: {existing_status}->{review_status}"
                )
        else:
            outcome_rev = 1
            outcome_id = stable_uuid("phase2-outcome", f"{item['outcome_id']}#r{outcome_rev}")
        if not existing_outcome or outcome_id != existing_outcome["outcome_id"]:
            outcome_rev = (existing_outcome["revision_no"] + 1) if existing_outcome else 1
            conn.execute(
                """INSERT INTO ledger.outcome_observation(outcome_id,forecast_id,revision_no,outcome_status,observed_value,observed_at,source_id,input_hash,rule_version,recorded_at,recorded_by,prior_outcome_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'phase2-v1',%s,%s,%s)""",
                (outcome_id, forecast_id, outcome_rev, review_status, item["outcome"], parse_dt(item["source_as_of"]), source_id, item["content_hash"], parse_dt(item["settled_at"]), item["settler"], existing_outcome["outcome_id"] if existing_outcome else None),
            )
        if review_status == "REVIEWED":
            score, rationale = research_correctness(forecast, float(item["outcome"]))
            rationale_hash = sha256_json(rationale)
            existing_settle = conn.execute(
                "SELECT * FROM ledger.settlement WHERE forecast_id=%s ORDER BY revision_no DESC LIMIT 1",
                (forecast_id,),
            ).fetchone()
            if existing_settle and existing_settle["settlement_status"] == "REVIEWED":
                assert_phase2_settlement_matches(existing_settle, outcome_id, score, rationale_hash, item)
            else:
                if existing_settle and existing_settle["settlement_status"] != "PENDING_REVIEW":
                    raise ValueError(
                        f"invalid phase2 settlement transition for forecast {item['forecast_id']}: {existing_settle['settlement_status']}->REVIEWED"
                    )
                settle_rev = (existing_settle["revision_no"] + 1) if existing_settle else 1
                settlement_id = stable_uuid("phase2-settlement", f"{item['outcome_id']}#r{settle_rev}")
                conn.execute(
                    """INSERT INTO ledger.settlement(settlement_id,forecast_id,outcome_id,revision_no,settlement_status,research_correctness_score,investment_return_status,settlement_hash,settled_at,reviewed_at,reviewed_by,prior_settlement_id)
                       VALUES (%s,%s,%s,%s,'REVIEWED',%s,'NOT_APPLICABLE',%s,%s,%s,%s,%s)""",
                    (settlement_id, forecast_id, outcome_id, settle_rev, score, rationale_hash, parse_dt(item["settled_at"]), parse_dt(item.get("reviewed_at")), item.get("reviewed_by"), existing_settle["settlement_id"] if existing_settle else None),
                )
                conn.execute(
                    """INSERT INTO ledger.evaluation(evaluation_id,settlement_id,evaluation_type,score,rationale_hash,rule_version,evaluated_at,evaluated_by)
                       VALUES (%s,%s,'RESEARCH_CORRECTNESS',%s,%s,'phase2-v1',%s,%s) ON CONFLICT (settlement_id,evaluation_type) DO NOTHING""",
                    (stable_uuid("phase2-evaluation", f"{item['outcome_id']}#r{settle_rev}"), settlement_id, score, rationale_hash, now_utc(), actor),
                )
        record_import(conn, "RESEARCH_SYSTEM_PHASE2", source_hash, "outcome", item["outcome_id"], item["row_hash"], "outcome_observation", outcome_id, item.get("settled_at"), item.get("reviewed_at"), "IMPORTED_SHADOW", actor)
        counts["outcome"] += 1

    for item in manifest.get("risk_vetoes", []):
        veto_id = stable_uuid("phase2-veto", item["veto_id"])
        batch_id = ids.get(f"batch:{item['batch_id']}") or stable_uuid("phase2-batch", item["batch_id"])
        case_id = stable_uuid("phase2-veto-case", item["batch_id"])
        conn.execute(
            """INSERT INTO ledger.research_case(case_id,external_key,title,status,classification,created_at,created_by,content_hash)
               VALUES (%s,%s,'Phase2 batch governance shadow','RISK_VETOED','FACT',%s,%s,%s) ON CONFLICT (case_id) DO NOTHING""",
            (case_id, f"phase2-veto:{item['batch_id']}", parse_dt(item["created_at"]), actor, item["evidence_hash"]),
        )
        round_id = stable_uuid("phase2-round", item["batch_id"])
        conn.execute(
            """INSERT INTO ledger.review_round(round_id,case_id,status,deadline_at,created_at,created_by,round_hash)
               VALUES (%s,%s,'CLOSED',%s,%s,%s,%s) ON CONFLICT (round_id) DO NOTHING""",
            (round_id, case_id, parse_dt(item["created_at"]), parse_dt(item["created_at"]), actor, sha256_json({"phase2_batch": item["batch_id"]})),
        )
        conn.execute(
            """INSERT INTO ledger.risk_decision(risk_decision_id,round_id,case_id,decision,reason_hash,evidence_gap_code,decided_at,actor_id)
               VALUES (%s,%s,%s,'VETO',%s,'IMPORTED_PHASE2_VETO',%s,%s) ON CONFLICT (risk_decision_id) DO NOTHING""",
            (veto_id, round_id, case_id, item["reason_hash"], parse_dt(item["created_at"]), item["reviewer"]),
        )
        record_import(conn, "RESEARCH_SYSTEM_PHASE2", source_hash, "risk_veto", item["veto_id"], item["row_hash"], "risk_decision", veto_id, item.get("created_at"), item.get("overridden_at"), "IMPORTED_SHADOW", actor)
        counts["risk_veto"] += 1
    add_audit(conn, actor, "PHASE2_SHADOW_IMPORT", "forecast_batch", None, dict(counts))
    return dict(counts)


def create_model_change_proposal(conn: psycopg.Connection, model_artifact_id: uuid.UUID, candidate_hash: str, champion_hash: str, rationale_hash: str, actor: str) -> uuid.UUID:
    artifact = conn.execute("SELECT status FROM ledger.model_artifact WHERE model_artifact_id=%s", (model_artifact_id,)).fetchone()
    if not artifact:
        raise ValueError("model artifact not registered")
    proposal_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.model_change_proposal(proposal_id,model_artifact_id,status,candidate_hash,champion_hash,created_at,created_by,rationale_hash)
           VALUES (%s,%s,'AWAITING_USER_REPLAY_APPROVAL',%s,%s,%s,%s,%s)""",
        (proposal_id, model_artifact_id, candidate_hash, champion_hash, now_utc(), actor, rationale_hash),
    )
    add_audit(conn, actor, "MODEL_CHANGE_PROPOSAL", "model_change_proposal", proposal_id, {"model_artifact_id": str(model_artifact_id), "candidate_hash": candidate_hash, "champion_hash": champion_hash})
    return proposal_id


def record_human_replay_approval(conn: psycopg.Connection, proposal_id: uuid.UUID, actor: str, reason_hash: str, decision: str = "APPROVED") -> uuid.UUID:
    if decision not in {"APPROVED", "REJECTED"}:
        raise ValueError("invalid human decision")
    proposal = conn.execute("SELECT status FROM ledger.model_change_proposal WHERE proposal_id=%s", (proposal_id,)).fetchone()
    if not proposal or proposal["status"] != "AWAITING_USER_REPLAY_APPROVAL":
        raise ValueError("proposal is not awaiting replay approval")
    approval_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.human_approval(approval_id,object_type,object_id,action,decision,reason_hash,actor_id,approved_at)
           VALUES (%s,'model_change_proposal',%s,'REPLAY_APPROVE',%s,%s,%s,%s)""",
        (approval_id, proposal_id, decision, reason_hash, actor, now_utc()),
    )
    add_audit(conn, actor, "HUMAN_REPLAY_APPROVAL", "human_approval", approval_id, {"proposal_id": str(proposal_id), "decision": decision})
    return approval_id


def run_isolated_replay(conn: psycopg.Connection, proposal_id: uuid.UUID, cutoff_at: str, split_hash: str, actor: str) -> uuid.UUID:
    cutoff = parse_dt(cutoff_at)
    if cutoff is None or cutoff > now_utc():
        raise ValueError("replay cutoff must not be in the future")
    proposal = conn.execute("SELECT * FROM ledger.model_change_proposal WHERE proposal_id=%s", (proposal_id,)).fetchone()
    approval = conn.execute("SELECT decision FROM ledger.human_approval WHERE object_type='model_change_proposal' AND object_id=%s AND action='REPLAY_APPROVE' ORDER BY approved_at DESC LIMIT 1", (proposal_id,)).fetchone()
    if not proposal or not approval or approval["decision"] != "APPROVED":
        raise PermissionError("explicit human replay approval is required")
    formal = calibration_status(conn)
    replay_id = uuid.uuid4()
    status = "INSUFFICIENT" if formal["status"] == "INSUFFICIENT_PROSPECTIVE_SAMPLES" else "COMPLETED"
    result_hash = sha256_json({"cutoff_at": iso(cutoff), "split_hash": split_hash, "formal": formal, "status": status})
    conn.execute(
        """INSERT INTO ledger.replay_run(replay_id,proposal_id,cutoff_at,split_hash,champion_hash,candidate_hash,formal_snapshot_hash,status,result_hash,created_at,completed_at,created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (replay_id, proposal_id, cutoff, split_hash, proposal["champion_hash"], proposal["candidate_hash"], sha256_json(formal), status, result_hash, now_utc(), now_utc(), actor),
    )
    add_audit(conn, actor, "ISOLATED_REPLAY", "replay_run", replay_id, {"proposal_id": str(proposal_id), "status": status, "cutoff_at": iso(cutoff)})
    return replay_id


def promote_candidate_model(*_args: Any, **_kwargs: Any) -> None:
    raise PermissionError("candidate promotion is not available to code, agents, workers, or timers; use the P4 human runbook")


def import_model_manifest(conn: psycopg.Connection, manifest: dict[str, Any], actor: str = "model_shadow_registrar") -> dict[str, int]:
    count = 0
    for item in manifest.get("artifacts", []):
        source_system = item["source_system"]
        role = "SCENARIO" if source_system == "KRONOS" else "EXPERIMENT"
        status = "SCENARIO_ONLY" if source_system == "KRONOS" else "REGISTERED"
        artifact_id = stable_uuid("model-artifact", source_system, item["artifact_hash"])
        conn.execute(
            """INSERT INTO ledger.model_artifact(model_artifact_id,source_system,artifact_uri,artifact_hash,artifact_role,data_cutoff,universe_hash,feature_hash,label_hash,code_hash,config_hash,random_seed,split_hash,registered_at,registered_by,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source_system,artifact_hash) DO NOTHING""",
            (artifact_id, source_system, item["artifact_uri"], item["artifact_hash"], role, parse_dt(item.get("data_cutoff")), item["universe_hash"], item["feature_hash"], item["label_hash"], item["code_hash"], item["config_hash"], item.get("random_seed"), item["split_hash"], now_utc(), actor, status),
        )
        count += 1
    add_audit(conn, actor, "MODEL_SHADOW_REGISTER", "model_artifact", None, {"count": count})
    return {"registered": count}


def count_summary(conn: psycopg.Connection) -> dict[str, int]:
    tables = [
        "research_case", "research_snapshot", "evidence_snapshot", "review_round", "review_submission", "risk_decision", "forecast_protocol", "forecast_batch", "forecast", "outcome_observation", "settlement", "evaluation", "calibration_snapshot", "calibration_suggestion", "model_artifact", "model_change_proposal", "human_approval", "replay_run", "delivery_event", "audit_event", "import_ledger",
    ]
    return {table: int(conn.execute(f"SELECT count(*) AS n FROM ledger.{table}").fetchone()["n"]) for table in tables}


class LedgerHandler(BaseHTTPRequestHandler):
    server_version = "ResearchLedger/0.1"

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 131072:
            raise ValueError("payload too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _capability(self) -> str | None:
        return self.headers.get("X-Ledger-Capability")

    def do_GET(self) -> None:
        try:
            with connect() as conn:
                if self.path == "/health":
                    migration = conn.execute("SELECT version,artifact_hash,applied_at FROM ledger.schema_migration ORDER BY applied_at DESC LIMIT 1").fetchone()
                    self._json(HTTPStatus.OK, {"status": "ok", "version": APP_VERSION, "migration": migration})
                    return
                if self.path == "/v1/calibration/status":
                    self._json(HTTPStatus.OK, calibration_status(conn))
                    return
                if self.path.startswith("/v1/review-rounds/") and self.path.endswith("/submissions"):
                    round_id = uuid.UUID(self.path.split("/")[3])
                    self._json(HTTPStatus.OK, {"submissions": list_visible_reviews(conn, self._capability(), round_id)})
                    return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except PermissionError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": type(exc).__name__})

    def do_POST(self) -> None:
        try:
            payload = self._body()
            with connect() as conn:
                if self.path == "/v1/cases":
                    cap = require_admin(conn, self._capability())
                    case_id = create_case(conn, payload["title"], payload.get("classification", "MIXED"), cap["actor_id"], payload.get("data_cutoff"))
                    self._json(HTTPStatus.CREATED, {"case_id": str(case_id)})
                    return
                if self.path == "/v1/review-rounds":
                    cap = require_admin(conn, self._capability())
                    round_id = create_review_round(conn, uuid.UUID(payload["case_id"]), payload["deadline_at"], cap["actor_id"])
                    self._json(HTTPStatus.CREATED, {"round_id": str(round_id)})
                    return
                if self.path == "/v1/review-submissions":
                    round_id = uuid.UUID(payload["round_id"])
                    submission_id = submit_review(conn, self._capability() or "", round_id, payload["role"], payload["content_hash"], payload.get("actor_id", "capability_actor"), payload.get("classification", "MIXED"))
                    self._json(HTTPStatus.CREATED, {"submission_id": str(submission_id)})
                    return
                if self.path == "/v1/risk-vetoes":
                    veto_id = risk_veto(conn, self._capability() or "", uuid.UUID(payload["round_id"]), payload["reason_hash"], payload.get("actor_id", "risk_officer"))
                    self._json(HTTPStatus.CREATED, {"risk_decision_id": str(veto_id)})
                    return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except PermissionError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": type(exc).__name__})

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), LedgerHandler)
    httpd.serve_forever()


def load_manifest(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def command(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        apply_migration(args.database)
        print(json.dumps({"migration": MIGRATION_VERSION, "status": "applied_or_verified"}))
        return 0
    if args.command == "health":
        with connect(args.database) as conn:
            print(json.dumps({"status": "ok", "counts": count_summary(conn), "calibration": calibration_status(conn)}, default=str))
        return 0
    if args.command == "import-legacy":
        with connect(args.database) as conn:
            result = import_legacy_manifest(conn, load_manifest(args.manifest))
        print(json.dumps(result))
        return 0
    if args.command == "import-astock":
        with connect(args.database) as conn:
            result = import_astock_manifest(conn, load_manifest(args.manifest))
        print(json.dumps(result))
        return 0
    if args.command == "import-phase2":
        with connect(args.database) as conn:
            result = import_phase2_manifest(conn, load_manifest(args.manifest))
            write_calibration_snapshot(conn)
        print(json.dumps(result))
        return 0
    if args.command == "import-models":
        with connect(args.database) as conn:
            result = import_model_manifest(conn, load_manifest(args.manifest))
        print(json.dumps(result))
        return 0
    if args.command == "calibration":
        with connect(args.database) as conn:
            result = calibration_status(conn)
            write_calibration_snapshot(conn)
        print(json.dumps(result))
        return 0
    if args.command == "serve":
        serve(args.host, args.port)
        return 0
    raise ValueError("unsupported command")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("health")
    for name in ("import-legacy", "import-astock", "import-phase2", "import-models"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", required=True)
    sub.add_parser("calibration")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=18769)
    return command(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
