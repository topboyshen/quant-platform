"""M8.1 morning-chain snapshot: stage readers, freeze, redaction."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.morning.chain import import_morning_chain

DAY = dt.date(2026, 11, 13)
CHAT_ID = "6325485529"


def write_roots(tmp_path: Path, *, status: str = "failed", chat_id: str = CHAT_ID) -> dict:
    astock = tmp_path / "astock"
    bus = tmp_path / "bus"
    state = tmp_path / "state"
    staging = astock / "work/cninfo-staging"
    staging.mkdir(parents=True, exist_ok=True)
    staging_payload = {"targetDate": DAY.isoformat(), "batchCompletedAt": "2026-11-13T07:50:03+08:00",
                       "collectorSourceSha256": "c" * 64,
                       "codes": {"600000": {"queriedAt": "2026-11-13T07:50:02+08:00", "rows": []}},
                       "documents": {"600000": {"title": "公告"}}}
    import hashlib as _h
    canonical = json.dumps(staging_payload, ensure_ascii=False, allow_nan=False,
                           sort_keys=True, separators=(",", ":")).encode("utf-8")
    staging_doc = {**staging_payload, "schemaVersion": 2,
                   "payloadSha256": _h.sha256(canonical).hexdigest()}
    (staging / f"{DAY}.json").write_text(json.dumps(staging_doc), encoding="utf-8")
    (astock / "work/orchestrator/states").mkdir(parents=True, exist_ok=True)
    (astock / "work/orchestrator/states" / f"morning-report-{DAY}.json").write_text(json.dumps({
        "status": status, "targetDate": DAY.isoformat(), "childExitCode": 1,
        "startedAt": "2026-11-13T00:00:02Z", "completedAt": "2026-11-13T00:00:11Z",
        "task": "morning-report", "invocationId": "inv-1"}), encoding="utf-8")
    seal_dir = astock / "work/research-seal" / DAY.isoformat()
    seal_dir.mkdir(parents=True, exist_ok=True)
    seal_sha = "a" * 64
    (seal_dir / f"{seal_sha}.json").write_text(json.dumps({
        "state": "SEALED", "kind": "astock-source", "schemaVersion": 1,
        "targetDate": DAY.isoformat(), "generatedAt": "2026-11-13T08:00:03+08:00"}),
        encoding="utf-8")
    (seal_dir / f"{seal_sha}.digest.json").write_text(json.dumps({
        "schemaVersion": "astock.research-digest.v1", "kind": "research_digest",
        "reportSha256": seal_sha, "reportDate": DAY.isoformat(),
        "dataDate": "2026-11-12", "inputSha256": "d" * 64, "selectionSha256": "e" * 64,
        "sourceIndex": [{"sourceId": "s1"}, {"sourceId": "s2"}], "limitations": []}),
        encoding="utf-8")
    (bus / "out").mkdir(parents=True, exist_ok=True)
    text = "morning body"
    (bus / "out/relay-attempts").mkdir(parents=True, exist_ok=True)
    (bus / "out/relay-delivery").mkdir(parents=True, exist_ok=True)
    (bus / "out/relay-attempts" / f"{DAY}.json").write_text(json.dumps({
        "target_date": DAY.isoformat(), "status": "held", "reason": "timestamp_invalid",
        "sent": False, "observedAt": "2026-11-13T08:05:00+08:00"}), encoding="utf-8")
    (bus / "out/relay-delivery" / f"{DAY}.json").write_text(json.dumps({
        "target_date": DAY.isoformat(), "status": "confirmed", "reason": "accepted",
        "payload_sha256": "f" * 64, "report_sha256": seal_sha, "content_match": True,
        "attempted_at": "2026-11-13T08:05:01+08:00"}), encoding="utf-8")
    (bus / "out/research-digest-latest.json").write_text(json.dumps({
        "target_date": DAY.isoformat(), "produced_at": "2026-11-13T08:05:00+08:00",
        "digest_type": "research", "health": "ok", "freshness": "fresh",
        "plain_text": text}), encoding="utf-8")
    state.mkdir(parents=True, exist_ok=True)
    (state / f"research-chain-alert-{DAY}.json").write_text(json.dumps({
        "businessAtoms": [f"{DAY}|generation_failed|", f"{DAY}|relay_blocked|timestamp_invalid"],
        "transition": {"added": [f"{DAY}|generation_failed|", f"{DAY}|relay_blocked|timestamp_invalid"],
                       "recovered": [], "sequence": 1},
        "incident": {"causes": [{"code": "generation_failed", "detail": "生成失败", "reason": ""}],
                     "first_seen": "2026-11-13T08:00:11+08:00",
                     "last_seen": "2026-11-13T08:15:01+08:00",
                     "notified_at": "2026-11-13T08:00:11+08:00",
                     "delivery_wording": "尚未确认（无投递回执）",
                     "attempts": [{"evidence": {"chat_id": chat_id}}]},
        "transitionCount": 1, "last_observed_at": "2026-11-13T08:15:01+08:00"}),
        encoding="utf-8")
    return {"astock": astock, "bus": bus, "state": state}


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (ReportReceipt, ReportDelivery, ReportSource, Report):
            session.execute(delete(model))
        session.commit()


def test_snapshot_freezes_chain_and_redacts_chat_id(engine, tmp_path: Path):
    cleanup(engine)
    try:
        roots = write_roots(tmp_path)
        with Session(engine) as session:
            report, created = import_morning_chain(session, day=DAY, astock_root=roots["astock"],
                                                   bus_root=roots["bus"], state_dir=roots["state"])
            session.commit()
            assert created is True and report.kind == "morning_chain"
            payload = report.payload
            assert payload["precollect"]["schema_version"] == 2
            assert payload["precollect"]["payload_sha256_ok"] is True
            assert payload["precollect"]["documents"] == 1
            assert payload["generation"]["status"] == "failed"
            assert payload["seal"]["state"] == "SEALED"
            assert payload["seal"]["digest_present"] is True
            assert payload["seal"]["report_sha_matches"] is True
            assert payload["seal"]["source_count"] == 2
            assert payload["relay"]["target_matches"] is True
            assert payload["relay"]["attempt"]["status"] == "held"
            assert payload["relay"]["delivery"]["status"] == "confirmed"
            assert payload["relay"]["plain_text_sha256"] == hashlib.sha256(b"morning body").hexdigest()
            assert payload["alerts"]["causes"][0]["code"] == "generation_failed"
            assert payload["classification"]["matches_legacy"] is True
            assert f"{DAY}|relay_blocked|timestamp_invalid" in payload["classification"]["expected_atoms"]
            dumped = json.dumps(payload, ensure_ascii=False)
            assert CHAT_ID not in dumped
            assert "chat_id" not in dumped
            assert "attempts" not in payload["alerts"]
            assert "message_id" not in dumped
            sources = session.query(ReportSource).filter_by(report_id=report.id).all()
            assert len(sources) == 5
    finally:
        cleanup(engine)


def test_precollect_tamper_is_detected(engine, tmp_path: Path):
    cleanup(engine)
    try:
        roots = write_roots(tmp_path)
        staging = roots["astock"] / "work/cninfo-staging" / f"{DAY}.json"
        doc = json.loads(staging.read_text(encoding="utf-8"))
        doc["documents"] = {"600000": {"title": "changed"}}
        staging.write_text(json.dumps(doc), encoding="utf-8")
        with Session(engine) as session:
            report, _ = import_morning_chain(session, day=DAY, astock_root=roots["astock"],
                                             bus_root=roots["bus"], state_dir=roots["state"])
            session.commit()
            assert report.payload["precollect"]["payload_sha256_ok"] is False
    finally:
        cleanup(engine)


def test_seal_digest_mismatch_is_detected(engine, tmp_path: Path):
    cleanup(engine)
    try:
        roots = write_roots(tmp_path)
        seal_dir = roots["astock"] / "work/research-seal" / DAY.isoformat()
        digest_path = next(seal_dir.glob("*.digest.json"))
        digest = json.loads(digest_path.read_text(encoding="utf-8"))
        digest["reportSha256"] = "b" * 64
        digest_path.write_text(json.dumps(digest), encoding="utf-8")
        with Session(engine) as session:
            report, _ = import_morning_chain(session, day=DAY, astock_root=roots["astock"],
                                             bus_root=roots["bus"], state_dir=roots["state"])
            session.commit()
            assert report.payload["seal"]["report_sha_matches"] is False
    finally:
        cleanup(engine)


def test_retry_bumps_version(engine, tmp_path: Path):
    cleanup(engine)
    try:
        roots = write_roots(tmp_path)
        with Session(engine) as session:
            first, _ = import_morning_chain(session, day=DAY, astock_root=roots["astock"],
                                                   bus_root=roots["bus"], state_dir=roots["state"])
            session.commit()
            assert first.version == 1
            write_roots(tmp_path, status="succeeded")
            second, created = import_morning_chain(session, day=DAY, astock_root=roots["astock"],
                                                   bus_root=roots["bus"], state_dir=roots["state"])
            session.commit()
            assert created is True and second.version == 2
            assert second.payload["generation"]["status"] == "succeeded"
    finally:
        cleanup(engine)


def test_job_skips_without_artifacts(engine, tmp_path: Path, monkeypatch):
    cleanup(engine)
    try:
        monkeypatch.setenv("QP_REPORT_DATE", DAY.isoformat())
        monkeypatch.setenv("QP_MORNING_ASTOCK_ROOT", str(tmp_path / "none"))
        monkeypatch.setenv("QP_SIGNAL_BUS_ROOT", str(tmp_path / "none2"))
        monkeypatch.setenv("QP_SIGNAL_BUS_STATE", str(tmp_path / "none3"))
        from qp.jobs.morning import import_morning_chain_job
        result = import_morning_chain_job(JobContext(engine=engine, run_id=0,
                                                     name="reports.import_morning_chain"))
        assert result["skipped"]
    finally:
        cleanup(engine)
