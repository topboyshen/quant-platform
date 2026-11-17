"""Read-only legacy market ingest and reconciliation."""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from qp.jobs.inventory import set_inventory
from qp.jobs.market_common import RETAIN_DAYS, canon, d, open_days
from qp.jobs.market_fetch import board_of
from qp.jobs.registry import JobContext, job
from qp.models.market import (
    ImportLog,
    IndexClose,
    IndexMember,
    Instrument,
    LegacySnapshot,
    MarketSummary,
    QlibStatus,
    TradingDay,
)


def _m():
    from qp.jobs import market

    return market


# ---------------------------------------------------------------- read-only legacy ingest + reconciliation
def _log(
    s: Session, item: str, path: str, file_sha: str | None, legacy_rows: int, legacy_ck: str, db_rows: int, db_ck: str
) -> dict:
    st = "match" if (legacy_rows == db_rows and legacy_ck == db_ck) else "mismatch"
    s.add(
        ImportLog(
            item=item,
            legacy_path=path,
            legacy_file_sha256=file_sha,
            legacy_rows=legacy_rows,
            legacy_checksum=legacy_ck,
            db_rows=db_rows,
            db_checksum=db_ck,
            status=st,
        )
    )
    return {"item": item, "legacy_rows": legacy_rows, "db_rows": db_rows, "status": st}


def _mirror_days(s: Session, origin: str, source: str, dates: list[str]) -> None:
    s.execute(delete(TradingDay).where(TradingDay.origin == origin))
    for i in range(0, len(dates), 2000):
        s.execute(
            insert(TradingDay).values(
                [{"origin": origin, "date": d(x), "is_open": True, "source": source} for x in dates[i : i + 2000]]
            )
        )


def ingest(s: Session, snap: dict) -> list[dict]:
    recon: list[dict] = []
    cal = snap.get("calendar") or {}
    if cal.get("ok"):
        _mirror_days(s, "legacy", "legacy:" + str(cal.get("source")), cal["dates"])
        s.flush()
        back = [x.isoformat() for x in open_days(s, "legacy")]
        recon.append(
            _log(
                s, "calendar", cal["path"], cal.get("file_sha256"), cal["rows"], cal["checksum"], len(back), canon(back)
            )
        )
    ic = snap.get("index_close") or {}
    if ic.get("ok") and ic["rows"]:
        vals = [
            {
                "origin": "legacy",
                "symbol": r["symbol"],
                "trade_date": d(r["trade_date"]),
                "close": r["close"],
                "source": r["source"],
                "fetched_at": dt.datetime.fromisoformat(ic["fetched_at"]) if ic.get("fetched_at") else None,
            }
            for r in ic["rows"]
        ]
        stmt = insert(IndexClose).values(vals)
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
        s.flush()
        keys = [(r["symbol"], d(r["trade_date"])) for r in ic["rows"]]
        back = (
            s.execute(
                select(IndexClose)
                .where(IndexClose.origin == "legacy", tuple_(IndexClose.symbol, IndexClose.trade_date).in_(keys))
                .order_by(IndexClose.symbol, IndexClose.trade_date)
            )
            .scalars()
            .all()
        )
        recon.append(
            _log(
                s,
                "index_close",
                ic["path"],
                ic.get("file_sha256"),
                len(ic["rows"]),
                ic["checksum"],
                len(back),
                canon(f"{b.symbol}|{b.trade_date}|{float(b.close):.4f}|{b.source}" for b in back),
            )
        )
    q = snap.get("qlib") or {}
    if q.get("ok"):
        _mirror_days(s, "qlib", "qlib:calendars/day.txt", q["calendar"])
        s.flush()
        back = [x.isoformat() for x in open_days(s, "qlib")]
        recon.append(
            _log(
                s,
                "qlib_calendar",
                q["root"] + "/calendars/day.txt",
                q.get("calendar_sha256"),
                len(q["calendar"]),
                q["calendar_checksum"],
                len(back),
                canon(back),
            )
        )
        insts = q["instruments"]
        s.execute(Instrument.__table__.update().values(in_qlib=False))
        for i in range(0, len(insts), 2000):
            chunk = insts[i : i + 2000]
            stmt = insert(Instrument).values(
                [
                    {
                        "symbol": a,
                        "exchange": a[:2],
                        "board": board_of(a),
                        "qlib_start": d(b),
                        "qlib_end": d(c),
                        "in_qlib": True,
                    }
                    for a, b, c in chunk
                ]
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["symbol"],
                    set_={
                        "qlib_start": stmt.excluded.qlib_start,
                        "qlib_end": stmt.excluded.qlib_end,
                        "in_qlib": True,
                        "updated_at": func.now(),
                    },
                )
            )
        s.flush()
        back = s.execute(
            select(Instrument.symbol, Instrument.qlib_start, Instrument.qlib_end).where(Instrument.in_qlib)
        ).all()
        recon.append(
            _log(
                s,
                "qlib_instruments",
                q["root"] + "/instruments/all.txt",
                q.get("instruments_sha256"),
                len(insts),
                q["instruments_checksum"],
                len(back),
                canon(sorted(f"{a}|{b}|{c}" for a, b, c in back)),
            )
        )
        for idx, m in (q.get("members") or {}).items():
            s.execute(delete(IndexMember).where(IndexMember.index_code == idx))
            rows = m["rows"]  # unique (symbol, start, end) tuples from the reader
            for k in range(0, len(rows), 2000):
                s.execute(
                    insert(IndexMember).values(
                        [
                            {"index_code": idx, "symbol": a, "start_date": d(b), "end_date": d(c)}
                            for a, b, c in rows[k : k + 2000]
                        ]
                    )
                )
            s.flush()
            back = s.execute(
                select(IndexMember.symbol, IndexMember.start_date, IndexMember.end_date).where(
                    IndexMember.index_code == idx
                )
            ).all()
            recon.append(
                _log(
                    s,
                    f"qlib_members:{idx}",
                    q["root"] + f"/instruments/{idx}.txt",
                    m.get("file_sha256"),
                    len(m["rows"]),
                    m["checksum"],
                    len(back),
                    canon(sorted(f"{idx}|{a}|{b}|{c}" for a, b, c in back)),
                )
            )
        s.add(
            QlibStatus(
                kind="catalog",
                data={
                    "tag": q.get("tag"),
                    "calendar_first": q["calendar"][0] if q["calendar"] else None,
                    "calendar_last": q["calendar"][-1] if q["calendar"] else None,
                    "calendar_days": len(q["calendar"]),
                    "instruments": len(insts),
                    "active_on_last": sum(1 for _, _, c in insts if q["calendar"] and c == q["calendar"][-1]),
                    "feature_dirs": q.get("feature_dirs"),
                    "update_status": q.get("update_status"),
                    "reference_dates": q.get("reference_dates"),
                },
            )
        )
    core = snap.get("core_market") or {}
    if core.get("ok"):
        s.execute(delete(MarketSummary).where(MarketSummary.origin == "legacy"))
        for r in core["rows"]:
            s.add(
                MarketSummary(
                    origin="legacy",
                    trade_date=d(r["date"]),
                    data=r["qlib"],
                    payload_sha256=r["payload_sha256"],
                    generated_at=dt.datetime.fromisoformat(r["generated_at"]) if r.get("generated_at") else None,
                )
            )
        s.flush()
        back = s.execute(
            select(MarketSummary.trade_date, MarketSummary.payload_sha256)
            .where(MarketSummary.origin == "legacy")
            .order_by(MarketSummary.trade_date)
        ).all()
        recon.append(
            _log(
                s,
                "core_market",
                core["path"],
                None,
                len(core["rows"]),
                core["checksum"],
                len(back),
                canon(f"{a}|{b}" for a, b in back),
            )
        )
        bad = [r["date"] for r in core["rows"] if r["payload_sha256"] != r["computed_sha256"]]
        if bad:
            recon.append({"item": "core_market.payload_sha256_selfcheck", "status": "mismatch", "dates": bad[:10]})
    sdb = snap.get("snapshot_db") or {}
    if sdb.get("ok"):
        s.execute(delete(LegacySnapshot))
        for r in sdb["rows"]:
            s.add(
                LegacySnapshot(
                    date=d(r["date"]),
                    generated_at=r.get("generated_at"),
                    payload_sha256=r["payload_sha256"],
                    payload_bytes=r["bytes"],
                    keys=r.get("keys") or [],
                )
            )
        s.flush()
        back = s.execute(select(LegacySnapshot.date, LegacySnapshot.payload_sha256).order_by(LegacySnapshot.date)).all()
        recon.append(
            _log(
                s,
                "snapshot_db",
                sdb["path"],
                None,
                len(sdb["rows"]),
                sdb["checksum"],
                len(back),
                canon(f"{a}|{b}" for a, b in back),
            )
        )
    for summ in snap.get("qlib_summary") or []:
        if summ.get("ok"):
            stmt = insert(MarketSummary).values(
                origin="qp",
                trade_date=d(summ["date"]),
                data=summ,
                generated_at=dt.datetime.fromisoformat(snap["collected_at"]),
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["origin", "trade_date"],
                    set_={
                        "data": stmt.excluded.data,
                        "generated_at": stmt.excluded.generated_at,
                        "updated_at": func.now(),
                    },
                )
            )
    at = dt.datetime.fromisoformat(snap["collected_at"])
    set_inventory(s, "market.legacy_health", at, snap.get("health"))
    set_inventory(s, "market.cninfo_staging", at, snap.get("cninfo"))
    set_inventory(
        s,
        "market.reader",
        at,
        {"duration_s": snap.get("duration_s"), "summaries": [x.get("date") for x in snap.get("qlib_summary") or []]},
    )
    return recon


@job(
    "market.ingest_legacy",
    "Read-only legacy market import/mirror with row-count + checksum reconciliation",
    schedule_hint="after qp-market-read (06:45, 17:00, 23:25 CST)",
)
def ingest_job(ctx: JobContext) -> dict:
    _m().mem_guard()
    if not _m().MARKET_INBOX.is_file():
        return {"ok": False, "error": "no market snapshot"}
    snap = json.loads(_m().MARKET_INBOX.read_text())
    with Session(ctx.engine) as s:
        recon = ingest(s, snap)
        s.execute(delete(ImportLog).where(ImportLog.at < _m().utcnow() - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    mism = [r for r in recon if r["status"] != "match"]
    return {"ok": not mism, "items": len(recon), "mismatch": mism, "collected_at": snap.get("collected_at")}
