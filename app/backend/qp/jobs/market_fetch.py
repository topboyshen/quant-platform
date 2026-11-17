"""Market fetch/probe jobs (calendar, index close, names, sources, cninfo, qlib)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from qp.jobs.market_common import CAL_FALLBACK_ORIGINS, DOLT_CAL_SOURCE, RETAIN_DAYS, d, now_cst
from qp.jobs.registry import JobContext, job
from qp.market.http import FetchError
from qp.models.market import AnnouncementCount, IndexClose, Instrument, QlibStatus, SourceStatus, TradingDay


def _m():
    from qp.jobs import market

    return market


# ---------------------------------------------------------------- fetch jobs (new platform, shadow)
@job(
    "market.calendar",
    "Trading calendar (SSE) from Dolt ts_trade_day_calendar -> market.trading_day[qp]",
    schedule_hint="06:30 CST daily (legacy 06:20)",
)
def calendar_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    today = now_cst().date()
    with Session(ctx.engine) as s:
        have = (
            s.scalar(
                select(func.count())
                .select_from(TradingDay)
                .where(TradingDay.origin == "qp", TradingDay.source == DOLT_CAL_SOURCE)
            )
            or 0
        )
    start = dt.date(2020, 1, 1) if have < 1000 else today - dt.timedelta(days=60)
    dolt_error = None
    try:
        rows = _m().src.dolt_calendar(start, dt.date(today.year + 1, 12, 31))
    except (FetchError, ValueError, TypeError, KeyError) as e:
        rows, dolt_error = [], str(e) or type(e).__name__
    if rows:
        with Session(ctx.engine) as s:
            # Dolt is authoritative once it succeeds.  Drop rows that only exist
            # because an earlier run mirrored the legacy/qlib calendar.
            s.execute(delete(TradingDay).where(TradingDay.origin == "qp", TradingDay.source.like("fallback:%")))
            stmt = insert(TradingDay).values(
                [{"origin": "qp", "date": x, "is_open": o, "source": DOLT_CAL_SOURCE} for x, o in rows]
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["origin", "date"],
                    set_={"is_open": stmt.excluded.is_open, "source": stmt.excluded.source, "updated_at": func.now()},
                )
            )
            s.commit()
            opened = [x for x, o in rows if o]
        ahead = (opened[-1] - today).days if opened else None
        return {
            "ok": True,
            "rows": len(rows),
            "source": DOLT_CAL_SOURCE,
            "dolt_error": dolt_error,
            "range": [rows[0][0].isoformat(), rows[-1][0].isoformat()],
            "max_open": opened[-1].isoformat() if opened else None,
            "days_ahead": ahead,
            "initial": have < 1000,
        }
    # Dolt unavailable: keep the platform calendar usable from the already-mirrored
    # legacy/qlib calendars.  This is an explicit, labelled fallback, not a Dolt claim.
    with Session(ctx.engine) as s:
        fb: dict[dt.date, str] = {}
        for origin in CAL_FALLBACK_ORIGINS:
            for day in s.execute(
                select(TradingDay.date).where(TradingDay.origin == origin, TradingDay.is_open)
            ).scalars():
                fb[day] = f"fallback:{origin}"
        if not fb:
            return {"ok": False, "error": "dolt_empty_and_no_calendar_fallback", "dolt_error": dolt_error or "empty"}
        stmt = insert(TradingDay).values(
            [{"origin": "qp", "date": x, "is_open": True, "source": source} for x, source in sorted(fb.items())]
        )
        s.execute(stmt.on_conflict_do_nothing(index_elements=["origin", "date"]))
        s.commit()
        opened = list(
            s.execute(
                select(TradingDay.date).where(TradingDay.origin == "qp", TradingDay.is_open).order_by(TradingDay.date)
            ).scalars()
        )
        sources = dict(
            s.execute(
                select(TradingDay.source, func.count()).where(TradingDay.origin == "qp").group_by(TradingDay.source)
            ).all()
        )
    ahead = (opened[-1] - today).days if opened else None
    return {
        "ok": True,
        "rows": len(fb),
        "source": "fallback",
        "fallback_sources": sorted(set(fb.values())),
        "dolt_error": dolt_error or "empty",
        "sources": sources,
        "range": [min(fb).isoformat(), max(fb).isoformat()] if fb else None,
        "max_open": opened[-1].isoformat() if opened else None,
        "days_ahead": ahead,
        "initial": have < 1000,
    }


@job(
    "market.index_close",
    "Four index closes (Tencent fqkline, Sina fallback) -> market.index_close[qp]",
    schedule_hint="Mon-Fri 16:30, 16:40 CST (legacy 15:35/45/55)",
)
def index_close_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    got, errors = [], {}
    fetched = _m().utcnow()
    now = now_cst()
    # never store an intraday bar as a close: today's row only after 15:30 CST (close + 30 min grace, as legacy)
    latest_ok = now.date() if now.time() >= dt.time(15, 30) else now.date() - dt.timedelta(days=1)
    for sym in _m().src.INDEXES:
        rows, source = None, None
        try:
            rows, source = _m().src.tencent_index(sym, 10), "tencent"
        except FetchError as e:
            errors[f"{sym}:tencent"] = str(e)
            try:
                rows, source = _m().src.sina_index(sym, 10), "sina"
            except FetchError as e2:
                errors[f"{sym}:sina"] = str(e2)
        for day, close in rows or []:
            if d(day) > latest_ok:
                continue
            got.append(
                {
                    "origin": "qp",
                    "symbol": sym,
                    "trade_date": d(day),
                    "close": round(close, 4),
                    "source": source,
                    "fetched_at": fetched,
                }
            )
    if got:
        with Session(ctx.engine) as s:
            stmt = insert(IndexClose).values(got)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["origin", "symbol", "trade_date"],
                    set_={
                        "close": stmt.excluded.close,
                        "source": stmt.excluded.source,
                        "fetched_at": stmt.excluded.fetched_at,
                        "updated_at": func.now(),
                    },
                )
            )
            s.commit()
    syms_ok = sorted({g["symbol"] for g in got})
    return {
        "ok": len(syms_ok) == len(_m().src.INDEXES),
        "rows": len(got),
        "symbols": syms_ok,
        "latest": max((g["trade_date"] for g in got), default=None).isoformat() if got else None,
        "errors": errors,
    }


@job(
    "market.instruments",
    "Instrument names from Tencent quote (Eastmoney clist fallback) -> market.instrument (qlib ranges come from ingest)",
    schedule_hint="Mon-Fri 17:10 CST",
)
def instruments_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    source, tencent_error = "tencent", None
    try:
        with Session(ctx.engine) as s:
            syms = list(
                s.scalars(select(Instrument.symbol).where(Instrument.in_qlib.is_(True)).order_by(Instrument.symbol))
            )
        names = _m().src.tencent_names(syms)
    except FetchError as e:
        # Tencent covers the instruments qp already tracks; Eastmoney can still list
        # the full market when Tencent rejects the production egress path.
        tencent_error = str(e)
        source = "eastmoney"
        names = _m().src.eastmoney_names()
    with Session(ctx.engine) as s:
        vals = [
            {"symbol": k, "name": v[:64], "exchange": k[:2], "board": board_of(k), "name_source": source}
            for k, v in names.items()
        ]
        stmt = insert(Instrument).values(vals)
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=["symbol"],
                set_={
                    "name": stmt.excluded.name,
                    "exchange": stmt.excluded.exchange,
                    "board": stmt.excluded.board,
                    "name_source": stmt.excluded.name_source,
                    "updated_at": func.now(),
                },
            )
        )
        s.commit()
    out = {"names": len(names), "source": source}
    if tencent_error:
        out["tencent_error"] = tencent_error
    return out


def board_of(sym: str) -> str:
    code = sym[2:]
    if sym.startswith("BJ") or code.startswith(("4", "8", "92")):
        return "bse"
    if code.startswith("688") or code.startswith("689"):
        return "star"
    if code.startswith(("300", "301", "302")):
        return "chinext"
    if (sym.startswith("SH") and code.startswith("000")) or (sym.startswith("SZ") and code.startswith("399")):
        return "index"
    return "main"


@job(
    "market.sources",
    "Reachability/freshness probe of legacy data sources -> market.source_status",
    schedule_hint="06:55, 09:05, 13:05, 19:05 CST",
)
def sources_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    res = _m().src.probe_all()
    with Session(ctx.engine) as s:
        for r in res:
            s.add(
                SourceStatus(source=r["source"], ok=r["ok"], via_proxy=r["via_proxy"], ms=r["ms"], detail=r["detail"])
            )
        s.execute(delete(SourceStatus).where(SourceStatus.at < _m().utcnow() - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    return {
        "ok_count": sum(r["ok"] for r in res),
        "total": len(res),
        "failed": [r["source"] for r in res if not r["ok"]],
    }


@job(
    "market.cninfo",
    "CNINFO announcement count for today (metadata only; direct like legacy)",
    schedule_hint="08:30 CST daily (legacy 07:50)",
)
def cninfo_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    day = now_cst().date()
    total = _m().src.cninfo_count(day)
    with Session(ctx.engine) as s:
        stmt = insert(AnnouncementCount).values(date=day, total=total, detail={"scope": "szse+sse fulltext"})
        s.execute(stmt.on_conflict_do_update(index_elements=["date"], set_={"total": total, "fetched_at": func.now()}))
        s.commit()
    return {"date": day.isoformat(), "total": total}


@job(
    "market.qlib_check",
    "Qlib update shadow (compare-only, no data copy): Dolt latest EOD + GitHub release tag",
    schedule_hint="06:40, 23:20 CST (legacy 06:30 retry / 22:00 nightly)",
)
def qlib_check_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    out: dict[str, Any] = {}
    try:
        out["dolt_latest_eod"] = _m().src.dolt_latest_eod()
    except FetchError as e:
        out["dolt_error"] = str(e)
    try:
        out["github_release_tag"] = _m().src.github_release_tag()
    except FetchError as e:
        out["github_error"] = str(e)
    if out.get("dolt_latest_eod"):
        out["latest_eod"], out["latest_eod_source"] = out["dolt_latest_eod"], "dolt"
    elif out.get("github_release_tag"):
        # DoltHub SQL API is frequently 504/timeout; the GitHub release tag is the
        # same upstream EOD date and is the documented release fallback.
        out["latest_eod"], out["latest_eod_source"] = out["github_release_tag"], "github_release"
    with Session(ctx.engine) as s:
        s.add(QlibStatus(kind="source", data=out))
        s.commit()
    out["ok"] = "latest_eod" in out
    return out
