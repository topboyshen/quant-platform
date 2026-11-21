"""ETF record chain identity and append-only primitives (M10).

Constants, hashing, deterministic identities and the low-level policy,
universe, batch and state appenders.  Writer and snapshot modules build on
these helpers; the public ``qp.signals.chain`` facade re-exports them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.models.chain import (
    ChainAttempt,
    ChainBatch,
    ChainPolicy,
    ChainState,
    ChainUniverse,
    ChainUniverseMember,
)
from qp.models.market import TradingDay
from qp.models.signals import EodRun

CST = dt.timezone(dt.timedelta(hours=8))
PROTOCOL_VERSION = "qp.record-chain.v1"
RULE_VERSION = "qp-swing.v1"
ELIGIBILITY_POLICY = "eligibility.v1"
STATS_POLICY = "stats.snapshot.v1"
ENGINE_VERSION = "qp-chain.v1"
HORIZONS = (1, 3, 5, 10, 20)
SAMPLE_THRESHOLD = 30
OOS_SPLITS = frozenset({"live_oos", "walk_forward_oos"})
WINDOW_EARLIEST = dt.time(15, 25)

ELIGIBILITY_DEFINITION = {
    "policy_version": ELIGIBILITY_POLICY,
    "protocol_version": PROTOCOL_VERSION,
    "candidate": "signal=triggered",
    "primary_horizon_sessions": 20,
    "min_samples": 30,
    "oos_splits": sorted(OOS_SPLITS),
    "require_full_batch": True,
    "window_earliest": WINDOW_EARLIEST.strftime("%H:%M:%S") + "+08:00",
    "precedence": [
        "attribution_unknown",
        "invalid_record",
        "data_incomplete",
        "no_trigger",
        "run_incomplete",
        "outside_decision_window",
        "non_oos",
        "risk_blocked",
        "eligible",
    ],
}
POLICIES: dict[str, tuple[str, dict[str, Any]]] = {
    ELIGIBILITY_POLICY: ("eligibility", ELIGIBILITY_DEFINITION),
    STATS_POLICY: ("stats", {"protocol_version": PROTOCOL_VERSION, "cutoff": "15:50", "primary_horizon_sessions": 20}),
    RULE_VERSION: ("rule", {"engine": "qp-swing", "horizons": list(HORIZONS), "universe": "etf40"}),
    ENGINE_VERSION: ("engine", {"name": "qp-record-chain", "version": "v1"}),
}


def enabled() -> bool:
    return bool(get_settings().record_chain)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uid(prefix: str, payload: Any) -> str:
    return prefix + sha256_hex(canonical_json(payload))[:32]


def input_evidence_digest(
    *,
    run_id: int,
    decision_date: dt.date,
    code: str,
    data_status: str,
    raw_trigger_count: int,
    raw_signal_count: int,
    reference_close: Any,
) -> str:
    """Stable digest of the decision inputs (same fields the migration can recompute)."""
    ref = "" if reference_close is None else f"{float(reference_close):.4f}"
    payload = "|".join(
        [
            str(int(run_id)),
            decision_date.isoformat(),
            str(code),
            str(data_status),
            str(int(raw_trigger_count or 0)),
            str(int(raw_signal_count)),
            ref,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decision_uid(trade_date: dt.date, code: str, batch_key: str, rule_version: str, universe_version: str) -> str:
    return _uid(
        "qd_",
        {
            "trade_date": trade_date.isoformat(),
            "code": code,
            "batch_key": batch_key,
            "rule_version": rule_version,
            "universe_version": universe_version,
        },
    )


def universe_version(trade_date: dt.date, codes: Iterable[str]) -> str:
    return "univ_" + sha256_hex(trade_date.isoformat() + "|" + ",".join(sorted(codes)))[:12]


def natural_batch_key(trade_date: dt.date) -> str:
    return f"eod:{trade_date.isoformat()}:15:30"


def _at(session: Session, model, *where) -> Any:
    return session.scalar(select(model).where(*where).limit(1))


def _ensure_policies(session: Session, now: dt.datetime) -> None:
    for version, (kind, definition) in POLICIES.items():
        session.execute(
            pg_insert(ChainPolicy)
            .values(
                policy_version=version,
                kind=kind,
                definition=definition,
                definition_sha256=sha256_hex(canonical_json(definition)),
                effective_from=now.isoformat(),
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["policy_version"])
        )


def _ensure_universe(session: Session, trade_date: dt.date, rows: Sequence[dict[str, Any]], now: dt.datetime) -> str:
    codes = [str(row["code"]) for row in rows]
    version = universe_version(trade_date, codes)
    payload = {"trade_date": trade_date.isoformat(), "codes": sorted(codes)}
    session.execute(
        pg_insert(ChainUniverse)
        .values(
            universe_version=version,
            trade_date=trade_date,
            code_count=len(codes),
            checksum=sha256_hex(canonical_json(payload)),
            source="qp-signals",
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["universe_version"])
    )
    for row in rows:
        session.execute(
            pg_insert(ChainUniverseMember)
            .values(universe_version=version, code=str(row["code"]), name=row.get("name"), sector=row.get("sector"))
            .on_conflict_do_nothing(index_elements=["universe_version", "code"])
        )
    return version


def _ensure_batch(session: Session, run: EodRun, universe: str, source: str, now: dt.datetime) -> str:
    started = run.started_at.astimezone(CST) if run.started_at else now.astimezone(CST)
    if source == "manual":
        key = f"manual:{run.decision_date.isoformat()}:{started.strftime('%H%M')}:{run.run_id}"
    else:
        key = natural_batch_key(run.decision_date)
    scheduled = dt.datetime.combine(run.decision_date, dt.time(15, 30), tzinfo=CST)
    session.execute(
        pg_insert(ChainBatch)
        .values(
            batch_key=key,
            trade_date=run.decision_date,
            scheduled_at=scheduled,
            universe_version=universe,
            expected_target_count=max(
                1,
                len(
                    session.scalars(
                        select(ChainUniverseMember.code).where(ChainUniverseMember.universe_version == universe)
                    ).all()
                ),
            ),
            source=source,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["batch_key"])
    )
    return key


def _append_state(
    session: Session,
    *,
    decision_uid: str,
    attempt_id: str,
    aspect: str,
    state: str,
    observed_at: dt.datetime,
    reason_code: str | None = None,
    basis: dict[str, Any] | None = None,
    evidence: list[Any] | None = None,
) -> bool:
    seen = session.scalar(
        select(ChainState.state_id)
        .where(
            ChainState.decision_uid == decision_uid,
            ChainState.aspect == aspect,
            ChainState.source_attempt_id == attempt_id,
        )
        .limit(1)
    )
    if seen:
        return False
    seq = (
        session.scalar(
            select(func.max(ChainState.state_seq)).where(
                ChainState.decision_uid == decision_uid, ChainState.aspect == aspect
            )
        )
        or 0
    ) + 1
    session.add(
        ChainState(
            state_id=_uid("st_", {"decision_uid": decision_uid, "aspect": aspect, "seq": seq}),
            decision_uid=decision_uid,
            source_attempt_id=attempt_id,
            aspect=aspect,
            state=state,
            reason_code=reason_code,
            basis=basis or {},
            evidence_refs=evidence or [],
            protocol_version=PROTOCOL_VERSION,
            observed_at=observed_at,
            state_seq=seq,
        )
    )
    session.flush()
    return True


def _upcoming_open_days(session: Session, after: dt.date, count: int = max(HORIZONS)) -> list[dt.date]:
    return list(
        session.scalars(
            select(TradingDay.date)
            .where(TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date > after)
            .order_by(TradingDay.date)
            .limit(count)
        ).all()
    )


def _attempt_run_statuses(session: Session, decision_uid: str) -> list[str]:
    return list(
        session.scalars(
            select(EodRun.status)
            .join(ChainAttempt, ChainAttempt.run_id == EodRun.run_id)
            .where(ChainAttempt.decision_uid == decision_uid)
        ).all()
    )
