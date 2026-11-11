"""M3 market-data jobs (SHADOW). Write only to db qp (schema market + ops alerts/outbox shadow).
Never write to legacy Qlib, research-data or any legacy file. Legacy outputs arrive read-only via
qp-market-read (ubuntu) -> /var/lib/qp-inbox/market-legacy.json -> market.ingest_legacy."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from qp.jobs.inventory import apply_alerts, set_inventory
from qp.jobs.registry import JobContext, job
from qp.market import sources as src
from qp.market.http import FetchError
from qp.models.base import utcnow
from qp.models.market import (AnnouncementCount, CompareResult, HealthResult, ImportLog, IndexClose, IndexMember,
                              Instrument, LegacySnapshot, MarketSummary, QlibStatus, SourceStatus, TradingDay)
from qp.models.ops import JobRun

CST = ZoneInfo("Asia/Shanghai")
MARKET_INBOX = Path(os.getenv("QP_MARKET_INBOX_FILE", "/var/lib/qp-inbox/market-legacy.json"))
MIN_MEM_MB = int(os.getenv("QP_MIN_MEM_MB", "600"))
RETAIN_DAYS = 90
SOURCE_MAX_AGE_H = 26
DOLT_CAL_SOURCE = "dolt.ts_trade_day_calendar"
CAL_FALLBACK_ORIGINS = ("qlib", "legacy")  # legacy wins where both have the same day


class LowMemory(RuntimeError):
    pass


def mem_guard() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemAvailable:"):
                    mb = int(ln.split()[1]) // 1024
                    if mb < MIN_MEM_MB:
                        raise LowMemory(f"MemAvailable {mb}MB < {MIN_MEM_MB}MB")
                    return mb
    except OSError:
        return None
    return None


def canon(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def d(v: str) -> dt.date:
    return dt.date.fromisoformat(str(v)[:10])


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


# ---------------------------------------------------------------- calendar helpers
def open_days(s: Session, origin: str = "qp") -> list[dt.date]:
    return list(s.execute(select(TradingDay.date).where(TradingDay.origin == origin, TradingDay.is_open)
                          .order_by(TradingDay.date)).scalars())


def calendar_span(s: Session, origin: str) -> tuple[list[dt.date], dt.date | None]:
    rows = s.execute(select(TradingDay.date, TradingDay.is_open).where(TradingDay.origin == origin)).all()
    if not rows:
        return [], None
    opens = sorted(d for d, o in rows if o)
    return opens, max(d for d, _ in rows)


def _gap_only_weekends(max_date: dt.date, today: dt.date) -> bool:
    d = max_date + dt.timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:  # Mon-Fri; A-share never trades Sat/Sun
            return False
        d += dt.timedelta(days=1)
    return True


def calendar_covers(opens: list[dt.date], max_date: dt.date | None, today: dt.date) -> bool:
    """True only when session status through today can be determined.

    A calendar that stores days through today is sufficient. An open-days-only calendar
    that ends on Friday is accepted on Sat/Sun. Any weekday after the last stored date
    means coverage is insufficient (the calendar may be stale).
    """
    if not opens or max_date is None:
        return False
    return max_date >= today or _gap_only_weekends(max_date, today)


def calendar_for_expectation(s: Session, today: dt.date | None = None) -> tuple[list[dt.date] | None, dict]:
    """Prefer the qp calendar when it covers today; otherwise a covering legacy calendar.

    A non-empty but stale calendar is not used. If no calendar covers today, return
    (None, meta) so callers keep the expected date unknown.
    """
    today = today or now_cst().date()
    meta: dict[str, Any] = {"today": today.isoformat()}
    for origin in ("qp", "legacy"):
        days, max_date = calendar_span(s, origin)
        usable = calendar_covers(days, max_date, today)
        meta[origin] = {"open_days": len(days), "max_date": max_date.isoformat() if max_date else None,
                        "covers_today": usable}
        if usable:
            meta["origin"] = origin
            return days, meta
    meta["origin"] = None
    meta["reason"] = "no calendar covers today"
    return None, meta


def expected_trade_date(days: list[dt.date], now: dt.datetime, cutoff: dt.time) -> dt.date | None:
    """Latest open day whose cutoff has passed (today if open and now >= cutoff)."""
    today = now.date()
    cands = [x for x in days if x < today or (x == today and now.time() >= cutoff)]
    return cands[-1] if cands else None


def trading_lag(days: list[dt.date], have: dt.date | None, want: dt.date | None) -> int | None:
    if want is None:
        return None
    if have is None:
        return 999
    if have >= want:
        return 0
    return sum(1 for x in days if have < x <= want)


# ---------------------------------------------------------------- fetch jobs (new platform, shadow)
@job("market.calendar", "Trading calendar (SSE) from Dolt ts_trade_day_calendar -> market.trading_day[qp]",
     schedule_hint="06:30 CST daily (legacy 06:20)")
def calendar_job(ctx: JobContext) -> dict:
    mem_guard()
    today = now_cst().date()
    with Session(ctx.engine) as s:
        have = s.scalar(select(func.count()).select_from(TradingDay)
                        .where(TradingDay.origin == "qp", TradingDay.source == DOLT_CAL_SOURCE)) or 0
    start = dt.date(2020, 1, 1) if have < 1000 else today - dt.timedelta(days=60)
    dolt_error = None
    try:
        rows = src.dolt_calendar(start, dt.date(today.year + 1, 12, 31))
    except (FetchError, ValueError, TypeError, KeyError) as e:
        rows, dolt_error = [], str(e) or type(e).__name__
    if rows:
        with Session(ctx.engine) as s:
            # Dolt is authoritative once it succeeds.  Drop rows that only exist
            # because an earlier run mirrored the legacy/qlib calendar.
            s.execute(delete(TradingDay).where(TradingDay.origin == "qp",
                                               TradingDay.source.like("fallback:%")))
            stmt = insert(TradingDay).values([{"origin": "qp", "date": x, "is_open": o, "source": DOLT_CAL_SOURCE}
                                              for x, o in rows])
            s.execute(stmt.on_conflict_do_update(index_elements=["origin", "date"],
                                                 set_={"is_open": stmt.excluded.is_open, "source": stmt.excluded.source,
                                                       "updated_at": func.now()}))
            s.commit()
            opened = [x for x, o in rows if o]
        ahead = (opened[-1] - today).days if opened else None
        return {"ok": True, "rows": len(rows), "source": DOLT_CAL_SOURCE, "dolt_error": dolt_error,
                "range": [rows[0][0].isoformat(), rows[-1][0].isoformat()],
                "max_open": opened[-1].isoformat() if opened else None, "days_ahead": ahead,
                "initial": have < 1000}
    # Dolt unavailable: keep the platform calendar usable from the already-mirrored
    # legacy/qlib calendars.  This is an explicit, labelled fallback, not a Dolt claim.
    with Session(ctx.engine) as s:
        fb: dict[dt.date, str] = {}
        for origin in CAL_FALLBACK_ORIGINS:
            for day in s.execute(select(TradingDay.date).where(TradingDay.origin == origin,
                                                               TradingDay.is_open)).scalars():
                fb[day] = f"fallback:{origin}"
        if not fb:
            return {"ok": False, "error": "dolt_empty_and_no_calendar_fallback", "dolt_error": dolt_error or "empty"}
        stmt = insert(TradingDay).values([{"origin": "qp", "date": x, "is_open": True, "source": source}
                                          for x, source in sorted(fb.items())])
        s.execute(stmt.on_conflict_do_nothing(index_elements=["origin", "date"]))
        s.commit()
        opened = list(s.execute(select(TradingDay.date).where(TradingDay.origin == "qp", TradingDay.is_open)
                                .order_by(TradingDay.date)).scalars())
        sources = dict(s.execute(select(TradingDay.source, func.count()).where(TradingDay.origin == "qp")
                                 .group_by(TradingDay.source)).all())
    ahead = (opened[-1] - today).days if opened else None
    return {"ok": True, "rows": len(fb), "source": "fallback", "fallback_sources": sorted(set(fb.values())),
            "dolt_error": dolt_error or "empty", "sources": sources,
            "range": [min(fb).isoformat(), max(fb).isoformat()] if fb else None,
            "max_open": opened[-1].isoformat() if opened else None, "days_ahead": ahead, "initial": have < 1000}


@job("market.index_close", "Four index closes (Tencent fqkline, Sina fallback) -> market.index_close[qp]",
     schedule_hint="Mon-Fri 16:30, 16:40 CST (legacy 15:35/45/55)")
def index_close_job(ctx: JobContext) -> dict:
    mem_guard()
    got, errors = [], {}
    fetched = utcnow()
    now = now_cst()
    # never store an intraday bar as a close: today's row only after 15:30 CST (close + 30 min grace, as legacy)
    latest_ok = now.date() if now.time() >= dt.time(15, 30) else now.date() - dt.timedelta(days=1)
    for sym in src.INDEXES:
        rows, source = None, None
        try:
            rows, source = src.tencent_index(sym, 10), "tencent"
        except FetchError as e:
            errors[f"{sym}:tencent"] = str(e)
            try:
                rows, source = src.sina_index(sym, 10), "sina"
            except FetchError as e2:
                errors[f"{sym}:sina"] = str(e2)
        for day, close in rows or []:
            if d(day) > latest_ok:
                continue
            got.append({"origin": "qp", "symbol": sym, "trade_date": d(day), "close": round(close, 4), "source": source,
                        "fetched_at": fetched})
    if got:
        with Session(ctx.engine) as s:
            stmt = insert(IndexClose).values(got)
            s.execute(stmt.on_conflict_do_update(
                index_elements=["origin", "symbol", "trade_date"],
                set_={"close": stmt.excluded.close, "source": stmt.excluded.source, "fetched_at": stmt.excluded.fetched_at,
                      "updated_at": func.now()}))
            s.commit()
    syms_ok = sorted({g["symbol"] for g in got})
    return {"ok": len(syms_ok) == len(src.INDEXES), "rows": len(got), "symbols": syms_ok,
            "latest": max((g["trade_date"] for g in got), default=None).isoformat() if got else None, "errors": errors}


@job("market.instruments", "Instrument names from Eastmoney clist -> market.instrument (qlib ranges come from ingest)",
     schedule_hint="Mon-Fri 17:10 CST")
def instruments_job(ctx: JobContext) -> dict:
    mem_guard()
    names = src.eastmoney_names()
    with Session(ctx.engine) as s:
        vals = [{"symbol": k, "name": v[:64], "exchange": k[:2], "board": board_of(k), "name_source": "eastmoney"}
                for k, v in names.items()]
        stmt = insert(Instrument).values(vals)
        s.execute(stmt.on_conflict_do_update(index_elements=["symbol"],
                                             set_={"name": stmt.excluded.name, "exchange": stmt.excluded.exchange,
                                                   "board": stmt.excluded.board, "name_source": stmt.excluded.name_source,
                                                   "updated_at": func.now()}))
        s.commit()
    return {"names": len(names)}


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


@job("market.sources", "Reachability/freshness probe of legacy data sources -> market.source_status",
     schedule_hint="09:05, 13:05, 19:05 CST")
def sources_job(ctx: JobContext) -> dict:
    mem_guard()
    res = src.probe_all()
    with Session(ctx.engine) as s:
        for r in res:
            s.add(SourceStatus(source=r["source"], ok=r["ok"], via_proxy=r["via_proxy"], ms=r["ms"], detail=r["detail"]))
        s.execute(delete(SourceStatus).where(SourceStatus.at < utcnow() - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    return {"ok_count": sum(r["ok"] for r in res), "total": len(res),
            "failed": [r["source"] for r in res if not r["ok"]]}


@job("market.cninfo", "CNINFO announcement count for today (metadata only; direct like legacy)",
     schedule_hint="08:30 CST daily (legacy 07:50)")
def cninfo_job(ctx: JobContext) -> dict:
    mem_guard()
    day = now_cst().date()
    total = src.cninfo_count(day)
    with Session(ctx.engine) as s:
        stmt = insert(AnnouncementCount).values(date=day, total=total, detail={"scope": "szse+sse fulltext"})
        s.execute(stmt.on_conflict_do_update(index_elements=["date"], set_={"total": total, "fetched_at": func.now()}))
        s.commit()
    return {"date": day.isoformat(), "total": total}


@job("market.qlib_check", "Qlib update shadow (compare-only, no data copy): Dolt latest EOD + GitHub release tag",
     schedule_hint="06:40, 23:20 CST (legacy 06:30 retry / 22:00 nightly)")
def qlib_check_job(ctx: JobContext) -> dict:
    mem_guard()
    out: dict[str, Any] = {}
    try:
        out["dolt_latest_eod"] = src.dolt_latest_eod()
    except FetchError as e:
        out["dolt_error"] = str(e)
    try:
        out["github_release_tag"] = src.github_release_tag()
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


# ---------------------------------------------------------------- read-only legacy ingest + reconciliation
def _log(s: Session, item: str, path: str, file_sha: str | None, legacy_rows: int, legacy_ck: str,
         db_rows: int, db_ck: str) -> dict:
    st = "match" if (legacy_rows == db_rows and legacy_ck == db_ck) else "mismatch"
    s.add(ImportLog(item=item, legacy_path=path, legacy_file_sha256=file_sha, legacy_rows=legacy_rows,
                    legacy_checksum=legacy_ck, db_rows=db_rows, db_checksum=db_ck, status=st))
    return {"item": item, "legacy_rows": legacy_rows, "db_rows": db_rows, "status": st}


def _mirror_days(s: Session, origin: str, source: str, dates: list[str]) -> None:
    s.execute(delete(TradingDay).where(TradingDay.origin == origin))
    for i in range(0, len(dates), 2000):
        s.execute(insert(TradingDay).values([{"origin": origin, "date": d(x), "is_open": True, "source": source}
                                             for x in dates[i:i + 2000]]))


def ingest(s: Session, snap: dict) -> list[dict]:
    recon: list[dict] = []
    cal = snap.get("calendar") or {}
    if cal.get("ok"):
        _mirror_days(s, "legacy", "legacy:" + str(cal.get("source")), cal["dates"])
        s.flush()
        back = [x.isoformat() for x in open_days(s, "legacy")]
        recon.append(_log(s, "calendar", cal["path"], cal.get("file_sha256"), cal["rows"], cal["checksum"],
                          len(back), canon(back)))
    ic = snap.get("index_close") or {}
    if ic.get("ok") and ic["rows"]:
        vals = [{"origin": "legacy", "symbol": r["symbol"], "trade_date": d(r["trade_date"]), "close": r["close"],
                 "source": r["source"], "fetched_at": dt.datetime.fromisoformat(ic["fetched_at"]) if ic.get("fetched_at") else None}
                for r in ic["rows"]]
        stmt = insert(IndexClose).values(vals)
        s.execute(stmt.on_conflict_do_update(index_elements=["origin", "symbol", "trade_date"],
                                             set_={"close": stmt.excluded.close, "source": stmt.excluded.source,
                                                   "fetched_at": stmt.excluded.fetched_at, "updated_at": func.now()}))
        s.flush()
        keys = [(r["symbol"], d(r["trade_date"])) for r in ic["rows"]]
        back = s.execute(select(IndexClose).where(IndexClose.origin == "legacy",
                                                  tuple_(IndexClose.symbol, IndexClose.trade_date).in_(keys))
                         .order_by(IndexClose.symbol, IndexClose.trade_date)).scalars().all()
        recon.append(_log(s, "index_close", ic["path"], ic.get("file_sha256"), len(ic["rows"]), ic["checksum"], len(back),
                          canon(f"{b.symbol}|{b.trade_date}|{float(b.close):.4f}|{b.source}" for b in back)))
    q = snap.get("qlib") or {}
    if q.get("ok"):
        _mirror_days(s, "qlib", "qlib:calendars/day.txt", q["calendar"])
        s.flush()
        back = [x.isoformat() for x in open_days(s, "qlib")]
        recon.append(_log(s, "qlib_calendar", q["root"] + "/calendars/day.txt", q.get("calendar_sha256"),
                          len(q["calendar"]), q["calendar_checksum"], len(back), canon(back)))
        insts = q["instruments"]
        s.execute(Instrument.__table__.update().values(in_qlib=False))
        for i in range(0, len(insts), 2000):
            chunk = insts[i:i + 2000]
            stmt = insert(Instrument).values([{"symbol": a, "exchange": a[:2], "board": board_of(a), "qlib_start": d(b),
                                               "qlib_end": d(c), "in_qlib": True} for a, b, c in chunk])
            s.execute(stmt.on_conflict_do_update(index_elements=["symbol"],
                                                 set_={"qlib_start": stmt.excluded.qlib_start, "qlib_end": stmt.excluded.qlib_end,
                                                       "in_qlib": True, "updated_at": func.now()}))
        s.flush()
        back = s.execute(select(Instrument.symbol, Instrument.qlib_start, Instrument.qlib_end).where(Instrument.in_qlib)).all()
        recon.append(_log(s, "qlib_instruments", q["root"] + "/instruments/all.txt", q.get("instruments_sha256"),
                          len(insts), q["instruments_checksum"], len(back),
                          canon(sorted(f"{a}|{b}|{c}" for a, b, c in back))))
        for idx, m in (q.get("members") or {}).items():
            s.execute(delete(IndexMember).where(IndexMember.index_code == idx))
            rows = m["rows"]  # unique (symbol, start, end) tuples from the reader
            for k in range(0, len(rows), 2000):
                s.execute(insert(IndexMember).values([{"index_code": idx, "symbol": a, "start_date": d(b), "end_date": d(c)}
                                                      for a, b, c in rows[k:k + 2000]]))
            s.flush()
            back = s.execute(select(IndexMember.symbol, IndexMember.start_date, IndexMember.end_date)
                             .where(IndexMember.index_code == idx)).all()
            recon.append(_log(s, f"qlib_members:{idx}", q["root"] + f"/instruments/{idx}.txt", m.get("file_sha256"),
                              len(m["rows"]), m["checksum"], len(back),
                              canon(sorted(f"{idx}|{a}|{b}|{c}" for a, b, c in back))))
        s.add(QlibStatus(kind="catalog", data={
            "tag": q.get("tag"), "calendar_first": q["calendar"][0] if q["calendar"] else None,
            "calendar_last": q["calendar"][-1] if q["calendar"] else None, "calendar_days": len(q["calendar"]),
            "instruments": len(insts), "active_on_last": sum(1 for _, _, c in insts if q["calendar"] and c == q["calendar"][-1]),
            "feature_dirs": q.get("feature_dirs"), "update_status": q.get("update_status"),
            "reference_dates": q.get("reference_dates")}))
    core = snap.get("core_market") or {}
    if core.get("ok"):
        s.execute(delete(MarketSummary).where(MarketSummary.origin == "legacy"))
        for r in core["rows"]:
            s.add(MarketSummary(origin="legacy", trade_date=d(r["date"]), data=r["qlib"], payload_sha256=r["payload_sha256"],
                                generated_at=dt.datetime.fromisoformat(r["generated_at"]) if r.get("generated_at") else None))
        s.flush()
        back = s.execute(select(MarketSummary.trade_date, MarketSummary.payload_sha256)
                         .where(MarketSummary.origin == "legacy").order_by(MarketSummary.trade_date)).all()
        recon.append(_log(s, "core_market", core["path"], None, len(core["rows"]), core["checksum"], len(back),
                          canon(f"{a}|{b}" for a, b in back)))
        bad = [r["date"] for r in core["rows"] if r["payload_sha256"] != r["computed_sha256"]]
        if bad:
            recon.append({"item": "core_market.payload_sha256_selfcheck", "status": "mismatch", "dates": bad[:10]})
    sdb = snap.get("snapshot_db") or {}
    if sdb.get("ok"):
        s.execute(delete(LegacySnapshot))
        for r in sdb["rows"]:
            s.add(LegacySnapshot(date=d(r["date"]), generated_at=r.get("generated_at"), payload_sha256=r["payload_sha256"],
                                 payload_bytes=r["bytes"], keys=r.get("keys") or []))
        s.flush()
        back = s.execute(select(LegacySnapshot.date, LegacySnapshot.payload_sha256).order_by(LegacySnapshot.date)).all()
        recon.append(_log(s, "snapshot_db", sdb["path"], None, len(sdb["rows"]), sdb["checksum"], len(back),
                          canon(f"{a}|{b}" for a, b in back)))
    for summ in snap.get("qlib_summary") or []:
        if summ.get("ok"):
            stmt = insert(MarketSummary).values(origin="qp", trade_date=d(summ["date"]), data=summ,
                                                generated_at=dt.datetime.fromisoformat(snap["collected_at"]))
            s.execute(stmt.on_conflict_do_update(index_elements=["origin", "trade_date"],
                                                 set_={"data": stmt.excluded.data, "generated_at": stmt.excluded.generated_at,
                                                       "updated_at": func.now()}))
    at = dt.datetime.fromisoformat(snap["collected_at"])
    set_inventory(s, "market.legacy_health", at, snap.get("health"))
    set_inventory(s, "market.cninfo_staging", at, snap.get("cninfo"))
    set_inventory(s, "market.reader", at, {"duration_s": snap.get("duration_s"),
                                           "summaries": [x.get("date") for x in snap.get("qlib_summary") or []]})
    return recon


@job("market.ingest_legacy", "Read-only legacy market import/mirror with row-count + checksum reconciliation",
     schedule_hint="after qp-market-read (06:45, 17:00, 23:25 CST)")
def ingest_job(ctx: JobContext) -> dict:
    mem_guard()
    if not MARKET_INBOX.is_file():
        return {"ok": False, "error": "no market snapshot"}
    snap = json.loads(MARKET_INBOX.read_text())
    with Session(ctx.engine) as s:
        recon = ingest(s, snap)
        s.execute(delete(ImportLog).where(ImportLog.at < utcnow() - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    mism = [r for r in recon if r["status"] != "match"]
    return {"ok": not mism, "items": len(recon), "mismatch": mism, "collected_at": snap.get("collected_at")}


# ---------------------------------------------------------------- compare (new vs legacy) + health
REQUIRED_COMPARE_ITEMS = (
    "calendar.legacy_vs_qp",
    "calendar.qlib_vs_qp",
    "index_close.legacy_vs_qp",
    "snapshot.core_vs_qp",
    "instruments.qlib_active_vs_names",
)
COMPARE_MAX_AGE_H = 26  # compare runs 07:00/17:15/23:45 CST; max natural gap is 7h15m
COMPARE_RUN_SKEW = dt.timedelta(seconds=30)  # run_at is captured immediately after the job row insert


def _cov(expected: int, compared: int, missing: int, diffs: int, **extra: Any) -> dict:
    return {"expected": expected, "compared": compared, "missing": missing, "diffs": diffs, **extra}


def _item_verdict(expected: int, missing: int, diffs: int) -> str:
    if diffs:
        return "mismatch"
    if expected <= 0:
        return "skipped"
    if missing:
        return "insufficient"
    return "match"


def _cov_summary(expected: int, compared: int, missing: int, diffs: int) -> str:
    return f"应比 {expected}，实比 {compared}，缺失 {missing}，差异 {diffs}"


def compare(s: Session) -> list[tuple[str, str, str, dict]]:
    out: list[tuple[str, str, str, dict]] = []
    qp_days = open_days(s, "qp")
    qp_all = dict(s.execute(select(TradingDay.date, TradingDay.is_open).where(TradingDay.origin == "qp")).all())
    for other in ("legacy", "qlib"):
        od = open_days(s, other)
        if not qp_all or not od:
            out.append((f"calendar.{other}_vs_qp", "skipped", "missing data",
                        _cov(0, 0, 0, 0, qp=len(qp_all), **{other: len(od)})))
            continue
        lo, hi = max(min(qp_all), od[0]), min(max(qp_all), od[-1])
        a = {x for x in od if lo <= x <= hi}
        b = {x for x in qp_days if lo <= x <= hi}
        only_o, only_q = sorted(a - b), sorted(b - a)
        expected = len(a | b)
        diffs_n = len(only_o) + len(only_q)
        st = _item_verdict(expected, 0, diffs_n)
        det = _cov(expected, expected, 0, diffs_n, range=[lo.isoformat(), hi.isoformat()], days=len(a),
                   **{f"{other}_only": [x.isoformat() for x in only_o[:20]],
                      "qp_only": [x.isoformat() for x in only_q[:20]]})
        out.append((f"calendar.{other}_vs_qp", st,
                    f"{lo}..{hi}: {_cov_summary(expected, expected, 0, diffs_n)}；{other}-only {len(only_o)}, qp-only {len(only_q)}",
                    det))
    leg = s.execute(select(IndexClose).where(IndexClose.origin == "legacy",
                                             IndexClose.trade_date >= now_cst().date() - dt.timedelta(days=20))).scalars().all()
    if leg:
        qp = {(r.symbol, r.trade_date): r for r in s.execute(select(IndexClose).where(
            IndexClose.origin == "qp", tuple_(IndexClose.symbol, IndexClose.trade_date).in_([(x.symbol, x.trade_date) for x in leg])
        )).scalars()}
        diff_rows, missing_rows = [], []
        for x in leg:
            y = qp.get((x.symbol, x.trade_date))
            if y is None:
                missing_rows.append(f"{x.symbol}@{x.trade_date}")
            elif abs(float(y.close) - float(x.close)) > 0.011:
                diff_rows.append({"symbol": x.symbol, "date": x.trade_date.isoformat(),
                                  "legacy": float(x.close), "qp": float(y.close)})
        expected, missing_n, diffs_n = len(leg), len(missing_rows), len(diff_rows)
        compared = expected - missing_n
        st = _item_verdict(expected, missing_n, diffs_n)
        out.append(("index_close.legacy_vs_qp", st, _cov_summary(expected, compared, missing_n, diffs_n),
                    _cov(expected, compared, missing_n, diffs_n,
                         diff_rows=diff_rows[:20], missing_rows=missing_rows[:20])))
    else:
        out.append(("index_close.legacy_vs_qp", "skipped", "no legacy rows in last 20 days",
                    _cov(0, 0, 0, 0)))
    legs = {r.trade_date: r for r in s.execute(select(MarketSummary).where(MarketSummary.origin == "legacy")).scalars()}
    qps = {r.trade_date: r for r in s.execute(select(MarketSummary).where(MarketSummary.origin == "qp")).scalars()}
    both = sorted(set(legs) & set(qps))
    if not both:
        out.append(("snapshot.core_vs_qp", "skipped", "no common date between legacy core-market and qp summary",
                    _cov(0, 0, 0, 0, legacy_latest=max(legs).isoformat() if legs else None,
                         qp_dates=[x.isoformat() for x in sorted(qps)][-3:])))
    else:
        day = both[-1]
        a, b = legs[day].data, qps[day].data
        fields = {}
        for k in ("breadth", "coverage"):
            if (a.get(k) or {}) != {kk: (b.get(k) or {}).get(kk) for kk in (a.get(k) or {})}:
                fields[k] = {"legacy": a.get(k), "qp": b.get(k)}
        for k in ("topGainers", "topLosers", "topAmount"):
            sa, sb = [x.get("sym") for x in a.get(k) or []], [x.get("sym") for x in b.get(k) or []]
            if sa != sb:
                fields[k] = {"legacy": sa, "qp": sb}
        diffs_n = len(fields)
        st = _item_verdict(1, 0, diffs_n)
        out.append(("snapshot.core_vs_qp", st,
                    f"{day}: {_cov_summary(1, 1, 0, diffs_n)}"
                    + (f"；differs in {','.join(fields)}" if fields else "；breadth/coverage/top lists equal"),
                    _cov(1, 1, 0, diffs_n, date=day.isoformat(), diff_fields=fields)))
    named = dict(s.execute(select(Instrument.symbol, Instrument.name).where(Instrument.name.is_not(None))).all())
    last = s.scalar(select(func.max(Instrument.qlib_end)))
    active = set(s.execute(select(Instrument.symbol).where(Instrument.in_qlib, Instrument.qlib_end == last)).scalars()) if last else set()
    stocks_named = {k for k in named if k[2:3] in "0123456789"}
    if active and stocks_named:
        missing_rows = sorted(active - stocks_named)
        ratio = len(missing_rows) / len(active)
        # names exist for the catalog; a small gap is a mismatch, not missing evidence
        diffs_n = len(missing_rows) if ratio > 0.02 else 0
        st = _item_verdict(len(active), 0, diffs_n)
        out.append(("instruments.qlib_active_vs_names", st,
                    f"qlib active {len(active)} on {last}, without name {len(missing_rows)} ({ratio:.1%})；"
                    + _cov_summary(len(active), len(active), 0, diffs_n),
                    _cov(len(active), len(active), 0, diffs_n, missing_sample=missing_rows[:20], ratio=round(ratio, 4))))
    else:
        out.append(("instruments.qlib_active_vs_names", "skipped", "no names or qlib catalog yet",
                    _cov(0, 0, 0, 0)))
    return out


@job("market.compare", "Daily compare of new (qp) vs legacy outputs; mismatches -> shadow alerts",
     schedule_hint="07:00, 17:15, 23:45 CST")
def compare_job(ctx: JobContext) -> dict:
    run_at = utcnow()
    with Session(ctx.engine) as s:
        res = compare(s)
        for item, st, summary, detail in res:
            s.add(CompareResult(run_at=run_at, item=item, status=st, summary=summary, detail=detail))
        firing = [(f"market:compare:{item}", "warning", f"compare {item}: {summary}", detail)
                  for item, st, summary, detail in res if st == "mismatch"]
        only_resolve = {f"market:compare:{item}" for item, st, _, _ in res if st == "match"}
        keep_open = {f"market:compare:{item}" for item, st, _, _ in res if st in ("skipped", "insufficient")}
        al = apply_alerts(s, firing, ctx.run_id, owns=lambda k: k.startswith("market:compare:"),
                          keep_open=keep_open, only_resolve=only_resolve)
        s.execute(delete(CompareResult).where(CompareResult.run_at < run_at - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    return {"items": len(res), "mismatch": [r[0] for r in res if r[1] == "mismatch"],
            "insufficient": [r[0] for r in res if r[1] == "insufficient"],
            "skipped": [r[0] for r in res if r[1] == "skipped"], "alerts": al}


def compare_latest(s: Session, now: dt.datetime) -> tuple[str, str, dict]:
    """Judge the latest compare run with a fixed required-item set and run identity.

    The denominator is REQUIRED_COMPARE_ITEMS, never the number of rows that happen to
    exist.  Results are accepted only when the latest market.compare job run succeeded,
    the rows fall inside that run's time window, and run_at is fresh.  A required item
    with no row at all counts as missing (unknown), not as a smaller denominator.
    """
    required = REQUIRED_COMPARE_ITEMS
    expected = len(required)
    now_utc = now.astimezone(dt.timezone.utc)
    jr = s.execute(select(JobRun).where(JobRun.job_name == "market.compare")
                   .order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1)).scalar_one_or_none()
    base = {"expected": expected, "compared": 0, "missing": expected, "diffs": 0,
            "required": list(required), "absent": list(required), "insufficient": [], "skipped": [],
            "mismatch": [], "extra": {}, "job_run_id": None, "job_status": None, "run_at": None,
            "job_started_at": None, "job_finished_at": None}
    if jr is None:
        return ("unknown", "尚无 market.compare 运行记录；" + _cov_summary(expected, 0, expected, 0),
                {**base, "reason": "no_job_run"})
    base.update(job_run_id=jr.id, job_status=jr.status, job_started_at=jr.started_at.isoformat(),
                job_finished_at=jr.finished_at.isoformat() if jr.finished_at else None)
    if jr.status != "succeeded" or jr.finished_at is None:
        return ("unknown", f"最近一次 market.compare 状态为 {jr.status}；" + _cov_summary(expected, 0, expected, 0),
                {**base, "reason": "job_not_succeeded"})
    run_at = s.scalar(select(func.max(CompareResult.run_at)).where(
        CompareResult.run_at >= jr.started_at - COMPARE_RUN_SKEW,
        CompareResult.run_at <= jr.finished_at + COMPARE_RUN_SKEW))
    if run_at is None:
        return ("unknown", "最近一次 market.compare 没有对应结果；" + _cov_summary(expected, 0, expected, 0),
                {**base, "reason": "no_results_for_run"})
    rows = s.execute(select(CompareResult).where(CompareResult.run_at == run_at)
                     .order_by(CompareResult.id)).scalars().all()
    by_item = {r.item: r for r in rows if r.item in required}
    absent = [i for i in required if i not in by_item]
    mismatch = [i for i in required if i in by_item and by_item[i].status == "mismatch"]
    insufficient = [i for i in required if i in by_item and by_item[i].status == "insufficient"]
    skipped = [i for i in required if i in by_item and by_item[i].status == "skipped"]
    compared = sum(1 for i in required if i in by_item and by_item[i].status in ("match", "mismatch"))
    missing = expected - compared
    diffs = len(mismatch)
    age_h = (now_utc - run_at).total_seconds() / 3600
    detail = {**base, "run_at": run_at.isoformat(), "compared": compared, "missing": missing, "diffs": diffs,
              "absent": absent, "insufficient": insufficient, "skipped": skipped, "mismatch": mismatch,
              "extra": {r.item: r.status for r in rows if r.item not in required},
              "age_hours": round(age_h, 2)}
    summary = _cov_summary(expected, compared, missing, diffs)
    if age_h > COMPARE_MAX_AGE_H:
        return "unknown", f"最近比较结果 {age_h:.1f}h 前，超过 {COMPARE_MAX_AGE_H}h；" + summary, {**detail, "reason": "stale"}
    if age_h < -0.1:
        return "unknown", "最近比较结果时间在未来；" + summary, {**detail, "reason": "future"}
    if diffs:
        return "warning", summary, detail
    if missing:
        return "unknown", summary, detail
    return "ok", summary, detail


SOURCE_FALLBACKS = {
    "dolt": ("github_release",),
    "github_release": ("dolt",),
    "tencent": ("sina",),
    "sina": ("tencent",),
}
SOURCE_OPTIONAL = {"akshare_sina_calendar", "baostock"}
# Replaced checks.  The old alert keys are resolved once so a retired check does not
# stay firing forever; the replacement evidence is snapshot.qp_current and
# collector.market_reader (the legacy status itself remains on the read-only card).
RETIRED_HEALTH_CHECKS = {
    "snapshot.legacy_core_current": "replaced by snapshot.qp_current",
    "legacy.data_health": "legacy status is shown on the read-only card; qp health uses collector.market_reader",
}


def source_verdict(latest: dict, *, calendar_covered: bool) -> tuple[list[str], dict[str, list[str]]]:
    """Return required failures and the fallback path used for degraded sources.

    A source failure is not blocking when its peer source is reachable (Tencent/Sina,
    Dolt/GitHub release).  Dolt's calendar capability is also covered when a calendar
    mirror already reaches today, so that fallback is passed in explicitly.
    """
    ok = {name: bool(row.ok) for name, row in latest.items()}
    covered: dict[str, list[str]] = {}
    for name in latest:
        if ok[name]:
            covered[name] = []
            continue
        alts = [a for a in SOURCE_FALLBACKS.get(name, ()) if ok.get(a)]
        if name == "dolt" and calendar_covered:
            alts.append("calendar_mirror")
        covered[name] = alts
    bad = sorted(name for name in latest
                 if not ok[name] and not covered[name] and name not in SOURCE_OPTIONAL)
    return bad, covered


def health(s: Session, now: dt.datetime | None = None) -> list[tuple[str, str, str, dict]]:
    now = now or now_cst()
    out = []
    days, cal_meta = calendar_for_expectation(s, now.date())
    qp_open = open_days(s, "qp")
    upd = s.scalar(select(func.max(TradingDay.updated_at)).where(TradingDay.origin == "qp"))
    cal_sources = dict(s.execute(select(TradingDay.source, func.count()).where(TradingDay.origin == "qp")
                                 .group_by(TradingDay.source)).all())
    if not qp_open:
        out.append(("calendar.qp", "warning", "qp calendar empty (Dolt not fetched yet)",
                    {"sources": cal_sources}))
    else:
        ahead = (qp_open[-1] - now.date()).days
        age_d = (utcnow() - upd).total_seconds() / 86400 if upd else 999
        st = "ok" if ahead >= 30 and age_d <= 8 else "warning"
        src_txt = ", ".join(f"{k}({v})" for k, v in sorted(cal_sources.items()))
        out.append(("calendar.qp", st,
                    f"max open {qp_open[-1]} ({ahead}d ahead), refreshed {age_d:.1f}d ago; sources: {src_txt}",
                    {"max_open": qp_open[-1].isoformat(), "days_ahead": ahead, "age_days": round(age_d, 2),
                     "sources": cal_sources}))

    def want_at(cutoff: dt.time) -> dt.date | None:
        return expected_trade_date(days, now, cutoff) if days else None

    def unknown_expect(have_date=None, **extra):
        return ("unknown", "无法确定期望交易日（日历覆盖不足或尚未到截止时间）",
                {"expected": None, "latest": str(have_date) if have_date else None, "lag": None,
                 "calendar": cal_meta, **extra})

    have = s.execute(select(IndexClose.trade_date, func.count()).where(IndexClose.origin == "qp")
                     .group_by(IndexClose.trade_date).order_by(IndexClose.trade_date.desc()).limit(1)).first()
    have_day, have_n = (have[0], have[1]) if have else (None, 0)
    want = want_at(dt.time(16, 30))
    if want is None:
        st, summary, det = unknown_expect(have_day, count=have_n)
        out.append(("index_close.qp_current", st, summary, det))
    else:
        lag = trading_lag(days, have_day, want)
        st = "ok" if lag == 0 and have_n == 4 else ("warning" if lag in (0, 1) else "critical")
        out.append(("index_close.qp_current", st,
                    f"expected {want}, latest qp {have_day} ({have_n}/4)",
                    {"expected": want.isoformat(), "latest": str(have_day) if have_day else None, "lag": lag,
                     "count": have_n}))

    cat = s.execute(select(QlibStatus).where(QlibStatus.kind == "catalog").order_by(QlibStatus.id.desc()).limit(1)).scalar()
    want_q = want_at(dt.time(23, 15))
    if cat:
        ql = cat.data.get("calendar_last")
        extra = {"calendar_last": ql, "tag": cat.data.get("tag"), "update_status": cat.data.get("update_status")}
        if want_q is None:
            st, summary, det = unknown_expect(ql, **extra)
            out.append(("qlib.current", st, summary, det))
        else:
            lag = trading_lag(days, d(ql) if ql else None, want_q)
            st = "ok" if lag == 0 else ("warning" if lag == 1 else "critical")
            out.append(("qlib.current", st, f"qlib calendar last {ql}, expected {want_q}, lag {lag}",
                        {"expected": want_q.isoformat(), "lag": lag, **extra}))
    else:
        out.append(("qlib.current", "unknown", "no qlib catalog yet", {}))
    srcq = s.execute(select(QlibStatus).where(QlibStatus.kind == "source").order_by(QlibStatus.id.desc()).limit(1)).scalar()
    eod = eod_source = None
    src_age_h = None
    if srcq:
        data = srcq.data or {}
        eod = data.get("latest_eod") or data.get("dolt_latest_eod")
        eod_source = data.get("latest_eod_source") or ("dolt" if data.get("dolt_latest_eod") else None)
        if not eod and data.get("github_release_tag"):
            eod, eod_source = data["github_release_tag"], "github_release"
        src_age_h = (utcnow() - srcq.at).total_seconds() / 3600
    eod_day = None
    if eod:
        try:
            eod_day = d(eod)
        except ValueError:
            eod = eod_source = None
    if not srcq:
        out.append(("qlib.source_dolt", "unknown", "Dolt latest EOD unknown (no source check yet)", {}))
    elif src_age_h is not None and src_age_h > SOURCE_MAX_AGE_H:
        out.append(("qlib.source_dolt", "unknown",
                    f"来源检查 {src_age_h:.1f}h 前，超过 {SOURCE_MAX_AGE_H}h；无法确认上游 EOD",
                    {**(srcq.data or {}), "age_hours": round(src_age_h, 1)}))
    elif eod and eod_day:
        if want_q is None:
            st, summary, det = unknown_expect(eod, **(srcq.data or {}), eod_source=eod_source,
                                              observed_at=srcq.at.isoformat())
            out.append(("qlib.source_dolt", st, summary, det))
        else:
            lag = trading_lag(days, eod_day, want_q)
            if eod_source == "dolt":
                summary = f"Dolt latest EOD {eod}, expected {want_q}, lag {lag}"
            else:
                why = (srcq.data or {}).get("dolt_error") or "不可达"
                summary = f"上游最新 EOD {eod}（GitHub release；Dolt {why}），expected {want_q}, lag {lag}"
            out.append(("qlib.source_dolt", "ok" if lag == 0 else "warning", summary,
                        {**(srcq.data or {}), "expected": want_q.isoformat(), "lag": lag,
                         "eod_source": eod_source, "observed_at": srcq.at.isoformat()}))
    else:
        out.append(("qlib.source_dolt", "unknown", "Dolt latest EOD unknown",
                    {**(srcq.data or {}), "age_hours": round(src_age_h, 1) if src_age_h is not None else None}))
    qp_core = s.scalar(select(func.max(MarketSummary.trade_date)).where(MarketSummary.origin == "qp"))
    leg_core = s.scalar(select(func.max(MarketSummary.trade_date)).where(MarketSummary.origin == "legacy"))
    want_s = want_at(dt.time(23, 15))
    if want_s is None:
        st, summary, det = unknown_expect(qp_core, legacy_latest=str(leg_core) if leg_core else None)
        out.append(("snapshot.qp_current", st, summary, det))
    else:
        lag = trading_lag(days, qp_core, want_s)
        st = "ok" if lag == 0 else ("warning" if lag == 1 else "critical")
        out.append(("snapshot.qp_current", st,
                    f"qp summary latest {qp_core}, expected {want_s}, lag {lag}"
                    f"；legacy core latest {leg_core}（仅参考）",
                    {"latest": str(qp_core) if qp_core else None, "expected": want_s.isoformat(), "lag": lag,
                     "legacy_latest": str(leg_core) if leg_core else None,
                     "legacy_lag": trading_lag(days, leg_core, want_s)}))
    from qp.models.ops import Inventory
    lh = s.get(Inventory, "market.legacy_health")
    if lh and lh.data and lh.data.get("ok"):
        data = lh.data
        age_h = (utcnow() - lh.at).total_seconds() / 3600
        out.append(("collector.market_reader", "ok" if age_h <= 26 else "warning",
                    f"legacy market snapshot {age_h:.1f}h old",
                    {"age_hours": round(age_h, 1), "legacy_status": data.get("status"),
                     "legacy_mode": data.get("businessMode"),
                     "legacy_effective": data.get("effectiveTradeDate"),
                     "legacy_generated_at": data.get("generatedAt"),
                     "legacy_issues": data.get("issues")}))
    else:
        out.append(("collector.market_reader", "unknown", "legacy market snapshot not ingested yet", {}))
    latest = {}
    for r in s.execute(select(SourceStatus).where(SourceStatus.at > utcnow() - dt.timedelta(hours=26))
                       .order_by(SourceStatus.at)).scalars():
        latest[r.source] = r
    if latest:
        calendar_covered = cal_meta.get("origin") is not None
        bad, covered = source_verdict(latest, calendar_covered=calendar_covered)
        degraded = sorted(f"{k}->{','.join(v)}" for k, v in covered.items() if v)
        summary = f"{len(latest) - len(bad)}/{len(latest)} 数据源有路径可用"
        if degraded:
            summary += "；降级: " + ", ".join(degraded)
        if bad:
            summary += "；失败: " + ", ".join(bad)
        out.append(("sources.reachable", "ok" if not bad else "warning", summary,
                    {k: {"ok": v.ok, "ms": v.ms, "via_proxy": v.via_proxy,
                         "covered_by": covered.get(k) or None,
                         "optional": k in SOURCE_OPTIONAL,
                         **(v.detail or {})} for k, v in latest.items()}))
    else:
        out.append(("sources.reachable", "unknown", "no probe in 26h", {}))
    c_status, c_summary, c_detail = compare_latest(s, now)
    out.append(("compare.latest", c_status, c_summary, c_detail))
    return out


@job("market.health", "Market data health checks -> market.health_result; warning/critical -> shadow alerts",
     schedule_hint="07:05, 17:20, 23:50 CST (legacy 22:45/23:00)")
def health_job(ctx: JobContext) -> dict:
    run_at = utcnow()
    with Session(ctx.engine) as s:
        res = health(s)
        for check, st, summary, detail in res:
            s.add(HealthResult(run_at=run_at, check=check, status=st, summary=summary, detail=detail))
        firing = [(f"market:health:{c}", "critical" if st == "critical" else "warning", f"market {c}: {summary}", detail)
                  for c, st, summary, detail in res if st in ("warning", "critical")]
        only_resolve = {f"market:health:{c}" for c, st, _, _ in res if st == "ok"}
        # Retire replaced checks once; their old keys must not stay firing forever just
        # because the check no longer appears in the latest run.
        retired = {f"market:health:{c}" for c in RETIRED_HEALTH_CHECKS}
        only_resolve |= retired
        keep_open = {f"market:health:{c}" for c, st, _, _ in res if st == "unknown"}
        al = apply_alerts(s, firing, ctx.run_id, owns=lambda k: k.startswith("market:health:"),
                          keep_open=keep_open, only_resolve=only_resolve)
        s.execute(delete(HealthResult).where(HealthResult.run_at < run_at - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    return {"checks": {c: st for c, st, _, _ in res}, "alerts": al, "retired": sorted(retired)}
