#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import ledger_app as app  # noqa: E402

if Path(app.__file__).resolve() != (ROOT / "app" / "ledger_app.py").resolve():
    raise RuntimeError("test imported ledger_app from the wrong project root")


def hashed(value: str) -> str:
    return app.sha256_text(value)


def make_manifest(prefix: str, status: str, **changes):
    proto_hash = hashed(f"{prefix}-protocol")
    reviewed = status == "APPROVED"
    outcome = {
        "outcome_id": f"{prefix}-o1",
        "forecast_id": f"{prefix}-f1",
        "source_url": f"https://example.invalid/{prefix}/outcome",
        "source_as_of": "2026-08-14T15:05:00+08:00",
        "content_hash": hashed(f"{prefix}-outcome-content"),
        "outcome": 1,
        "settled_at": "2026-08-14T15:05:30+08:00",
        "status": status,
        "settler": "phase2-settler",
        "reviewed_at": "2026-08-14T16:00:00+08:00" if reviewed else None,
        "reviewed_by": "phase2-reviewer" if reviewed else None,
        "review_hash": hashed(f"{prefix}-review") if reviewed else None,
    }
    outcome.update(changes)
    outcome["row_hash"] = app.sha256_json(outcome)
    return {
        "source_database_hash": app.sha256_json({"prefix": prefix, "outcome": outcome}),
        "protocols": [{
            "protocol_hash": proto_hash,
            "version": "1",
            "universe_rule_hash": hashed(f"{prefix}-universe"),
            "target_definition_hash": hashed(f"{prefix}-target"),
            "source_policy_hash": hashed(f"{prefix}-source-policy"),
            "frozen_at": "2026-08-01T00:00:00+08:00",
            "created_at": "2026-08-01T00:00:00+08:00",
            "row_hash": hashed(f"{prefix}-protocol-row"),
        }],
        "batches": [{
            "batch_id": f"{prefix}-b1",
            "protocol_hash": proto_hash,
            "cohort_id": f"{prefix}-cohort",
            "target_session": "2026-08-14",
            "status": "SEALED",
            "expected_n": 1,
            "qualified_n": 1,
            "created_at": "2026-08-13T15:00:00+08:00",
            "sealed_at": "2026-08-13T16:00:00+08:00",
            "row_hash": hashed(f"{prefix}-batch-row"),
        }],
        "forecasts": [{
            "forecast_id": f"{prefix}-f1",
            "batch_id": f"{prefix}-b1",
            "symbol": "000001",
            "target_session": "2026-08-14",
            "status": "SEALED",
            "probability": 0.6,
            "model_version": "test-model-v1",
            "feature_snapshot_hash": hashed(f"{prefix}-features"),
            "source_hashes": [hashed(f"{prefix}-forecast-source")],
            "data_cutoff": "2026-08-13T15:00:00+08:00",
            "created_at": "2026-08-13T15:30:00+08:00",
            "sealed_at": "2026-08-13T16:00:00+08:00",
            "row_hash": hashed(f"{prefix}-forecast-row"),
        }],
        "outcomes": [outcome],
    }


def forecast_id(prefix: str):
    return app.stable_uuid("phase2-forecast", f"{prefix}-f1")


def table_counts(conn):
    names = ("source_record", "outcome_observation", "settlement", "evaluation", "import_ledger", "audit_event")
    return {
        name: conn.execute(f"SELECT count(*) AS n FROM ledger.{name}").fetchone()["n"]
        for name in names
    }


def expect_rejected(conn, payload, message: str):
    before = table_counts(conn)
    conn.execute("SAVEPOINT expected_rejection")
    try:
        app.import_phase2_manifest(conn, payload, "revision-test")
    except (ValueError, RuntimeError):
        conn.execute("ROLLBACK TO SAVEPOINT expected_rejection")
        conn.execute("RELEASE SAVEPOINT expected_rejection")
    else:
        conn.execute("ROLLBACK TO SAVEPOINT expected_rejection")
        conn.execute("RELEASE SAVEPOINT expected_rejection")
        raise AssertionError(message)
    assert table_counts(conn) == before, f"rejected import left side effects: {message}"


def outcome_rows(conn, prefix: str):
    return list(conn.execute(
        "SELECT * FROM ledger.outcome_observation WHERE forecast_id=%s ORDER BY revision_no",
        (forecast_id(prefix),),
    ))


def settlement_rows(conn, prefix: str):
    return list(conn.execute(
        "SELECT * FROM ledger.settlement WHERE forecast_id=%s ORDER BY revision_no",
        (forecast_id(prefix),),
    ))


def insert_cloned_outcome(conn, prefix: str, status: str):
    previous = outcome_rows(conn, prefix)[-1]
    revision = previous["revision_no"] + 1
    outcome_id = app.stable_uuid("phase2-outcome", f"{prefix}-o1#r{revision}")
    conn.execute(
        """INSERT INTO ledger.outcome_observation(
             outcome_id,forecast_id,revision_no,outcome_status,observed_value,observed_at,
             source_id,input_hash,rule_version,recorded_at,recorded_by,prior_outcome_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            outcome_id, previous["forecast_id"], revision, status,
            previous["observed_value"], previous["observed_at"], previous["source_id"],
            previous["input_hash"], previous["rule_version"], previous["recorded_at"],
            previous["recorded_by"], previous["outcome_id"],
        ),
    )
    return outcome_id


def insert_settlement(conn, prefix: str, outcome_id, status: str):
    prior = settlement_rows(conn, prefix)[-1]
    revision = prior["revision_no"] + 1
    settlement_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO ledger.settlement(
             settlement_id,forecast_id,outcome_id,revision_no,settlement_status,
             research_correctness_score,investment_return_status,settlement_hash,
             settled_at,reviewed_at,reviewed_by,prior_settlement_id)
           VALUES (%s,%s,%s,%s,%s,%s,'NOT_APPLICABLE',%s,%s,%s,%s,%s)""",
        (
            settlement_id, prior["forecast_id"], outcome_id, revision, status,
            prior["research_correctness_score"], prior["settlement_hash"], prior["settled_at"],
            prior["reviewed_at"] if status == "REVIEWED" else None,
            prior["reviewed_by"] if status == "REVIEWED" else None, prior["settlement_id"],
        ),
    )
    return settlement_id


def main() -> int:
    app.apply_migration()
    threshold_rows = [
        {
            "batch_id": f"retry-{index}",
            "independent_session": f"2026-07-{(index % 10) + 1:02d}",
            "probability": ((index % 10) + 0.5) / 10,
            "observed_value": index % 2,
        }
        for index in range(300)
    ]
    raw_threshold = app.calibration_cohort(threshold_rows, formal_allowed=False)
    reviewed_threshold = app.calibration_cohort(threshold_rows, formal_allowed=True)
    assert raw_threshold["sample_size_threshold_met"] is True
    assert raw_threshold["formal_threshold_met"] is False
    assert raw_threshold["status"] == "DIAGNOSTIC_ONLY"
    assert raw_threshold["brier"] is None and raw_threshold["ece"] is None and raw_threshold["bins"] == []
    assert reviewed_threshold["status"] == "STABLE" and reviewed_threshold["formal_threshold_met"] is True
    assert reviewed_threshold["brier"] is not None and reviewed_threshold["ece"] is not None
    with app.connect() as conn:
        initial_calibration = app.calibration_status(conn)
        pending = make_manifest("main", "PENDING_REVIEW")
        approved = make_manifest("main", "APPROVED")
        app.import_phase2_manifest(conn, pending, "revision-test")
        assert [(r["revision_no"], r["outcome_status"]) for r in outcome_rows(conn, "main")] == [(1, "PENDING_REVIEW")]
        assert settlement_rows(conn, "main") == []
        pending_calibration = app.calibration_status(conn)
        assert pending_calibration["raw_cohort"]["due_forecast_count"] == initial_calibration["raw_cohort"]["due_forecast_count"] + 1
        assert pending_calibration["raw_cohort"]["observed_forecast_count"] == initial_calibration["raw_cohort"]["observed_forecast_count"] + 1
        assert pending_calibration["pending_review_count"] == initial_calibration["pending_review_count"] + 1
        assert pending_calibration["eligible_forecast_count"] == initial_calibration["eligible_forecast_count"]
        assert pending_calibration["maturity_reason_code"] == "AWAITING_OUTCOME_REVIEW"
        assert pending_calibration["raw_cohort"]["diagnostic_metrics"]["status"] == "DIAGNOSTIC_ONLY"
        assert pending_calibration["raw_cohort"]["diagnostic_metrics"]["brier"] is None
        assert pending_calibration["raw_cohort"]["diagnostic_metrics"]["ece"] is None
        assert pending_calibration["raw_cohort"]["diagnostic_metrics"]["bins"] == []
        app.import_phase2_manifest(conn, copy.deepcopy(pending), "revision-test")
        assert len(outcome_rows(conn, "main")) == 1
        app.import_phase2_manifest(conn, approved, "revision-test")
        outcomes = outcome_rows(conn, "main")
        settlements = settlement_rows(conn, "main")
        assert [(r["revision_no"], r["outcome_status"]) for r in outcomes] == [(1, "PENDING_REVIEW"), (2, "REVIEWED")]
        assert outcomes[1]["prior_outcome_id"] == outcomes[0]["outcome_id"]
        assert len(settlements) == 1 and settlements[0]["outcome_id"] == outcomes[1]["outcome_id"]
        assert conn.execute("SELECT count(*) AS n FROM ledger.evaluation WHERE settlement_id=%s", (settlements[0]["settlement_id"],)).fetchone()["n"] == 1
        reviewed_calibration = app.calibration_status(conn)
        assert reviewed_calibration["raw_cohort"]["due_forecast_count"] == pending_calibration["raw_cohort"]["due_forecast_count"]
        assert reviewed_calibration["eligible_forecast_count"] == initial_calibration["eligible_forecast_count"] + 1
        assert reviewed_calibration["coverage"]["reviewed_settled_count"] == initial_calibration["eligible_forecast_count"] + 1
        assert reviewed_calibration["review_coverage_rate"] == 1.0
        app.import_phase2_manifest(conn, copy.deepcopy(approved), "revision-test")
        assert len(outcome_rows(conn, "main")) == 2 and len(settlement_rows(conn, "main")) == 1

        no_outcome = make_manifest("no-outcome", "PENDING_REVIEW")
        no_outcome["outcomes"] = []
        app.import_phase2_manifest(conn, no_outcome, "revision-test")
        missing_calibration = app.calibration_status(conn)
        assert missing_calibration["raw_cohort"]["due_forecast_count"] == reviewed_calibration["raw_cohort"]["due_forecast_count"] + 1
        assert missing_calibration["latest_outcome_status_counts"]["NO_OUTCOME"] == 1
        assert missing_calibration["coverage"]["outcome_over_raw"] < 1.0

        future = make_manifest("future", "PENDING_REVIEW")
        future["batches"][0]["target_session"] = "2099-08-14"
        future["forecasts"][0]["target_session"] = "2099-08-14"
        future["outcomes"] = []
        app.import_phase2_manifest(conn, future, "revision-test")
        assert app.calibration_status(conn)["raw_cohort"]["due_forecast_count"] == missing_calibration["raw_cohort"]["due_forecast_count"]

        vetoed = make_manifest("vetoed", "PENDING_REVIEW")
        vetoed["batches"][0]["status"] = "VETOED"
        vetoed["outcomes"] = []
        app.import_phase2_manifest(conn, vetoed, "revision-test")
        assert app.calibration_status(conn)["raw_cohort"]["due_forecast_count"] == missing_calibration["raw_cohort"]["due_forecast_count"]

        direct = make_manifest("direct", "APPROVED")
        app.import_phase2_manifest(conn, direct, "revision-test")
        assert [(r["revision_no"], r["outcome_status"]) for r in outcome_rows(conn, "direct")] == [(1, "REVIEWED")]
        assert len(settlement_rows(conn, "direct")) == 1
        app.import_phase2_manifest(conn, copy.deepcopy(direct), "revision-test")
        assert len(outcome_rows(conn, "direct")) == 1 and len(settlement_rows(conn, "direct")) == 1

        legacy_pending = make_manifest("legacy", "PENDING_REVIEW")
        legacy_seed = copy.deepcopy(legacy_pending)
        legacy_seed["outcomes"] = []
        app.import_phase2_manifest(conn, legacy_seed, "revision-test")
        legacy_item = legacy_pending["outcomes"][0]
        legacy_source_id = app.stable_uuid("phase2-outcome-source", legacy_item["content_hash"])
        source_time = app.parse_dt(legacy_item["source_as_of"])
        conn.execute(
            """INSERT INTO ledger.source_record(
                 source_id,source_type,source_locator,published_at,observed_at,data_cutoff,
                 content_hash,captured_at,captured_by,metadata)
               VALUES (%s,'PHASE2_PUBLIC_MARKET',%s,%s,%s,%s,%s,%s,'legacy-test','{}')""",
            (legacy_source_id, legacy_item["source_url"], source_time, source_time, source_time, legacy_item["content_hash"], app.now_utc()),
        )
        legacy_outcome_id = app.stable_uuid("phase2-outcome", legacy_item["outcome_id"])
        conn.execute(
            """INSERT INTO ledger.outcome_observation(
                 outcome_id,forecast_id,revision_no,outcome_status,observed_value,observed_at,
                 source_id,input_hash,rule_version,recorded_at,recorded_by)
               VALUES (%s,%s,1,'PENDING_REVIEW',%s,%s,%s,%s,'phase2-v1',%s,%s)""",
            (legacy_outcome_id, forecast_id("legacy"), legacy_item["outcome"], source_time, legacy_source_id, legacy_item["content_hash"], app.parse_dt(legacy_item["settled_at"]), legacy_item["settler"]),
        )
        app.import_phase2_manifest(conn, make_manifest("legacy", "APPROVED"), "revision-test")
        legacy_rows = outcome_rows(conn, "legacy")
        assert len(legacy_rows) == 2 and legacy_rows[1]["prior_outcome_id"] == legacy_outcome_id

        drift_base = make_manifest("drift", "PENDING_REVIEW")
        app.import_phase2_manifest(conn, drift_base, "revision-test")
        drift_cases = (
            {"outcome_id": "drift-other-outcome"}, {"outcome": 0},
            {"content_hash": hashed("drift-other-content")},
            {"source_url": "https://example.invalid/drift/other"},
            {"source_as_of": "2026-08-14T15:06:00+08:00"},
            {"settled_at": "2026-08-14T15:06:30+08:00"}, {"settler": "other-settler"},
        )
        for changes in drift_cases:
            expect_rejected(conn, make_manifest("drift", "PENDING_REVIEW", **changes), f"same-status drift was accepted: {changes}")
        expect_rejected(conn, make_manifest("drift", "APPROVED", outcome=0), "material drift during PENDING->APPROVED was accepted")
        expect_rejected(conn, make_manifest("direct", "PENDING_REVIEW"), "REVIEWED->PENDING was accepted")
        expect_rejected(conn, make_manifest("unknown", "REJECTED"), "unknown outcome status was accepted")
        expect_rejected(conn, make_manifest("invalid-pending", "PENDING_REVIEW", reviewed_at="2026-08-14T16:00:00+08:00", reviewed_by="x"), "pending outcome with review metadata was accepted")
        expect_rejected(conn, make_manifest("invalid-approved", "APPROVED", reviewed_at=None), "approved outcome without review metadata was accepted")
        expect_rejected(conn, make_manifest("invalid-value", "PENDING_REVIEW", outcome=2), "non-binary outcome was accepted")

        binding = make_manifest("binding", "APPROVED")
        app.import_phase2_manifest(conn, binding, "revision-test")
        insert_cloned_outcome(conn, "binding", "REVIEWED")
        count_without_binding = app.calibration_status(conn)["eligible_forecast_count"]
        expect_rejected(conn, copy.deepcopy(binding), "reviewed settlement bound to an older outcome was reused")
        assert app.calibration_status(conn)["eligible_forecast_count"] == count_without_binding

        pending_latest = make_manifest("cal-pending", "APPROVED")
        app.import_phase2_manifest(conn, pending_latest, "revision-test")
        count_before_pending = app.calibration_status(conn)["eligible_forecast_count"]
        insert_cloned_outcome(conn, "cal-pending", "PENDING_REVIEW")
        assert app.calibration_status(conn)["eligible_forecast_count"] == count_before_pending - 1

        pair = make_manifest("cal-pair", "APPROVED")
        app.import_phase2_manifest(conn, pair, "revision-test")
        count_with_pair = app.calibration_status(conn)["eligible_forecast_count"]
        reviewed_outcome = insert_cloned_outcome(conn, "cal-pair", "REVIEWED")
        assert app.calibration_status(conn)["eligible_forecast_count"] == count_with_pair - 1
        insert_settlement(conn, "cal-pair", reviewed_outcome, "PENDING_REVIEW")
        assert app.calibration_status(conn)["eligible_forecast_count"] == count_with_pair - 1
        insert_settlement(conn, "cal-pair", reviewed_outcome, "REVIEWED")
        assert app.calibration_status(conn)["eligible_forecast_count"] == count_with_pair
        assert app.calibration_status(conn)["eligible_forecast_count"] >= 3

        snapshots_before = conn.execute("SELECT count(*) AS n FROM ledger.calibration_snapshot").fetchone()["n"]
        snapshot_one = app.write_calibration_snapshot(conn, actor="revision-test")
        snapshot_two = app.write_calibration_snapshot(conn, actor="revision-test")
        snapshots_after = conn.execute("SELECT count(*) AS n FROM ledger.calibration_snapshot").fetchone()["n"]
        persisted = conn.execute(
            "SELECT raw_due_forecast_count,raw_due_independent_batch_count FROM ledger.calibration_snapshot WHERE calibration_snapshot_id=%s",
            (snapshot_one,),
        ).fetchone()
        final_calibration = app.calibration_status(conn)
        assert snapshot_one == snapshot_two and snapshots_after == snapshots_before + 1
        assert final_calibration["raw_cohort"]["independent_batch_count"] == 1
        assert final_calibration["reviewed_cohort"]["independent_batch_count"] == 1
        assert persisted["raw_due_forecast_count"] == final_calibration["raw_cohort"]["due_forecast_count"]
        assert persisted["raw_due_independent_batch_count"] == final_calibration["raw_cohort"]["independent_batch_count"]

    print("PHASE2_OUTCOME_REVISION_GUARDS=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
