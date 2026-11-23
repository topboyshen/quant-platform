"""Read-only ETF research score shadow (M15).

Scores are produced by offline, frozen models and imported as snapshots.  The
20d and 5d models are independent artifacts; each snapshot is scoped by label
horizon so one cannot masquerade as the other.  qp only validates, stores and
displays them; it never trains, orders, notifies or writes
target_decision/the record chain.  A stale snapshot is shown as stale.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.models.market import TradingDay
from qp.models.research import EtfScore, EtfScoreRun
from qp.signals.eod import now_cst
from qp.signals.universe import TARGETS

CODES = {target.code for target in TARGETS}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
UNIVERSE = "fixed_40_current_pool"
DISCLAIMER = "研究只读评分：固定当前 ETF 池、离线模型快照；不是交易信号，不下单、不通知、不写正式记录链。"
SCORE_READY_TIME = dt.time(17, 15)


class ScorePayloadError(ValueError):
    pass


def _date(value: Any, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise ScorePayloadError(f"bad_{field}") from None


def _datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ScorePayloadError("bad_generated_at") from None
    if parsed.tzinfo is None:
        raise ScorePayloadError("generated_at_naive")
    return parsed


def _scores_sha256(rows: list[dict[str, Any]]) -> str:
    """Content hash of the score table, independent of payload key order."""

    def score_value(value: Any) -> float | None:
        if value is None:
            return None
        number = round(float(value), 8)
        return 0.0 if number == 0 else number

    canonical = sorted(
        (
            {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "score": score_value(row["score"]),
                "rank": row["rank"],
                "eligible": bool(row["eligible"]),
                "reason": str(row["reason"]),
            }
            for row in rows
        ),
        key=lambda row: row["code"],
    )
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ScorePayloadError("bad_score")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScorePayloadError("bad_score") from None
    if not math.isfinite(number):
        raise ScorePayloadError("non_finite_score")
    return number


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScorePayloadError("payload_not_object")
    version = str(payload.get("model_version") or "").strip()
    if not version or len(version) > 64:
        raise ScorePayloadError("bad_model_version")
    model_sha = str(payload.get("model_sha256") or "").strip().lower()
    if not HEX64.match(model_sha):
        raise ScorePayloadError("bad_model_sha256")
    text_sha = str(payload.get("model_text_sha256") or "").strip().lower()
    if text_sha and not HEX64.match(text_sha):
        raise ScorePayloadError("bad_model_text_sha256")
    universe = str(payload.get("universe") or "").strip()
    if universe != UNIVERSE:
        raise ScorePayloadError("bad_universe")
    cutoff = _date(payload.get("data_cutoff"), "data_cutoff")
    if cutoff > now_cst().date():
        raise ScorePayloadError("data_cutoff_in_future")
    train_start = _date(payload.get("train_start"), "train_start")
    train_end = _date(payload.get("train_end"), "train_end")
    if train_start > train_end:
        raise ScorePayloadError("bad_train_window")
    if train_end > cutoff:
        raise ScorePayloadError("train_end_after_cutoff")
    horizon = payload.get("label_horizon")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 250:
        raise ScorePayloadError("bad_label_horizon")
    snapshot = str(payload.get("data_master_snapshot") or "").strip()
    if not snapshot or len(snapshot) > 64:
        raise ScorePayloadError("bad_data_master_snapshot")
    scores = payload.get("scores")
    if not isinstance(scores, list) or not scores:
        raise ScorePayloadError("scores_missing")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    ranks: set[int] = set()
    eligible = 0
    for item in scores:
        if not isinstance(item, dict):
            raise ScorePayloadError("bad_score_row")
        code = str(item.get("code") or "").strip()
        if code not in CODES:
            raise ScorePayloadError(f"unknown_code:{code}")
        if code in seen:
            raise ScorePayloadError(f"duplicate_code:{code}")
        seen.add(code)
        is_eligible = item.get("eligible")
        if not isinstance(is_eligible, bool):
            raise ScorePayloadError(f"bad_eligible_flag:{code}")
        score = _number(item.get("score"))
        rank = item.get("rank")
        reason = str(item.get("reason") or "")[:128]
        if is_eligible:
            eligible += 1
            if score is None:
                raise ScorePayloadError(f"eligible_score_missing:{code}")
            if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
                raise ScorePayloadError(f"bad_rank:{code}")
            if rank in ranks:
                raise ScorePayloadError(f"duplicate_rank:{rank}")
            ranks.add(rank)
        else:
            if rank is not None:
                raise ScorePayloadError(f"ineligible_rank:{code}")
            if not reason:
                reason = "not_eligible"
        clean.append(
            {
                "code": code,
                "name": str(item.get("name") or "")[:64],
                "score": score,
                "rank": rank,
                "eligible": is_eligible,
                "reason": reason,
            }
        )
    missing = sorted(CODES - seen)
    if missing:
        raise ScorePayloadError(f"missing_codes:{len(missing)}")
    if eligible < 8:
        raise ScorePayloadError("fewer_than_8_eligible")
    if sorted(ranks) != list(range(1, eligible + 1)):
        raise ScorePayloadError("ranks_not_contiguous")
    return {
        "model_version": version,
        "model_sha256": model_sha,
        "model_text_sha256": text_sha or None,
        "universe": universe,
        "data_cutoff": cutoff,
        "generated_at": _datetime(payload.get("generated_at")),
        "label_horizon": horizon,
        "train_start": train_start,
        "train_end": train_end,
        "data_master_snapshot": snapshot,
        "metrics": payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
        "notes": str(payload.get("notes") or "")[:2000],
        "scores": clean,
        "scores_sha256": _scores_sha256(clean),
    }


def import_payload(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    data = validate(payload)
    existing = session.scalar(
        select(EtfScoreRun).where(
            EtfScoreRun.model_version == data["model_version"], EtfScoreRun.data_cutoff == data["data_cutoff"]
        )
    )
    if existing is not None:
        # The same identity must carry the same artifact and the same content;
        # a retrain or rescore under an unchanged version is a conflict, not a
        # harmless duplicate.
        if existing.model_sha256 != data["model_sha256"]:
            raise ScorePayloadError("duplicate_model_sha256_mismatch")
        stored = [
            {
                "code": row.code,
                "name": row.name,
                "score": row.score,
                "rank": row.rank,
                "eligible": row.eligible,
                "reason": row.reason,
            }
            for row in session.scalars(select(EtfScore).where(EtfScore.run_id == existing.id))
        ]
        if _scores_sha256(stored) != data["scores_sha256"]:
            raise ScorePayloadError("duplicate_content_mismatch")
        return {
            "ok": True,
            "skipped": "duplicate",
            "run_id": existing.id,
            "model_version": existing.model_version,
            "data_cutoff": existing.data_cutoff.isoformat(),
            "content_sha256": data["scores_sha256"],
        }
    run = EtfScoreRun(
        model_version=data["model_version"],
        model_sha256=data["model_sha256"],
        data_cutoff=data["data_cutoff"],
        generated_at=data["generated_at"],
        label_horizon=data["label_horizon"],
        train_start=data["train_start"],
        train_end=data["train_end"],
        data_master_snapshot=data["data_master_snapshot"],
        metrics=data["metrics"],
        notes=data["notes"],
    )
    session.add(run)
    session.flush()
    for row in data["scores"]:
        session.add(EtfScore(run_id=run.id, **row))
    return {
        "ok": True,
        "run_id": run.id,
        "model_version": run.model_version,
        "data_cutoff": run.data_cutoff.isoformat(),
        "eligible": sum(1 for row in data["scores"] if row["eligible"]),
        "scores": len(data["scores"]),
        "content_sha256": data["scores_sha256"],
    }


def expected_session(today: dt.date) -> dt.date:
    """Most recent weekday on or before today.

    Holidays are not modelled here; the offline scorer owns the full trading
    calendar.  A conservative weekday expectation is enough to flag a snapshot
    that is older than the last plausible session.
    """
    day = today
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def expected_score_session(session: Session, now: dt.datetime | None = None) -> dt.date:
    """Expected score cutoff for the current moment.

    Before the scoring window closes, today's offline score is not due yet.
    The expected cutoff is the previous open session, so a previous snapshot is
    not falsely marked stale during the trading day.  After 17:15 the current
    open session becomes expected.
    """
    current = now or now_cst()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    today = current.date()
    ready = current.time() >= SCORE_READY_TIME
    stmt = select(func.max(TradingDay.date)).where(TradingDay.origin == "qp", TradingDay.is_open.is_(True))
    stmt = stmt.where(TradingDay.date <= today if ready else TradingDay.date < today)
    value = session.scalar(stmt)
    if value is not None:
        return value
    day = today if ready else today - dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def horizons(session: Session) -> list[int]:
    """Label horizons that currently have at least one imported snapshot."""
    return [
        int(h)
        for h in session.scalars(select(EtfScoreRun.label_horizon).distinct().order_by(EtfScoreRun.label_horizon))
    ]


def latest(
    session: Session, *, run_id: int | None = None, horizon: int | None = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    """Latest snapshot, optionally scoped to one label horizon.

    The 5d and 20d shadows are independent model artifacts.  Without a filter
    the newest snapshot wins (historical behaviour); the page and API pass an
    explicit horizon so one model can never masquerade as the other.
    """
    stmt = select(EtfScoreRun)
    if horizon is not None:
        stmt = stmt.where(EtfScoreRun.label_horizon == horizon)
    run = (
        session.get(EtfScoreRun, run_id)
        if run_id
        else session.scalar(stmt.order_by(EtfScoreRun.data_cutoff.desc(), EtfScoreRun.id.desc()).limit(1))
    )
    available = horizons(session)
    if run is None:
        return {
            "read_only": True,
            "is_trade_signal": False,
            "disclaimer": DISCLAIMER,
            "run": None,
            "scores": [],
            "history": [],
            "horizon": horizon,
            "horizons": available,
        }
    rows = list(
        session.scalars(
            select(EtfScore)
            .where(EtfScore.run_id == run.id)
            .order_by(EtfScore.rank.is_(None), EtfScore.rank, EtfScore.score.desc())
        )
    )
    history = []
    for r in session.scalars(
        select(EtfScoreRun)
        .where(EtfScoreRun.label_horizon == run.label_horizon)
        .order_by(EtfScoreRun.data_cutoff.desc(), EtfScoreRun.id.desc())
        .limit(20)
    ):
        eligible_count = (
            session.scalar(
                select(func.count()).select_from(EtfScore).where(EtfScore.run_id == r.id, EtfScore.eligible.is_(True))
            )
            or 0
        )
        history.append(
            {
                "id": r.id,
                "model_version": r.model_version,
                "data_cutoff": r.data_cutoff.isoformat(),
                "generated_at": r.generated_at,
                "eligible_count": int(eligible_count),
                "created_at": r.created_at,
            }
        )
    current = now or now_cst()
    expected = expected_score_session(session, now=current)
    today = current.date()
    today_open = (
        session.scalar(
            select(func.count())
            .select_from(TradingDay)
            .where(TradingDay.origin == "qp", TradingDay.date == today, TradingDay.is_open.is_(True))
        )
        or 0
    )
    pending_today = bool(
        today_open and current.time() < SCORE_READY_TIME and expected < today and run.data_cutoff < today
    )
    return {
        "read_only": True,
        "is_trade_signal": False,
        "disclaimer": DISCLAIMER,
        "horizon": run.label_horizon,
        "horizons": available,
        "run": {
            "id": run.id,
            "model_version": run.model_version,
            "model_sha256": run.model_sha256,
            "data_cutoff": run.data_cutoff.isoformat(),
            "generated_at": run.generated_at,
            "label_horizon": run.label_horizon,
            "train_start": run.train_start.isoformat() if run.train_start else None,
            "train_end": run.train_end.isoformat() if run.train_end else None,
            "data_master_snapshot": run.data_master_snapshot,
            "stale": run.data_cutoff < expected,
            "expected_cutoff": expected.isoformat(),
            "staleness_days": (expected - run.data_cutoff).days,
            "score_ready_at": SCORE_READY_TIME.strftime("%H:%M"),
            "pending_today": pending_today,
            "metrics": run.metrics,
            "notes": run.notes,
            "created_at": run.created_at,
        },
        "scores": [
            {
                "code": row.code,
                "name": row.name,
                "score": float(row.score) if row.score is not None else None,
                "rank": row.rank,
                "eligible": row.eligible,
                "reason": row.reason,
            }
            for row in rows
        ],
        "history": history,
    }
