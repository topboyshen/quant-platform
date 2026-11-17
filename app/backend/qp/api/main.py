"""qp-api (FastAPI). Served on 127.0.0.1:18800; nginx maps /next/api/ -> /api/."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from qp import __version__
from qp.auth.passwords import hash_password, needs_rehash, verify_password
from qp.auth.sessions import (
    create_session,
    resolve_service_token,
    resolve_session,
    revoke_session,
)
from qp.config import get_settings
from qp.db import get_engine, session_factory
from qp.models.base import utcnow
from qp.models.core import AuditEvent, User
from qp.models.notify import Outbox
from qp.models.ops import HealthCheck, JobDef, JobRun

app = FastAPI(title="quant-platform", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = dt.timedelta(minutes=15)


def get_db():
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def client_ip(req: Request) -> str | None:
    import ipaddress

    cand = req.headers.get("x-real-ip") or (req.client.host if req.client else None)
    try:
        return str(ipaddress.ip_address(cand)) if cand else None
    except ValueError:
        return None


class Principal(BaseModel):
    kind: str
    name: str
    role: str | None = None
    scopes: list[str] | None = None


def require_auth(req: Request, db: Session = Depends(get_db)) -> Principal:
    st = get_settings()
    user = resolve_session(db, req.cookies.get(st.cookie_name))
    if user is not None:
        return Principal(kind="user", name=user.username, role=user.role)
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = resolve_service_token(db, auth.split(None, 1)[1].strip())
        if tok is not None:
            return Principal(kind="service", name=tok.name, scopes=list(tok.scopes or []))
    raise HTTPException(status_code=401, detail="unauthorized")


def require_scope(scope: str):
    """Service tokens are limited to their scopes; interactive user sessions are not."""

    def dep(p: Principal = Depends(require_auth)) -> Principal:
        if p.kind == "user" or scope in (p.scopes or []):
            return p
        raise HTTPException(status_code=403, detail=f"missing scope {scope}")

    return dep


def check_same_origin(req: Request) -> None:
    """Defense in depth on top of SameSite=Lax: reject cross-origin state-changing requests."""
    origin = req.headers.get("origin")
    host = req.headers.get("x-forwarded-host") or req.headers.get("host")
    if origin and host and origin.split("://", 1)[-1] != host:
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.get("/api/health")
def health() -> dict[str, Any]:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "version": __version__}


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


@app.post("/api/auth/login")
def login(body: LoginIn, req: Request, resp: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    check_same_origin(req)
    st = get_settings()
    ip = client_ip(req)
    since = utcnow() - LOGIN_WINDOW
    failures = db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == "login",
            AuditEvent.ok.is_(False),
            AuditEvent.at >= since,
            AuditEvent.actor == body.username,
        )
    ).scalar_one()
    if failures >= LOGIN_MAX_FAILURES:
        db.add(AuditEvent(actor=body.username, action="login", ok=False, ip=ip, detail={"reason": "locked"}))
        raise HTTPException(status_code=429, detail="too many failed attempts, try later")
    user = db.execute(select(User).where(User.username == body.username, User.is_active.is_(True))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        db.add(AuditEvent(actor=body.username, action="login", ok=False, ip=ip, detail={"reason": "bad_credentials"}))
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")
    detail: dict[str, Any] = {"scheme": user.hash_scheme}
    if needs_rehash(user.password_hash):
        user.password_hash, user.hash_scheme = hash_password(body.password), "argon2"
        user.password_changed_at = utcnow()
        detail["rehashed_to"] = "argon2"
    user.last_login_at = utcnow()
    token = create_session(db, user, st.session_ttl_hours, ip, req.headers.get("user-agent"))
    db.add(AuditEvent(actor=user.username, action="login", ok=True, ip=ip, detail=detail))
    # get_db commits during teardown, after the response is sent; commit here so a
    # client that calls /api/auth/me immediately after login cannot race the session row.
    db.commit()
    resp.set_cookie(
        st.cookie_name,
        token,
        max_age=st.session_ttl_hours * 3600,
        path=st.cookie_path,
        secure=st.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return {"username": user.username, "role": user.role}


@app.post("/api/auth/logout")
def logout(req: Request, resp: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    check_same_origin(req)
    st = get_settings()
    revoke_session(db, req.cookies.get(st.cookie_name))
    resp.delete_cookie(st.cookie_name, path=st.cookie_path, secure=st.cookie_secure, httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/auth/me")
def me(p: Principal = Depends(require_auth)) -> Principal:
    return p


@app.get("/api/ops/jobs", dependencies=[Depends(require_scope("ops:read"))])
def ops_jobs(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    out = []
    for d in db.execute(select(JobDef).order_by(JobDef.name)).scalars():
        # skipped_locked is a no-op attempt while another run holds the lock; it must not
        # mask the real outcome of the job.
        last = db.execute(
            select(JobRun)
            .where(JobRun.job_name == d.name, JobRun.status != "skipped_locked")
            .order_by(JobRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        out.append(
            {
                "name": d.name,
                "description": d.description,
                "schedule_hint": d.schedule_hint,
                "enabled": d.enabled,
                "last_status": last.status if last else None,
                "last_started_at": last.started_at if last else None,
            }
        )
    return out


@app.get("/api/ops/job-runs", dependencies=[Depends(require_scope("ops:read"))])
def ops_job_runs(
    limit: int = 50, job: str | None = None, p: Principal = Depends(require_auth), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    q = select(JobRun).order_by(JobRun.id.desc()).limit(max(1, min(limit, 500)))
    if job:
        q = q.where(JobRun.job_name == job)
    return [
        {
            "id": r.id,
            "job_name": r.job_name,
            "status": r.status,
            "trigger": r.trigger,
            "dedup_key": r.dedup_key,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration_s": r.duration_s,
            "result": r.result,
            "error": (r.error or "")[:500] or None,
        }
        for r in db.execute(q).scalars()
    ]


@app.get("/api/ops/health", dependencies=[Depends(require_scope("ops:read"))])
def ops_health(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    # healthcheck and inventory write different checks in alternating runs; show the
    # latest observation per check instead of only the most recent run's rows.
    checks = [
        {"check": h.check, "ok": h.ok, "value": h.value, "at": h.at, "detail": h.detail}
        for h in db.execute(
            select(HealthCheck).distinct(HealthCheck.check).order_by(HealthCheck.check, HealthCheck.at.desc())
        ).scalars()
    ]
    counts = dict(db.execute(select(Outbox.status, func.count()).group_by(Outbox.status)).all())
    st = get_settings()
    return {
        "checks": checks,
        "outbox": {
            "pending": counts.get("pending", 0),
            "shadowed": counts.get("shadowed", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "mode": st.notify_mode,
            "signals_mode": st.signals_notify_mode,
            "kill_switch": st.notify_kill_switch,
        },
    }


from qp.api.ops_m2 import router as _m2_router  # noqa: E402

app.include_router(_m2_router)
from qp.api.market import router as _market_router  # noqa: E402

app.include_router(_market_router)
from qp.api.hooks import router as _hooks_router  # noqa: E402

app.include_router(_hooks_router)
from qp.api.overview import router as _overview_router  # noqa: E402

app.include_router(_overview_router)
from qp.api.research import router as _research_router  # noqa: E402

app.include_router(_research_router)
from qp.api.signals import router as _signals_router  # noqa: E402

app.include_router(_signals_router)
from qp.api.reports import router as _reports_router  # noqa: E402

app.include_router(_reports_router)
from qp.api.chain import router as _chain_router  # noqa: E402

app.include_router(_chain_router)
from qp.api.settings import router as _settings_router  # noqa: E402

app.include_router(_settings_router)
