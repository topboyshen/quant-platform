"""Market health checks and source verdict application."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.jobs.inventory import apply_alerts
from qp.jobs.market_common import (
    RETAIN_DAYS,
    SOURCE_MAX_AGE_H,
    calendar_for_expectation,
    d,
    expected_trade_date,
    now_cst,
    open_days,
    trading_lag,
)
from qp.jobs.market_compare import RETIRED_HEALTH_CHECKS, SOURCE_OPTIONAL, SOURCE_ROLES, compare_latest, source_verdict
from qp.jobs.registry import JobContext, job
from qp.models.market import HealthResult, IndexClose, MarketSummary, QlibStatus, SourceStatus, TradingDay


def _m():
    from qp.jobs import market

    return market


def health(s: Session, now: dt.datetime | None = None) -> list[tuple[str, str, str, dict]]:
    now = now or now_cst()
    out = []
    days, cal_meta = calendar_for_expectation(s, now.date())
    qp_open = open_days(s, "qp")
    upd = s.scalar(select(func.max(TradingDay.updated_at)).where(TradingDay.origin == "qp"))
    cal_sources = dict(
        s.execute(
            select(TradingDay.source, func.count()).where(TradingDay.origin == "qp").group_by(TradingDay.source)
        ).all()
    )
    if not qp_open:
        out.append(("calendar.qp", "warning", "qp calendar empty (Dolt not fetched yet)", {"sources": cal_sources}))
    else:
        ahead = (qp_open[-1] - now.date()).days
        age_d = (_m().utcnow() - upd).total_seconds() / 86400 if upd else 999
        st = "ok" if ahead >= 30 and age_d <= 8 else "warning"
        src_txt = ", ".join(f"{k}({v})" for k, v in sorted(cal_sources.items()))
        out.append(
            (
                "calendar.qp",
                st,
                f"max open {qp_open[-1]} ({ahead}d ahead), refreshed {age_d:.1f}d ago; sources: {src_txt}",
                {
                    "max_open": qp_open[-1].isoformat(),
                    "days_ahead": ahead,
                    "age_days": round(age_d, 2),
                    "sources": cal_sources,
                },
            )
        )

    def want_at(cutoff: dt.time) -> dt.date | None:
        return expected_trade_date(days, now, cutoff) if days else None

    def unknown_expect(have_date=None, **extra):
        return (
            "unknown",
            "无法确定期望交易日（日历覆盖不足或尚未到截止时间）",
            {
                "expected": None,
                "latest": str(have_date) if have_date else None,
                "lag": None,
                "calendar": cal_meta,
                **extra,
            },
        )

    have = s.execute(
        select(IndexClose.trade_date, func.count())
        .where(IndexClose.origin == "qp")
        .group_by(IndexClose.trade_date)
        .order_by(IndexClose.trade_date.desc())
        .limit(1)
    ).first()
    have_day, have_n = (have[0], have[1]) if have else (None, 0)
    want = want_at(dt.time(16, 30))
    if want is None:
        st, summary, det = unknown_expect(have_day, count=have_n)
        out.append(("index_close.qp_current", st, summary, det))
    else:
        lag = trading_lag(days, have_day, want)
        st = "ok" if lag == 0 and have_n == 4 else ("warning" if lag in (0, 1) else "critical")
        out.append(
            (
                "index_close.qp_current",
                st,
                f"expected {want}, latest qp {have_day} ({have_n}/4)",
                {
                    "expected": want.isoformat(),
                    "latest": str(have_day) if have_day else None,
                    "lag": lag,
                    "count": have_n,
                },
            )
        )

    cat = s.execute(
        select(QlibStatus).where(QlibStatus.kind == "catalog").order_by(QlibStatus.id.desc()).limit(1)
    ).scalar()
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
            out.append(
                (
                    "qlib.current",
                    st,
                    f"qlib calendar last {ql}, expected {want_q}, lag {lag}",
                    {"expected": want_q.isoformat(), "lag": lag, **extra},
                )
            )
    else:
        out.append(("qlib.current", "unknown", "no qlib catalog yet", {}))
    srcq = s.execute(
        select(QlibStatus).where(QlibStatus.kind == "source").order_by(QlibStatus.id.desc()).limit(1)
    ).scalar()
    eod = eod_source = None
    src_age_h = None
    if srcq:
        data = srcq.data or {}
        eod = data.get("latest_eod") or data.get("dolt_latest_eod")
        eod_source = data.get("latest_eod_source") or ("dolt" if data.get("dolt_latest_eod") else None)
        if not eod and data.get("github_release_tag"):
            eod, eod_source = data["github_release_tag"], "github_release"
        src_age_h = (_m().utcnow() - srcq.at).total_seconds() / 3600
    eod_day = None
    if eod:
        try:
            eod_day = d(eod)
        except ValueError:
            eod = eod_source = None
    if not srcq:
        out.append(("qlib.source_dolt", "unknown", "Dolt latest EOD unknown (no source check yet)", {}))
    elif src_age_h is not None and src_age_h > SOURCE_MAX_AGE_H:
        out.append(
            (
                "qlib.source_dolt",
                "unknown",
                f"来源检查 {src_age_h:.1f}h 前，超过 {SOURCE_MAX_AGE_H}h；无法确认上游 EOD",
                {**(srcq.data or {}), "age_hours": round(src_age_h, 1)},
            )
        )
    elif eod and eod_day:
        if want_q is None:
            st, summary, det = unknown_expect(
                eod, **(srcq.data or {}), eod_source=eod_source, observed_at=srcq.at.isoformat()
            )
            out.append(("qlib.source_dolt", st, summary, det))
        else:
            lag = trading_lag(days, eod_day, want_q)
            if eod_source == "dolt":
                summary = f"Dolt latest EOD {eod}, expected {want_q}, lag {lag}"
            else:
                why = (srcq.data or {}).get("dolt_error") or "不可达"
                summary = f"上游最新 EOD {eod}（GitHub release；Dolt {why}），expected {want_q}, lag {lag}"
            out.append(
                (
                    "qlib.source_dolt",
                    "ok" if lag == 0 else "warning",
                    summary,
                    {
                        **(srcq.data or {}),
                        "expected": want_q.isoformat(),
                        "lag": lag,
                        "eod_source": eod_source,
                        "observed_at": srcq.at.isoformat(),
                    },
                )
            )
    else:
        out.append(
            (
                "qlib.source_dolt",
                "unknown",
                "Dolt latest EOD unknown",
                {**(srcq.data or {}), "age_hours": round(src_age_h, 1) if src_age_h is not None else None},
            )
        )
    qp_core = s.scalar(select(func.max(MarketSummary.trade_date)).where(MarketSummary.origin == "qp"))
    leg_core = s.scalar(select(func.max(MarketSummary.trade_date)).where(MarketSummary.origin == "legacy"))
    want_s = want_at(dt.time(23, 15))
    if want_s is None:
        st, summary, det = unknown_expect(qp_core, legacy_latest=str(leg_core) if leg_core else None)
        out.append(("snapshot.qp_current", st, summary, det))
    else:
        lag = trading_lag(days, qp_core, want_s)
        st = "ok" if lag == 0 else ("warning" if lag == 1 else "critical")
        out.append(
            (
                "snapshot.qp_current",
                st,
                f"qp summary latest {qp_core}, expected {want_s}, lag {lag}；legacy core latest {leg_core}（仅参考）",
                {
                    "latest": str(qp_core) if qp_core else None,
                    "expected": want_s.isoformat(),
                    "lag": lag,
                    "legacy_latest": str(leg_core) if leg_core else None,
                    "legacy_lag": trading_lag(days, leg_core, want_s),
                },
            )
        )
    from qp.models.ops import Inventory

    lh = s.get(Inventory, "market.legacy_health")
    if lh and lh.data and lh.data.get("ok"):
        data = lh.data
        age_h = (_m().utcnow() - lh.at).total_seconds() / 3600
        out.append(
            (
                "collector.market_reader",
                "ok" if age_h <= 26 else "warning",
                f"legacy market snapshot {age_h:.1f}h old",
                {
                    "age_hours": round(age_h, 1),
                    "legacy_status": data.get("status"),
                    "legacy_mode": data.get("businessMode"),
                    "legacy_effective": data.get("effectiveTradeDate"),
                    "legacy_generated_at": data.get("generatedAt"),
                    "legacy_issues": data.get("issues"),
                },
            )
        )
    else:
        out.append(("collector.market_reader", "unknown", "legacy market snapshot not ingested yet", {}))
    latest = {}
    for r in s.execute(
        select(SourceStatus).where(SourceStatus.at > _m().utcnow() - dt.timedelta(hours=26)).order_by(SourceStatus.at)
    ).scalars():
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
        out.append(
            (
                "sources.reachable",
                "ok" if not bad else "warning",
                summary,
                {
                    k: {
                        "ok": v.ok,
                        "ms": v.ms,
                        "via_proxy": v.via_proxy,
                        "covered_by": covered.get(k) or None,
                        "optional": k in SOURCE_OPTIONAL,
                        "role": SOURCE_ROLES.get(k, "optional" if k in SOURCE_OPTIONAL else "standalone"),
                        **(v.detail or {}),
                    }
                    for k, v in latest.items()
                },
            )
        )
    else:
        out.append(("sources.reachable", "unknown", "no probe in 26h", {}))
    c_status, c_summary, c_detail = compare_latest(s, now)
    out.append(("compare.latest", c_status, c_summary, c_detail))
    return out


@job(
    "market.health",
    "Market data health checks -> market.health_result; warning/critical -> shadow alerts",
    schedule_hint="07:05, 17:20, 23:50 CST (legacy 22:45/23:00)",
)
def health_job(ctx: JobContext) -> dict:
    run_at = _m().utcnow()
    with Session(ctx.engine) as s:
        res = health(s)
        for check, st, summary, detail in res:
            s.add(HealthResult(run_at=run_at, check=check, status=st, summary=summary, detail=detail))
        firing = [
            (f"market:health:{c}", "critical" if st == "critical" else "warning", f"market {c}: {summary}", detail)
            for c, st, summary, detail in res
            if st in ("warning", "critical")
        ]
        only_resolve = {f"market:health:{c}" for c, st, _, _ in res if st == "ok"}
        # Retire replaced checks once; their old keys must not stay firing forever just
        # because the check no longer appears in the latest run.
        retired = {f"market:health:{c}" for c in RETIRED_HEALTH_CHECKS}
        only_resolve |= retired
        keep_open = {f"market:health:{c}" for c, st, _, _ in res if st == "unknown"}
        al = apply_alerts(
            s,
            firing,
            ctx.run_id,
            owns=lambda k: k.startswith("market:health:"),
            keep_open=keep_open,
            only_resolve=only_resolve,
        )
        s.execute(delete(HealthResult).where(HealthResult.run_at < run_at - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    return {"checks": {c: st for c, st, _, _ in res}, "alerts": al, "retired": sorted(retired)}
