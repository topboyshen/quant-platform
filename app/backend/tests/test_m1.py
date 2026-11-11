import io
import shutil
import sys

import bcrypt
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

PW = "correct horse battery staple"


def test_schemas(engine):
    with engine.connect() as c:
        got = {r[0] for r in c.execute(text("select schema_name from information_schema.schemata"))}
    for s in ("core", "market", "research", "signals", "execution", "reports", "ops", "notify", "archive"):
        assert s in got


def test_passwords():
    from qp.auth.passwords import hash_password, needs_rehash, scheme_of, verify_password
    h = hash_password(PW)
    assert scheme_of(h) == "argon2" and verify_password(PW, h) and not verify_password("x", h)
    assert not needs_rehash(h)
    b = bcrypt.hashpw(PW.encode(), bcrypt.gensalt(rounds=4)).decode()
    assert scheme_of(b) == "legacy_bcrypt" and verify_password(PW, b) and needs_rehash(b)
    import hashlib
    legacy = "sha256$abc$" + hashlib.sha256((PW + "abc").encode()).hexdigest()
    assert verify_password(PW, legacy) and not verify_password("y", legacy)
    assert not verify_password(PW, "garbage")


@pytest.fixture()
def seeded(engine, monkeypatch):
    from qp.cli import main
    b = bcrypt.hashpw(PW.encode(), bcrypt.gensalt(rounds=4)).decode()
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"admin\t{b}\n"))
    assert main(["user", "import-legacy", "--stdin", "--force"]) == 0
    return "admin"


@pytest.fixture()
def client(engine):  # noqa: F811
    from fastapi.testclient import TestClient
    from qp.api.main import app
    return TestClient(app, base_url="https://testserver")


def test_health_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["db"] is True


def test_unauth_401(client):
    for p in ("/api/auth/me", "/api/ops/job-runs", "/api/ops/health", "/api/ops/jobs"):
        assert client.get(p).status_code == 401


def test_login_rehash_logout(seeded, client, engine):
    from qp.models.core import User
    r = client.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    sc = r.headers["set-cookie"].lower()
    for attr in ("httponly", "secure", "samesite=lax", "path=/next/"):
        assert attr in sc
    with Session(engine) as s:
        u = s.execute(select(User).where(User.username == "admin")).scalar_one()
        assert u.hash_scheme == "argon2" and u.password_hash.startswith("$argon2")
    # TestClient sends cookie only for matching path; call with explicit cookie
    tok = r.cookies.get("qp_session")
    assert client.get("/api/auth/me", cookies={"qp_session": tok}).status_code == 200
    assert client.get("/api/ops/jobs", cookies={"qp_session": tok}).status_code == 200
    assert client.post("/api/auth/logout", cookies={"qp_session": tok}).status_code == 200
    assert client.get("/api/auth/me", cookies={"qp_session": tok}).status_code == 401
    # argon2 login works afterwards too
    assert client.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 200


def test_cross_origin_login_rejected(seeded, client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": PW},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_lockout(engine, client):
    from qp.models.core import AuditEvent
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 429


def test_service_token(engine, client):
    from qp.auth.sessions import create_service_token
    with Session(engine) as s:
        tok = create_service_token(s, "test-svc", ["ops:read"])
        s.commit()
    assert client.get("/api/ops/job-runs", headers={"authorization": f"Bearer {tok}"}).status_code == 200
    assert client.get("/api/ops/job-runs", headers={"authorization": "Bearer qpst_wrong"}).status_code == 401


def test_healthcheck_failure_goes_to_shadow_outbox(engine):
    from qp.jobs import run_job
    from qp.models.notify import DeliveryAttempt, Outbox
    run = run_job(engine, "ops.healthcheck", trigger="test")
    assert run.status == "failed"  # api url unreachable in tests
    assert run.result["checks"]["db"]["ok"] is True
    with Session(engine) as s:
        assert s.execute(select(Outbox).where(Outbox.status == "pending")).scalars().first() is not None
    sent = run_job(engine, "notify.send", trigger="test")
    assert sent.status == "succeeded" and sent.result["sent"] == 0 and sent.result["shadowed"] >= 1
    with Session(engine) as s:
        rows = s.execute(select(Outbox)).scalars().all()
        assert all(o.status == "shadowed" and o.mode == "shadow" for o in rows)
        assert s.execute(select(DeliveryAttempt)).scalars().first().mode == "shadow"
    # dedup of outbox within the hour
    run_job(engine, "ops.healthcheck", trigger="test")
    with Session(engine) as s:
        assert s.execute(select(Outbox).where(Outbox.status == "pending")).scalars().first() is None


def test_dedup_and_lock(engine):
    from qp.jobs.registry import JOBS, _lock_key, job, run_job

    @job("test.noop", "noop")
    def _noop(ctx):
        return {"ok": True}
    a = run_job(engine, "test.noop", dedup_key="k1")
    b = run_job(engine, "test.noop", dedup_key="k1")
    assert a.status == "succeeded" and b.status == "skipped_dup"
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(20816, :k)"), {"k": _lock_key("test.noop")})
        c = run_job(engine, "test.noop", dedup_key="k2")
        assert c.status == "skipped_locked"
        holder.execute(text("SELECT pg_advisory_unlock(20816, :k)"), {"k": _lock_key("test.noop")})
    assert run_job(engine, "test.noop", dedup_key="k2").status == "succeeded"
    JOBS.pop("test.noop")


def test_unknown_job(engine):
    from qp.jobs import run_job
    with pytest.raises(KeyError):
        run_job(engine, "nope")


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not on PATH")
def test_backup(engine):
    from qp.jobs import run_job
    r = run_job(engine, "ops.backup", trigger="test", dedup_key="test-day")
    assert r.status == "succeeded", r.error
    assert r.result["bytes"] > 0 and len(r.result["sha256"]) == 64
    assert run_job(engine, "ops.backup", trigger="test", dedup_key="test-day").status == "skipped_dup"


def test_single_account_guard(seeded, monkeypatch):
    from qp.cli import main
    monkeypatch.setattr(sys, "stdin", io.StringIO("someoneelse\t$2b$04$abcdefghijklmnopqrstuuabcdefghijklmnopqrstuvwxyzABCDE\n"))
    assert main(["user", "import-legacy", "--stdin"]) == 3
