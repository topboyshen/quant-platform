"""Counterexamples for M3 health/compare verdict bugs (must fail on a5d5bb1, then pass after the fix)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.market import CompareResult, HealthResult, IndexClose, MarketSummary, SourceStatus, TradingDay
from qp.models.ops import Alert

CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 11, 17, 0, tzinfo=CST)  # Wed, after 16:30 cutoff
DAY = dt.date(2026, 11, 4)  # stale calendar / quote day
INDEXES = ("sh000001", "sh000300", "sz399001", "sz399006")


def _wipe(s: Session) -> None:
    s.execute(delete(Alert).where(Alert.key.like("market:%")))
    for model in (HealthResult, CompareResult, SourceStatus, IndexClose, MarketSummary, TradingDay):
        s.execute(delete(model))
    s.commit()


def _open_days(s: Session, origin: str, days: list[dt.date], *, also_closed_until: dt.date | None = None) -> None:
    have = set(days)
    span = list(days)
    if also_closed_until is not None:
        d = min(days)
        while d <= also_closed_until:
            span.append(d)
            d += dt.timedelta(days=1)
        span = sorted(set(span))
    for d in span:
        s.add(TradingDay(origin=origin, date=d, is_open=d in have, source="test"))


def _closes(s: Session, origin: str, day: dt.date, symbols=INDEXES, price: float = 100.0) -> None:
    for sym in symbols:
        s.add(IndexClose(origin=origin, symbol=sym, trade_date=day, close=price, source="test"))


def _alert(s: Session, key: str, summary: str = "old fault") -> Alert:
    a = Alert(key=key, severity="warning", state="firing", summary=summary, last_seen_at=utcnow())
    s.add(a)
    return a


def _by_check(rows) -> dict:
    return {c: (st, summary, detail) for c, st, summary, detail in rows}


# ---------------------------------------------------------------------------
# 1 + 4. Missing evidence / unknown / absent must not resolve an open alert
# ---------------------------------------------------------------------------
def test_unknown_health_does_not_resolve_open_alert(engine):
    """sources.reachable is unknown when no probe exists; an open alert must stay firing."""
    from qp.jobs import run_job

    with Session(engine) as s:
        _wipe(s)
        _alert(s, "market:health:sources.reachable")
        s.commit()
    r = run_job(engine, "market.health", trigger="test")
    assert r.status == "succeeded", r.error
    with Session(engine) as s:
        a = s.execute(select(Alert).where(Alert.key == "market:health:sources.reachable")).scalar_one()
        hs = {h.check: h.status for h in s.execute(select(HealthResult).where(
            HealthResult.run_at == s.scalar(select(HealthResult.run_at).order_by(HealthResult.run_at.desc()).limit(1))
        )).scalars()}
        assert hs.get("sources.reachable") == "unknown"
        assert a.state == "firing" and a.resolved_at is None, (a.state, a.resolved_at, hs)


def test_absent_check_does_not_resolve_open_alert(engine):
    """An open alert whose check is missing from this run must stay firing (old-state migration)."""
    from qp.jobs import run_job

    with Session(engine) as s:
        _wipe(s)
        _alert(s, "market:health:removed.check")
        s.commit()
    r = run_job(engine, "market.health", trigger="test")
    assert r.status == "succeeded", r.error
    with Session(engine) as s:
        a = s.execute(select(Alert).where(Alert.key == "market:health:removed.check")).scalar_one()
        assert a.state == "firing" and a.resolved_at is None, (a.state, a.resolved_at)


def test_health_ok_does_resolve_open_alert(engine):
    """Valid recovery evidence (status ok) is allowed to close the matching open alert."""
    from qp.jobs import run_job

    with Session(engine) as s:
        _wipe(s)
        # dense qp calendar covering today and ahead, so calendar.qp is ok
        start, end = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
        d = start
        while d <= end:
            s.add(TradingDay(origin="qp", date=d, is_open=d.weekday() < 5, source="test"))
            d += dt.timedelta(days=1)
        _alert(s, "market:health:calendar.qp", summary="old calendar warning")
        s.commit()
    r = run_job(engine, "market.health", trigger="test")
    assert r.status == "succeeded", r.error
    with Session(engine) as s:
        hs = {h.check: h.status for h in s.execute(select(HealthResult)).scalars()}
        assert hs.get("calendar.qp") == "ok", hs
        a = s.execute(select(Alert).where(Alert.key == "market:health:calendar.qp")).scalar_one()
        assert a.state == "resolved" and a.resolved_at is not None


# ---------------------------------------------------------------------------
# 2. Stale calendar must not lower the expected trade date
# ---------------------------------------------------------------------------
def test_stale_calendar_keeps_expected_unknown(engine):
    """If the qp calendar and quotes both stop on the same day, quotes must not be judged current."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        # qp calendar only has open days through 2026-11-04 (and closed days up to that day)
        opens = [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)]
        _open_days(s, "qp", opens, also_closed_until=dt.date(2026, 11, 4))
        _closes(s, "qp", DAY)
        s.commit()
        rows = _by_check(health(s, now=NOW))
        st, summary, detail = rows["index_close.qp_current"]
        assert st == "unknown", (st, summary, detail)
        assert detail.get("expected") in (None, "None"), detail
        assert st != "ok"


def test_stale_qp_does_not_hide_behind_empty_legacy(engine):
    """A non-empty but stale qp calendar must not be preferred just because it is non-empty."""
    from qp.jobs.market import calendar_for_expectation, expected_trade_date, health

    with Session(engine) as s:
        _wipe(s)
        opens = [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)]
        _open_days(s, "qp", opens, also_closed_until=dt.date(2026, 11, 4))
        _closes(s, "qp", DAY)
        s.commit()
        raw = calendar_for_expectation(s)
        days, meta = (raw if isinstance(raw, tuple) else (raw, {}))
        want = expected_trade_date(days or [], NOW, dt.time(16, 30)) if days else None
        assert want is None, (want, days, meta)
        st, _, detail = _by_check(health(s, now=NOW))["index_close.qp_current"]
        assert st == "unknown" and detail.get("latest") == "2026-11-04"


def test_valid_legacy_calendar_used_when_qp_is_stale(engine):
    """If qp is stale but legacy still covers today, expectation should use legacy (quotes then lag)."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _open_days(s, "qp", [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)],
                   also_closed_until=dt.date(2026, 11, 4))
        # legacy open-days-only calendar that includes today and the next day
        legacy_opens = [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4),
                        dt.date(2026, 11, 5), dt.date(2026, 11, 6), dt.date(2026, 11, 9),
                        dt.date(2026, 11, 10), dt.date(2026, 11, 11), dt.date(2026, 11, 12)]
        _open_days(s, "legacy", legacy_opens)
        _closes(s, "qp", DAY)
        s.commit()
        st, summary, detail = _by_check(health(s, now=NOW))["index_close.qp_current"]
        assert detail.get("expected") == "2026-11-11", (st, summary, detail)
        assert st in ("warning", "critical")  # lag of several sessions, not ok/unknown


# ---------------------------------------------------------------------------
# 3. Partial / skipped compares must not be summarised as "0 mismatches" / match
# ---------------------------------------------------------------------------
def test_partial_index_compare_is_not_match(engine):
    """Missing required index-close rows must not yield match even when compared prices agree."""
    from qp.jobs.market import compare

    with Session(engine) as s:
        _wipe(s)
        _closes(s, "legacy", dt.date(2026, 11, 10), INDEXES, 100.0)
        _closes(s, "qp", dt.date(2026, 11, 10), INDEXES[:2], 100.0)  # two of four missing
        s.commit()
        res = {item: (st, summary, detail) for item, st, summary, detail in compare(s)}
        st, summary, detail = res["index_close.legacy_vs_qp"]
        assert st not in ("match",), (st, summary, detail)
        assert st == "insufficient", (st, summary, detail)
        assert detail["expected"] == 4 and detail["compared"] == 2
        assert detail["missing"] == 2 and detail["diffs"] == 0


def test_compare_latest_insufficient_when_required_skipped(engine):
    """A compare run with no mismatches but skipped/incomplete items is 证据不足, not ok."""
    from qp.jobs.market import health

    run_at = utcnow()
    with Session(engine) as s:
        _wipe(s)
        for item, st in (("calendar.legacy_vs_qp", "skipped"),
                         ("calendar.qlib_vs_qp", "skipped"),
                         ("index_close.legacy_vs_qp", "skipped"),
                         ("snapshot.core_vs_qp", "match"),
                         ("instruments.qlib_active_vs_names", "skipped")):
            s.add(CompareResult(run_at=run_at, item=item, status=st, summary="t", detail={}))
        s.commit()
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st != "ok", (st, summary, detail)
        assert st == "unknown", (st, summary, detail)
        assert detail["expected"] == 5 and detail["compared"] == 1
        assert detail["missing"] == 4 and detail["diffs"] == 0
        assert "应比" in summary and "实比" in summary and "缺失" in summary and "差异" in summary
        assert "0 mismatches" not in summary and "0 处不一致" not in summary


def test_compare_job_skipped_does_not_resolve_mismatch_alert(engine):
    """If a required compare cannot run (skipped), a previously open mismatch alert stays open."""
    from qp.jobs import run_job

    with Session(engine) as s:
        _wipe(s)
        _alert(s, "market:compare:index_close.legacy_vs_qp", summary="old mismatch")
        s.commit()
    r = run_job(engine, "market.compare", trigger="test")
    assert r.status == "succeeded", r.error
    with Session(engine) as s:
        res = {c.item: c.status for c in s.execute(select(CompareResult)).scalars()}
        assert res.get("index_close.legacy_vs_qp") in ("skipped", "insufficient")
        a = s.execute(select(Alert).where(Alert.key == "market:compare:index_close.legacy_vs_qp")).scalar_one()
        assert a.state == "firing" and a.resolved_at is None, (a.state, a.resolved_at, res)


def test_inventory_still_resolves_its_own_alerts(engine):
    """ops.inventory keeps auto-resolve behaviour for keys it owns; it must not touch market alerts."""
    from qp.jobs.inventory import apply_alerts

    with Session(engine) as s:
        _wipe(s)
        _alert(s, "host:disk")
        _alert(s, "market:health:index_close.qp_current")
        apply_alerts(s, [], 0)
        s.commit()
        states = {a.key: a.state for a in s.execute(select(Alert)).scalars()}
        assert states["host:disk"] == "resolved"
        assert states["market:health:index_close.qp_current"] == "firing"
