#!/usr/bin/env python3
"""Classify a research digest file as current or stale. No network."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")


def freshness(digest, now=None):
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        raise ValueError("now_requires_timezone")
    local = now.astimezone(CST)
    today = local.date().isoformat()
    target = digest.get("target_date") if isinstance(digest, dict) else None
    produced = digest.get("produced_at") if isinstance(digest, dict) else None
    as_of = digest.get("as_of") if isinstance(digest, dict) else None
    stale = not isinstance(target, str) or target != today
    reason = None
    if not isinstance(digest, dict):
        reason = "digest_not_object"
    elif not isinstance(target, str) or not target:
        reason = "target_date_missing"
    elif target != today:
        reason = "target_date_is_not_today"
    return {
        "stale": stale,
        "reason": reason,
        "target_date": target if isinstance(target, str) else None,
        "as_of": as_of if isinstance(as_of, str) else None,
        "produced_at": produced if isinstance(produced, str) else None,
        "checked_at": local.isoformat(),
        "today": today,
    }


def attach_freshness(digest, now=None):
    if not isinstance(digest, dict):
        raise ValueError("digest_not_object")
    marked = dict(digest)
    marked["freshness"] = freshness(digest, now)
    return marked
