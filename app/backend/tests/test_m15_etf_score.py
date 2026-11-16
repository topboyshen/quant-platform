"""M15 read-only ETF research score shadow: import contract, isolation and API."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from qp.models.research import EtfScore, EtfScoreRun
from qp.models.signals import TargetDecision
from qp.research import etf_score
from qp.signals.universe import TARGETS

CODES = [t.code for t in TARGETS[:10]]


def payload() -> dict:
    scores = []
    for i, code in enumerate(CODES):
        eligible = i < 8
        scores.append({"code": code, "name": f"ETF-{code}",
                       "score": 0.01 - i * 0.001 if eligible else None,
                       "rank": i + 1 if eligible else None,
                       "eligible": eligible, "reason": "" if eligible else "not_eligible"})
    return {
        "model_version": "qp-etf-test-v1",
        "model_sha256": "a" * 64,
        "data_cutoff": (dt.date.today() - dt.timedelta(days=1)).isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label_horizon": 20,
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
        names = set(c.execute(text("select table_name from information_schema.tables "
                                   "where table_schema='research'")).scalars())
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
            assert s.scalar(select(func.count()).select_from(EtfScoreRun)) == 1
            assert s.scalar(select(func.count()).select_from(EtfScore)) == 10
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


def test_api_auth_scope_and_payload(engine):
    from fastapi.testclient import TestClient
    from qp.api.main import app
    from qp.auth.sessions import create_service_token
    cleanup(engine)
    try:
        with Session(engine) as s:
            etf_score.import_payload(s, payload())
            token = create_service_token(s, f"m15-{dt.datetime.now().timestamp()}", ["research:read"])
            other = create_service_token(s, f"m15-other-{dt.datetime.now().timestamp()}", ["signals:read"])
            s.commit()
        client = TestClient(app, base_url="https://testserver")
        assert client.get("/api/research/etf-score").status_code == 401
        assert client.get("/api/research/etf-score",
                          headers={"authorization": f"Bearer {other}"}).status_code == 403
        response = client.get("/api/research/etf-score",
                              headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["read_only"] is True and body["is_trade_signal"] is False
        assert body["run"]["model_version"] == "qp-etf-test-v1"
        assert len(body["scores"]) == 10 and body["scores"][0]["rank"] == 1
        assert body["history"][0]["eligible_count"] == 8
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
