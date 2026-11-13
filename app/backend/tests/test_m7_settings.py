"""M7.5 settings API: account and platform read view."""
from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from qp.models.core import User


def cleanup(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(User))
        session.commit()


def test_settings_by_service_token(engine, client):
    import uuid
    from qp.auth.sessions import create_service_token
    cleanup(engine)
    try:
        with Session(engine) as s:
            tok = create_service_token(s, f"m7-settings-{uuid.uuid4().hex[:8]}", ["ops:read"])
            s.commit()
        r = client.get("/api/settings", headers={"authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        data = r.json()
        assert data["account"]["kind"] == "service"
        assert data["platform"]["version"]
        assert data["platform"]["notify_mode"] == "shadow"
        assert data["platform"]["signals_notify_mode"] == "shadow"
        assert data["platform"]["notify_kill_switch"] is False
        assert data["platform"]["shadow_only"] is True
    finally:
        cleanup(engine)


def test_settings_by_user_session(engine, client):
    from qp.auth.passwords import hash_password
    cleanup(engine)
    try:
        with Session(engine) as s:
            s.add(User(username="m7", password_hash=hash_password("pw123456"), hash_scheme="argon2",
                       role="admin", is_active=True))
            s.commit()
        login = client.post("/api/auth/login", json={"username": "m7", "password": "pw123456"})
        assert login.status_code == 200
        value = login.headers["set-cookie"].split("qp_session=", 1)[1].split(";", 1)[0]
        data = client.get("/api/settings", cookies={"qp_session": value}).json()
        assert data["account"]["username"] == "m7"
        assert data["account"]["role"] == "admin"
        assert data["account"]["hash_scheme"] == "argon2"
    finally:
        client.post("/api/auth/logout")
        cleanup(engine)


def test_settings_requires_auth(client):
    assert client.get("/api/settings").status_code == 401
