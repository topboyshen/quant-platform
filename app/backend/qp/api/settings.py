"""M7 API: read-only settings/account view."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth
from qp.config import get_settings
from qp.models.core import User
from qp.release import release_info

router = APIRouter(prefix="/api/settings")


@router.get("")
def settings(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.username == p.name)) if p.kind == "user" else None
    mode = get_settings().notify_mode
    info = release_info()
    return {
        "account": {
            "username": p.name,
            "kind": p.kind,
            "role": p.role or (user.role if user else None),
            "hash_scheme": user.hash_scheme if user else None,
            "last_login_at": user.last_login_at if user else None,
            "password_changed_at": user.password_changed_at if user else None,
        },
        "platform": {
            "version": info["version"],
            "release_sha": info["sha"],
            "notify_mode": mode,
            "shadow_only": mode == "shadow",
        },
    }
