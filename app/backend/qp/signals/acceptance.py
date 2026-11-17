"""M4 live acceptance: invariants that replace the legacy shadow comparison.

The legacy chain stopped on 2026-11-13, so "compare against legacy" can no longer
validate a live day.  These checks assert what a correct EOD day must satisfy on
its own: full universe, self-consistent counts, fresh inputs, matured outcomes
materialised, and the daily notification actually sent.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.models.market import IndexClose, TradingDay
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.signals import (
    DecisionOutcome,
    EodRun,
    RiskSnapshot,
    TargetDecision,
    TrendSignal,
)
from qp.signals import shadow as shadow_channel
from qp.signals.universe import TARGETS

HORIZONS = (1, 3, 5, 10, 20)
DUE_MIN_RATIO = 0.8
OUTCOME_LOOKBACK_SESSIONS = 30
INDEX_SYMBOLS_EXPECTED = 4
STRATEGY_WINDOW_SESSIONS = 20


def _check(cid: str, status: str, detail: str, **metrics: Any) -> dict[str, Any]:
    return {"id": cid, "status": status, "detail": detail, "metrics": metrics}


def _latest_run(session: Session, day: dt.date) -> EodRun | None:
    return session.scalar(select(EodRun).where(EodRun.decision_date == day)
                          .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc()).limit(1))


def _open_days(session: Session, today: dt.date) -> list[dt.date]:
    return [d for d in session.scalars(
        select(TradingDay.date).where(TradingDay.origin == "qp",
                                      TradingDay.is_open.is_(True),
                                      TradingDay.date <= today)
        .order_by(TradingDay.date)) if d <= today]


def _strategy_summary(session: Session, today: dt.date) -> dict[str, Any]:
    """Observation only: how often the strategy actually produces allowed entries.

    A month without an allowed decision makes the outcome checks vacuous, so the
    acceptance report carries the window counts and the last allowed/trigger dates.
    """
    open_days = _open_days(session, today)
    position = {d: i for i, d in enumerate(open_days)}
    window = open_days[-STRATEGY_WINDOW_SESSIONS:]
    counts: dict[str, int] = {}
    if window:
        for state, n in session.execute(
                select(TargetDecision.decision_state, func.count())
                .where(TargetDecision.decision_date.in_(window))
                .group_by(TargetDecision.decision_state)).all():
            counts[str(state)] = int(n)

    def elapsed(day: dt.date | None) -> int | None:
        if day is None or day not in position:
            return None
        return len(open_days) - 1 - position[day]

    last_allowed = session.scalar(
        select(func.max(TargetDecision.decision_date))
        .where(TargetDecision.decision_state == "allowed",
               TargetDecision.decision_date <= today))
    last_trigger = session.scalar(
        select(func.max(TargetDecision.decision_date))
        .where(TargetDecision.raw_trigger_count > 0,
               TargetDecision.decision_date <= today))
    return {"window_sessions": len(window),
            "window_start": window[0].isoformat() if window else None,
            "counts": counts,
            "last_allowed_date": last_allowed.isoformat() if last_allowed else None,
            "sessions_since_allowed": elapsed(last_allowed),
            "last_trigger_date": last_trigger.isoformat() if last_trigger else None,
            "sessions_since_trigger": elapsed(last_trigger),
            "shadow": shadow_channel.summary(session)}


def _outcome_backlog(session: Session, today: dt.date) -> dict[str, Any]:
    """Allowed decisions whose horizon has matured vs the outcomes materialised.

    Uses the qp trading calendar, so a Friday decision matured by Monday counts one
    session.  Limited to the last ``OUTCOME_LOOKBACK_SESSIONS`` open sessions: older
    holes are history, not an operational signal.
    """
    open_days = _open_days(session, today)
    position = {d: i for i, d in enumerate(open_days)}
    window_start = open_days[max(0, len(open_days) - OUTCOME_LOOKBACK_SESSIONS)] if open_days else None

    allowed: dict[dt.date, int] = {}
    if window_start is not None:
        allowed = dict(session.execute(
            select(TargetDecision.decision_date, func.count())
            .where(TargetDecision.decision_state == "allowed",
                   TargetDecision.decision_date >= window_start,
                   TargetDecision.decision_date <= today)
            .group_by(TargetDecision.decision_date)).all())

    materialised: dict[dt.date, dict[int, int]] = {}
    if window_start is not None:
        for day, horizon, count in session.execute(
                select(TargetDecision.decision_date, DecisionOutcome.horizon_sessions, func.count())
                .join(DecisionOutcome, DecisionOutcome.decision_id == TargetDecision.id)
                .where(TargetDecision.decision_state == "allowed",
                       TargetDecision.decision_date >= window_start,
                       TargetDecision.decision_date <= today)
                .group_by(TargetDecision.decision_date, DecisionOutcome.horizon_sessions)).all():
            materialised.setdefault(day, {})[int(horizon)] = int(count)

    expected = actual = 0
    detail: list[dict[str, Any]] = []
    for day, allowed_count in allowed.items():
        idx = position.get(day)
        if idx is None:
            continue
        elapsed = len(open_days) - 1 - idx
        due = [h for h in HORIZONS if h <= elapsed]
        day_expected = int(allowed_count) * len(due)
        day_actual = sum(materialised.get(day, {}).get(h, 0) for h in due)
        expected += day_expected
        actual += day_actual
        if day_expected != day_actual:
            detail.append({"date": day.isoformat(), "due": due,
                           "expected": day_expected, "actual": day_actual})
    return {"expected": expected, "actual": actual, "short_days": detail[:10],
            "lookback_sessions": OUTCOME_LOOKBACK_SESSIONS,
            "window_start": window_start.isoformat() if window_start else None}


def evaluate(session: Session, day: dt.date, *, today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    checks: list[dict[str, Any]] = []
    run = _latest_run(session, day)
    decisions = list(session.scalars(select(TargetDecision)
                                     .where(TargetDecision.decision_date == day)))
    by_state: dict[str, int] = {}
    for row in decisions:
        by_state[row.decision_state] = by_state.get(row.decision_state, 0) + 1
    triggered = [row for row in decisions if (row.raw_trigger_count or 0) > 0]

    # A1 — the day has a terminal, successful (or explicitly partial) run
    if run is None:
        checks.append(_check("A1_run", "FAIL", "no EOD run for this date"))
    else:
        ok = run.status in ("success", "partial") and run.finished_at is not None
        checks.append(_check("A1_run", "PASS" if ok else "FAIL",
                             f"run {run.run_id} attempt {run.attempt_no}: "
                             f"{run.status}/{run.decision_outcome}, finished={run.finished_at is not None}",
                             run_id=run.run_id, attempt=run.attempt_no, state=run.status))

    # A2 — every universe member has a decision row
    expected_codes = {t.code for t in TARGETS}
    codes = {row.code for row in decisions}
    missing = sorted(expected_codes - codes)
    extra = sorted(codes - expected_codes)
    target_count = run.target_count if run is not None else 0
    ok = (target_count == len(TARGETS) and len(decisions) == len(TARGETS)
          and not missing and not extra)
    checks.append(_check("A2_universe", "PASS" if ok else "FAIL",
                         f"target={target_count} decisions={len(decisions)} "
                         f"missing={len(missing)} extra={len(extra)}",
                         universe=len(TARGETS), missing=missing[:10], extra=extra[:10]))

    # A3 — run counts agree with the decision rows
    failed_rows = by_state.get("data_failed", 0)
    evaluated_rows = len([row for row in decisions if row.data_status == "ok"])
    if run is None:
        checks.append(_check("A3_counts", "FAIL", "no run"))
    else:
        ok = (run.failed_count == failed_rows
              and run.evaluated_count == evaluated_rows
              and run.signal_count == (run.allowed_signal_count + run.blocked_signal_count)
              and (run.signal_count == 0 or by_state.get("allowed", 0) + by_state.get("blocked", 0) > 0)
              and sum(by_state.values()) == len(decisions))
        checks.append(_check("A3_counts", "PASS" if ok else "FAIL",
                             f"failed={run.failed_count}/{failed_rows} "
                             f"evaluated={run.evaluated_count}/{evaluated_rows} "
                             f"signals={run.signal_count}="
                             f"{run.allowed_signal_count}+{run.blocked_signal_count}",
                             states=by_state))

    # A4 — data failures are only acceptable on a partial run
    if run is None:
        checks.append(_check("A4_data", "FAIL", "no run"))
    elif failed_rows == 0 and run.failed_count == 0:
        checks.append(_check("A4_data", "PASS", "no data failures", failed=0))
    elif run.status == "partial":
        checks.append(_check("A4_data", "WARN", f"partial run with {failed_rows} data failures",
                             failed=failed_rows))
    else:
        checks.append(_check("A4_data", "FAIL",
                             f"{failed_rows} data failures on a {run.status} run", failed=failed_rows))

    # A5 — risk gate snapshot exists and is interpretable
    if run is None:
        checks.append(_check("A5_risk", "FAIL", "no run"))
    else:
        risk = session.scalar(select(RiskSnapshot).where(RiskSnapshot.run_id == run.run_id)
                              .order_by(RiskSnapshot.id.desc()).limit(1))
        if risk is None:
            checks.append(_check("A5_risk", "WARN", "no risk snapshot"))
        elif risk.level == "unavailable":
            checks.append(_check("A5_risk", "WARN", "risk level unavailable"))
        else:
            checks.append(_check("A5_risk", "PASS", f"risk level {risk.level}", level=risk.level))

    # A6 — stored trend signals are consistent with the decisions that triggered
    signals = list(session.scalars(select(TrendSignal).where(TrendSignal.decision_date == day)))
    triggered_codes = {row.code for row in triggered if row.data_status == "ok"}
    orphans = sorted({s.code for s in signals} - triggered_codes)
    if run is None:
        checks.append(_check("A6_signals", "FAIL", "no run"))
    elif orphans:
        checks.append(_check("A6_signals", "FAIL",
                             f"{len(orphans)} codes have signals without a trigger", orphans=orphans[:10]))
    elif run.signal_count > 0 and not signals:
        checks.append(_check("A6_signals", "WARN",
                             f"{run.signal_count} planned signals but no rows stored "
                             "(fully deduplicated against earlier dates?)", signals=0))
    else:
        checks.append(_check("A6_signals", "PASS",
                             f"{len(signals)} stored signals, {len(triggered_codes)} triggering codes",
                             signals=len(signals), triggered=len(triggered_codes)))

    # A7 — matured allowed decisions have their outcome rows (global backlog)
    backlog = _outcome_backlog(session, today)
    if backlog["expected"] == 0:
        checks.append(_check("A7_outcomes", "PASS", "no matured horizon in the lookback window",
                             expected=0, actual=0))
    else:
        ratio = backlog["actual"] / backlog["expected"]
        status = "PASS" if ratio >= 1 else ("WARN" if ratio >= DUE_MIN_RATIO else "FAIL")
        checks.append(_check("A7_outcomes", status,
                             f"{backlog['actual']}/{backlog['expected']} due outcomes "
                             f"materialised ({ratio:.0%}, last {backlog['lookback_sessions']} sessions)",
                             expected=backlog["expected"], actual=backlog["actual"],
                             ratio=round(ratio, 4), short_days=backlog["short_days"]))

    # A8 — the daily notification for this date was actually sent
    outbox = session.scalar(select(Outbox)
                            .where(Outbox.dedup_key == f"watchdog:{day.isoformat()}:daily").limit(1))
    live_mode = get_settings().signals_notify_mode.strip().lower() == "live"
    if outbox is None:
        checks.append(_check("A8_notify", "WARN", "no daily watchdog notification row"))
    elif outbox.mode != "live" or not live_mode:
        checks.append(_check("A8_notify", "WARN", "signal notifications are shadow",
                             mode=outbox.mode, outbox_status=outbox.status))
    elif outbox.status == "sent":
        attempts = session.scalar(select(func.count()).select_from(DeliveryAttempt)
                                  .where(DeliveryAttempt.outbox_id == outbox.id,
                                         DeliveryAttempt.ok.is_(True))) or 0
        checks.append(_check("A8_notify", "PASS" if attempts else "WARN",
                             f"sent, {attempts} successful delivery attempt(s)",
                             mode=outbox.mode, attempts=attempts))
    else:
        checks.append(_check("A8_notify", "FAIL", f"daily notification {outbox.status}",
                             mode=outbox.mode, outbox_status=outbox.status))

    # A9 — one decision per code, one row per attempt number
    dup_codes = session.execute(select(TargetDecision.code, func.count())
                                .where(TargetDecision.decision_date == day)
                                .group_by(TargetDecision.code)
                                .having(func.count() > 1)).all()
    runs = list(session.scalars(select(EodRun).where(EodRun.decision_date == day)))
    attempts = [r.attempt_no for r in runs]
    ok = not dup_codes and len(set(attempts)) == len(attempts)
    checks.append(_check("A9_duplicates", "PASS" if ok else "FAIL",
                         f"runs={len(runs)} attempts={sorted(attempts)} "
                         f"duplicate_codes={len(dup_codes)}",
                         duplicate_codes=[c for c, _ in dup_codes][:10]))

    # A10 — the market inputs for the day exist
    index_rows = session.scalar(select(func.count(func.distinct(IndexClose.symbol)))
                                .where(IndexClose.origin == "qp",
                                       IndexClose.trade_date == day)) or 0
    open_day = session.scalar(select(func.count()).select_from(TradingDay).where(
        TradingDay.origin == "qp", TradingDay.date == day, TradingDay.is_open.is_(True))) or 0
    if not open_day:
        status = "FAIL"
    elif index_rows >= INDEX_SYMBOLS_EXPECTED:
        status = "PASS"
    else:
        # market.index_close runs at 16:30; an intraday check must not fail on that.
        status = "WARN" if day >= today else "FAIL"
    checks.append(_check("A10_inputs", status,
                         f"index_close symbols={index_rows} (>= {INDEX_SYMBOLS_EXPECTED}), "
                         f"open calendar day={open_day}",
                         index_symbols=index_rows, open_day=open_day))

    failed = [c["id"] for c in checks if c["status"] == "FAIL"]
    warned = [c["id"] for c in checks if c["status"] == "WARN"]
    verdict = "FAIL" if failed else ("WARN" if warned else "PASS")
    return {"date": day.isoformat(), "verdict": verdict, "failed": failed, "warned": warned,
            "checks": checks, "strategy": _strategy_summary(session, today),
            "generated_at": dt.datetime.now(dt.UTC).isoformat()}


def latest_decision_date(session: Session, on_or_before: dt.date) -> dt.date | None:
    return session.scalar(select(func.max(EodRun.decision_date))
                          .where(EodRun.decision_date <= on_or_before))
