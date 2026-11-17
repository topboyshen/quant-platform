"""M3 API: market data (read-only views over schema market)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth, require_scope
from qp.models.market import (
    AnnouncementCount,
    CompareResult,
    HealthResult,
    ImportLog,
    IndexClose,
    IndexMember,
    Instrument,
    LegacySnapshot,
    MarketSummary,
    QlibStatus,
    SourceStatus,
    TradingDay,
)
from qp.models.ops import Inventory

router = APIRouter(prefix="/api/market", dependencies=[Depends(require_scope("market:read"))])


def _row(o, cols):
    return {c: getattr(o, c) for c in cols}


@router.get("/instruments")
def instruments(q: str | None = None, board: str | None = None, in_qlib: bool | None = None, index: str | None = None,
                limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Instrument)
    if q:
        like = f"%{q.strip()[:32]}%"
        stmt = stmt.where(or_(Instrument.symbol.ilike(like), Instrument.name.ilike(like)))
    if board:
        stmt = stmt.where(Instrument.board == board)
    if in_qlib is not None:
        stmt = stmt.where(Instrument.in_qlib.is_(in_qlib))
    if index:
        last = db.scalar(select(func.max(IndexMember.end_date)).where(IndexMember.index_code == index))
        stmt = stmt.where(Instrument.symbol.in_(select(IndexMember.symbol).where(IndexMember.index_code == index,
                                                                                 IndexMember.end_date == last)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(Instrument.symbol).limit(limit).offset(offset)).scalars().all()
    boards = dict(db.execute(select(Instrument.board, func.count()).group_by(Instrument.board)).all())
    idx = dict(db.execute(select(IndexMember.index_code, func.count(func.distinct(IndexMember.symbol)))
                          .group_by(IndexMember.index_code)).all())
    return {"total": total, "boards": boards, "indexes": idx,
            "rows": [_row(r, ("symbol", "name", "exchange", "board", "qlib_start", "qlib_end", "in_qlib", "name_source",
                              "updated_at")) for r in rows]}


@router.get("/calendar")
def calendar(year: int | None = None, p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    year = year or dt.date.today().year
    lo, hi = dt.date(year, 1, 1), dt.date(year, 12, 31)
    out: dict[str, Any] = {"year": year}
    for origin in ("qp", "legacy", "qlib"):
        rows = db.execute(select(TradingDay.date, TradingDay.is_open).where(TradingDay.origin == origin,
                                                                           TradingDay.date.between(lo, hi))
                          .order_by(TradingDay.date)).all()
        out[origin] = {"open": [r[0] for r in rows if r[1]], "closed": [r[0] for r in rows if not r[1]]}
        meta = db.execute(select(func.min(TradingDay.date), func.max(TradingDay.date), func.count(), func.max(TradingDay.updated_at))
                          .where(TradingDay.origin == origin, TradingDay.is_open)).first()
        out[origin]["meta"] = {"first": meta[0], "last": meta[1], "open_days": meta[2], "updated_at": meta[3]}
    return out


@router.get("/index-close")
def index_close(days: int = Query(60, ge=1, le=400), p: Principal = Depends(require_auth),
                db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    since = dt.date.today() - dt.timedelta(days=days)
    rows = db.execute(select(IndexClose).where(IndexClose.trade_date >= since)
                      .order_by(IndexClose.trade_date, IndexClose.symbol, IndexClose.origin)).scalars()
    return [{"origin": r.origin, "symbol": r.symbol, "trade_date": r.trade_date, "close": float(r.close),
             "source": r.source, "fetched_at": r.fetched_at} for r in rows]


def _latest_run(db: Session, model) -> list[Any]:
    last = db.scalar(select(func.max(model.run_at)))
    return db.execute(select(model).where(model.run_at == last)).scalars().all() if last else []


@router.get("/health")
def health(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    checks = [_row(r, ("run_at", "check", "status", "summary", "detail")) for r in _latest_run(db, HealthResult)]
    hist = db.execute(select(HealthResult.run_at, HealthResult.status, func.count()).where(
        HealthResult.run_at > func.now() - dt.timedelta(days=14)).group_by(HealthResult.run_at, HealthResult.status)
        .order_by(HealthResult.run_at)).all()
    src = {}
    for r in db.execute(select(SourceStatus).where(SourceStatus.at > func.now() - dt.timedelta(days=3))
                        .order_by(SourceStatus.at)).scalars():
        src[r.source] = _row(r, ("source", "at", "ok", "via_proxy", "ms", "detail"))
    qs = {}
    for kind in ("catalog", "source"):
        r = db.execute(select(QlibStatus).where(QlibStatus.kind == kind).order_by(QlibStatus.id.desc()).limit(1)).scalar()
        qs[kind] = {"at": r.at, "data": r.data} if r else None
    lh = db.get(Inventory, "market.legacy_health")
    return {"checks": checks, "history": [{"run_at": a, "status": b, "n": c} for a, b, c in hist], "sources": src,
            "qlib": qs, "legacy_health": {"at": lh.at, "data": lh.data} if lh else None}


@router.get("/coverage")
def coverage(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    sums = db.execute(select(MarketSummary).order_by(MarketSummary.trade_date)).scalars().all()
    ann = db.execute(select(AnnouncementCount).order_by(AnnouncementCount.date.desc()).limit(30)).scalars().all()
    snaps = db.execute(select(LegacySnapshot).order_by(LegacySnapshot.date)).scalars().all()
    reader = db.get(Inventory, "market.reader")
    staging = db.get(Inventory, "market.cninfo_staging")
    return {
        "summaries": [{"origin": s.origin, "trade_date": s.trade_date, "coverage": (s.data or {}).get("coverage"),
                       "breadth": (s.data or {}).get("breadth"), "payload_sha256": s.payload_sha256,
                       "generated_at": s.generated_at} for s in sums],
        "announcements": [_row(a, ("date", "total", "fetched_at")) for a in ann],
        "legacy_snapshots": [_row(x, ("date", "generated_at", "payload_sha256", "payload_bytes")) for x in snaps],
        "reader": {"at": reader.at, "data": reader.data} if reader else None,
        "cninfo_staging": {"at": staging.at, "data": staging.data} if staging else None,
    }


@router.get("/compare")
def compare(runs: int = Query(6, ge=1, le=60), p: Principal = Depends(require_auth),
            db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    last = db.execute(select(CompareResult.run_at).group_by(CompareResult.run_at)
                      .order_by(CompareResult.run_at.desc()).limit(runs)).scalars().all()
    if not last:
        return []
    rows = db.execute(select(CompareResult).where(CompareResult.run_at.in_(last))
                      .order_by(CompareResult.run_at.desc(), CompareResult.item)).scalars()
    return [_row(r, ("run_at", "item", "status", "summary", "detail")) for r in rows]


@router.get("/compare/meta")
def compare_meta(p: Principal = Depends(require_scope("market:read")),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """Show how current the two sides of the compare actually are."""
    def latest_date(model, origin: str):
        return db.scalar(select(func.max(model.trade_date)).where(model.origin == origin))

    last_run = db.scalar(select(func.max(CompareResult.run_at)))
    return {"last_compare_at": last_run,
            "index_close": {"legacy": latest_date(IndexClose, "legacy"), "qp": latest_date(IndexClose, "qp")},
            "market_summary": {"legacy": latest_date(MarketSummary, "legacy"), "qp": latest_date(MarketSummary, "qp")},
            "legacy_chain_stopped_on": dt.date(2026, 11, 13)}


@router.get("/imports")
def imports(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    sub = select(ImportLog.item, func.max(ImportLog.id).label("mid")).group_by(ImportLog.item).subquery()
    latest = db.execute(select(ImportLog).join(sub, ImportLog.id == sub.c.mid).order_by(ImportLog.item)).scalars().all()
    first = db.execute(select(func.min(ImportLog.at))).scalar()
    cols = ("at", "item", "legacy_path", "legacy_file_sha256", "legacy_rows", "legacy_checksum", "db_rows", "db_checksum",
            "status")
    return {"first_import_at": first, "latest": [_row(r, cols) for r in latest]}
