"""M4.5 approved-signal intake (HMAC/risk/idempotency) and shadow execution digest."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JOBS
from qp.models.signals import ApprovedIntake, ExecutionDigest
from qp.signals.approved import (
    build_digest_body,
    build_previous_day_digest,
    canonical_body,
    canonical_payload_sha256,
    expected_signature,
    load_hmac_secret,
    record_intake,
    risk_check,
    sign_digest,
)

UTC = dt.UTC
CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2027, 4, 6, 12, 0, tzinfo=UTC)
DIGEST_NOW = dt.datetime(2027, 4, 7, 8, 35, tzinfo=CST)
DIGEST_DAY = dt.date(2027, 4, 6)
HOOK = "/hooks/v1/approved-signals"
SECRET = b"test-intake-secret"


def cleanup(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(ApprovedIntake))
        session.execute(delete(ExecutionDigest))
        session.commit()


def payload(key: str = "ak-1", **overrides) -> dict:
    body = {
        "idempotency_key": key,
        "signal_type": "weekly_macd_golden_cross",
        "symbol": "510300",
        "confidence": 0.8,
        "expires_at": "2027-04-06T13:00:00Z",
        "provenance": {"source": "quant"},
        "decision": "approve",
        "side": "long",
    }
    body.update(overrides)
    return body


def signed(raw: bytes, *, ts: str | None = None, secret: bytes = SECRET, source: str = "dedirock") -> dict[str, str]:
    timestamp = ts if ts is not None else str(int(time.time()))
    return {
        "X-Source": source,
        "X-Timestamp": timestamp,
        "X-Signature": expected_signature(secret, timestamp, raw),
        "Content-Type": "application/json",
    }


@pytest.fixture()
def hmaced(tmp_path, monkeypatch):
    path = tmp_path / "approved_signals_hmac"
    path.write_text(SECRET.decode() + "\n", encoding="utf-8")
    monkeypatch.setenv("QP_APPROVED_SIGNALS_HMAC_FILE", str(path))
    return path


def seed_intake(
    engine, *, key: str, status: str, received_at: dt.datetime, detail: str = "ok", body: dict | None = None
) -> None:
    with Session(engine) as session:
        session.add(
            ApprovedIntake(
                idempotency_key=key,
                source="dedirock",
                received_at=received_at,
                status=status,
                detail=detail,
                risk_check={"passed": status == "accepted", "detail": detail},
                payload_sha256=hashlib.sha256(key.encode()).hexdigest(),
                payload=body if body is not None else payload(key),
            )
        )
        session.commit()


# --------------------------------------------------------------------------- risk gate
def test_risk_check_matches_legacy_branches():
    assert risk_check(payload(), now=NOW) == (True, "ok")
    assert risk_check(payload(side=None), now=NOW) == (True, "ok")
    assert risk_check({"decision": "approve"}, now=NOW)[1] == (
        "missing_fields:idempotency_key,signal_type,symbol,confidence,expires_at,provenance"
    )
    assert risk_check(payload(decision="reject"), now=NOW) == (False, "decision_not_approve")
    assert risk_check(payload(review_status="pending"), now=NOW) == (False, "still_pending")
    assert risk_check(payload(confidence="high"), now=NOW) == (False, "bad_confidence")
    assert risk_check(payload(confidence=0.49), now=NOW) == (False, "confidence_below_floor")
    assert risk_check(payload(expires_at="not-a-date"), now=NOW) == (False, "bad_expires_at")
    assert risk_check(payload(expires_at="2027-04-06T11:59:59Z"), now=NOW) == (False, "expired")
    assert risk_check(payload(expires_at="2027-04-06T13:00:00"), now=NOW) == (True, "ok")
    assert risk_check(payload(side="buy"), now=NOW) == (False, "bad_side")


def test_hmac_secret_is_stripped_and_signature_is_exact():
    assert load_hmac_secret("/nonexistent/secret") is None
    ts = "1700000000"
    raw = b'{"a":1}'
    sig = expected_signature(SECRET, ts, raw)
    assert sig.startswith("sha256=") and len(sig) == 71
    assert sig == "sha256=" + hmac.new(SECRET, ts.encode() + b"." + raw, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- intake API
def test_valid_request_is_accepted_and_persisted(engine, client, hmaced):
    cleanup(engine)
    raw = json.dumps(payload(), ensure_ascii=False).encode()
    try:
        response = client.post(HOOK, content=raw, headers=signed(raw))
        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "status": "accepted",
            "detail": "ok",
            "idempotency_key": "ak-1",
            "order": "queued_stub",
        }
        with Session(engine) as session:
            row = session.execute(select(ApprovedIntake).where(ApprovedIntake.idempotency_key == "ak-1")).scalar_one()
            assert row.status == "accepted" and row.source == "dedirock"
            assert row.payload_sha256 == canonical_payload_sha256(json.loads(raw))
            assert row.risk_check == {"passed": True, "detail": "ok"}
            assert row.payload["symbol"] == "510300"
    finally:
        cleanup(engine)


def test_rejected_request_is_persisted_with_422(engine, client, hmaced):
    cleanup(engine)
    raw = json.dumps(payload(decision="reject")).encode()
    try:
        response = client.post(HOOK, content=raw, headers=signed(raw))
        assert response.status_code == 422
        assert response.json()["detail"] == "decision_not_approve"
        with Session(engine) as session:
            row = session.execute(select(ApprovedIntake)).scalar_one()
            assert (row.status, row.detail) == ("rejected", "decision_not_approve")
            assert row.risk_check == {"passed": False, "detail": "decision_not_approve"}
    finally:
        cleanup(engine)


def test_duplicate_key_is_409_and_never_overwritten(engine, client, hmaced):
    cleanup(engine)
    first = json.dumps(payload()).encode()
    second = json.dumps(payload(confidence=0.99)).encode()
    try:
        assert client.post(HOOK, content=first, headers=signed(first)).status_code == 200
        response = client.post(HOOK, content=second, headers=signed(second))
        assert response.status_code == 409
        body = response.json()
        assert body["duplicate"] is True and body["status"] == "accepted"
        with Session(engine) as session:
            row = session.execute(select(ApprovedIntake)).scalar_one()
            assert row.payload["confidence"] == 0.8
            assert row.payload_sha256 == canonical_payload_sha256(json.loads(first))
    finally:
        cleanup(engine)


@pytest.mark.parametrize(
    "mutate,expected_status,error",
    [
        (lambda raw, headers: {**headers, "X-Source": "other"}, 401, "bad_source"),
        (lambda raw, headers: {**headers, "X-Timestamp": "not-int"}, 401, "bad_timestamp"),
        (lambda raw, headers: {**headers, "X-Timestamp": str(int(time.time()) - 301)}, 401, "timestamp_skew"),
        (lambda raw, headers: {**headers, "X-Signature": "sha256=" + "0" * 64}, 401, "bad_signature"),
    ],
)
def test_auth_failures_write_nothing(engine, client, hmaced, mutate, expected_status, error):
    cleanup(engine)
    raw = json.dumps(payload()).encode()
    try:
        response = client.post(HOOK, content=raw, headers=mutate(raw, signed(raw)))
        assert response.status_code == expected_status
        assert response.json()["error"] == error
        with Session(engine) as session:
            assert session.scalar(select(ApprovedIntake.id)) is None
    finally:
        cleanup(engine)


def test_bad_json_object_and_key_are_400(engine, client, hmaced):
    cleanup(engine)
    cases = [(b"{", "invalid_json"), (b"[]", "payload_not_object"), (b'{"signal_type":"x"}', "missing_idempotency_key")]
    try:
        for raw, error in cases:
            response = client.post(HOOK, content=raw, headers=signed(raw))
            assert response.status_code == 400, (raw, response.text)
            assert response.json()["error"] == error
        with Session(engine) as session:
            assert session.scalar(select(ApprovedIntake.id)) is None
    finally:
        cleanup(engine)


def test_missing_secret_fails_closed(engine, client, monkeypatch, tmp_path):
    cleanup(engine)
    monkeypatch.setenv("QP_APPROVED_SIGNALS_HMAC_FILE", str(tmp_path / "absent"))
    raw = json.dumps(payload()).encode()
    try:
        response = client.post(HOOK, content=raw, headers=signed(raw))
        assert response.status_code == 503 and response.json()["error"] == "not_configured"
        with Session(engine) as session:
            assert session.scalar(select(ApprovedIntake.id)) is None
    finally:
        cleanup(engine)


def test_record_intake_signature_and_duplicate_helper(engine):
    cleanup(engine)
    raw = json.dumps(payload("ak-helper")).encode()
    try:
        with Session(engine) as session:
            first = record_intake(
                session, payload=payload("ak-helper"), raw=raw, source="dedirock", received_at=NOW, now=NOW
            )
            session.commit()
        assert first == {"duplicate": False, "status": "accepted", "detail": "ok", "idempotency_key": "ak-helper"}
        with Session(engine) as session:
            second = record_intake(
                session,
                payload=payload("ak-helper", confidence=0.1),
                raw=raw,
                source="dedirock",
                received_at=NOW,
                now=NOW,
            )
            session.commit()
        assert second["duplicate"] is True and second["status"] == "accepted"
    finally:
        cleanup(engine)


# --------------------------------------------------------------------------- digest
def test_digest_body_counts_noise_and_truncates_to_20(engine, tmp_path):
    cleanup(engine)
    try:
        seed_intake(engine, key="ok-1", status="accepted", received_at=dt.datetime(2027, 4, 6, 1, 0, tzinfo=UTC))
        seed_intake(
            engine,
            key="bad-1",
            status="rejected",
            detail="expired",
            received_at=dt.datetime(2027, 4, 6, 2, 0, tzinfo=UTC),
        )
        seed_intake(
            engine, key="selftest-noise", status="accepted", received_at=dt.datetime(2027, 4, 6, 3, 0, tzinfo=UTC)
        )
        seed_intake(
            engine, key="outside-before", status="accepted", received_at=dt.datetime(2027, 4, 5, 23, 59, tzinfo=UTC)
        )
        seed_intake(
            engine, key="outside-after", status="accepted", received_at=dt.datetime(2027, 4, 7, 0, 0, tzinfo=UTC)
        )
        for index in range(22):
            seed_intake(
                engine,
                key=f"bulk-{index:02d}",
                status="accepted",
                received_at=dt.datetime(2027, 4, 6, 4, 0, tzinfo=UTC),
            )
        with Session(engine) as session:
            out = build_previous_day_digest(session, now=DIGEST_NOW, output_dir=tmp_path)
            session.commit()
        assert (out["digest_date"], out["row_count"], out["item_count"]) == (DIGEST_DAY.isoformat(), 24, 20)
        assert (out["accepted"], out["rejected"]) == (23, 1)
        body = json.loads((tmp_path / f"execution_digest_{DIGEST_DAY}.json").read_text())
        assert body["schema_version"] == "0.2" and body["report_type"] == "execution_digest"
        assert body["as_of_day"] == DIGEST_DAY.isoformat()
        assert body["generated_at"] == "2027-04-07T08:35:00+08:00"
        assert body["summary"]["accepted"] == 23 and body["summary"]["rejected"] == 1
        assert len(body["items"]) == 20
        keys = {item["idempotency_key"] for item in body["items"]}
        assert "selftest-noise" not in keys and "outside-before" not in keys
        assert body["items"][0]["received_at"] == "2027-04-06T01:00:00Z"
        assert "受理 23 条" in body["text"] and "拒绝 1 条" in body["text"]
        assert (tmp_path / "latest.md").read_text() == body["markdown"]
        assert json.loads((tmp_path / "latest.json").read_text()) == body
        assert out["push_state"] == "shadowed" and out["would_post"] is False
        with Session(engine) as session:
            row = session.get(ExecutionDigest, DIGEST_DAY)
            assert (row.item_count, row.accepted, row.rejected) == (20, 23, 1)
            assert row.push_state == "shadowed" and row.pushed_at is None
            assert row.sha256 == out["sha256"]
    finally:
        cleanup(engine)


def test_digest_empty_day_still_writes_artifacts(engine, tmp_path):
    cleanup(engine)
    try:
        with Session(engine) as session:
            out = build_previous_day_digest(session, now=DIGEST_NOW, output_dir=tmp_path)
            session.commit()
        assert (out["row_count"], out["item_count"], out["accepted"], out["rejected"]) == (0, 0, 0, 0)
        body = json.loads((tmp_path / "latest.json").read_text())
        assert body["items"] == []
        assert "昨日无业务受理/拒绝（已过滤测试探针）。" in body["markdown"]
        with Session(engine) as session:
            assert session.get(ExecutionDigest, DIGEST_DAY) is not None
    finally:
        cleanup(engine)


def test_digest_is_idempotent_and_hmac_verifies(engine, tmp_path):
    cleanup(engine)
    try:
        seed_intake(engine, key="ok-1", status="accepted", received_at=dt.datetime(2027, 4, 6, 1, 0, tzinfo=UTC))
        with Session(engine) as session:
            first = build_previous_day_digest(session, now=DIGEST_NOW, output_dir=tmp_path)
            session.commit()
        with Session(engine) as session:
            second = build_previous_day_digest(session, now=DIGEST_NOW, output_dir=tmp_path)
            session.commit()
        assert first["sha256"] == second["sha256"]
        with Session(engine) as session:
            rows = list(session.execute(select(ExecutionDigest)).scalars())
            assert len(rows) == 1 and rows[0].push_state == "shadowed"
            body = rows[0].body
        ts = "1700000000"
        signature = sign_digest(body, b"digest-secret", ts)
        assert (
            signature
            == "sha256="
            + hmac.new(b"digest-secret", ts.encode() + b"." + canonical_body(body), hashlib.sha256).hexdigest()
        )
    finally:
        cleanup(engine)


def test_digest_builder_keeps_zero_row_message():
    body = build_digest_body([], digest_date=DIGEST_DAY, generated_at=DIGEST_NOW)
    assert body["summary"]["fills"] == 0 and body["summary"]["pnl"] is None
    assert "不支持" not in body["markdown"]
    assert body["items"] == []


def test_digest_job_is_registered_manual_only():
    assert "signals.execution_digest" in JOBS
    hint = JOBS["signals.execution_digest"].schedule_hint or ""
    assert "manual only" in hint and "timer removed" in hint
