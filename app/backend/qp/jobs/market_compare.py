"""Market compare jobs and source verdicts."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from qp.jobs.inventory import apply_alerts
from qp.jobs.market_common import RETAIN_DAYS, now_cst, open_days
from qp.jobs.registry import JobContext, job
from qp.models.market import CompareResult, IndexClose, Instrument, MarketSummary, TradingDay
from qp.models.ops import JobRun


def _m():
    from qp.jobs import market

    return market


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
            out.append(
                (
                    f"calendar.{other}_vs_qp",
                    "skipped",
                    "missing data",
                    _cov(0, 0, 0, 0, qp=len(qp_all), **{other: len(od)}),
                )
            )
            continue
        lo, hi = max(min(qp_all), od[0]), min(max(qp_all), od[-1])
        a = {x for x in od if lo <= x <= hi}
        b = {x for x in qp_days if lo <= x <= hi}
        only_o, only_q = sorted(a - b), sorted(b - a)
        expected = len(a | b)
        diffs_n = len(only_o) + len(only_q)
        st = _item_verdict(expected, 0, diffs_n)
        outside = sorted(x for x in od if x < lo or x > hi)
        det = _cov(
            expected,
            expected,
            0,
            diffs_n,
            range=[lo.isoformat(), hi.isoformat()],
            days=len(a),
            outside=len(outside),
            **{f"{other}_only": [x.isoformat() for x in only_o[:20]], "qp_only": [x.isoformat() for x in only_q[:20]]},
        )
        out.append(
            (
                f"calendar.{other}_vs_qp",
                st,
                f"{lo}..{hi}: {_cov_summary(expected, expected, 0, diffs_n)}；{other}-only {len(only_o)}, qp-only {len(only_q)}"
                + (
                    f"；{other} 另有 {len(outside)} 天在区间外（{outside[0]}..{outside[-1]}），未参与比较"
                    if outside
                    else ""
                ),
                det,
            )
        )
    leg = (
        s.execute(
            select(IndexClose).where(
                IndexClose.origin == "legacy", IndexClose.trade_date >= now_cst().date() - dt.timedelta(days=20)
            )
        )
        .scalars()
        .all()
    )
    if leg:
        qp = {
            (r.symbol, r.trade_date): r
            for r in s.execute(
                select(IndexClose).where(
                    IndexClose.origin == "qp",
                    tuple_(IndexClose.symbol, IndexClose.trade_date).in_([(x.symbol, x.trade_date) for x in leg]),
                )
            ).scalars()
        }
        diff_rows, missing_rows = [], []
        for x in leg:
            y = qp.get((x.symbol, x.trade_date))
            if y is None:
                missing_rows.append(f"{x.symbol}@{x.trade_date}")
            elif abs(float(y.close) - float(x.close)) > 0.011:
                diff_rows.append(
                    {
                        "symbol": x.symbol,
                        "date": x.trade_date.isoformat(),
                        "legacy": float(x.close),
                        "qp": float(y.close),
                    }
                )
        expected, missing_n, diffs_n = len(leg), len(missing_rows), len(diff_rows)
        compared = expected - missing_n
        st = _item_verdict(expected, missing_n, diffs_n)
        out.append(
            (
                "index_close.legacy_vs_qp",
                st,
                _cov_summary(expected, compared, missing_n, diffs_n),
                _cov(expected, compared, missing_n, diffs_n, diff_rows=diff_rows[:20], missing_rows=missing_rows[:20]),
            )
        )
    else:
        out.append(("index_close.legacy_vs_qp", "skipped", "no legacy rows in last 20 days", _cov(0, 0, 0, 0)))
    legs = {r.trade_date: r for r in s.execute(select(MarketSummary).where(MarketSummary.origin == "legacy")).scalars()}
    qps = {r.trade_date: r for r in s.execute(select(MarketSummary).where(MarketSummary.origin == "qp")).scalars()}
    both = sorted(set(legs) & set(qps))
    if not both:
        out.append(
            (
                "snapshot.core_vs_qp",
                "skipped",
                "no common date between legacy core-market and qp summary",
                _cov(
                    0,
                    0,
                    0,
                    0,
                    legacy_latest=max(legs).isoformat() if legs else None,
                    qp_dates=[x.isoformat() for x in sorted(qps)][-3:],
                ),
            )
        )
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
        out.append(
            (
                "snapshot.core_vs_qp",
                st,
                f"{day}: {_cov_summary(1, 1, 0, diffs_n)}"
                + (f"；differs in {','.join(fields)}" if fields else "；breadth/coverage/top lists equal"),
                _cov(1, 1, 0, diffs_n, date=day.isoformat(), diff_fields=fields),
            )
        )
    named = dict(s.execute(select(Instrument.symbol, Instrument.name).where(Instrument.name.is_not(None))).all())
    last = s.scalar(select(func.max(Instrument.qlib_end)))
    active = (
        set(s.execute(select(Instrument.symbol).where(Instrument.in_qlib, Instrument.qlib_end == last)).scalars())
        if last
        else set()
    )
    stocks_named = {k for k in named if k[2:3] in "0123456789"}
    if active and stocks_named:
        missing_rows = sorted(active - stocks_named)
        ratio = len(missing_rows) / len(active)
        # names exist for the catalog; a small gap is a mismatch, not missing evidence
        diffs_n = len(missing_rows) if ratio > 0.02 else 0
        st = _item_verdict(len(active), 0, diffs_n)
        out.append(
            (
                "instruments.qlib_active_vs_names",
                st,
                f"qlib active {len(active)} on {last}, without name {len(missing_rows)} ({ratio:.1%})；"
                + _cov_summary(len(active), len(active), 0, diffs_n),
                _cov(len(active), len(active), 0, diffs_n, missing_sample=missing_rows[:20], ratio=round(ratio, 4)),
            )
        )
    else:
        out.append(("instruments.qlib_active_vs_names", "skipped", "no names or qlib catalog yet", _cov(0, 0, 0, 0)))
    return out


@job(
    "market.compare",
    "Daily compare of new (qp) vs legacy outputs; mismatches -> shadow alerts",
    schedule_hint="07:00, 17:15, 23:45 CST",
)
def compare_job(ctx: JobContext) -> dict:
    run_at = _m().utcnow()
    with Session(ctx.engine) as s:
        res = compare(s)
        for item, st, summary, detail in res:
            s.add(CompareResult(run_at=run_at, item=item, status=st, summary=summary, detail=detail))
        firing = [
            (f"market:compare:{item}", "warning", f"compare {item}: {summary}", detail)
            for item, st, summary, detail in res
            if st == "mismatch"
        ]
        only_resolve = {f"market:compare:{item}" for item, st, _, _ in res if st == "match"}
        keep_open = {f"market:compare:{item}" for item, st, _, _ in res if st in ("skipped", "insufficient")}
        al = apply_alerts(
            s,
            firing,
            ctx.run_id,
            owns=lambda k: k.startswith("market:compare:"),
            keep_open=keep_open,
            only_resolve=only_resolve,
        )
        s.execute(delete(CompareResult).where(CompareResult.run_at < run_at - dt.timedelta(days=RETAIN_DAYS)))
        s.commit()
    return {
        "items": len(res),
        "mismatch": [r[0] for r in res if r[1] == "mismatch"],
        "insufficient": [r[0] for r in res if r[1] == "insufficient"],
        "skipped": [r[0] for r in res if r[1] == "skipped"],
        "alerts": al,
    }


def compare_latest(s: Session, now: dt.datetime) -> tuple[str, str, dict]:
    """Judge the latest compare run with a fixed required-item set and run identity.

    The denominator is REQUIRED_COMPARE_ITEMS, never the number of rows that happen to
    exist.  Results are accepted only when the latest market.compare job run succeeded,
    the rows fall inside that run's time window, and run_at is fresh.  A required item
    with no row at all counts as missing (unknown), not as a smaller denominator.
    """
    required = REQUIRED_COMPARE_ITEMS
    expected = len(required)
    now_utc = now.astimezone(dt.UTC)
    jr = s.execute(
        select(JobRun)
        .where(JobRun.job_name == "market.compare")
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    base = {
        "expected": expected,
        "compared": 0,
        "missing": expected,
        "diffs": 0,
        "required": list(required),
        "absent": list(required),
        "insufficient": [],
        "skipped": [],
        "mismatch": [],
        "extra": {},
        "job_run_id": None,
        "job_status": None,
        "run_at": None,
        "job_started_at": None,
        "job_finished_at": None,
    }
    if jr is None:
        return (
            "unknown",
            "尚无 market.compare 运行记录；" + _cov_summary(expected, 0, expected, 0),
            {**base, "reason": "no_job_run"},
        )
    base.update(
        job_run_id=jr.id,
        job_status=jr.status,
        job_started_at=jr.started_at.isoformat(),
        job_finished_at=jr.finished_at.isoformat() if jr.finished_at else None,
    )
    if jr.status != "succeeded" or jr.finished_at is None:
        return (
            "unknown",
            f"最近一次 market.compare 状态为 {jr.status}；" + _cov_summary(expected, 0, expected, 0),
            {**base, "reason": "job_not_succeeded"},
        )
    run_at = s.scalar(
        select(func.max(CompareResult.run_at)).where(
            CompareResult.run_at >= jr.started_at - COMPARE_RUN_SKEW,
            CompareResult.run_at <= jr.finished_at + COMPARE_RUN_SKEW,
        )
    )
    if run_at is None:
        return (
            "unknown",
            "最近一次 market.compare 没有对应结果；" + _cov_summary(expected, 0, expected, 0),
            {**base, "reason": "no_results_for_run"},
        )
    rows = (
        s.execute(select(CompareResult).where(CompareResult.run_at == run_at).order_by(CompareResult.id))
        .scalars()
        .all()
    )
    by_item = {r.item: r for r in rows if r.item in required}
    absent = [i for i in required if i not in by_item]
    mismatch = [i for i in required if i in by_item and by_item[i].status == "mismatch"]
    insufficient = [i for i in required if i in by_item and by_item[i].status == "insufficient"]
    skipped = [i for i in required if i in by_item and by_item[i].status == "skipped"]
    compared = sum(1 for i in required if i in by_item and by_item[i].status in ("match", "mismatch"))
    missing = expected - compared
    diffs = len(mismatch)
    age_h = (now_utc - run_at).total_seconds() / 3600
    detail = {
        **base,
        "run_at": run_at.isoformat(),
        "compared": compared,
        "missing": missing,
        "diffs": diffs,
        "absent": absent,
        "insufficient": insufficient,
        "skipped": skipped,
        "mismatch": mismatch,
        "extra": {r.item: r.status for r in rows if r.item not in required},
        "age_hours": round(age_h, 2),
    }
    summary = _cov_summary(expected, compared, missing, diffs)
    if age_h > COMPARE_MAX_AGE_H:
        return (
            "unknown",
            f"最近比较结果 {age_h:.1f}h 前，超过 {COMPARE_MAX_AGE_H}h；" + summary,
            {**detail, "reason": "stale"},
        )
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
    # Tencent is the primary name source and Eastmoney clist the full-market fallback;
    # when Eastmoney is down Tencent still refreshes the instruments qp tracks
    # (see instruments_job).
    "eastmoney": ("tencent",),
}
SOURCE_OPTIONAL = {"akshare_sina_calendar", "baostock"}
# Dashboard role: primary is the path qp prefers, fallback is only used when the
# primary is down.  Display metadata only; source_verdict stays authoritative.
SOURCE_ROLES = {
    "tencent": "primary",  # names + index closes
    "eastmoney": "fallback",  # full-market names
    "sina": "fallback",  # index closes
    "dolt": "primary",  # calendar + EOD
    "github_release": "fallback",  # EOD release tag
}
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
    Tencent/Eastmoney names, Dolt/GitHub release).  Dolt's calendar capability is also
    covered when a calendar mirror already reaches today, so that fallback is passed in
    explicitly.
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
    bad = sorted(name for name in latest if not ok[name] and not covered[name] and name not in SOURCE_OPTIONAL)
    return bad, covered
