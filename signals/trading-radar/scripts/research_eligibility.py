#!/usr/bin/env python3
"""Fail-closed eligibility controls for the personal A-share research radar.

This module is deliberately standard-library only.  It does not score stocks,
run models, place trades, or infer missing evidence.  Unknown evidence fails a
hard gate and can only produce a research workflow state.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
GATE_RULE_VERSION = "quantdinger.personal-research-eligibility.p0.v1"
CALENDAR_SCHEMA_VERSION = 1
CALENDAR_SOURCE_ALLOWLIST = frozenset({"akshare.tool_trade_date_hist_sina"})
CALENDAR_MAX_AGE = timedelta(days=14)
CALENDAR_FUTURE_TOLERANCE = timedelta(minutes=5)
MARKET_CLOSE_READY = time(15, 5)
QUOTE_CLOSE_EARLIEST = time(14, 55)

HARD_GATE_ORDER = (
    "GATE_CALENDAR",
    "GATE_MKT_FRESH",
    "GATE_DATA_LINEAGE",
    "GATE_STRATEGY_VERSION",
    "GATE_INDICATOR_ID",
    "GATE_BACKTEST_BASELINE",
    "GATE_BACKTEST_REPRO",
    "GATE_LOOKAHEAD",
)

ADVISORY_ORDER = (
    "GATE_P2_VALIDATION",
    "GATE_MANUAL_REVIEW",
    "AI_STALE",
)

REASON_LABELS = {
    "GATE_CALENDAR": "交易日历不可核验",
    "GATE_MKT_FRESH": "最近收盘行情不完整",
    "GATE_DATA_LINEAGE": "数据血缘不完整",
    "GATE_STRATEGY_VERSION": "策略版本不可核验",
    "GATE_INDICATOR_ID": "个人指标未关联",
    "GATE_BACKTEST_BASELINE": "回测或同口径基准缺失",
    "GATE_BACKTEST_REPRO": "回测不可复现",
    "GATE_LOOKAHEAD": "前视隔离未证明",
    "GATE_P2_VALIDATION": "P2稳健性验证未完成",
    "GATE_MANUAL_REVIEW": "人工复核未完成",
    "AI_STALE": "AI研究证据过期",
}


@dataclass(frozen=True)
class TradingCalendarSnapshot:
    valid: bool
    sessions: tuple[date, ...]
    source: str | None
    generated_at: datetime | None
    min_date: date | None
    max_date: date | None
    sha256: str | None
    error_code: str | None


@dataclass(frozen=True)
class GateDecision:
    symbol: str
    qualification: str
    workflow: str
    reason_codes: tuple[str, ...]
    advisory_codes: tuple[str, ...]
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qualification": self.qualification,
            "workflow": self.workflow,
            "reason_codes": list(self.reason_codes),
            "advisory_codes": list(self.advisory_codes),
            "evidence": dict(self.evidence),
        }


def _invalid_calendar(error_code: str, sha256: str | None = None) -> TradingCalendarSnapshot:
    return TradingCalendarSnapshot(False, (), None, None, None, None, sha256, error_code)


def _aware_shanghai(value: datetime) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(SHANGHAI)


def _parse_aware_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_shanghai(parsed)


def load_trading_calendar(path: Path | str, now: datetime) -> TradingCalendarSnapshot:
    """Load and validate the local A-share calendar without a network fallback."""

    local_now = _aware_shanghai(now)
    if local_now is None:
        return _invalid_calendar("CLOCK_INVALID")

    calendar_path = Path(path)
    try:
        metadata = calendar_path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1_000 or metadata.st_size > 4_000_000:
            return _invalid_calendar("CALENDAR_FILE_INVALID")
        raw = calendar_path.read_bytes()
    except OSError:
        return _invalid_calendar("CALENDAR_FILE_UNAVAILABLE")

    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _invalid_calendar("CALENDAR_JSON_INVALID", digest)
    if not isinstance(payload, dict):
        return _invalid_calendar("CALENDAR_SCHEMA_INVALID", digest)

    if payload.get("schema_version") != CALENDAR_SCHEMA_VERSION:
        return _invalid_calendar("CALENDAR_SCHEMA_INVALID", digest)
    source = payload.get("source")
    if source not in CALENDAR_SOURCE_ALLOWLIST:
        return _invalid_calendar("CALENDAR_SOURCE_INVALID", digest)

    generated_at = _parse_aware_datetime(payload.get("generated_at"))
    if generated_at is None:
        return _invalid_calendar("CALENDAR_GENERATED_AT_INVALID", digest)
    age = local_now - generated_at
    if age < -CALENDAR_FUTURE_TOLERANCE:
        return _invalid_calendar("CALENDAR_FROM_FUTURE", digest)
    if age > CALENDAR_MAX_AGE:
        return _invalid_calendar("CALENDAR_EXPIRED", digest)

    raw_dates = payload.get("trading_dates")
    if not isinstance(raw_dates, list) or len(raw_dates) < 1_000:
        return _invalid_calendar("CALENDAR_DATES_INVALID", digest)
    sessions: list[date] = []
    try:
        for item in raw_dates:
            if not isinstance(item, str) or len(item) != 10:
                raise ValueError
            parsed = date.fromisoformat(item)
            if parsed.weekday() >= 5:
                raise ValueError
            sessions.append(parsed)
    except ValueError:
        return _invalid_calendar("CALENDAR_DATES_INVALID", digest)

    if any(left >= right for left, right in zip(sessions, sessions[1:])):
        return _invalid_calendar("CALENDAR_ORDER_INVALID", digest)
    if payload.get("count") != len(sessions):
        return _invalid_calendar("CALENDAR_COUNT_INVALID", digest)
    if payload.get("min_date") != sessions[0].isoformat():
        return _invalid_calendar("CALENDAR_MIN_INVALID", digest)
    if payload.get("max_date") != sessions[-1].isoformat():
        return _invalid_calendar("CALENDAR_MAX_INVALID", digest)
    if sessions[-1] > local_now.date() + timedelta(days=550):
        return _invalid_calendar("CALENDAR_RANGE_INVALID", digest)
    if max((right - left).days for left, right in zip(sessions[-800:], sessions[-799:])) > 21:
        return _invalid_calendar("CALENDAR_DENSITY_INVALID", digest)

    if sessions[-1] < local_now.date():
        cursor = sessions[-1] + timedelta(days=1)
        while cursor <= local_now.date():
            if cursor.weekday() < 5:
                return _invalid_calendar("CALENDAR_COVERAGE_INSUFFICIENT", digest)
            cursor += timedelta(days=1)

    return TradingCalendarSnapshot(
        True,
        tuple(sessions),
        str(source),
        generated_at,
        sessions[0],
        sessions[-1],
        digest,
        None,
    )


def expected_last_closed_trade_date(
    snapshot: TradingCalendarSnapshot,
    now: datetime,
    *,
    close_ready: time = MARKET_CLOSE_READY,
) -> date | None:
    """Return the latest fully closed A-share session, or ``None`` on uncertainty."""

    local_now = _aware_shanghai(now)
    if not snapshot.valid or not snapshot.sessions or local_now is None:
        return None
    today = local_now.date()
    index = bisect.bisect_left(snapshot.sessions, today)
    if index < len(snapshot.sessions) and snapshot.sessions[index] == today:
        if local_now.time() >= close_ready:
            return today
        index -= 1
    else:
        index -= 1
    return snapshot.sessions[index] if index >= 0 else None


def trading_session_age(
    snapshot: TradingCalendarSnapshot,
    observed_date: date,
    expected_date: date,
) -> int | None:
    if not snapshot.valid or observed_date > expected_date:
        return None
    left = bisect.bisect_left(snapshot.sessions, observed_date)
    right = bisect.bisect_left(snapshot.sessions, expected_date)
    if left >= len(snapshot.sessions) or snapshot.sessions[left] != observed_date:
        return None
    if right >= len(snapshot.sessions) or snapshot.sessions[right] != expected_date:
        return None
    return right - left


def classify_closed_quote(
    snapshot: TradingCalendarSnapshot,
    generated_at: datetime,
    observed_at: datetime | None,
    price: Any,
) -> tuple[str, date | None]:
    """Classify whether a quote proves the expected official close."""

    expected = expected_last_closed_trade_date(snapshot, generated_at)
    local_now = _aware_shanghai(generated_at)
    local_observed = _aware_shanghai(observed_at) if observed_at is not None else None
    try:
        numeric_price = float(price)
    except (TypeError, ValueError):
        numeric_price = math.nan
    if expected is None or local_now is None:
        return "calendar_invalid", expected
    if local_observed is None or not math.isfinite(numeric_price) or numeric_price <= 0:
        return "missing", expected
    if local_observed > local_now + timedelta(minutes=5):
        return "future", expected
    if local_observed.date() != expected:
        return "stale", expected
    if local_observed.time() < QUOTE_CLOSE_EARLIEST:
        return "incomplete", expected
    return "fresh_close", expected


def _ordered(codes: Sequence[str], order: Sequence[str]) -> tuple[str, ...]:
    values = set(codes)
    return tuple(code for code in order if code in values)


def evaluate_eligibility(
    symbol: str,
    *,
    calendar: TradingCalendarSnapshot,
    generated_at: datetime,
    quote_observed_at: datetime | None,
    quote_price: Any,
    quote_source: str | None,
    market_data_lineage_complete: bool,
    strategy_hash: str | None,
    backtest_hash: str | None,
    strategy_fingerprint_complete: bool,
    backtest_id: Any,
    indicator_id: Any,
    expected_indicator_id: Any,
    benchmark_present: bool,
    backtest_reproducible: bool,
    lookahead_proven: bool,
    p2_validated: bool = False,
    manual_reviewed: bool = False,
    ai_stale: bool = False,
) -> GateDecision:
    """Evaluate eligibility once; renderers must consume this decision verbatim."""

    quote_state, expected_date = classify_closed_quote(
        calendar, generated_at, quote_observed_at, quote_price
    )
    hard: list[str] = []
    if not calendar.valid or expected_date is None:
        hard.append("GATE_CALENDAR")
    if quote_state != "fresh_close":
        hard.append("GATE_MKT_FRESH")
    if not quote_source or not market_data_lineage_complete:
        hard.append("GATE_DATA_LINEAGE")

    current_hash = str(strategy_hash or "").lower()
    historical_hash = str(backtest_hash or "").lower()
    valid_hashes = (
        len(current_hash) == 64
        and len(historical_hash) == 64
        and all(char in "0123456789abcdef" for char in current_hash + historical_hash)
    )
    if not valid_hashes or current_hash != historical_hash or not strategy_fingerprint_complete:
        hard.append("GATE_STRATEGY_VERSION")

    def _same_identifier(left: Any, right: Any) -> bool:
        try:
            return int(left) == int(right)
        except (TypeError, ValueError):
            return False

    if not _same_identifier(indicator_id, expected_indicator_id):
        hard.append("GATE_INDICATOR_ID")
    if backtest_id is None or not benchmark_present:
        hard.append("GATE_BACKTEST_BASELINE")
    if backtest_id is None or not backtest_reproducible:
        hard.append("GATE_BACKTEST_REPRO")
    if backtest_id is None or not lookahead_proven:
        hard.append("GATE_LOOKAHEAD")

    advisory = []
    if not p2_validated:
        advisory.append("GATE_P2_VALIDATION")
    if not manual_reviewed:
        advisory.append("GATE_MANUAL_REVIEW")
    if ai_stale:
        advisory.append("AI_STALE")

    hard_codes = _ordered(hard, HARD_GATE_ORDER)
    advisory_codes = _ordered(advisory, ADVISORY_ORDER)
    if hard_codes:
        qualification = "BLOCKED"
        workflow = "EVIDENCE_REPAIR"
    elif p2_validated and manual_reviewed:
        qualification = "VALIDATED_CANDIDATE"
        workflow = "MANUAL_DECISION"
    else:
        qualification = "RESEARCH_ONLY"
        workflow = "MANUAL_REVIEW"

    return GateDecision(
        symbol=str(symbol),
        qualification=qualification,
        workflow=workflow,
        reason_codes=hard_codes,
        advisory_codes=advisory_codes,
        evidence={
            "gate_rule_version": GATE_RULE_VERSION,
            "calendar_sha256": calendar.sha256,
            "calendar_error": calendar.error_code,
            "expected_market_asof": expected_date.isoformat() if expected_date else None,
            "quote_state": quote_state,
            "quote_observed_at": (
                quote_observed_at.astimezone(SHANGHAI).isoformat()
                if quote_observed_at is not None and quote_observed_at.tzinfo is not None
                else None
            ),
            "quote_source": quote_source,
            "strategy_hash": strategy_hash,
            "backtest_hash": backtest_hash,
            "backtest_id": backtest_id,
            "indicator_id": indicator_id,
            "expected_indicator_id": expected_indicator_id,
            "benchmark_present": bool(benchmark_present),
        },
    )


def reason_labels(codes: Sequence[str]) -> list[str]:
    return [REASON_LABELS.get(code, code) for code in codes]


def write_audit_record(
    audit_dir: Path | str,
    payload: Mapping[str, Any],
    *,
    generated_at: datetime,
) -> Path:
    """Write one immutable, user-only JSON record. Existing records are never replaced."""

    local_now = _aware_shanghai(generated_at)
    if local_now is None:
        raise ValueError("generated_at must be timezone-aware")
    directory = Path(audit_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    stem = local_now.strftime("%Y%m%dT%H%M%S.%f%z")
    body = (json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    for counter in range(1_000):
        suffix = "" if counter == 0 else f"-{counter}"
        target = directory / f"eligibility-{stem}{suffix}.json"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return target
    raise FileExistsError("unable to allocate a unique audit filename")

