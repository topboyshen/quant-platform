"""Read-only ETF research score shadow (M15).

Scores are produced by an offline, frozen model and imported as snapshots.  qp
only validates, stores and displays them; it never trains, orders, notifies or
writes target_decision/the record chain.  A stale snapshot is shown as stale.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.models.research import EtfScore, EtfScoreRun
from qp.signals.eod import now_cst
from qp.signals.universe import TARGETS

CODES = {target.code for target in TARGETS}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DISCLAIMER = ("研究只读评分：固定当前 ETF 池、离线模型快照；不是交易信号，"
              "不下单、不通知、不写正式记录链。")


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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _number(value: Any) -> float | None:
    if value is None:
        return None
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
    cutoff = _date(payload.get("data_cutoff"), "data_cutoff")
    if cutoff > now_cst().date():
        raise ScorePayloadError("data_cutoff_in_future")
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
        is_eligible = bool(item.get("eligible"))
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
        clean.append({"code": code, "name": str(item.get("name") or "")[:64],
                      "score": score, "rank": rank, "eligible": is_eligible,
                      "reason": reason})
    if eligible < 8:
        raise ScorePayloadError("fewer_than_8_eligible")
    if sorted(ranks) != list(range(1, eligible + 1)):
        raise ScorePayloadError("ranks_not_contiguous")
    return {
        "model_version": version,
        "model_sha256": model_sha,
        "data_cutoff": cutoff,
        "generated_at": _datetime(payload.get("generated_at")),
        "label_horizon": int(payload.get("label_horizon") or 20),
        "train_start": _date(payload["train_start"], "train_start") if payload.get("train_start") else None,
        "train_end": _date(payload["train_end"], "train_end") if payload.get("train_end") else None,
        "data_master_snapshot": (str(payload["data_master_snapshot"])[:64]
                                 if payload.get("data_master_snapshot") else None),
        "metrics": payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
        "notes": str(payload.get("notes") or "")[:2000],
        "scores": clean,
    }


def import_payload(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    data = validate(payload)
    existing = session.scalar(select(EtfScoreRun).where(
        EtfScoreRun.model_version == data["model_version"],
        EtfScoreRun.data_cutoff == data["data_cutoff"]))
    if existing is not None:
        return {"ok": True, "skipped": "duplicate", "run_id": existing.id,
                "model_version": existing.model_version,
                "data_cutoff": existing.data_cutoff.isoformat()}
    run = EtfScoreRun(
        model_version=data["model_version"], model_sha256=data["model_sha256"],
        data_cutoff=data["data_cutoff"], generated_at=data["generated_at"],
        label_horizon=data["label_horizon"], train_start=data["train_start"],
        train_end=data["train_end"], data_master_snapshot=data["data_master_snapshot"],
        metrics=data["metrics"], notes=data["notes"])
    session.add(run)
    session.flush()
    for row in data["scores"]:
        session.add(EtfScore(run_id=run.id, **row))
    return {"ok": True, "run_id": run.id, "model_version": run.model_version,
            "data_cutoff": run.data_cutoff.isoformat(),
            "eligible": sum(1 for row in data["scores"] if row["eligible"]),
            "scores": len(data["scores"])}


def latest(session: Session, *, run_id: int | None = None) -> dict[str, Any]:
    run = session.get(EtfScoreRun, run_id) if run_id else session.scalar(
        select(EtfScoreRun).order_by(EtfScoreRun.data_cutoff.desc(),
                                     EtfScoreRun.id.desc()).limit(1))
    if run is None:
        return {"read_only": True, "is_trade_signal": False, "disclaimer": DISCLAIMER,
                "run": None, "scores": [], "history": []}
    rows = list(session.scalars(select(EtfScore).where(EtfScore.run_id == run.id)
                               .order_by(EtfScore.rank.is_(None), EtfScore.rank,
                                         EtfScore.score.desc())))
    history = []
    for r in session.scalars(select(EtfScoreRun).order_by(EtfScoreRun.data_cutoff.desc(),
                                                           EtfScoreRun.id.desc()).limit(20)):
        eligible_count = session.scalar(select(func.count()).select_from(EtfScore).where(
            EtfScore.run_id == r.id, EtfScore.eligible.is_(True))) or 0
        history.append({"id": r.id, "model_version": r.model_version,
                        "data_cutoff": r.data_cutoff.isoformat(),
                        "generated_at": r.generated_at, "eligible_count": int(eligible_count),
                        "created_at": r.created_at})
    return {
        "read_only": True, "is_trade_signal": False, "disclaimer": DISCLAIMER,
        "run": {"id": run.id, "model_version": run.model_version,
                "model_sha256": run.model_sha256, "data_cutoff": run.data_cutoff.isoformat(),
                "generated_at": run.generated_at, "label_horizon": run.label_horizon,
                "train_start": run.train_start.isoformat() if run.train_start else None,
                "train_end": run.train_end.isoformat() if run.train_end else None,
                "data_master_snapshot": run.data_master_snapshot,
                "metrics": run.metrics, "notes": run.notes, "created_at": run.created_at},
        "scores": [{"code": row.code, "name": row.name,
                    "score": float(row.score) if row.score is not None else None,
                    "rank": row.rank, "eligible": row.eligible, "reason": row.reason}
                   for row in rows],
        "history": history,
    }
