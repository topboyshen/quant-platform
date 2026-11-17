"""Shadow comparison for the record chain: snapshot funnel vs legacy signals tables.

Used during the shadow window; a difference is only acceptable when it is
explained (for example ``admitted`` excludes decisions whose run was not a
success while the legacy ``allowed`` column does not know about runs).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.models.chain import ChainSnapshot
from qp.models.signals import DecisionOutcome, EodRun, TargetDecision


def _legacy_counts(session: Session, business_date: dt.date) -> dict[str, int]:
    rows = dict(
        session.execute(
            select(TargetDecision.decision_state, func.count())
            .where(TargetDecision.decision_date == business_date)
            .group_by(TargetDecision.decision_state)
        ).all()
    )
    triggered = (
        session.scalar(
            select(func.count())
            .select_from(TargetDecision)
            .where(TargetDecision.decision_date == business_date, TargetDecision.raw_trigger_count > 0)
        )
        or 0
    )
    runs = session.scalar(select(func.count()).select_from(EodRun).where(EodRun.decision_date == business_date)) or 0
    outcomes = (
        session.scalar(
            select(func.count())
            .select_from(DecisionOutcome)
            .join(TargetDecision, TargetDecision.id == DecisionOutcome.decision_id)
            .where(TargetDecision.decision_date == business_date)
        )
        or 0
    )
    return {
        "checked": sum(rows.values()),
        "triggered": int(triggered),
        "risk_blocked": int(rows.get("blocked", 0)),
        "risk_allowed": int(rows.get("allowed", 0)),
        "admitted": int(rows.get("allowed", 0)),
        "runs": int(runs),
        "outcomes": int(outcomes),
    }


def compare(session: Session, business_date: dt.date) -> dict[str, Any]:
    snapshot = session.scalar(
        select(ChainSnapshot)
        .where(ChainSnapshot.business_date == business_date)
        .order_by(ChainSnapshot.generated_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return {"ok": False, "reason": "no_snapshot", "business_date": business_date.isoformat()}
    funnel = (snapshot.body or {}).get("funnel") or {}
    legacy = _legacy_counts(session, business_date)
    checks = []
    for item in ("checked", "triggered", "risk_blocked", "risk_allowed", "admitted"):
        chain_value = int(funnel.get(item, 0))
        legacy_value = legacy[item]
        checks.append(
            {"item": item, "chain": chain_value, "legacy": legacy_value, "match": chain_value == legacy_value}
        )
    diffs = [c["item"] for c in checks if not c["match"]]
    return {
        "ok": not diffs,
        "business_date": business_date.isoformat(),
        "snapshot_id": snapshot.snapshot_id,
        "status": (snapshot.body or {}).get("status"),
        "checks": checks,
        "diffs": diffs,
        "legacy": legacy,
    }
