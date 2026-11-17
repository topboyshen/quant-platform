"""Opaque session tokens (cookie) and service tokens; only sha256 of tokens is stored."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from qp.models.base import utcnow
from qp.models.core import ServiceToken, Session, User


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: DbSession, user: User, ttl_hours: int, ip: str | None, ua: str | None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        Session(
            user_id=user.id,
            token_sha256=token_hash(token),
            expires_at=utcnow() + dt.timedelta(hours=ttl_hours),
            ip=ip,
            user_agent=(ua or "")[:500],
        )
    )
    return token


def resolve_session(db: DbSession, token: str | None) -> User | None:
    if not token:
        return None
    row = db.execute(
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(
            Session.token_sha256 == token_hash(token),
            Session.revoked_at.is_(None),
            Session.expires_at > utcnow(),
            User.is_active.is_(True),
        )
    ).first()
    if not row:
        return None
    sess, user = row
    sess.last_seen_at = utcnow()
    return user


def revoke_session(db: DbSession, token: str | None) -> None:
    if token:
        db.execute(
            update(Session)
            .where(Session.token_sha256 == token_hash(token), Session.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )


def create_service_token(db: DbSession, name: str, scopes: list[str]) -> str:
    token = "qpst_" + secrets.token_urlsafe(32)
    db.add(ServiceToken(name=name, token_sha256=token_hash(token), scopes=scopes))
    return token


def resolve_service_token(db: DbSession, token: str | None) -> ServiceToken | None:
    if not token or not token.startswith("qpst_"):
        return None
    st = db.execute(
        select(ServiceToken).where(ServiceToken.token_sha256 == token_hash(token), ServiceToken.revoked_at.is_(None))
    ).scalar_one_or_none()
    if st is None or (st.expires_at is not None and st.expires_at <= utcnow()):
        return None
    st.last_used_at = utcnow()
    return st
