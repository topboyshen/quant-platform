"""Counterexamples for M3 health/compare verdict bugs (must fail on a5d5bb1, then pass after the fix)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.market import (
    CompareResult,
    HealthResult,
    IndexClose,
    MarketSummary,
    QlibStatus,
    SourceStatus,
    TradingDay,
)
from qp.models.ops import Alert, Inventory, JobRun

CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 11, 17, 0, tzinfo=CST)  # Wed, after 16:30 cutoff
DAY = dt.date(2026, 11, 4)  # stale calendar / quote day
INDEXES = ("sh000001", "sh000300", "sz399001", "sz399006")
ALL_COMPARE_ITEMS = ("calendar.legacy_vs_qp", "calendar.qlib_vs_qp", "index_close.legacy_vs_qp",
                     "snapshot.core_vs_qp", "instruments.qlib_active_vs_names")


def _wipe(s: Session) -> None:
    s.execute(delete(Alert))
    s.execute(delete(JobRun).where(JobRun.job_name == "market.compare"))
    s.execute(delete(Inventory).where(Inventory.kind.in_(("market.legacy_health", "market.reader"))))
    for model in (HealthResult, CompareResult, SourceStatus, QlibStatus, IndexClose, MarketSummary, TradingDay):
        s.execute(delete(model))
    s.commit()


def _compare_run(s: Session, statuses: dict[str, str], *, at: dt.datetime | None = None,
                 job_status: str = "succeeded") -> dt.datetime:
    """Insert one succeeded compare run plus its results at a fixed timestamp."""
    at = at or (NOW.astimezone(dt.UTC) - dt.timedelta(minutes=5))
    jr = JobRun(job_name="market.compare", status=job_status, trigger="test", started_at=at,
                finished_at=at + dt.timedelta(seconds=10), result={})
    s.add(jr)
    s.flush()
    run_at = at + dt.timedelta(seconds=1)
    for item, st in statuses.items():
        s.add(CompareResult(run_at=run_at, item=item, status=st, summary="t", detail={}))
    s.commit()
    return run_at


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

    with Session(engine) as s:
        _wipe(s)
        _compare_run(s, {"calendar.legacy_vs_qp": "skipped",
                         "calendar.qlib_vs_qp": "skipped",
                         "index_close.legacy_vs_qp": "skipped",
                         "snapshot.core_vs_qp": "match",
                         "instruments.qlib_active_vs_names": "skipped"})
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st != "ok", (st, summary, detail)
        assert st == "unknown", (st, summary, detail)
        assert detail["expected"] == 5 and detail["compared"] == 1
        assert detail["missing"] == 4 and detail["diffs"] == 0
        assert "应比" in summary and "实比" in summary and "缺失" in summary and "差异" in summary
        assert "0 mismatches" not in summary and "0 处不一致" not in summary


def test_compare_latest_absent_required_item_is_missing(engine):
    """A required item with no row at all must count as missing instead of shrinking the denominator."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _compare_run(s, {item: "match" for item in ALL_COMPARE_ITEMS if item != "index_close.legacy_vs_qp"})
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st == "unknown", (st, summary, detail)
        assert detail["expected"] == 5 and detail["compared"] == 4
        assert detail["missing"] == 1 and detail["diffs"] == 0
        assert detail["absent"] == ["index_close.legacy_vs_qp"]


def test_compare_latest_all_required_match_is_ok(engine):
    """Only a fresh, succeeded run covering every required item may pass."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _compare_run(s, {**{item: "match" for item in ALL_COMPARE_ITEMS}, "extra.item": "match"})
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st == "ok", (st, summary, detail)
        assert detail["expected"] == 5 and detail["compared"] == 5
        assert detail["missing"] == 0 and detail["diffs"] == 0
        assert detail["extra"] == {"extra.item": "match"}
        assert detail["job_status"] == "succeeded" and detail["job_run_id"]


def test_compare_latest_mismatch_is_warning(engine):
    """A fresh, complete run with a real mismatch is a warning, not unknown."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _compare_run(s, {**{item: "match" for item in ALL_COMPARE_ITEMS},
                         "index_close.legacy_vs_qp": "mismatch"})
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st == "warning", (st, summary, detail)
        assert detail["compared"] == 5 and detail["missing"] == 0 and detail["diffs"] == 1
        assert detail["mismatch"] == ["index_close.legacy_vs_qp"]


def test_compare_latest_stale_run_is_unknown(engine):
    """An old compare run must not pass even when every required item matched."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _compare_run(s, {item: "match" for item in ALL_COMPARE_ITEMS},
                     at=NOW.astimezone(dt.UTC) - dt.timedelta(hours=27))
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st == "unknown", (st, summary, detail)
        assert detail["reason"] == "stale" and detail["age_hours"] > 26
        assert detail["compared"] == 5


def test_compare_latest_failed_job_is_unknown(engine):
    """Results are not accepted when the latest compare job did not succeed."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _compare_run(s, {item: "match" for item in ALL_COMPARE_ITEMS}, job_status="failed")
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st == "unknown", (st, summary, detail)
        assert detail["reason"] == "job_not_succeeded" and detail["job_status"] == "failed"
        assert detail["expected"] == 5 and detail["missing"] == 5


def test_compare_latest_without_job_run_is_unknown(engine):
    """No compare job run at all is unknown, not an implicit pass."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        st, summary, detail = _by_check(health(s, now=NOW))["compare.latest"]
        assert st == "unknown", (st, summary, detail)
        assert detail["reason"] == "no_job_run"
        assert detail["expected"] == 5 and detail["compared"] == 0 and detail["missing"] == 5


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


def test_apply_alerts_resolves_only_explicit_recovery_keys(engine):
    """Without recovery evidence an owned alert stays firing; market keys stay out of scope."""
    from qp.jobs.inventory import apply_alerts

    with Session(engine) as s:
        _wipe(s)
        _alert(s, "host:disk")
        _alert(s, "market:health:index_close.qp_current")
        apply_alerts(s, [], 0, only_resolve=set())
        s.commit()
        states = {a.key: a.state for a in s.execute(select(Alert)).scalars()}
        assert states["host:disk"] == "firing"
        assert states["market:health:index_close.qp_current"] == "firing"
        apply_alerts(s, [], 0, only_resolve={"host:disk", "market:health:index_close.qp_current"})
        s.commit()
        states = {a.key: a.state for a in s.execute(select(Alert)).scalars()}
        assert states["host:disk"] == "resolved"
        assert states["market:health:index_close.qp_current"] == "firing"


# ---------------------------------------------------------------------------
# 5. Calendar/source fallbacks and qp-owned health checks
# ---------------------------------------------------------------------------
def _source_rows(s: Session, failed: set[str], at: dt.datetime | None = None) -> None:
    at = at or (NOW.astimezone(dt.UTC) - dt.timedelta(hours=1))
    for name in ("tencent", "sina", "akshare_sina_calendar", "eastmoney", "cninfo", "dolt",
                 "github_release", "baostock"):
        s.add(SourceStatus(source=name, ok=name not in failed, via_proxy=name != "cninfo", ms=1.0,
                           at=at, detail={}))


def test_calendar_falls_back_to_mirrored_calendar(engine, monkeypatch):
    """A Dolt outage must leave a usable, labelled qp calendar instead of an empty one."""
    from qp.jobs import market as m
    from qp.jobs import run_job
    from qp.market.http import FetchError

    with Session(engine) as s:
        _wipe(s)
        _open_days(s, "legacy", [dt.date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)])
        s.commit()
    monkeypatch.setattr(m.src, "dolt_calendar",
                        lambda a, b: (_ for _ in ()).throw(FetchError("http_504")))
    r = run_job(engine, "market.calendar", trigger="test")
    assert r.status == "succeeded", (r.error, r.result)
    assert r.result["source"] == "fallback" and r.result["dolt_error"] == "http_504"
    with Session(engine) as s:
        rows = s.execute(select(TradingDay).where(TradingDay.origin == "qp")).scalars().all()
        assert {x.date for x in rows} == {dt.date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)}
        assert {x.source for x in rows} == {"fallback:legacy"}
    # A later Dolt success replaces the fallback rows; the source is then Dolt.
    monkeypatch.setattr(m.src, "dolt_calendar",
                        lambda a, b: [(dt.date(2026, 11, 10), True), (dt.date(2026, 11, 11), True)])
    r2 = run_job(engine, "market.calendar", trigger="test")
    assert r2.status == "succeeded", (r2.error, r2.result)
    with Session(engine) as s:
        rows = s.execute(select(TradingDay).where(TradingDay.origin == "qp")).scalars().all()
        assert {x.date for x in rows} == {dt.date(2026, 11, 10), dt.date(2026, 11, 11)}
        assert {x.source for x in rows} == {m.DOLT_CAL_SOURCE}


def test_snapshot_qp_current_uses_qp_summary(engine):
    """Currency is judged on the qp summary; the frozen legacy core stays informational."""
    from qp.jobs.market import health

    with Session(engine) as s:
        _wipe(s)
        _open_days(s, "qp", [dt.date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)])
        s.add(MarketSummary(origin="qp", trade_date=dt.date(2026, 11, 10), data={"coverage": {"ratio": 1.0}},
                            generated_at=NOW.astimezone(dt.UTC)))
        s.add(MarketSummary(origin="legacy", trade_date=dt.date(2026, 11, 4), data={},
                            generated_at=NOW.astimezone(dt.UTC)))
        s.commit()
        rows = _by_check(health(s, now=NOW))
        st, summary, detail = rows["snapshot.qp_current"]
        assert st == "ok", (st, summary, detail)
        assert detail["latest"] == "2026-11-10" and detail["lag"] == 0
        assert detail["legacy_latest"] == "2026-11-04" and detail["legacy_lag"] == 4
        assert "legacy.data_health" not in rows and "snapshot.legacy_core_current" not in rows


def test_qlib_source_falls_back_to_release_tag(engine, monkeypatch):
    """DoltHub 504 is covered by the GitHub release tag, not reported as unknown."""
    from qp.jobs import market as m

    with Session(engine) as s:
        _wipe(s)
        _open_days(s, "qp", [dt.date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)])
        s.add(QlibStatus(kind="source", at=NOW.astimezone(dt.UTC) - dt.timedelta(hours=1),
                         data={"dolt_error": "http_504", "github_release_tag": "2026-11-10"}))
        s.commit()
        monkeypatch.setattr(m, "utcnow", lambda: NOW.astimezone(dt.UTC))
        st, summary, detail = _by_check(m.health(s, now=NOW))["qlib.source_dolt"]
        assert st == "ok", (st, summary, detail)
        assert detail["eod_source"] == "github_release"
        assert detail["expected"] == "2026-11-10" and detail["lag"] == 0
        assert "GitHub release" in summary and "http_504" in summary


def test_qlib_source_stale_check_is_unknown(engine, monkeypatch):
    """A source check older than 26h is not fresh evidence for upstream EOD."""
    from qp.jobs import market as m

    with Session(engine) as s:
        _wipe(s)
        _open_days(s, "qp", [dt.date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12)])
        s.add(QlibStatus(kind="source", at=NOW.astimezone(dt.UTC) - dt.timedelta(hours=27),
                         data={"github_release_tag": "2026-11-10"}))
        s.commit()
        monkeypatch.setattr(m, "utcnow", lambda: NOW.astimezone(dt.UTC))
        st, summary, detail = _by_check(m.health(s, now=NOW))["qlib.source_dolt"]
        assert st == "unknown", (st, summary, detail)
        assert "超过 26h" in summary and detail["age_hours"] > 26


def test_sources_reachable_uses_peer_fallback(engine, monkeypatch):
    """Tencent/Dolt failures are degraded, not blocking, when their peer is reachable."""
    from qp.jobs import market as m

    monkeypatch.setattr(m, "utcnow", lambda: NOW.astimezone(dt.UTC))
    health = m.health
    with Session(engine) as s:
        _wipe(s)
        _source_rows(s, {"tencent", "dolt"})
        s.commit()
        st, summary, detail = _by_check(health(s, now=NOW))["sources.reachable"]
        assert st == "ok", (st, summary, detail)
        assert detail["tencent"]["covered_by"] == ["sina"]
        assert detail["dolt"]["covered_by"] == ["github_release"]
        assert "降级" in summary and "tencent->sina" in summary and "dolt->github_release" in summary


def test_sources_reachable_marks_display_roles(engine, monkeypatch):
    """The dashboard needs to know which raw failure is on the active vs fallback path."""
    from qp.jobs import market as m

    monkeypatch.setattr(m, "utcnow", lambda: NOW.astimezone(dt.UTC))
    health = m.health
    with Session(engine) as s:
        _wipe(s)
        _source_rows(s, set())
        s.commit()
        st, summary, detail = _by_check(health(s, now=NOW))["sources.reachable"]
        assert st == "ok", (st, summary, detail)
        assert detail["tencent"]["role"] == "primary"
        assert detail["dolt"]["role"] == "primary"
        assert detail["eastmoney"]["role"] == "fallback"
        assert detail["sina"]["role"] == "fallback"
        assert detail["github_release"]["role"] == "fallback"
        assert detail["cninfo"]["role"] == "standalone"
        assert detail["akshare_sina_calendar"]["role"] == "optional"
        assert detail["baostock"]["role"] == "optional"


def test_sources_reachable_both_peers_fail_is_warning(engine, monkeypatch):
    """If neither path of a source pair is reachable, the check must stay warning."""
    from qp.jobs import market as m

    monkeypatch.setattr(m, "utcnow", lambda: NOW.astimezone(dt.UTC))
    health = m.health
    with Session(engine) as s:
        _wipe(s)
        _source_rows(s, {"tencent", "sina", "dolt", "github_release"})
        s.commit()
        st, summary, detail = _by_check(health(s, now=NOW))["sources.reachable"]
        assert st == "warning", (st, summary, detail)
        assert all(x in summary for x in ("tencent", "sina", "dolt", "github_release"))


def test_health_retires_replaced_checks(engine):
    """Old alert keys are resolved once when their checks are replaced."""
    from qp.jobs import run_job

    with Session(engine) as s:
        _wipe(s)
        _alert(s, "market:health:legacy.data_health")
        _alert(s, "market:health:snapshot.legacy_core_current")
        s.commit()
    r = run_job(engine, "market.health", trigger="test")
    assert r.status == "succeeded", (r.error, r.result)
    with Session(engine) as s:
        states = {a.key: a.state for a in s.execute(select(Alert)).scalars()}
        assert states["market:health:legacy.data_health"] == "resolved"
        assert states["market:health:snapshot.legacy_core_current"] == "resolved"
        checks = {h.check for h in s.execute(select(HealthResult)).scalars()}
        assert "snapshot.qp_current" in checks and "collector.market_reader" in checks
        assert "legacy.data_health" not in checks and "snapshot.legacy_core_current" not in checks
