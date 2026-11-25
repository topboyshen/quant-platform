"""C13 paper portfolio: 250-session momentum, Top8, every 20 open sessions.

Dedicated shadow tables only. Historical raw bars warm up indicators; they are
NOT prospective evidence. A forward account starts with fresh cash and its own
rebalance clock. Plans are persisted at the signal close (including the newest
close) and simulated at the *next* open, never regenerated from that day's close.

The previous exploratory backtest's split heuristic is used for ranking only.
Execution/positions always use raw prices and real share units. A new extreme
price discontinuity blocks the paper ledger rather than fabricating a zero return.
Both benchmarks are separately funded, costed paper portfolios: buy-and-hold
510300 and all-eligible equal weight rebalanced on the same 20-session clock.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.market.http import FetchError, fetch
from qp.market.kline import DailyBar, exchange_prefixed_code
from qp.models.market import TradingDay
from qp.models.signals import (
    RiskState,
    RotationShadowAccount,
    RotationShadowBar,
    RotationShadowDay,
    RotationShadowRun,
)
from qp.signals.eod import CST, now_cst
from qp.signals.risk import is_growth_risk_target
from qp.signals.universe import TARGETS

RULE = "qp-rotation.v1"
FORWARD_START = dt.date(2026, 11, 16)
START_DATE = dt.date(2015, 1, 5)  # exploratory history, never the forward start
BENCHMARK = "510300"
LOOKBACK = 250
TOP_K = 8
REBALANCE = 20
INITIAL = 1_000_000.0
COMMISSION = 0.0003
MIN_COMMISSION = 5.0
SLIPPAGE = 0.0005
LOT = 100
MIN_AMOUNT = 5_000_000.0
NAMES = {target.code: target.name for target in TARGETS}
SECTORS = {target.code: target.sector for target in TARGETS}
CODES = tuple(NAMES)
GROWTH_CODES = {code for code in CODES if is_growth_risk_target(SECTORS.get(code), NAMES.get(code))}
NO_NEW_RISK = frozenset({"critical", "warning", "unavailable"})
# Existing freshness parameter. Calendar days, not trading days, and not extended
# across weekends or holidays. Anchored to the plan decision cutoff, not the
# execution-day end.
RISK_RULE_VERSION = "qp-rotation-risk.v1"
# Repo timers, not an assumed 15:00 close. EOD writes RiskState.observed_at from its
# own clock. The rotation job freezes the plan later the same afternoon.
SCHEDULE_CONTRACT = {
    "risk_observation": "signals.eod Mon-Fri 15:30 CST, retry 15:48; observed_at is that job clock",
    "plan_freeze": "signals.rotation_shadow Mon-Fri 15:50 and 16:40 CST; decided_at is now_cst() at plan insert",
    "execution": "next open session, using only the risk binding stored when the plan was frozen",
}
VISIBILITY_LIMITATION = (
    "created_at can be backdated on insert and is not an independent publication receipt. "
    "Matching created_at<=decided_at does not strictly prove the observation was known then. "
    "Historical plans without a stored decided_at are unverified; their clocks are not reconstructed."
)


class DataIssue(ValueError):
    """Safe evidence failure, not a reason to advance the portfolio."""


@dataclass
class Frame:
    bars: list[DailyBar]  # raw tradable prices, never synthetic share units
    idx: dict[dt.date, int]
    closes: list[float]  # causally split-normalized prices for ranking only
    amount20: list[float | None]  # actual CNY turnover, raw volume in board lots


def parameters(codes: Sequence[str] = CODES) -> dict[str, Any]:
    return {
        "lookback": LOOKBACK,
        "top_k": TOP_K,
        "rebalance": REBALANCE,
        "initial": INITIAL,
        "commission_rate": COMMISSION,
        "minimum_commission": MIN_COMMISSION,
        "slippage_rate": SLIPPAGE,
        "minimum_amount": MIN_AMOUNT,
        "lot": LOT,
        "universe_codes": list(codes),
        "ranking": "causal_split_normalized_price_return",
        "ranking_split_thresholds": [-0.25, 0.40],
        "dividends": "excluded_price_only",
        "clock": "first_execution_then_20_sessions",
        "ew": "all_eligible_20_session_equal_weight_same_costs",
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _lock(session: Session, rule: str, phase: str) -> None:
    key = zlib.crc32(f"{rule}:{phase}".encode()) & 0x7FFFFFFF
    session.execute(text("select pg_advisory_xact_lock(20818, :key)"), {"key": key})


def _normalise(bars: Sequence[DailyBar], until: dt.date | None = None) -> list[DailyBar]:
    unique: dict[dt.date, DailyBar] = {}
    for bar in bars:
        if until is not None and bar.date > until:
            continue
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if (
            not all(math.isfinite(float(v)) for v in values)
            or min(values[:4]) <= 0
            or bar.volume < 0
            or bar.high < bar.low
        ):
            raise DataIssue("invalid_raw_bar")
        if bar.date in unique and unique[bar.date] != bar:
            raise DataIssue("conflicting_duplicate_bar")
        unique[bar.date] = bar
    return [unique[day] for day in sorted(unique)]


def repair_bars(bars: Sequence[DailyBar]) -> list[DailyBar]:
    """Causal split heuristic for *ranking*, never for account P&L/turnover."""
    ordered = _normalise(bars)
    if not ordered:
        return []
    out = [ordered[0]]
    factor = 1.0
    for previous, bar in zip(ordered, ordered[1:], strict=False):
        change = bar.close / previous.close - 1
        if change < -0.25 or change > 0.40:
            factor *= previous.close / bar.close
        out.append(
            DailyBar(
                bar.date,
                bar.open * factor,
                bar.high * factor,
                bar.low * factor,
                bar.close * factor,
                bar.volume / factor,
            )
        )
    return out


def _frame(bars: Sequence[DailyBar]) -> Frame:
    raw = _normalise(bars)
    closes = [bar.close for bar in repair_bars(raw)]
    amount: list[float | None] = [None] * len(raw)
    total = 0.0
    for i, bar in enumerate(raw):
        total += bar.volume * bar.close * 100
        if i >= 20:
            total -= raw[i - 20].volume * raw[i - 20].close * 100
        if i >= 19:
            amount[i] = total / 20
    return Frame(raw, {b.date: i for i, b in enumerate(raw)}, closes, amount)


def seed_bars(session: Session, payload: Any, *, source: str = "tencent_day_seed") -> dict[str, Any]:
    """Append raw frames.json bars. Never silently overwrite ledger input history."""
    frames = payload.get("frames", payload) if isinstance(payload, dict) else None
    if not isinstance(frames, dict):
        raise DataIssue("invalid_seed_payload")
    count = 0
    codes = 0
    frozen_through = session.scalar(select(func.max(RotationShadowDay.trade_date)))
    for code, rows in frames.items():
        if code not in CODES:
            continue
        bars = _normalise(
            [
                DailyBar(
                    dt.date.fromisoformat(str(r[0])[:10]),
                    float(r[1]),
                    float(r[3]),
                    float(r[4]),
                    float(r[2]),
                    float(r[5]),
                )
                for r in rows
            ]
        )
        if not bars:
            continue
        existing = {
            r.trade_date: tuple(float(v) for v in r[1:])
            for r in session.execute(
                select(
                    RotationShadowBar.trade_date,
                    RotationShadowBar.open,
                    RotationShadowBar.high,
                    RotationShadowBar.low,
                    RotationShadowBar.close,
                    RotationShadowBar.volume,
                ).where(RotationShadowBar.code == code)
            )
        }
        new = []
        for bar in bars:
            values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            previous = existing.get(bar.date)
            if previous is not None:
                if any(abs(a - b) > 0.00011 for a, b in zip(values, previous, strict=False)):
                    raise DataIssue(f"raw_history_revision:{code}:{bar.date}")
                continue
            if frozen_through is not None and bar.date <= frozen_through:
                raise DataIssue(f"raw_history_extension_after_freeze:{code}:{bar.date}")
            new.append(
                {
                    "code": code,
                    "trade_date": bar.date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "source": source,
                }
            )
        for i in range(0, len(new), 1000):
            stmt = pg_insert(RotationShadowBar).values(new[i : i + 1000])
            session.execute(stmt.on_conflict_do_nothing(index_elements=["code", "trade_date"]))
            count += len(new[i : i + 1000])
        codes += 1
    session.flush()
    return {"ok": True, "codes": codes, "bars": count}


def _tencent_raw(code: str, days: int = 320) -> list[DailyBar]:
    symbol = exchange_prefixed_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},day"
    payload = fetch(url, proxy=False, timeout=15, headers={"Referer": "https://gu.qq.com/"}).json()
    if payload.get("code") != 0:
        raise DataIssue("tencent_bad_code")
    # qfqday is deliberately NOT a fallback: cannot mix adjusted and raw history.
    rows = (payload.get("data", {}).get(symbol) or {}).get("day")
    if not rows:
        raise DataIssue("tencent_raw_missing")
    return _normalise(
        [
            DailyBar(
                dt.date.fromisoformat(str(r[0])[:10]), float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5])
            )
            for r in rows
        ]
    )


def _sina_raw(code: str) -> list[DailyBar]:
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    rows = fetch(
        url,
        params={"symbol": exchange_prefixed_code(code), "scale": 240, "ma": "no", "datalen": 320},
        proxy=False,
        timeout=15,
        headers={"Referer": "https://finance.sina.com.cn/"},
    ).json()
    if not isinstance(rows, list) or not rows:
        raise DataIssue("sina_raw_missing")
    # Sina reports individual shares, Tencent reports lots. Persist Tencent units.
    return _normalise(
        [
            DailyBar(
                dt.date.fromisoformat(r["day"]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["volume"]) / 100,
            )
            for r in rows
        ]
    )


def sync_bars(
    session: Session, *, as_of: dt.date | None = None, codes: Sequence[str] = CODES, fetcher: Any | None = None
) -> dict[str, Any]:
    """Append raw recent data, detecting revision and publication lag explicitly."""
    as_of = as_of or now_cst().date()
    failed: dict[str, str] = {}
    updated = 0
    sources: dict[str, str] = {}
    for code in codes:
        source = "tencent_day"
        try:
            if fetcher is not None:
                bars = _normalise(list(fetcher.get(code).bars), as_of)
            else:
                try:
                    bars = _normalise(_tencent_raw(code), as_of)
                    if not bars or bars[-1].date < as_of:
                        raise DataIssue("tencent_publication_lag")
                except (DataIssue, FetchError, ValueError, KeyError, TypeError):
                    bars = _normalise(_sina_raw(code), as_of)
                    source = "sina_day"
            if not bars or bars[-1].date != as_of:
                raise DataIssue("publication_lag")
            # Cross-vendor old rows may differ slightly. Verify recent overlap,
            # append *only new dates*, never revise warm-up or priced ledger inputs.
            latest = session.scalar(
                select(func.max(RotationShadowBar.trade_date)).where(RotationShadowBar.code == code)
            )
            if latest is not None:
                overlap = next((b for b in bars if b.date == latest), None)
                old = session.get(RotationShadowBar, (code, latest))
                if overlap is not None and old is not None:
                    for field in ("open", "high", "low", "close"):
                        if abs(getattr(overlap, field) - float(getattr(old, field))) > 0.0021:
                            raise DataIssue("overlap_price_revision")
                stored_dates = set(
                    session.scalars(
                        select(RotationShadowBar.trade_date).where(
                            RotationShadowBar.code == code, RotationShadowBar.trade_date >= bars[0].date
                        )
                    )
                )
                # Publication gaps older than max(date) must be recoverable too.
                bars = [b for b in bars if b.date not in stored_dates]
            if bars:
                payload = {code: [[b.date.isoformat(), b.open, b.close, b.high, b.low, b.volume] for b in bars]}
                updated += seed_bars(session, payload, source=source)["bars"]
            sources[code] = source
        except (DataIssue, FetchError, ValueError, KeyError, TypeError) as exc:
            failed[code] = str(exc) if isinstance(exc, (DataIssue, FetchError)) else type(exc).__name__
    return {
        "ok": not failed,
        "codes": len(codes),
        "bars": updated,
        "failed": failed,
        "sources": sources,
        "as_of": as_of.isoformat(),
    }


def _load_frames(session: Session, *, until: dt.date, codes: Sequence[str]) -> dict[str, Frame]:
    grouped: dict[str, list[DailyBar]] = {}
    stmt = (
        select(
            RotationShadowBar.code,
            RotationShadowBar.trade_date,
            RotationShadowBar.open,
            RotationShadowBar.high,
            RotationShadowBar.low,
            RotationShadowBar.close,
            RotationShadowBar.volume,
        )
        .where(RotationShadowBar.trade_date <= until, RotationShadowBar.code.in_(codes))
        .order_by(RotationShadowBar.code, RotationShadowBar.trade_date)
        .execution_options(yield_per=1000)
    )
    for code, day, o, h, lo, cl, volume in session.execute(stmt):
        grouped.setdefault(code, []).append(DailyBar(day, float(o), float(h), float(lo), float(cl), float(volume)))
    return {code: _frame(bars) for code, bars in grouped.items()}


def _selection(
    frames: dict[str, Frame], day: dt.date, *, lookback: int = LOOKBACK, top: int = TOP_K
) -> tuple[list[dict[str, Any]], int]:
    ranked = []
    for code, frame in frames.items():
        j = frame.idx.get(day)
        if j is None or j < lookback or (frame.amount20[j] or 0) < MIN_AMOUNT:
            continue
        ranked.append((frame.closes[j] / frame.closes[j - lookback] - 1, code))
    ranked.sort(reverse=True)
    return (
        [
            {"code": code, "name": NAMES.get(code, code), "score": round(score, 8), "weight": 1 / top}
            for score, code in ranked[:top]
        ],
        len(ranked),
    )


def _ew_weights(frames: dict[str, Frame], day: dt.date) -> dict[str, float]:
    codes = [c for c, f in frames.items() if day in f.idx and (f.amount20[f.idx[day]] or 0) >= MIN_AMOUNT]
    return {c: 1 / len(codes) for c in sorted(codes)} if codes else {}


def _open_days(session: Session, start: dt.date, end: dt.date) -> list[dt.date]:
    return list(
        session.scalars(
            select(TradingDay.date)
            .where(
                TradingDay.origin == "qp",
                TradingDay.is_open.is_(True),
                TradingDay.date >= start,
                TradingDay.date <= end,
            )
            .order_by(TradingDay.date)
        )
    )


def _next_open(session: Session, day: dt.date) -> dt.date | None:
    return session.scalar(
        select(func.min(TradingDay.date)).where(
            TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date > day
        )
    )


def _fresh(frames: dict[str, Frame], codes: Sequence[str], day: dt.date) -> list[str]:
    return [code for code in codes if code not in frames or day not in frames[code].idx]


def _closed_session_error(day: dt.date, phase: str) -> str | None:
    if phase != "forward":
        return None
    stamp = now_cst()
    if day > stamp.date():
        return "future_forward_session"
    if day == stamp.date() and (stamp.hour, stamp.minute) < (15, 5):
        return "session_not_closed"
    return None


def _history_check(session: Session, frames: dict[str, Frame], day: dt.date, codes: Sequence[str]) -> list[str]:
    """A shared calendar defines lookback sessions; no silent row-count stretching."""
    anchor_days = list(
        session.scalars(
            select(TradingDay.date)
            .where(TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date <= day)
            .order_by(TradingDay.date.desc())
            .limit(LOOKBACK + 1)
        )
    )
    if len(anchor_days) < LOOKBACK + 1:
        return ["calendar_warmup_incomplete"]
    start = anchor_days[-1]
    calendar = dict(
        session.execute(
            select(TradingDay.date, TradingDay.is_open).where(
                TradingDay.origin == "qp", TradingDay.date >= start, TradingDay.date <= day
            )
        ).all()
    )
    missing = []
    for code in codes:
        f = frames.get(code)
        if f is None:
            missing.append(code)
            continue
        if any(start <= b.date <= day and calendar.get(b.date) is not True for b in f.bars):
            missing.append(code)
            continue
        # Do not penalize a genuine new listing for dates before its first bar.
        first = f.bars[0].date
        expected = [d for d in anchor_days if d >= first]
        if any(d not in f.idx for d in expected):
            missing.append(code)
    return missing


def _calendar_holes(session: Session, frames: dict[str, Frame], first: dt.date, last: dt.date) -> list[str]:
    states = dict(
        session.execute(
            select(TradingDay.date, TradingDay.is_open).where(
                TradingDay.origin == "qp", TradingDay.date >= first, TradingDay.date <= last
            )
        ).all()
    )
    observed = {b.date for frame in frames.values() for b in frame.bars if first <= b.date <= last}
    cursor = first
    unknown = []
    while cursor <= last:
        # Calendar completeness is explicit, including weekends/holidays. A date
        # absent from every price source is still not silently a closed session.
        if cursor not in states or (cursor in observed and states[cursor] is not True):
            unknown.append(str(cursor))
        cursor += dt.timedelta(days=1)
    return unknown


def _input_digest(frames: dict[str, Frame], day: dt.date) -> str:
    payload = []
    for code, frame in sorted(frames.items()):
        j = frame.idx.get(day)
        if j is None:
            continue
        payload.append(
            [
                code,
                [
                    [b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume]
                    for b in frame.bars[max(0, j - LOOKBACK) : j + 1]
                ],
            ]
        )
    return _digest(payload)


def _binding_record(selection: dict[str, Any]) -> dict[str, Any]:
    observed = selection.get("observed_at")
    created = selection.get("created_at")
    decided = selection.get("decided_at")
    return {
        "status": selection["status"],
        "level": selection.get("level"),
        "observation_id": selection.get("observation_id"),
        "observed_at": None if observed is None else observed.isoformat(),
        "created_at": None if created is None else created.isoformat(),
        "decided_at": None if decided is None else decided.isoformat(),
        "used_previous_ok": False,
    }


def _plan(
    session: Session, account: RotationShadowAccount, frames: dict[str, Frame], day: dt.date, execute: dt.date | None
) -> RotationShadowRun:
    selected, universe = _selection(frames, day)
    decided_at = now_cst()
    binding = _bind_for_new_plan(session, day, decided_at)
    detail = {
        "parameters_sha256": _digest(account.parameters),
        "input_sha256": _input_digest(frames, day),
        "ew_weights": _ew_weights(frames, day),
        "risk_rule_version": RISK_RULE_VERSION,
        "risk_observation_id": binding.get("observation_id"),
        "risk_binding": _binding_record(binding),
        "schedule": SCHEDULE_CONTRACT,
        "visibility_limitation": VISIBILITY_LIMITATION,
    }
    if binding["status"] != "unverified":
        detail["decided_at"] = decided_at.isoformat()
    row = RotationShadowRun(
        rule_version=account.rule_version,
        phase=account.phase,
        signal_date=day,
        execute_date=execute,
        status="pending",
        universe=universe,
        top_k=TOP_K,
        selected=selected,
        detail=detail,
    )
    session.add(row)
    session.flush()
    return row


def initialize(
    session: Session,
    *,
    start_date: dt.date = FORWARD_START,
    phase: str = "forward",
    rule_version: str = RULE,
    codes: Sequence[str] = CODES,
) -> dict[str, Any]:
    """Register a fresh-money account and persist its initial, next-open plan."""
    if phase not in ("forward", "replay"):
        raise ValueError("bad_phase")
    if phase == "forward" and start_date < now_cst().date():
        return {"ok": False, "error": "cannot_backdate_forward_start"}
    if BENCHMARK not in codes or len(set(codes)) != len(codes) or any(c not in CODES for c in codes):
        return {"ok": False, "error": "invalid_registered_universe"}
    _lock(session, rule_version, phase)
    old = session.get(RotationShadowAccount, (rule_version, phase))
    if old is not None:
        return {"ok": True, "skipped": "already_initialized", "start": str(old.start_date)}
    if phase == "forward":
        stamp = now_cst()
        if stamp.date() >= start_date and (stamp.hour, stamp.minute) >= (9, 30):
            return {"ok": False, "error": "forward_registration_after_open"}
    baseline = session.scalar(
        select(func.max(TradingDay.date)).where(
            TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date < start_date
        )
    )
    if baseline is None:
        return {"ok": False, "error": "baseline_calendar_missing"}
    first = _next_open(session, baseline)
    if first != start_date:
        return {"ok": False, "error": "start_is_not_next_open_session"}
    closed_error = _closed_session_error(baseline, phase)
    if closed_error:
        return {"ok": False, "error": closed_error}
    frames = _load_frames(session, until=baseline, codes=codes)
    missing = _fresh(frames, codes, baseline)
    if missing:
        return {"ok": False, "error": "baseline_bars_missing", "missing_codes": missing}
    history_missing = _history_check(session, frames, baseline, codes)
    if history_missing:
        return {"ok": False, "error": "ranking_warmup_session_gap", "missing_codes": history_missing}
    selected, universe = _selection(frames, baseline)
    if universe < TOP_K:
        return {"ok": False, "error": "insufficient_warmup_universe", "eligible": universe}
    params = parameters(codes)
    account = RotationShadowAccount(
        rule_version=rule_version,
        phase=phase,
        start_date=start_date,
        baseline_date=baseline,
        parameters=params,
        quality={"status": "ready", "message": "基线已锁定，等待前向交易日"},
    )
    session.add(account)
    cash_state = {"cash": INITIAL, "shares": {}}
    session.add(
        RotationShadowDay(
            rule_version=rule_version,
            phase=phase,
            trade_date=baseline,
            session_index=-1,
            nav=INITIAL,
            cash=INITIAL,
            invested=0,
            positions={},
            benchmark_nav=INITIAL,
            ew_nav=INITIAL,
            exposure=0,
            detail={
                "baseline": True,
                "state": {"benchmark": dict(cash_state), "ew": dict(cash_state)},
                "marks": {},
                "input_sha256": _input_digest(frames, baseline),
            },
        )
    )
    session.flush()
    plan = _plan(session, account, frames, baseline, first)
    return {
        "ok": True,
        "phase": phase,
        "baseline": str(baseline),
        "start": str(start_date),
        "plan_id": plan.id,
        "eligible": universe,
        "selected": selected,
        "parameters_sha256": _digest(params),
    }


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CST)
    return value.astimezone(CST)


def bind_same_day_risk(rows: Sequence[Any], signal_date: dt.date, decided_at: dt.datetime) -> dict[str, Any]:
    """Bind only a same-day assessment that already existed at decided_at.

    Yesterday's ok is not a substitute when today's assessment is absent. A row
    observed or inserted after decided_at cannot be bound. This does not claim
    that created_at proves the observation was known; see VISIBILITY_LIMITATION.
    """
    decided = _aware(decided_at)
    visible: list[tuple[dt.datetime, Any]] = []
    older_exists = False
    for row in rows:
        observed = _aware(row.observed_at)
        created = _aware(row.created_at)
        if observed <= decided and created <= decided:
            older_exists = True
            if observed.astimezone(CST).date() == signal_date:
                visible.append((observed, row))
    if not visible:
        return {
            "status": "assessment_pending" if older_exists else "missing",
            "level": None,
            "observation_id": None,
            "observed_at": None,
            "created_at": None,
            "decided_at": decided,
            "used_previous_ok": False,
        }
    observed, row = max(visible, key=lambda item: item[0])
    return {
        "status": "fresh",
        "level": str(row.level),
        "observation_id": row.id,
        "observed_at": observed,
        "created_at": _aware(row.created_at),
        "decided_at": decided,
        "used_previous_ok": False,
    }


def _unverified_selection() -> dict[str, Any]:
    return {
        "status": "unverified",
        "level": None,
        "observation_id": None,
        "observed_at": None,
        "created_at": None,
        "decided_at": None,
        "used_previous_ok": False,
    }


def _bind_for_new_plan(session: Session, signal_date: dt.date, decided_at: dt.datetime) -> dict[str, Any]:
    if _aware(decided_at).astimezone(CST).date() != signal_date:
        return _unverified_selection()
    rows = session.scalars(select(RiskState)).all()
    return bind_same_day_risk(rows, signal_date, decided_at)


def _selection_from_plan(plan: RotationShadowRun) -> dict[str, Any]:
    """Use the binding frozen with the plan. Do not invent a clock or re-query."""
    detail = plan.detail or {}
    binding = detail.get("risk_binding")
    if not detail.get("decided_at") or not isinstance(binding, dict):
        return _unverified_selection()
    return {
        "status": binding.get("status") or "unverified",
        "level": binding.get("level"),
        "observation_id": binding.get("observation_id"),
        "observed_at": None,
        "created_at": None,
        "decided_at": _aware(dt.datetime.fromisoformat(detail["decided_at"])),
        "used_previous_ok": False,
    }


def _increase_blocked(selection: dict[str, Any], codes: set[str]) -> set[str]:
    """Codes that must not be opened or increased. Reductions stay allowed."""
    if selection["status"] in {"missing", "stale", "assessment_pending", "unverified"}:
        return set(codes)
    level = selection.get("level")
    if level == "ok":
        return set()
    if level == "growth_blocked":
        return {code for code in codes if code in GROWTH_CODES}
    return set(codes)


def _risk_gate_record(selection: dict[str, Any], blocked: set[str]) -> dict[str, Any]:
    status = selection["status"]
    level = selection.get("level")
    if status in {"missing", "stale", "assessment_pending", "unverified"}:
        exposure = "blocked"
    elif level == "growth_blocked":
        exposure = "growth_capped"
    elif level == "ok":
        exposure = "open"
    else:
        exposure = "blocked"
    observed = selection.get("observed_at")
    created = selection.get("created_at")
    decided = selection.get("decided_at")
    return {
        "rule_version": RISK_RULE_VERSION,
        "level": level,
        "reason": status,
        "new_exposure": exposure,
        "increase_blocked": sorted(blocked),
        "forced_liquidation": False,
        "benchmarks_ungated": True,
        "benchmark_policy": "ungated_control",
        "ew_policy": "ungated_control",
        "decided_at": None if decided is None else decided.isoformat(),
        "observation_id": selection.get("observation_id"),
        "observation_observed_at": None if observed is None else observed.isoformat(),
        "observation_created_at": None if created is None else created.isoformat(),
        "schedule": SCHEDULE_CONTRACT,
        "same_day_assessment_required": True,
        "previous_ok_is_substitute": False,
        "visibility": "same-day observed_at<=decided_at and created_at<=decided_at",
        "visibility_limitation": VISIBILITY_LIMITATION,
        "effective_boundary": "new plans and day rows only; existing nav and historical plans are not rewritten",
        "nav_rewritten": False,
    }


def _cap_increases(desired: dict[str, int], shares: dict[str, int], blocked: set[str]) -> dict[str, int]:
    capped = dict(desired)
    for code in blocked:
        capped[code] = min(capped.get(code, 0), shares.get(code, 0))
        if capped[code] <= 0:
            capped.pop(code, None)
    return capped


def _trade(
    cash: float,
    shares: dict[str, int],
    target: dict[str, float],
    bars: dict[str, DailyBar],
    *,
    increase_blocked: set[str] | None = None,
) -> tuple[float, list[dict[str, Any]], float, float]:
    """Real raw-price lots; sell every reduction before any buy, both sides costed."""
    needed = set(shares) | set(target)
    if needed - bars.keys():
        raise DataIssue("execution_bar_missing")
    if any(bars[c].volume <= 0 for c in needed):
        raise DataIssue("execution_volume_zero")
    value_open = cash + sum(qty * bars[code].open for code, qty in shares.items())
    budget = value_open * (1 - 2 * (COMMISSION + SLIPPAGE))
    desired = {
        code: math.floor(budget * weight / (bars[code].open * (1 + SLIPPAGE)) / LOT) * LOT
        for code, weight in target.items()
        if weight > 0
    }
    if increase_blocked:
        desired = _cap_increases(desired, shares, increase_blocked)
    traded = 0.0
    fills = []
    for side in ("sell", "buy"):
        for code in sorted(needed):
            difference = desired.get(code, 0) - shares.get(code, 0)
            if (side == "sell" and difference >= 0) or (side == "buy" and difference <= 0):
                continue
            qty = abs(difference)
            price = bars[code].open * (1 - SLIPPAGE if side == "sell" else 1 + SLIPPAGE)
            if side == "buy":
                affordable = math.floor(max(0, cash - MIN_COMMISSION) / (price * (1 + COMMISSION)) / LOT) * LOT
                qty = min(qty, affordable)
            if qty <= 0:
                continue
            amount = qty * price
            fee = max(amount * COMMISSION, MIN_COMMISSION)
            cash = round(cash + (amount - fee if side == "sell" else -amount - fee), 4)
            shares[code] = shares.get(code, 0) + (qty if side == "buy" else -qty)
            if not shares[code]:
                del shares[code]
            traded += amount
            fills.append({"code": code, "side": side, "qty": qty, "price": round(price, 8), "fee": round(fee, 4)})
    if cash < -0.0001 or any(q < 0 or q % LOT for q in shares.values()):
        raise DataIssue("account_invariant_failed")
    return cash, fills, traded, value_open


def _mark(cash: float, shares: dict[str, int], bars: dict[str, DailyBar]) -> float:
    if set(shares) - bars.keys():
        raise DataIssue("mark_bar_missing")
    return round(cash + sum(qty * bars[code].close for code, qty in shares.items()), 4)


def _quality(account: RotationShadowAccount, day: dt.date, error: str, **details: Any) -> dict[str, Any]:
    account.quality = {"status": "blocked", "message": error, "stale_date": str(day), **details}
    return {"ok": False, "error": error, "date": str(day), **details}


def replay(
    session: Session, *, until: dt.date | None = None, phase: str = "forward", rule_version: str = RULE
) -> dict[str, Any]:
    """Advance a registered account; batch and incremental calls are identical."""
    until = until or now_cst().date()
    closed_error = _closed_session_error(until, phase)
    if closed_error:
        return {"ok": False, "error": closed_error}
    _lock(session, rule_version, phase)
    account = session.get(RotationShadowAccount, (rule_version, phase))
    if account is None:
        return {"ok": False, "error": "not_initialized", "phase": phase}
    codes = tuple(account.parameters["universe_codes"])
    if account.parameters != parameters(codes):
        return _quality(account, until, "registered_parameters_changed")
    last = session.scalar(
        select(RotationShadowDay)
        .where(RotationShadowDay.rule_version == rule_version, RotationShadowDay.phase == phase)
        .order_by(RotationShadowDay.trade_date.desc())
        .limit(1)
    )
    if last is None:
        return _quality(account, until, "baseline_state_missing")
    days = _open_days(session, last.trade_date + dt.timedelta(days=1), until)
    calendar_state = session.scalar(
        select(TradingDay.is_open).where(TradingDay.origin == "qp", TradingDay.date == until)
    )
    if calendar_state is None and until > last.trade_date:
        return _quality(account, until, "calendar_day_unknown")
    if not days:
        return {"ok": True, "skipped": "up_to_date", "last": str(last.trade_date), "days": 0}
    frames = _load_frames(session, until=until, codes=codes)
    calendar_gaps = _calendar_holes(session, frames, last.trade_date + dt.timedelta(days=1), until)
    if calendar_gaps:
        return _quality(account, until, "交易日历缺口，暂停时钟推进", missing_dates=calendar_gaps)
    cash = float(last.cash)
    shares = {c: int(q) for c, q in last.positions.items()}
    state = last.detail["state"]
    benchmark = {"cash": float(state["benchmark"]["cash"]), "shares": dict(state["benchmark"]["shares"])}
    ew = {"cash": float(state["ew"]["cash"]), "shares": dict(state["ew"]["shares"])}
    plans = {
        row.signal_date: row
        for row in session.scalars(
            select(RotationShadowRun)
            .where(
                RotationShadowRun.rule_version == rule_version,
                RotationShadowRun.phase == phase,
                RotationShadowRun.status == "pending",
            )
            .order_by(RotationShadowRun.signal_date)
        )
    }
    count = executed = 0
    index = int(last.session_index)
    for day in days:
        missing = _fresh(frames, codes, day)
        if missing:
            return {**_quality(account, day, "当日行情未齐，净值与调仓均未推进", missing_codes=missing), "days": count}
        bars = {code: f.bars[f.idx[day]] for code, f in frames.items()}
        jumps = []
        for code, f in frames.items():
            j = f.idx[day]
            if j:
                gap = bars[code].close / f.bars[j - 1].close - 1
                if gap < -0.25 or gap > 0.40:
                    jumps.append(code)
        if jumps:
            return {
                **_quality(account, day, "价格大幅跳变，需核验份额折算/数据后恢复", missing_codes=jumps),
                "days": count,
            }
        if (index + 2) % REBALANCE == 0:
            missing_history = _history_check(session, frames, day, codes)
            if missing_history:
                return _quality(account, day, "ranking_warmup_session_gap", missing_codes=missing_history)
        if index < 0 or (index + 1) % REBALANCE == 0:
            if not plans:
                return _quality(account, day, "persisted_rebalance_plan_missing")
        due = [p for p in plans.values() if p.signal_date < day and (p.execute_date is None or p.execute_date == day)]
        if len(due) > 1 or any(p.execute_date is not None and p.execute_date < day for p in plans.values()):
            return _quality(account, day, "pending_plan_clock_conflict")
        plan = due[0] if due else None
        selection: dict[str, Any] | None = None
        increase_blocked: set[str] = set()
        if plan is not None:
            # Only infer next date for a plan persisted before the calendar horizon.
            if _next_open(session, plan.signal_date) != day:
                return _quality(account, day, "execution_is_not_next_open")
            if plan.detail.get("parameters_sha256") != _digest(account.parameters):
                return _quality(account, day, "plan_parameters_changed")
            target = {s["code"]: float(s["weight"]) for s in plan.selected}
            selection = _selection_from_plan(plan)
            increase_blocked = _increase_blocked(selection, set(shares) | set(target))
            if selection["status"] != "unverified":
                plan.detail = {**plan.detail, "risk_gate": _risk_gate_record(selection, increase_blocked)}
            # Daily OHLC lock is an explicitly conservative fill approximation.
            for code in (
                set(shares)
                | set(target)
                | set(ew["shares"])
                | set(plan.detail["ew_weights"])
                | set(benchmark["shares"])
                | {BENCHMARK}
            ):
                f = frames[code]
                j = f.idx[day]
                if bars[code].volume <= 0:
                    return _quality(account, day, "零成交量无法确认成交，纸面账本暂停", missing_codes=[code])
                if j and bars[code].high - bars[code].low <= 0.0001:
                    change = bars[code].open / f.bars[j - 1].close - 1
                    if abs(change) >= 0.095:
                        return _quality(account, day, "涨跌停无法确认成交，纸面账本暂停", missing_codes=[code])
            cash, fills, traded, value_open = _trade(cash, shares, target, bars, increase_blocked=increase_blocked)
            plan.status = "executed"
            plan.execute_date = day
            plan.fills = fills
            plan.nav_before = round(value_open, 4)
            plan.turnover = round(traded / value_open, 4) if value_open > 0 else 0
            ew["cash"], ew_fills, _, _ = _trade(ew["cash"], ew["shares"], dict(plan.detail["ew_weights"]), bars)
            bench_fills = []
            if index < 0:  # buy-and-hold, not silently rebalanced every 20 days
                benchmark["cash"], bench_fills, _, _ = _trade(
                    benchmark["cash"], benchmark["shares"], {BENCHMARK: 1.0}, bars
                )
            plan.detail = {**plan.detail, "ew_fills": ew_fills, "benchmark_fills": bench_fills}
            plans.pop(plan.signal_date)
            executed += 1
        nav = _mark(cash, shares, bars)
        bench_nav = _mark(benchmark["cash"], benchmark["shares"], bars)
        ew_nav = _mark(ew["cash"], ew["shares"], bars)
        index += 1
        invested = round(nav - cash, 4)
        row = RotationShadowDay(
            rule_version=rule_version,
            phase=phase,
            trade_date=day,
            session_index=index,
            nav=nav,
            cash=cash,
            invested=invested,
            positions=dict(shares),
            benchmark_nav=bench_nav,
            ew_nav=ew_nav,
            exposure=invested / nav if nav else 0,
            detail={
                "baseline": False,
                "bars": len(bars),
                "state": {
                    "benchmark": {"cash": benchmark["cash"], "shares": dict(benchmark["shares"])},
                    "ew": {"cash": ew["cash"], "shares": dict(ew["shares"])},
                },
                "marks": {c: {"close": bars[c].close, "value": round(q * bars[c].close, 4)} for c, q in shares.items()},
                "input_sha256": _digest(
                    [[c, str(day), b.open, b.high, b.low, b.close, b.volume] for c, b in sorted(bars.items())]
                ),
                "risk_gate": _risk_gate_record(selection, increase_blocked)
                if selection is not None
                else {
                    "rule_version": RISK_RULE_VERSION,
                    "reason": "no_rebalance",
                    "level": None,
                    "new_exposure": "no_order",
                    "forced_liquidation": False,
                    "benchmarks_ungated": True,
                    "benchmark_policy": "ungated_control",
                    "ew_policy": "ungated_control",
                    "nav_rewritten": False,
                    "effective_boundary": (
                        "new day rows written by this process only; existing nav rows are not rewritten"
                    ),
                },
            },
        )
        session.add(row)
        if plan is not None:
            plan.nav_after = nav
        # Crucial: persist the last known close's plan without requiring future bars.
        if (index + 1) % REBALANCE == 0:
            new_plan = _plan(session, account, frames, day, _next_open(session, day))
            plans[day] = new_plan
        count += 1
    session.flush()
    account.quality = {"status": "ok", "message": "纸面持仓与两条成本一致基准已更新", "last_checked": str(until)}
    return {"ok": True, "days": count, "runs_executed": executed, "last": str(days[-1]), "phase": phase}


def step(
    session: Session, *, as_of: dt.date | None = None, fetcher: Any | None = None, rule_version: str = RULE
) -> dict[str, Any]:
    as_of = as_of or now_cst().date()
    closed_error = _closed_session_error(as_of, "forward")
    if closed_error:
        return {"ok": False, "error": closed_error}
    _lock(session, rule_version, "forward")
    account = session.get(RotationShadowAccount, (rule_version, "forward"))
    if account is None:
        return {"ok": False, "error": "initialize_forward_account_first"}
    out = sync_bars(session, as_of=as_of, codes=account.parameters["universe_codes"], fetcher=fetcher)
    revisions = [c for c, reason in out["failed"].items() if "revision" in reason]
    if revisions:
        return {**_quality(account, as_of, "行情源历史价格修订，暂停推进", missing_codes=revisions), "sync": out}
    result = replay(session, until=as_of, rule_version=rule_version)
    result["sync"] = out
    if as_of >= account.start_date:
        last = session.scalar(
            select(func.max(RotationShadowDay.trade_date)).where(
                RotationShadowDay.rule_version == rule_version, RotationShadowDay.phase == "forward"
            )
        )
        is_open = session.scalar(select(TradingDay.is_open).where(TradingDay.origin == "qp", TradingDay.date == as_of))
        if is_open is None or (is_open and (last is None or last < as_of)):
            return {**_quality(account, as_of, "行情或交易日历未齐，尚未生成今日净值"), "sync": out}
    return result


def _metrics(navs: Sequence[float], dates: Sequence[dt.date]) -> dict[str, Any]:
    if not navs:
        return {k: None for k in ("total_return", "cagr", "vol", "sharpe", "max_drawdown")}
    total = navs[-1] / navs[0] - 1
    peak = navs[0]
    mdd = 0.0
    for value in navs:
        peak = max(peak, value)
        mdd = max(mdd, 1 - value / peak)
    rets = [b / a - 1 for a, b in zip(navs, navs[1:], strict=False)]
    vol = statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else None
    elapsed = (dates[-1] - dates[0]).days
    cagr = (navs[-1] / navs[0]) ** (365.25 / elapsed) - 1 if elapsed >= 365 else None
    sharpe = statistics.mean(rets) * 252 / vol if vol and len(rets) >= 20 else None
    return {
        "total_return": round(total, 8),
        "cagr": round(cagr, 8) if cagr is not None else None,
        "vol": round(vol, 8) if vol is not None else None,
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown": round(mdd, 8),
    }


def _run_dict(run: RotationShadowRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "signal_date": str(run.signal_date),
        "execute_date": str(run.execute_date) if run.execute_date else None,
        "status": run.status,
        "universe": run.universe,
        "nav_before": float(run.nav_before) if run.nav_before is not None else None,
        "nav_after": float(run.nav_after) if run.nav_after is not None else None,
        "turnover": float(run.turnover) if run.turnover is not None else None,
        "selected": run.selected,
        "fills": run.fills,
        "detail": run.detail,
    }


def _performance_versions(days: Sequence[RotationShadowDay]) -> dict[str, Any]:
    ruled = sum(1 for row in days if (row.detail or {}).get("risk_gate", {}).get("rule_version") == RISK_RULE_VERSION)
    return {
        "current_rule": RISK_RULE_VERSION,
        "ruled_days": ruled,
        "unmarked_days": len(days) - ruled,
        "nav_rewritten": False,
    }


def summary(
    session: Session, rule_version: str = RULE, *, phase: str = "forward", series: bool = True, runs_limit: int = 30
) -> dict[str, Any]:
    account = session.get(RotationShadowAccount, (rule_version, phase))
    empty = {
        "rule_version": rule_version,
        "phase": phase,
        "forward_start": str(FORWARD_START),
        "parameters": parameters(),
        "days": 0,
        "start": None,
        "last_date": None,
        "nav": None,
        "cash": None,
        "total_return": None,
        "metrics": None,
        "benchmark": {"nav": None, "metrics": None},
        "ew": {"nav": None, "metrics": None},
        "positions": [],
        "runs": [],
        "last_run": None,
        "pending": None,
        "next_rebalance_date": None,
        "next_execute_date": None,
        "series": [],
        "quality": None,
    }
    if account is None:
        return empty
    days = list(
        session.scalars(
            select(RotationShadowDay)
            .where(RotationShadowDay.rule_version == rule_version, RotationShadowDay.phase == phase)
            .order_by(RotationShadowDay.trade_date)
        )
    )
    if not days:
        return {**empty, "quality": account.quality}
    dates = [row.trade_date for row in days]
    navs = [float(row.nav) for row in days]
    bench = [float(row.benchmark_nav) for row in days]
    ew = [float(row.ew_nav) for row in days]
    latest = days[-1]
    marks = latest.detail.get("marks", {})
    positions = [
        {
            "code": c,
            "name": NAMES.get(c, c),
            "shares": int(q),
            "close": marks.get(c, {}).get("close"),
            "value": marks.get(c, {}).get("value"),
            "weight": marks.get(c, {}).get("value", 0) / float(latest.nav),
        }
        for c, q in latest.positions.items()
    ]
    positions.sort(key=lambda item: -(item["weight"] or 0))
    runs = list(
        session.scalars(
            select(RotationShadowRun)
            .where(RotationShadowRun.rule_version == rule_version, RotationShadowRun.phase == phase)
            .order_by(RotationShadowRun.signal_date.desc())
            .limit(runs_limit)
        )
    )
    pending = next((r for r in runs if r.status == "pending"), None)
    next_signal = None
    next_execute = pending.execute_date if pending else None
    if pending:
        next_signal = pending.signal_date
    elif runs:
        future = list(
            session.scalars(
                select(TradingDay.date)
                .where(TradingDay.origin == "qp", TradingDay.is_open.is_(True), TradingDay.date > runs[0].signal_date)
                .order_by(TradingDay.date)
                .limit(REBALANCE)
            )
        )
        if len(future) == REBALANCE:
            next_signal = future[-1]
            next_execute = _next_open(session, next_signal)
    metric = _metrics(navs, dates)
    result = {
        **empty,
        "parameters": account.parameters,
        "forward_start": str(account.start_date) if phase == "forward" else str(FORWARD_START),
        "days": sum(not row.detail.get("baseline", False) for row in days),
        "start": str(dates[0]),
        "last_date": str(dates[-1]),
        "nav": navs[-1],
        "cash": float(latest.cash),
        "total_return": metric["total_return"],
        "metrics": metric,
        "benchmark": {"nav": bench[-1], "metrics": _metrics(bench, dates)},
        "ew": {"nav": ew[-1], "metrics": _metrics(ew, dates)},
        "positions": positions,
        "runs": [_run_dict(r) for r in runs],
        "last_run": _run_dict(runs[0]) if runs else None,
        "pending": _run_dict(pending) if pending else None,
        "next_rebalance_date": str(next_signal) if next_signal else None,
        "next_execute_date": str(next_execute) if next_execute else None,
        "quality": account.quality,
        "risk_gate": latest.detail.get("risk_gate"),
        "risk_rule_version": RISK_RULE_VERSION,
        "performance_versions": _performance_versions(days),
        "benchmark_policy": "ungated_control",
        "ew_policy": "ungated_control",
    }
    if series:
        result["series"] = [
            {"date": str(d), "nav": n, "benchmark_nav": b, "ew_nav": e}
            for d, n, b, e in zip(dates, navs, bench, ew, strict=False)
        ]
    return result
