#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from integration_guard import require_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import ledger_app as app  # noqa: E402


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def token(value: str) -> str:
    return f"test-{value}-capability"


def add_capability(conn, value: str, role: str, scope_id=None):
    conn.execute(
        """INSERT INTO ledger.capability(capability_id,capability_hash,actor_id,role_name,scope_type,scope_id,created_at,created_by)
           VALUES (%s,%s,%s,%s,'TEST',%s,%s,'integration_test')""",
        (uuid.uuid4(), app.capability_hash(token(value)), f"test-{role.lower()}", role, scope_id, app.now_utc()),
    )


def add_source(conn) -> uuid.UUID:
    source_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.source_record(source_id,source_type,source_locator,content_hash,captured_at,captured_by)
           VALUES (%s,'TEST','test://source',%s,%s,'integration_test')""",
        (source_id, digest(b"source"), app.now_utc()),
    )
    return source_id


def main() -> int:
    require_test_database()
    app.apply_migration()
    # P2 governance: independent blind roles, risk evidence-gap veto, and sealed snapshot immutability.
    with app.connect() as conn:
        add_capability(conn, "admin", "ADMIN")
        case_id = app.create_case(conn, "Integration governance case", "FACT", "test-admin")
        round_id = app.create_review_round(conn, case_id, app.iso(app.now_utc() + timedelta(hours=1)), "test-admin")
        add_capability(conn, "bull", "BULL", round_id)
        add_capability(conn, "bear", "BEAR", round_id)
        add_capability(conn, "risk", "RISK_OFFICER", round_id)
        app.submit_review(conn, token("bull"), round_id, "BULL", digest(b"bull"), "test-bull")
        assert app.list_visible_reviews(conn, token("bear"), round_id) == [], "blind round leaked bull review to bear"
        assert len(app.list_visible_reviews(conn, token("bull"), round_id)) == 1, "bull cannot read own blinded submission"
        app.submit_review(conn, token("bear"), round_id, "BEAR", digest(b"bear"), "test-bear")
        assert len(app.list_visible_reviews(conn, token("bull"), round_id)) == 1, "blind round leaked bear review to bull"
        veto_id = app.risk_veto(conn, token("risk"), round_id, digest(b"missing-primary-source"), "test-risk", "PRIMARY_SOURCE_MISSING")
        gap = conn.execute("SELECT evidence_gap_code FROM ledger.risk_decision WHERE risk_decision_id=%s", (veto_id,)).fetchone()["evidence_gap_code"]
        assert gap == "PRIMARY_SOURCE_MISSING", "risk veto did not retain evidence gap"
        snapshot_id = uuid.uuid4()
        conn.execute(
            """INSERT INTO ledger.research_snapshot(snapshot_id,case_id,version_no,snapshot_type,classification,sealed_at,storage_uri,content_hash,created_at,created_by)
               VALUES (%s,%s,1,'TEST_SEALED','FACT',%s,'file:///test/snapshot',%s,%s,'integration_test')""",
            (snapshot_id, case_id, app.now_utc(), digest(b"snapshot"), app.now_utc()),
        )
    with app.connect() as conn:
        try:
            conn.execute("UPDATE ledger.research_snapshot SET storage_uri='file:///tampered' WHERE snapshot_id=%s", (snapshot_id,))
            raise AssertionError("sealed snapshot was mutable")
        except Exception:
            conn.rollback()
    # P2 settlement: formal-eligible test fixture only, separate from production; duplicate writes fail closed.
    with app.connect() as conn:
        source_id = add_source(conn)
        start = app.now_utc() + timedelta(days=1)
        payload = {
            "forecast_type": "BINARY_EVENT", "status": "SEALED", "target_key": "TEST_EVENT", "metric_definition_hash": digest(b"metric"),
            "window_start": app.iso(start), "window_end": app.iso(start + timedelta(hours=5)), "probability": 0.2,
            "baseline_hash": digest(b"baseline"), "invalidation_hash": digest(b"invalid"), "settlement_rule_hash": digest(b"settle"),
            "model_version": "test-v1", "feature_snapshot_hash": digest(b"features"), "source_hashes": [digest(b"source")],
            "data_cutoff": app.iso(app.now_utc()), "created_at": app.iso(app.now_utc()), "sealed_at": app.iso(app.now_utc()),
        }
        forecast_id = app.register_forecast(conn, payload, "test-producer")
        settlement_id = app.settle_forecast(conn, forecast_id, 0.0, source_id, digest(b"outcome"), "test-settler", reviewed=True)
        score = conn.execute("SELECT research_correctness_score,investment_return_status FROM ledger.settlement WHERE settlement_id=%s", (settlement_id,)).fetchone()
        assert float(score["research_correctness_score"]) == 1.0 and score["investment_return_status"] == "NOT_APPLICABLE", "evaluation layers mixed"
        try:
            app.settle_forecast(conn, forecast_id, 0.0, source_id, digest(b"outcome2"), "test-settler", reviewed=True)
            raise AssertionError("duplicate settlement was allowed")
        except ValueError:
            pass
        calibration = app.calibration_status(conn)
        assert calibration["status"] == "INSUFFICIENT_PROSPECTIVE_SAMPLES", "insufficient formal sample was promoted"
        assert calibration["brier"] is None and calibration["ece"] is None and calibration["bins"] == [], "formal calibration metrics not blank under threshold"
        # Notification metadata fixture confirms no body/CAS artifact is required by Ledger import.
        notification_hash = digest(b"notification-body-never-in-cas")
        astock_manifest = {"manifest_hash": digest(b"astock-test"), "generated_at": app.iso(), "artifacts": [{"kind": "notification", "event_id": "test-event", "status": "sent", "artifact_hash": notification_hash, "relative_path_hash": digest(b"notifications/file"), "occurred_at": app.iso(), "bytes": 1, "entry_mode": "METADATA_ONLY"}]}
        imported = app.import_astock_manifest(conn, astock_manifest)
        assert imported["metadata"] == 1
        stored = conn.execute("SELECT payload_hash FROM ledger.delivery_event LIMIT 1").fetchone()
        assert stored["payload_hash"] == notification_hash
    # P3: register → proposal → user replay approval → isolated replay. Promotion has no code path.
    with app.connect() as conn:
        qlib_manifest = {"artifacts": [{"source_system": "QLIB", "artifact_uri": "file:///test/qlib", "artifact_hash": digest(b"qlib-artifact"), "data_cutoff": app.iso(), "universe_hash": "u", "feature_hash": "f", "label_hash": "l", "code_hash": "c", "config_hash": "cfg", "random_seed": "7", "split_hash": "s"}]}
        app.import_model_manifest(conn, qlib_manifest)
        artifact = conn.execute("SELECT model_artifact_id,status FROM ledger.model_artifact WHERE source_system='QLIB'").fetchone()
        assert artifact["status"] == "REGISTERED"
        proposal_id = app.create_model_change_proposal(conn, artifact["model_artifact_id"], digest(b"candidate"), digest(b"champion"), digest(b"rationale"), "test-model-registrar")
        try:
            app.run_isolated_replay(conn, proposal_id, app.iso(app.now_utc() - timedelta(minutes=1)), digest(b"split"), "test-replayer")
            raise AssertionError("replay ran without explicit human approval")
        except PermissionError:
            pass
        app.record_human_replay_approval(conn, proposal_id, "test-human", digest(b"approval-reason"))
        replay_id = app.run_isolated_replay(conn, proposal_id, app.iso(app.now_utc() - timedelta(minutes=1)), digest(b"split"), "test-replayer")
        replay = conn.execute("SELECT status FROM ledger.replay_run WHERE replay_id=%s", (replay_id,)).fetchone()
        assert replay["status"] == "INSUFFICIENT", "replay bypassed formal-sample gate"
        try:
            app.promote_candidate_model(proposal_id)
            raise AssertionError("candidate promotion path existed")
        except PermissionError:
            pass
        active = conn.execute("SELECT count(*) AS n FROM ledger.human_approval WHERE action='MODEL_ACTIVATE'").fetchone()["n"]
        assert active == 0, "test activated a model"
    print("p2_p3_isolated_integration_tests=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
