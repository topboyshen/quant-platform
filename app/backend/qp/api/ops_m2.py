"""M2 API: task center (read-only legacy units), inventory/monitors, alerts, dashboard replacements, service tokens."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, check_same_origin, get_db, require_auth, require_scope
from qp.auth.sessions import create_service_token
from qp.models.base import utcnow
from qp.models.core import AuditEvent, ServiceToken
from qp.models.market import HealthResult, IndexClose, QlibStatus, TradingDay
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


@router.get("/ops/units", dependencies=[Depends(require_scope("ops:read"))])
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


@router.get("/ops/inventory", dependencies=[Depends(require_scope("ops:read"))])
def inventory(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {k: (_mark_retired(_inv(db, k)) if k == "containers" else _inv(db, k))
            for k in ("collector", "host", "pg", "containers", "ports", "certs", "egress", "logs")}


@router.get("/ops/alerts", dependencies=[Depends(require_scope("ops:read"))])
def alerts(state: str | None = "firing", limit: int = 200, p: Principal = Depends(require_auth),
           db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(Alert).order_by(Alert.id.desc()).limit(max(1, min(limit, 1000)))
    if state:
        q = q.where(Alert.state == state)
    return [{"id": a.id, "key": a.key, "severity": a.severity, "state": a.state, "summary": a.summary,
             "created_at": a.created_at, "last_seen_at": a.last_seen_at, "resolved_at": a.resolved_at}
            for a in db.execute(q).scalars()]


@router.get("/dash/services", dependencies=[Depends(require_scope("ops:read"))])
def dash_services(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    by_cat = db.execute(select(LegacyUnit.category, LegacyUnit.kind, LegacyUnit.active_state, func.count())
                        .where(LegacyUnit.present.is_(True)).group_by(LegacyUnit.category, LegacyUnit.kind,
                                                                      LegacyUnit.active_state)).all()
    attention = db.execute(select(LegacyUnit).where(
        LegacyUnit.present.is_(True), LegacyUnit.category != "os",
        LegacyUnit.active_state.in_(("failed", "activating"))
    ).order_by(LegacyUnit.active_state, LegacyUnit.category, LegacyUnit.name)).scalars()
    return {"unit_states": [{"category": c, "kind": k, "active_state": a, "n": n} for c, k, a, n in by_cat],
            "attention": [{"name": u.name, "category": u.category, "kind": u.kind, "active_state": u.active_state,
                           "sub_state": u.sub_state, "result": u.result, "last_exit": u.last_exit}
                          for u in attention],
            "containers": _mark_retired(_inv(db, "containers"))}


def _latest_health(db: Session, check: str) -> dict[str, Any]:
    row = db.execute(select(HealthResult).where(HealthResult.check == check)
                     .order_by(HealthResult.run_at.desc(), HealthResult.id.desc()).limit(1)).scalar_one_or_none()
    return row.detail if row else {}


def _qp_coverage(db: Session) -> dict[str, Any]:
    """qp-native coverage only: market tables, qlib catalog, health results."""
    qlib = db.execute(select(QlibStatus).where(QlibStatus.kind == "catalog")
                      .order_by(QlibStatus.at.desc(), QlibStatus.id.desc()).limit(1)).scalar_one_or_none()
    qd = qlib.data if qlib else {}
    update = qd.get("update_status") or {}
    snap = _latest_health(db, "snapshot.qp_current")
    cmp = _latest_health(db, "compare.latest")
    idx = db.execute(select(func.max(IndexClose.trade_date), func.count(func.distinct(IndexClose.symbol)))
                     .where(IndexClose.origin == "qp")).one()
    cal = db.execute(select(func.max(TradingDay.date)).where(TradingDay.origin == "qp")).scalar_one_or_none()

    today = dt.date.today()
    days = db.execute(select(TradingDay.date).where(
        TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date <= today)
        .order_by(TradingDay.date.desc()).limit(20)).scalars().all()
    days = sorted(days)
    counts = dict(db.execute(
        select(IndexClose.trade_date, func.count(func.distinct(IndexClose.symbol)))
        .where(IndexClose.origin == "qp", IndexClose.trade_date.in_(days))
        .group_by(IndexClose.trade_date)).all()) if days else {}
    qlib_latest = qd.get("calendar_last")
    recent = [{"date": d.isoformat(), "index_symbols": int(counts.get(d, 0)),
               "qlib": bool(qlib_latest and d.isoformat() <= qlib_latest)} for d in days]

    issues: list[dict[str, Any]] = []
    if not qd:
        issues.append({"code": "qlib_catalog_missing", "severity": "warning",
                       "summary": "no qlib catalog snapshot"})
    else:
        if update.get("status") not in (None, "current"):
            stage = update.get("failureStage")
            when = (update.get("time") or "")[:16].replace("T", " ")
            issues.append({"code": "qlib_status", "severity": "warning",
                           "summary": f"qlib update {update.get('status')}"
                                      + (f" at {stage}" if stage else "")
                                      + (f" ({when})" if when else "")
                                      + (f"；planned {update.get('plannedDate')}" if update.get("plannedDate") else "")
                                      + f"；数据停在 {qlib_latest}"})
        if qlib_latest is None:
            issues.append({"code": "qlib_calendar_missing", "severity": "warning",
                           "summary": "qlib calendar_last missing"})
    if snap.get("lag") not in (None, 0):
        issues.append({"code": "snapshot_lag", "severity": "warning",
                       "summary": f"qp snapshot lag {snap.get('lag')} day(s)"})
    if cmp.get("diffs") or cmp.get("missing") or (
            cmp.get("compared") is not None and cmp.get("expected") is not None
            and cmp["compared"] < cmp["expected"]):
        issues.append({"code": "compare_gap", "severity": "critical",
                       "summary": f"compare {cmp.get('compared')}/{cmp.get('expected')} "
                                  f"diffs={cmp.get('diffs')} missing={cmp.get('missing')}"})
    if idx[0] is None:
        issues.append({"code": "index_close_missing", "severity": "critical",
                       "summary": "no qp index_close rows"})
    elif days and idx[0] < days[-1]:
        issues.append({"code": "index_close_lag", "severity": "warning",
                       "summary": f"index_close {idx[0]} < latest qp open day {days[-1]}"})
    if cal is None:
        issues.append({"code": "calendar_missing", "severity": "warning",
                       "summary": "no qp trading calendar"})

    return {
        "at": utcnow().isoformat(),
        "qlib": {"latest": qlib_latest, "instruments": qd.get("instruments"),
                 "calendar_days": qd.get("calendar_days"), "active": qd.get("active_on_last"),
                 "status": update.get("status"), "target_date": update.get("targetDate"),
                 "planned_date": update.get("plannedDate"), "failure_stage": update.get("failureStage"),
                 "update_at": update.get("time"), "error": update.get("error")},
        "snapshot": {"latest": snap.get("latest"), "expected": snap.get("expected"), "lag": snap.get("lag")},
        "compare": {"compared": cmp.get("compared"), "expected": cmp.get("expected"),
                    "missing": cmp.get("missing"), "diffs": cmp.get("diffs")},
        "index_close": {"latest": idx[0].isoformat() if idx and idx[0] else None,
                        "symbols": int(idx[1]) if idx and idx[1] is not None else 0},
        "calendar": {"qp_max": cal.isoformat() if cal else None},
        "recent": recent,
        "issues": issues,
    }


@router.get("/dash/coverage", dependencies=[Depends(require_scope("ops:read"))])
def dash_coverage(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _qp_coverage(db)


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
