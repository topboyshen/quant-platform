"""M2 API: task center (read-only legacy units), inventory/monitors, alerts, dashboard replacements, service tokens."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, check_same_origin, get_db, require_auth
from qp.auth.sessions import create_service_token
from qp.models.base import utcnow
from qp.models.core import AuditEvent, ServiceToken
from qp.models.ops import Alert, Inventory, LegacyUnit
from qp.retired import RETIRED_CONTAINERS

router = APIRouter(prefix="/api")


def _inv(db: Session, kind: str) -> dict[str, Any] | None:
    r = db.get(Inventory, kind)
    return {"at": r.at, "data": r.data} if r else None


def _mark_retired(inv: dict[str, Any] | None) -> dict[str, Any] | None:
    """Tag containers retired in M9 so the UI can show them as a separate slice."""
    if not inv or not isinstance(inv.get("data"), list):
        return inv
    return {**inv, "data": [{**c, "retired": c.get("name") in RETIRED_CONTAINERS}
                            for c in inv["data"] if isinstance(c, dict)]}


@router.get("/ops/units")
def units(scope: str | None = None, kind: str | None = None, category: str | None = None, include_os: bool = False,
          p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(LegacyUnit).where(LegacyUnit.present.is_(True)).order_by(LegacyUnit.scope, LegacyUnit.name)
    if scope:
        q = q.where(LegacyUnit.scope == scope)
    if kind:
        q = q.where(LegacyUnit.kind == kind)
    if category:
        q = q.where(LegacyUnit.category == category)
    elif not include_os:
        q = q.where(LegacyUnit.category != "os")
    cols = ("scope", "name", "kind", "description", "load_state", "active_state", "sub_state", "result", "exit_status",
            "run_user", "activates", "triggered_by", "last_trigger", "next_elapse", "last_start", "last_exit",
            "category", "updated_at")
    return [{c: getattr(u, c) for c in cols} for u in db.execute(q).scalars()]


@router.get("/ops/inventory")
def inventory(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {k: _inv(db, k) for k in ("collector", "host", "pg", "containers", "ports", "certs", "egress", "logs")}


@router.get("/ops/alerts")
def alerts(state: str | None = "firing", limit: int = 200, p: Principal = Depends(require_auth),
           db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(Alert).order_by(Alert.id.desc()).limit(max(1, min(limit, 1000)))
    if state:
        q = q.where(Alert.state == state)
    return [{"id": a.id, "key": a.key, "severity": a.severity, "state": a.state, "summary": a.summary,
             "created_at": a.created_at, "last_seen_at": a.last_seen_at, "resolved_at": a.resolved_at}
            for a in db.execute(q).scalars()]


@router.get("/dash/services")
def dash_services(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    by_cat = db.execute(select(LegacyUnit.category, LegacyUnit.kind, LegacyUnit.active_state, func.count())
                        .where(LegacyUnit.present.is_(True)).group_by(LegacyUnit.category, LegacyUnit.kind,
                                                                      LegacyUnit.active_state)).all()
    return {"unit_states": [{"category": c, "kind": k, "active_state": a, "n": n} for c, k, a, n in by_cat],
            "containers": _mark_retired(_inv(db, "containers")), "projects": _inv(db, "dash.projects"),
            "legacy_services": _inv(db, "dash.services")}


@router.get("/dash/coverage")
def dash_coverage(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any] | None:
    return _inv(db, "dash.coverage")


# ---- service tokens ----
class TokenIn(BaseModel):
    name: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    scopes: list[str] = Field(default_factory=lambda: ["ops:read"], max_length=10)


def _user_only(p: Principal) -> None:
    if p.kind != "user":
        raise HTTPException(status_code=403, detail="service tokens cannot manage tokens")


@router.get("/ops/tokens")
def list_tokens(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _user_only(p)
    return [{"id": t.id, "name": t.name, "scopes": t.scopes, "created_at": t.created_at, "expires_at": t.expires_at,
             "revoked_at": t.revoked_at, "last_used_at": t.last_used_at}
            for t in db.execute(select(ServiceToken).order_by(ServiceToken.id.desc())).scalars()]


@router.post("/ops/tokens")
def new_token(body: TokenIn, req: Request, p: Principal = Depends(require_auth),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    check_same_origin(req)
    _user_only(p)
    for sc in body.scopes:
        if not re.fullmatch(r"[a-z]+:(read|write)", sc):
            raise HTTPException(status_code=422, detail=f"bad scope {sc}")
    if db.execute(select(ServiceToken.id).where(ServiceToken.name == body.name)).first():
        raise HTTPException(status_code=409, detail="name exists")
    tok = create_service_token(db, body.name, body.scopes)
    db.add(AuditEvent(actor=p.name, action="service_token.create", ok=True, detail={"name": body.name, "scopes": body.scopes}))
    db.flush()
    tid = db.execute(select(ServiceToken.id).where(ServiceToken.name == body.name)).scalar_one()
    return {"id": tid, "name": body.name, "token": tok, "note": "shown once; only its sha256 is stored"}


@router.delete("/ops/tokens/{token_id}")
def revoke_token(token_id: int, req: Request, p: Principal = Depends(require_auth),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    check_same_origin(req)
    _user_only(p)
    t = db.get(ServiceToken, token_id)
    if t is None:
        raise HTTPException(status_code=404, detail="not found")
    if t.revoked_at is None:
        t.revoked_at = utcnow()
        db.add(AuditEvent(actor=p.name, action="service_token.revoke", ok=True, detail={"name": t.name}))
    return {"ok": True}
