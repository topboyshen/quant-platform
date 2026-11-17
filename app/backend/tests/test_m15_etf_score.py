"""M15 read-only ETF research score shadow: import contract, isolation and API."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from qp.models.research import EtfScore, EtfScoreRun
from qp.models.signals import TargetDecision
from qp.research import etf_score
from qp.signals.universe import TARGETS

CODES = [t.code for t in TARGETS]


def payload(*, horizon: int = 20, version: str = "qp-etf-test-v1", cutoff: dt.date | None = None) -> dict:
    scores = []
    for i, code in enumerate(CODES):
        eligible = i < 8
        scores.append(
            {
                "code": code,
                "name": f"ETF-{code}",
                "score": 0.01 - i * 0.001 if eligible else None,
                "rank": i + 1 if eligible else None,
                "eligible": eligible,
                "reason": "" if eligible else "not_eligible",
            }
        )
    return {
        "model_version": version,
        "model_sha256": "a" * 64,
        "model_text_sha256": "b" * 64,
        "universe": "fixed_40_current_pool",
        "data_cutoff": (cutoff or dt.date.today() - dt.timedelta(days=1)).isoformat(),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "label_horizon": horizon,
        "train_start": "2020-01-01",
        "train_end": "2025-12-24",
        "data_master_snapshot": "etf-master-test",
        "metrics": {"cagr": 0.12, "sharpe": 0.7, "alpha_vs_ew20": 0.03, "alpha_t": 1.0},
        "notes": "research only",
        "scores": scores,
    }


def cleanup(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(EtfScore))
        s.execute(delete(EtfScoreRun))
        s.commit()


def test_tables_and_empty(engine):
    with engine.connect() as c:
        names = set(
            c.execute(text("select table_name from information_schema.tables where table_schema='research'")).scalars()
        )
    assert {"etf_score_run", "etf_score"} <= names
    with Session(engine) as s:
        out = etf_score.latest(s)
    assert out["read_only"] is True and out["is_trade_signal"] is False
    assert out["run"] is None and out["scores"] == []


def test_import_idempotent_and_read_only(engine):
    cleanup(engine)
    try:
        with Session(engine) as s:
            before = s.scalar(select(func.count()).select_from(TargetDecision)) or 0
            first = etf_score.import_payload(s, payload())
            s.commit()
            second = etf_score.import_payload(s, payload())
            s.commit()
            assert first["ok"] and first["eligible"] == 8
            assert second["skipped"] == "duplicate"
            assert second["content_sha256"] == first["content_sha256"]
            assert s.scalar(select(func.count()).select_from(EtfScoreRun)) == 1
            assert s.scalar(select(func.count()).select_from(EtfScore)) == len(CODES)
            assert (s.scalar(select(func.count()).select_from(TargetDecision)) or 0) == before
            out = etf_score.latest(s)
            assert out["run"]["model_version"] == "qp-etf-test-v1"
            assert out["scores"][0]["rank"] == 1
            assert len(out["history"]) == 1
    finally:
        cleanup(engine)


def test_validation_rejects_bad_payloads(engine):
    bad = payload()
    bad["scores"][0]["code"] = "999999"
    with pytest.raises(etf_score.ScorePayloadError, match="unknown_code"):
        etf_score.validate(bad)
    bad = payload()
    bad["scores"][1]["rank"] = 1
    with pytest.raises(etf_score.ScorePayloadError, match="duplicate_rank"):
        etf_score.validate(bad)
    bad = payload()
    bad["data_cutoff"] = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    with pytest.raises(etf_score.ScorePayloadError, match="future"):
        etf_score.validate(bad)
    bad = payload()
    bad["model_sha256"] = "not-a-hash"
    with pytest.raises(etf_score.ScorePayloadError, match="model_sha256"):
        etf_score.validate(bad)
    bad = payload()
    for row in bad["scores"]:
        row["eligible"] = False
        row["rank"] = None
        row["score"] = None
    with pytest.raises(etf_score.ScorePayloadError, match="fewer_than_8"):
        etf_score.validate(bad)


def test_strict_evidence_fields(engine):
    bad = payload()
    bad["scores"][0]["eligible"] = "false"
    with pytest.raises(etf_score.ScorePayloadError, match="bad_eligible_flag"):
        etf_score.validate(bad)
    bad = payload()
    bad["scores"].pop()
    with pytest.raises(etf_score.ScorePayloadError, match="missing_codes"):
        etf_score.validate(bad)
    bad = payload()
    bad["universe"] = "fixed_10_current_pool"
    with pytest.raises(etf_score.ScorePayloadError, match="bad_universe"):
        etf_score.validate(bad)
    bad = payload()
    bad["generated_at"] = "2026-11-16T12:00:00"
    with pytest.raises(etf_score.ScorePayloadError, match="generated_at_naive"):
        etf_score.validate(bad)
    bad = payload()
    bad["train_end"] = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    with pytest.raises(etf_score.ScorePayloadError, match="train_end_after_cutoff"):
        etf_score.validate(bad)
    bad = payload()
    bad["train_start"], bad["train_end"] = "2025-12-24", "2020-01-01"
    with pytest.raises(etf_score.ScorePayloadError, match="bad_train_window"):
        etf_score.validate(bad)
    bad = payload()
    bad["label_horizon"] = 0
    with pytest.raises(etf_score.ScorePayloadError, match="bad_label_horizon"):
        etf_score.validate(bad)
    bad = payload()
    bad["data_master_snapshot"] = " " * 3
    with pytest.raises(etf_score.ScorePayloadError, match="bad_data_master_snapshot"):
        etf_score.validate(bad)
    bad = payload()
    bad["model_text_sha256"] = "xyz"
    with pytest.raises(etf_score.ScorePayloadError, match="model_text_sha256"):
        etf_score.validate(bad)
    bad = payload()
    bad["scores"][0]["score"] = True
    with pytest.raises(etf_score.ScorePayloadError, match="bad_score"):
        etf_score.validate(bad)


def test_duplicate_identity_conflicts_on_new_content(engine):
    cleanup(engine)
    try:
        with Session(engine) as s:
            etf_score.import_payload(s, payload())
            s.commit()
            changed_model = payload()
            changed_model["model_sha256"] = "c" * 64
            with pytest.raises(etf_score.ScorePayloadError, match="duplicate_model_sha256_mismatch"):
                etf_score.import_payload(s, changed_model)
            s.rollback()
            changed_scores = payload()
            changed_scores["scores"][0]["score"] = 0.5
            with pytest.raises(etf_score.ScorePayloadError, match="duplicate_content_mismatch"):
                etf_score.import_payload(s, changed_scores)
            s.rollback()
            assert s.scalar(select(func.count()).select_from(EtfScoreRun)) == 1
            assert s.scalar(select(func.count()).select_from(EtfScore)) == len(CODES)
    finally:
        cleanup(engine)


def test_expected_session_uses_weekdays():
    assert etf_score.expected_session(dt.date(2026, 11, 16)) == dt.date(2026, 11, 16)
    assert etf_score.expected_session(dt.date(2026, 11, 14)) == dt.date(2026, 11, 13)
    assert etf_score.expected_session(dt.date(2026, 11, 15)) == dt.date(2026, 11, 13)


def test_stale_flag_and_expected_cutoff(engine):
    cleanup(engine)
    expected = etf_score.expected_session(etf_score.now_cst().date())
    try:
        fresh = payload()
        fresh["data_cutoff"] = expected.isoformat()
        with Session(engine) as s:
            etf_score.import_payload(s, fresh)
            s.commit()
            out = etf_score.latest(s)
            assert out["run"]["stale"] is False
            assert out["run"]["expected_cutoff"] == expected.isoformat()
            assert out["run"]["staleness_days"] == 0
        cleanup(engine)
        old = payload()
        old["data_cutoff"] = (expected - dt.timedelta(days=7)).isoformat()
        with Session(engine) as s:
            etf_score.import_payload(s, old)
            s.commit()
            out = etf_score.latest(s)
            assert out["run"]["stale"] is True
            assert out["run"]["staleness_days"] == 7
    finally:
        cleanup(engine)


def test_horizons_coexist_and_history_is_scoped(engine):
    cleanup(engine)
    try:
        with Session(engine) as s:
            etf_score.import_payload(s, payload(version="qp-etf-test-20", horizon=20))
            s.commit()
            etf_score.import_payload(s, payload(version="qp-etf-test-5", horizon=5))
            s.commit()
            out20 = etf_score.latest(s, horizon=20)
            out5 = etf_score.latest(s, horizon=5)
            assert out20["run"]["label_horizon"] == 20
            assert out20["run"]["model_version"] == "qp-etf-test-20"
            assert out5["run"]["label_horizon"] == 5
            assert out5["run"]["model_version"] == "qp-etf-test-5"
            assert out20["horizons"] == [5, 20]
            assert [r["model_version"] for r in out20["history"]] == ["qp-etf-test-20"]
            assert [r["model_version"] for r in out5["history"]] == ["qp-etf-test-5"]
            assert etf_score.latest(s, horizon=99)["run"] is None
    finally:
        cleanup(engine)


def test_job_imports_both_horizons(tmp_path, engine, monkeypatch):
    cleanup(engine)
    try:
        (tmp_path / "latest.json").write_text(json.dumps(payload(version="qp-etf-job-20", horizon=20)))
        (tmp_path / "latest-5d.json").write_text(json.dumps(payload(version="qp-etf-job-5", horizon=5)))
        monkeypatch.setenv("QP_ETF_SCORE_INBOX", str(tmp_path / "latest.json"))
        from qp.jobs.registry import JobContext
        from qp.jobs.research import import_etf_score_job

        out = import_etf_score_job(JobContext(engine=engine, run_id=0, name="research.etf_score_import"))
        assert out["ok"] is True
        assert [row["model_version"] for row in out["imports"]] == ["qp-etf-job-20", "qp-etf-job-5"]
        with Session(engine) as s:
            assert etf_score.latest(s, horizon=20)["run"]["model_version"] == "qp-etf-job-20"
            assert etf_score.latest(s, horizon=5)["run"]["model_version"] == "qp-etf-job-5"
    finally:
        cleanup(engine)


def test_job_bad_5d_keeps_20d_and_reports_failure(tmp_path, engine, monkeypatch):
    cleanup(engine)
    try:
        (tmp_path / "latest.json").write_text(json.dumps(payload(version="qp-etf-job-good-20", horizon=20)))
        bad = payload(version="qp-etf-job-bad-5", horizon=5)
        bad["scores"].pop()
        (tmp_path / "latest-5d.json").write_text(json.dumps(bad))
        monkeypatch.setenv("QP_ETF_SCORE_INBOX", str(tmp_path / "latest.json"))
        from qp.jobs.registry import JobContext
        from qp.jobs.research import import_etf_score_job

        out = import_etf_score_job(JobContext(engine=engine, run_id=0, name="research.etf_score_import"))
        assert out["ok"] is False and out["failed"] == ["5d"]
        by_feed = {row["feed"]: row for row in out["imports"]}
        assert by_feed["20d"]["ok"] is True
        assert by_feed["5d"]["ok"] is False and "missing_codes" in by_feed["5d"]["error"]
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(EtfScoreRun)) == 1
            assert s.scalar(select(func.count()).select_from(EtfScore)) == len(CODES)
            assert etf_score.latest(s, horizon=20)["run"]["model_version"] == "qp-etf-job-good-20"
            assert etf_score.latest(s, horizon=5)["run"] is None
    finally:
        cleanup(engine)


def test_job_bad_20d_keeps_5d(tmp_path, engine, monkeypatch):
    cleanup(engine)
    try:
        bad = payload(version="qp-etf-job-bad-20", horizon=20)
        bad["scores"].pop()
        (tmp_path / "latest.json").write_text(json.dumps(bad))
        (tmp_path / "latest-5d.json").write_text(json.dumps(payload(version="qp-etf-job-good-5", horizon=5)))
        monkeypatch.setenv("QP_ETF_SCORE_INBOX", str(tmp_path / "latest.json"))
        from qp.jobs.registry import JobContext
        from qp.jobs.research import import_etf_score_job

        out = import_etf_score_job(JobContext(engine=engine, run_id=0, name="research.etf_score_import"))
        assert out["ok"] is False and out["failed"] == ["20d"]
        with Session(engine) as s:
            assert etf_score.latest(s, horizon=20)["run"] is None
            assert etf_score.latest(s, horizon=5)["run"]["model_version"] == "qp-etf-job-good-5"
    finally:
        cleanup(engine)


def test_api_auth_scope_and_payload(engine):
    from fastapi.testclient import TestClient

    from qp.api.main import app
    from qp.auth.sessions import create_service_token

    cleanup(engine)
    try:
        with Session(engine) as s:
            etf_score.import_payload(s, payload())
            etf_score.import_payload(s, payload(version="qp-etf-test-5d", horizon=5))
            token = create_service_token(s, f"m15-{dt.datetime.now().timestamp()}", ["research:read"])
            other = create_service_token(s, f"m15-other-{dt.datetime.now().timestamp()}", ["signals:read"])
            s.commit()
        client = TestClient(app, base_url="https://testserver")
        assert client.get("/api/research/etf-score").status_code == 401
        assert client.get("/api/research/etf-score", headers={"authorization": f"Bearer {other}"}).status_code == 403
        response = client.get("/api/research/etf-score", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["read_only"] is True and body["is_trade_signal"] is False
        assert body["run"]["model_version"] == "qp-etf-test-v1"
        assert body["horizon"] == 20
        assert len(body["scores"]) == len(CODES) and body["scores"][0]["rank"] == 1
        assert body["history"][0]["eligible_count"] == 8
        five = client.get("/api/research/etf-score?horizon=5", headers={"authorization": f"Bearer {token}"})
        assert five.status_code == 200
        assert five.json()["run"]["model_version"] == "qp-etf-test-5d"
        assert five.json()["horizon"] == 5
        assert (
            client.get("/api/research/etf-score?horizon=0", headers={"authorization": f"Bearer {token}"}).status_code
            == 422
        )
    finally:
        cleanup(engine)


def test_job_registered():
    from qp.jobs.registry import JOBS

    assert JOBS["research.etf_score_import"].schedule_hint.startswith("Mon-Fri 16:55")


def test_cli_help_contains_import():
    from qp.cli import main

    with pytest.raises(SystemExit) as out:
        main(["research", "etf-score", "--help"])
    assert out.value.code == 0
