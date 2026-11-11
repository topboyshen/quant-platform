"""Isolated read-only API for Quant -> AI-Filter market context.

This process deliberately does not import ``dashboard.app`` and therefore does
not register Dashboard, portfolio, log, SSE or configuration routes.  It reads
only the Swing SQLite database and the persisted macro-risk JSON snapshot.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request

from dashboard import service_auth
from dashboard.etf_pool import load_etf_pool, pool_expected_date
from radar.swing.validity import ValidityPolicy, build_validity_summary

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
TOKEN_FILE_ENV = "RADAR_INTEGRATION_TOKEN_FILE"
MAX_JSON_FILE_BYTES = 256 * 1024
MAX_FACTORS = 16
MAX_SIGNALS = 50
RISK_STALE_SECONDS = 3600
RISK_FUTURE_SKEW_SECONDS = 60
MARKET_CLOSE = wall_time(15, 0)
MARKET_EOD_GRACE_MINUTES_ENV = "RADAR_INTEGRATION_EOD_GRACE_MINUTES"
DEFAULT_MARKET_EOD_GRACE_MINUTES = 30
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SWING_DECISION_TIME = "15:30 Asia/Shanghai"
DECISION_WINDOW_EARLY_MINUTES = 5
VALIDITY_HORIZONS = (1, 3, 5, 10, 20)

REQUIRED_MACRO_COLUMNS = frozenset(
    {
        "date",
        "total_amount",
        "margin_balance",
        "margin_growth_pct",
        "limit_up",
        "limit_down",
        "limit_ratio",
        "source",
        "extra",
    }
)
REQUIRED_SIGNAL_COLUMNS = frozenset(
    {
        "id",
        "date",
        "data_date",
        "code",
        "name",
        "signal_type",
        "period",
        "title",
        "confidence",
    }
)
REQUIRED_RUN_COLUMNS = frozenset(
    {
        "id",
        "run_id",
        "decision_date",
        "data_date",
        "status",
        "started_at",
        "completed_at",
        "target_count",
        "evaluated_count",
        "failed_count",
        "signal_count",
        "allowed_signal_count",
        "blocked_signal_count",
        "decision_outcome",
        "reason_codes",
    }
)
REQUIRED_TARGET_DECISION_COLUMNS = frozenset(
    {
        "decision_id",
        "run_id",
        "decision_date",
        "decision_at",
        "decision_state",
        "raw_trigger_count",
        "validation_split",
    }
)
REQUIRED_OUTCOME_COLUMNS = frozenset(
    {
        "decision_id",
        "horizon_sessions",
        "net_return",
        "excess_return",
        "mfe",
        "mae",
        "fill_status",
    }
)

_RUN_STATUSES = {"running", "success", "partial", "failed", "skipped"}
_DECISION_OUTCOMES = {
    "not_evaluated",
    "emitted",
    "no_signal",
    "blocked",
    "data_unavailable",
    "skipped",
}
_DECISION_STATES = ("no_trigger", "allowed", "blocked", "data_failed")
_VALIDITY_STATE_BY_LABEL = {
    "样本不足": "sample_insufficient",
    "有效性不确定": "uncertain",
    "初步有效": "preliminary_effective",
    "未见有效": "not_effective",
}

_FACTOR_META = {
    "total_amount": ("全市场成交额", "亿元"),
    "margin_growth_pct": ("两融余额增速", "%"),
    "limit_ratio": ("涨跌停家数比", ""),
}

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 1024


def _configured_token() -> str:
    return service_auth.read_token_file(TOKEN_FILE_ENV)


def _supplied_bearer_token() -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        return ""
    if token != token.strip() or " " in token:
        return ""
    return token


def _auth_error(status: int) -> Response:
    response = jsonify(
        {"error": "service_unavailable" if status == 503 else "unauthorized"}
    )
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="quant-integration"'
    return response


@app.before_request
def _require_integration_token():
    if not request.path.startswith("/integration/v1/"):
        return None
    expected = _configured_token()
    if not expected:
        return _auth_error(503)
    supplied = _supplied_bearer_token()
    if not service_auth.constant_time_matches(supplied, expected):
        return _auth_error(401)
    return None


@app.after_request
def _security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _finite_number(value: Any) -> Optional[Union[int, float]]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) <= 10**308 else None
    if not isinstance(value, float):
        return None
    return value if math.isfinite(value) else None


def _short_text(value: Any, limit: int = 160) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def _iso_date(value: Any) -> Optional[str]:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def _today_shanghai() -> date:
    return _now_shanghai().date()


def _now_shanghai() -> datetime:
    return datetime.now(tz=SHANGHAI_TZ)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_factor(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    return {
        "factor": _short_text(item.get("factor"), 64),
        "label": _short_text(item.get("label"), 64),
        "unit": _short_text(item.get("unit"), 16),
        "value": _finite_number(item.get("value")),
        "value_date": _short_text(item.get("value_date"), 32),
        "is_stale": bool(item.get("is_stale", False)),
        "percentile": _finite_number(item.get("percentile")),
        "level": _short_text(item.get("level"), 32),
        "sample_size": _finite_number(item.get("sample_size")),
        "history_ready": bool(item.get("history_ready", False)),
        "collect_available": bool(item.get("collect_available", False)),
        "collect_source": _short_text(item.get("collect_source"), 64),
    }


def _clean_signal(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    decision_date = _iso_date(item.get("decision_date"))
    data_date = _iso_date(item.get("data_date"))
    if decision_date is None or data_date is None:
        return None
    if (
        data_date > decision_date
        or date.fromisoformat(decision_date) > _today_shanghai()
    ):
        return None
    return {
        "decision_date": decision_date,
        "data_date": data_date,
        "code": _short_text(item.get("code"), 32),
        "name": _short_text(item.get("name"), 80),
        "signal_type": _short_text(item.get("signal_type"), 80),
        "period": _short_text(item.get("period"), 16),
        "title": _short_text(item.get("title"), 160),
        "confidence": _finite_number(item.get("confidence")),
    }


def _iso_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _configured_decision_time(value: Any) -> wall_time:
    """Parse the bounded ``HH:MM Asia/Shanghai`` strategy contract."""
    text = str(value or DEFAULT_SWING_DECISION_TIME).strip()
    parts = text.split()
    if len(parts) != 2 or parts[1] != "Asia/Shanghai":
        return wall_time(15, 30)
    try:
        parsed = wall_time.fromisoformat(parts[0])
    except ValueError:
        return wall_time(15, 30)
    return parsed.replace(second=0, microsecond=0, tzinfo=None)


def _decision_window_eligibility(
    decision_date: str,
    observed_at: str,
    decision_time: Any = DEFAULT_SWING_DECISION_TIME,
) -> tuple[bool, str]:
    """Classify a run without changing its immutable audit record."""
    parsed_date = _iso_date(decision_date)
    normalized_timestamp = _iso_timestamp(observed_at)
    if parsed_date is None or normalized_timestamp is None:
        return False, "invalid_time"
    observed = datetime.fromisoformat(
        normalized_timestamp.replace("Z", "+00:00")
    ).astimezone(SHANGHAI_TZ)
    decision_day = date.fromisoformat(parsed_date)
    if observed.date() != decision_day:
        return False, "date_mismatch"
    scheduled = datetime.combine(
        decision_day,
        _configured_decision_time(decision_time),
        tzinfo=SHANGHAI_TZ,
    )
    if observed < scheduled - timedelta(minutes=DECISION_WINDOW_EARLY_MINUTES):
        return False, "before_decision_window"
    return True, "scheduled_window"


def _nonnegative_int(value: Any, *, maximum: int = 10_000) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def _clean_daily_audit(
    item: Any,
    *,
    decision_time: Any = DEFAULT_SWING_DECISION_TIME,
) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    decision_date = _iso_date(item.get("decision_date"))
    data_date = _iso_date(item.get("data_date")) if item.get("data_date") else None
    status = str(item.get("status") or "")
    outcome = str(item.get("decision_outcome") or "")
    started_at = _iso_timestamp(item.get("started_at"))
    completed_at = (
        _iso_timestamp(item.get("completed_at"))
        if item.get("completed_at")
        else None
    )
    target_count = _nonnegative_int(item.get("target_count"))
    evaluated_count = _nonnegative_int(item.get("evaluated_count"))
    failed_count = _nonnegative_int(item.get("failed_count"))
    signal_count = _nonnegative_int(item.get("signal_count"), maximum=1_000)
    allowed_signal_count = _nonnegative_int(
        item.get("allowed_signal_count"), maximum=1_000
    )
    blocked_signal_count = _nonnegative_int(
        item.get("blocked_signal_count"), maximum=1_000
    )
    if (
        decision_date is None
        or date.fromisoformat(decision_date) > _today_shanghai()
        or (data_date is not None and data_date > decision_date)
        or status not in _RUN_STATUSES
        or outcome not in _DECISION_OUTCOMES
        or started_at is None
        or any(
            value is None
            for value in (
                target_count,
                evaluated_count,
                failed_count,
                signal_count,
                allowed_signal_count,
                blocked_signal_count,
            )
        )
        or evaluated_count + failed_count > target_count
        or allowed_signal_count + blocked_signal_count != signal_count
        or (status == "success" and (failed_count != 0 or evaluated_count != target_count))
    ):
        return None
    if status == "running":
        if completed_at is not None or outcome != "not_evaluated":
            return None
    else:
        if completed_at is None or completed_at < started_at:
            return None
    allowed_outcomes = {
        "success": {"emitted", "no_signal", "blocked"},
        "partial": {"emitted", "blocked", "data_unavailable"},
        "failed": {"data_unavailable"},
        "skipped": {"skipped"},
    }
    if status != "running" and outcome not in allowed_outcomes.get(status, set()):
        return None
    if (outcome == "emitted" and signal_count == 0) or (
        outcome in {"no_signal", "data_unavailable", "skipped"}
        and signal_count != 0
    ) or (outcome == "blocked" and (signal_count == 0 or allowed_signal_count != 0)) or (
        outcome == "emitted" and allowed_signal_count == 0
    ):
        return None

    reasons = []
    raw_reasons = item.get("reason_codes")
    if isinstance(raw_reasons, str):
        try:
            raw_reasons = json.loads(raw_reasons)
        except (TypeError, ValueError):
            raw_reasons = []
    for value in _as_list(raw_reasons)[:8]:
        text = str(value or "")[:64]
        if text and text.replace("_", "").isalnum() and text not in reasons:
            reasons.append(text)

    decision_eligible, eligibility_reason = _decision_window_eligibility(
        decision_date,
        started_at,
        decision_time,
    )

    return {
        "decision_date": decision_date,
        "data_date": data_date,
        "run": {
            "run_id": _short_text(item.get("run_id"), 64),
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "reason_codes": reasons,
            "decision_eligible": decision_eligible,
            "eligibility_reason": eligibility_reason,
        },
        "decision": {
            "outcome": outcome,
            "signal_count": signal_count,
            "allowed_signal_count": allowed_signal_count,
            "blocked_signal_count": blocked_signal_count,
        },
        "coverage": {
            "target_count": target_count,
            "evaluated_count": evaluated_count,
            "failed_count": failed_count,
        },
        "target_decisions": {
            "available": False,
            **{state: 0 for state in _DECISION_STATES},
        },
    }


def _clean_strategy(item: Any) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    return {
        "role": _short_text(item.get("role"), 64),
        "decision_time": _short_text(item.get("decision_time"), 64),
        "ma_periods": [
            number
            for number in (
                _finite_number(value) for value in _as_list(item.get("ma_periods"))[:8]
            )
            if number is not None
        ],
        "volume_breakout_ratio": _finite_number(item.get("volume_breakout_ratio")),
    }


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _holiday_calendar_class():
    try:
        from radar.core.scan_scheduler import HolidayCalendar

        return HolidayCalendar
    except Exception:
        return None


def _expected_market_as_of(now: Optional[datetime] = None) -> Optional[str]:
    """Return the most recent A-share EOD slice that should already exist."""
    calendar_class = _holiday_calendar_class()
    if calendar_class is None:
        return None
    current = now or _now_shanghai()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    grace_minutes = _bounded_int(
        os.getenv(MARKET_EOD_GRACE_MINUTES_ENV),
        DEFAULT_MARKET_EOD_GRACE_MINUTES,
        0,
        180,
    )
    today_cutoff = datetime.combine(
        current.date(), MARKET_CLOSE, tzinfo=SHANGHAI_TZ
    ) + timedelta(minutes=grace_minutes)

    candidate = current.date()
    for _ in range(370):
        if candidate.weekday() < 5:
            probe = datetime.combine(candidate, wall_time(12, 0), tzinfo=SHANGHAI_TZ)
            try:
                is_holiday = bool(calendar_class.is_holiday(probe))
            except Exception:
                return None
            if not is_holiday and (
                candidate < current.date() or current >= today_cutoff
            ):
                return candidate.isoformat()
        candidate -= timedelta(days=1)
    return None


def _market_freshness(value: Any) -> dict[str, Any]:
    normalized = _iso_date(value)
    today = _today_shanghai()
    expected_as_of = _expected_market_as_of()
    calendar_available = expected_as_of is not None
    if normalized is None:
        return {
            "age_days": None,
            "expected_as_of": expected_as_of,
            "calendar_available": calendar_available,
            "is_stale": True,
        }
    observed = date.fromisoformat(normalized)
    delta_days = (today - observed).days
    return {
        "age_days": delta_days if delta_days >= 0 else None,
        "expected_as_of": expected_as_of,
        "calendar_available": calendar_available,
        "is_stale": (
            delta_days < 0 or expected_as_of is None or normalized < expected_as_of
        ),
    }


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(parsed, maximum))


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read(MAX_JSON_FILE_BYTES + 1)
        if len(raw) > MAX_JSON_FILE_BYTES:
            return {}
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_swing_db(config: dict[str, Any]) -> Optional[Path]:
    swing = config.get("swing", {})
    swing = swing if isinstance(swing, dict) else {}
    store_file = str(swing.get("store_file", "data/swing_macro.db"))
    data_root = (BASE_DIR / "data").resolve()
    candidate = (BASE_DIR / store_file).resolve()
    if not _is_within(candidate, data_root) or not candidate.is_file():
        return None
    return candidate


def _open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def _table_columns(connection: sqlite3.Connection, table_name: str) -> frozenset[str]:
    if table_name not in {
        "macro_slice",
        "trend_signal",
        "swing_eod_run",
        "eod_target_decision",
        "eod_decision_outcome",
    }:
        return frozenset()
    return frozenset(
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    )


def _readiness_ok() -> bool:
    if not _configured_token():
        return False
    config = _read_json_file(BASE_DIR / "config.swing.json")
    if not isinstance(config.get("swing"), dict):
        return False
    db_path = _resolve_swing_db(config)
    if db_path is None:
        return False
    try:
        with closing(_open_readonly_sqlite(db_path)) as connection:
            connection.execute("BEGIN")
            macro_columns = _table_columns(connection, "macro_slice")
            signal_columns = _table_columns(connection, "trend_signal")
            return REQUIRED_MACRO_COLUMNS.issubset(
                macro_columns
            ) and REQUIRED_SIGNAL_COLUMNS.issubset(signal_columns)
    except (OSError, sqlite3.Error):
        return False


def _percentile(values: list[float], current: float) -> float:
    if not values:
        return 0.0
    below_or_equal = sum(1 for value in values if value <= current)
    return round(below_or_equal / len(values) * 100, 1)


def _load_factor(
    connection: sqlite3.Connection,
    column: str,
    latest: sqlite3.Row,
    collect_status: dict[str, Any],
    minimum_history: int,
    extreme_low: float,
    extreme_high: float,
) -> dict[str, Any]:
    value = _finite_number(latest[column])
    value_date = str(latest["date"])
    is_stale = False
    if value is None:
        fallback = connection.execute(
            f"SELECT date, {column} AS value FROM macro_slice "
            f"WHERE {column} IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if fallback is not None:
            value = _finite_number(fallback["value"])
            value_date = str(fallback["date"])
            is_stale = value_date != str(latest["date"])

    rows = connection.execute(
        f"SELECT {column} AS value FROM macro_slice "
        f"WHERE {column} IS NOT NULL AND date <> ? ORDER BY date DESC LIMIT 250",
        (value_date,),
    ).fetchall()
    history = [
        number
        for number in (_finite_number(row["value"]) for row in rows)
        if number is not None
    ]
    history_ready = len(history) >= minimum_history
    percentile = (
        _percentile([float(item) for item in history], float(value))
        if value is not None and history_ready
        else None
    )
    level = "正常"
    if percentile is not None and percentile <= extreme_low:
        level = "冰点"
    elif percentile is not None and percentile >= extreme_high:
        level = "沸点"

    raw_status = collect_status.get(column, {})
    raw_status = raw_status if isinstance(raw_status, dict) else {}
    label, unit = _FACTOR_META[column]
    return {
        "factor": column,
        "label": label,
        "unit": unit,
        "value": value,
        "value_date": value_date,
        "is_stale": is_stale,
        "percentile": percentile,
        "level": level,
        "sample_size": len(history),
        "history_ready": history_ready,
        "collect_available": bool(raw_status.get("available", value is not None)),
        "collect_source": _short_text(
            raw_status.get("source", latest["source"] or ""), 64
        ),
    }


def _load_signals(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _table_columns(connection, "trend_signal")
    if not REQUIRED_SIGNAL_COLUMNS.issubset(columns):
        return []
    rows = connection.execute(
        "SELECT date, data_date, code, name, signal_type, period, title, confidence "
        "FROM trend_signal ORDER BY date DESC, id DESC LIMIT ?",
        (MAX_SIGNALS * 4,),
    ).fetchall()

    signals = []
    for row in rows:
        signal = _clean_signal(
            {
                "decision_date": row["date"],
                "data_date": row["data_date"],
                "code": _short_text(row["code"], 32),
                "name": _short_text(row["name"], 80),
                "signal_type": _short_text(row["signal_type"], 80),
                "period": _short_text(row["period"], 16),
                "title": _short_text(row["title"], 160),
                "confidence": _finite_number(row["confidence"]),
            }
        )
        if signal is None:
            continue
        signals.append(signal)
        if len(signals) >= MAX_SIGNALS:
            break
    return signals


def _load_daily_audit(
    connection: sqlite3.Connection,
    *,
    decision_time: Any = DEFAULT_SWING_DECISION_TIME,
) -> tuple[Optional[dict[str, Any]], bool]:
    """Load the newest EOD attempt from the same read-only SQLite snapshot."""
    if not REQUIRED_RUN_COLUMNS.issubset(
        _table_columns(connection, "swing_eod_run")
    ):
        return None, False
    row = connection.execute(
        "SELECT run_id, decision_date, data_date, status, started_at, completed_at, "
        "target_count, evaluated_count, failed_count, signal_count, "
        "allowed_signal_count, blocked_signal_count, "
        "decision_outcome, reason_codes "
        "FROM swing_eod_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, False
    cleaned = _clean_daily_audit(dict(row), decision_time=decision_time)
    if cleaned is not None and REQUIRED_TARGET_DECISION_COLUMNS.issubset(
        _table_columns(connection, "eod_target_decision")
    ):
        counts = {state: 0 for state in _DECISION_STATES}
        decision_rows = connection.execute(
            "SELECT decision_state, COUNT(*) AS count "
            "FROM eod_target_decision WHERE run_id=? GROUP BY decision_state",
            (str(row["run_id"]),),
        ).fetchall()
        invalid_state = False
        for decision_row in decision_rows:
            state = str(decision_row["decision_state"] or "")
            count = _nonnegative_int(decision_row["count"])
            if state not in counts or count is None:
                invalid_state = True
                break
            counts[state] = count
        if invalid_state or sum(counts.values()) > cleaned["coverage"]["target_count"]:
            return None, True
        cleaned["target_decisions"] = {"available": True, **counts}
    return cleaned, cleaned is None


def _load_effectiveness(
    connection: sqlite3.Connection,
    *,
    validity_config: Any,
    decision_time: Any = DEFAULT_SWING_DECISION_TIME,
) -> tuple[Optional[dict[str, Any]], bool]:
    """Return the same conservative validity state as the Quant dashboard."""
    if not REQUIRED_TARGET_DECISION_COLUMNS.issubset(
        _table_columns(connection, "eod_target_decision")
    ) or not REQUIRED_OUTCOME_COLUMNS.issubset(
        _table_columns(connection, "eod_decision_outcome")
    ):
        return None, False

    config = validity_config if isinstance(validity_config, dict) else {}
    minimum_samples = _bounded_int(config.get("min_samples"), 30, 1, 10_000)
    primary_horizon = _bounded_int(
        config.get("primary_horizon_sessions"), 20, 1, 20
    )
    if primary_horizon not in VALIDITY_HORIZONS:
        primary_horizon = 20

    rows = connection.execute(
        "SELECT d.decision_date,d.decision_at,d.decision_state,d.raw_trigger_count,"
        "d.validation_split,d.code,r.status AS run_status,r.started_at AS run_started_at,"
        "o.horizon_sessions,o.net_return,o.excess_return,o.mfe,o.mae,o.fill_status "
        "FROM eod_decision_outcome o "
        "JOIN eod_target_decision d ON d.decision_id=o.decision_id "
        "JOIN swing_eod_run r ON r.run_id=d.run_id "
        "WHERE o.fill_status='filled'"
    ).fetchall()
    eligible_rows = []
    horizon_samples = {str(horizon): 0 for horizon in VALIDITY_HORIZONS}
    for raw_row in rows:
        item = dict(raw_row)
        eligible, _reason = _decision_window_eligibility(
            str(item.get("decision_date") or ""),
            str(item.get("decision_at") or item.get("run_started_at") or ""),
            decision_time,
        )
        if not eligible or item.get("run_status") != "success":
            continue
        eligible_rows.append(item)
        horizon = _nonnegative_int(item.get("horizon_sessions"), maximum=20)
        if (
            horizon in VALIDITY_HORIZONS
            and int(item.get("raw_trigger_count") or 0) > 0
            and item.get("decision_state") == "allowed"
        ):
            horizon_samples[str(horizon)] += 1

    primary_rows = [
        item
        for item in eligible_rows
        if item.get("horizon_sessions") == primary_horizon
    ]
    summary = build_validity_summary(
        primary_rows,
        policy=ValidityPolicy(
            min_samples=minimum_samples,
            primary_horizon=primary_horizon,
        ),
    )
    label = str(summary.get("label") or "")
    state = _VALIDITY_STATE_BY_LABEL.get(label)
    sample_size = _nonnegative_int(summary.get("sample_size"), maximum=1_000_000)
    counterfactual = summary.get("risk_counterfactual")
    counterfactual = counterfactual if isinstance(counterfactual, dict) else {}
    blocked_sample_size = _nonnegative_int(
        counterfactual.get("sample_size"), maximum=1_000_000
    )
    if state is None or sample_size is None or blocked_sample_size is None:
        return None, True
    return {
        "state": state,
        "sample_size": sample_size,
        "minimum_samples": minimum_samples,
        "primary_horizon_sessions": primary_horizon,
        "method": "live_oos_or_walk_forward_oos",
        "metrics_available": summary.get("metrics") is not None,
        "horizon_samples": horizon_samples,
        "blocked_counterfactual_sample_size": blocked_sample_size,
    }, False


def _empty_market_overview() -> dict[str, Any]:
    return {
        "available": False,
        "updated": None,
        "source": None,
        "limit_up": None,
        "limit_down": None,
        "margin_balance": None,
        "factors": [],
        "signals": [],
        "recent_signals": [],
        "latest_decision_date": None,
        "daily_audit": None,
        "daily_audit_invalid": False,
        "effectiveness": None,
        "effectiveness_invalid": False,
        "strategy": {},
    }


def _load_market_overview() -> dict[str, Any]:
    config = _read_json_file(BASE_DIR / "config.swing.json")
    pool_now = _now_shanghai()
    pool_date = pool_expected_date(BASE_DIR / "data/trading_calendar.json", pool_now)
    research_pool = load_etf_pool(config, None, None, expected_date=pool_date, now=pool_now)
    db_path = _resolve_swing_db(config)
    if db_path is None:
        return {**_empty_market_overview(), "research_pool": research_pool}

    swing = config.get("swing", {})
    swing = swing if isinstance(swing, dict) else {}
    macro = swing.get("macro", {})
    macro = macro if isinstance(macro, dict) else {}
    validity_config = swing.get("validity", {})
    validity_config = validity_config if isinstance(validity_config, dict) else {}
    decision_time = swing.get("decision_time", DEFAULT_SWING_DECISION_TIME)
    minimum_history = _bounded_int(macro.get("min_history_days"), 20, 1, 250)
    extreme_low = _bounded_float(macro.get("extreme_percentile_low"), 5, 0, 50)
    extreme_high = _bounded_float(macro.get("extreme_percentile_high"), 95, 50, 100)

    try:
        with closing(_open_readonly_sqlite(db_path)) as connection:
            # All reads for one response share a single explicit, read-only
            # SQLite snapshot even while the EOD writer commits a new slice.
            connection.execute("BEGIN")
            try:
                daily_audit, daily_audit_invalid = _load_daily_audit(
                    connection,
                    decision_time=decision_time,
                )
            except sqlite3.Error:
                daily_audit, daily_audit_invalid = None, True
            research_pool = load_etf_pool(
                config, connection, daily_audit, expected_date=pool_date, now=pool_now
            )
            try:
                effectiveness, effectiveness_invalid = _load_effectiveness(
                    connection,
                    validity_config=validity_config,
                    decision_time=decision_time,
                )
            except (sqlite3.Error, ValueError, TypeError):
                effectiveness, effectiveness_invalid = None, True
            if not REQUIRED_MACRO_COLUMNS.issubset(
                _table_columns(connection, "macro_slice")
            ):
                overview = _empty_market_overview()
                overview["daily_audit"] = daily_audit
                overview["daily_audit_invalid"] = daily_audit_invalid
                overview["effectiveness"] = effectiveness
                overview["effectiveness_invalid"] = effectiveness_invalid
                overview["research_pool"] = research_pool
                return overview
            latest = connection.execute(
                "SELECT date, total_amount, margin_balance, margin_growth_pct, "
                "limit_up, limit_down, limit_ratio, source, extra "
                "FROM macro_slice ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                overview = _empty_market_overview()
                overview["daily_audit"] = daily_audit
                overview["daily_audit_invalid"] = daily_audit_invalid
                overview["effectiveness"] = effectiveness
                overview["effectiveness_invalid"] = effectiveness_invalid
                overview["research_pool"] = research_pool
                return overview
            try:
                extra = json.loads(latest["extra"] or "{}")
            except (TypeError, ValueError):
                extra = {}
            extra = extra if isinstance(extra, dict) else {}
            collect_status = extra.get("collect_status", {})
            collect_status = collect_status if isinstance(collect_status, dict) else {}
            factors = [
                _load_factor(
                    connection,
                    column,
                    latest,
                    collect_status,
                    minimum_history,
                    extreme_low,
                    extreme_high,
                )
                for column in tuple(_FACTOR_META)[:MAX_FACTORS]
            ]
            try:
                recent_signals = _load_signals(connection)
            except sqlite3.Error:
                recent_signals = []
    except (OSError, sqlite3.Error):
        logger.warning("Unable to read the Swing database", exc_info=True)
        return {**_empty_market_overview(), "research_pool": research_pool}

    updated = str(latest["date"])
    latest_decision_date = (
        recent_signals[0]["decision_date"] if recent_signals else None
    )
    return {
        "available": True,
        "updated": updated,
        "source": _short_text(latest["source"], 64),
        "limit_up": _finite_number(latest["limit_up"]),
        "limit_down": _finite_number(latest["limit_down"]),
        "margin_balance": _finite_number(latest["margin_balance"]),
        "factors": factors,
        "signals": [
            item for item in recent_signals if item["decision_date"] == updated
        ],
        "recent_signals": recent_signals,
        "latest_decision_date": latest_decision_date,
        "daily_audit": daily_audit,
        "daily_audit_invalid": daily_audit_invalid,
        "effectiveness": effectiveness,
        "effectiveness_invalid": effectiveness_invalid,
        "research_pool": research_pool,
        "strategy": {
            "role": _short_text(swing.get("system_role", "core_trend_swing_eod"), 64),
            "decision_time": _short_text(
                swing.get("decision_time", "15:30 Asia/Shanghai"), 64
            ),
            "ma_periods": [
                number
                for number in (
                    _finite_number(item)
                    for item in (
                        swing.get("ma_periods", [])
                        if isinstance(swing.get("ma_periods"), list)
                        else []
                    )[:8]
                )
                if number is not None
            ],
            "volume_breakout_ratio": _finite_number(swing.get("volume_breakout_ratio")),
        },
    }


def _load_persisted_risk_state() -> dict[str, Any]:
    configured = os.getenv("RADAR_RISK_STATE_FILE", "").strip()
    path = (
        Path(configured) if configured else BASE_DIR / "data" / "radar_risk_state.json"
    )
    state = _read_json_file(path)
    if not state:
        return {}
    timestamp = _finite_number(state.get("timestamp"))
    state["age_seconds"] = (
        max(0, int(time.time() - timestamp)) if timestamp is not None else None
    )
    return state


def _clean_risk(state: Any) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    allowed_fields = {"vix", "us_10y", "crude_oil"}
    raw_missing = state.get("missing_fields", [])
    raw_cached = state.get("cached_fields", [])
    raw_missing = raw_missing if isinstance(raw_missing, list) else []
    raw_cached = raw_cached if isinstance(raw_cached, list) else []
    missing_set = allowed_fields.intersection(str(item) for item in raw_missing)
    missing_set.update(
        field for field in allowed_fields if _finite_number(state.get(field)) is None
    )
    missing = sorted(missing_set)
    cached = sorted(allowed_fields.intersection(str(item) for item in raw_cached))
    raw_source_timestamps = (
        state.get("source_timestamps")
        if isinstance(state.get("source_timestamps"), dict)
        else {}
    )
    raw_available_at = (
        state.get("available_at") if isinstance(state.get("available_at"), dict) else {}
    )
    raw_sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    source_timestamps = {
        field: _finite_number(raw_source_timestamps.get(field))
        for field in allowed_fields
        if _finite_number(raw_source_timestamps.get(field)) is not None
    }
    available_at = {
        field: _finite_number(raw_available_at.get(field))
        for field in allowed_fields
        if _finite_number(raw_available_at.get(field)) is not None
    }
    timestamp = _finite_number(state.get("timestamp"))
    observed_at = None
    if timestamp is not None and timestamp > 0:
        try:
            observed_at = (
                datetime.fromtimestamp(timestamp, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OSError, OverflowError, ValueError):
            observed_at = None
    now = time.time()
    is_future = (
        (timestamp is not None and timestamp > now + RISK_FUTURE_SKEW_SECONDS)
        or any(value > now + RISK_FUTURE_SKEW_SECONDS for value in source_timestamps.values())
        or any(value > now + RISK_FUTURE_SKEW_SECONDS for value in available_at.values())
    )
    age_seconds = max(0, int(now - timestamp)) if timestamp is not None else None
    stale = age_seconds is None or age_seconds > RISK_STALE_SECONDS
    return {
        "available": bool(state),
        "level": _short_text(state.get("level"), 32),
        "observed_at": observed_at,
        "age_seconds": age_seconds,
        "is_stale": stale,
        "is_future": is_future,
        "data_complete": bool(state) and not missing,
        "missing_fields": missing,
        "cached_fields": cached,
        "source_timestamps": source_timestamps,
        "available_at": available_at,
        "sources": {
            field: _short_text(raw_sources.get(field), 64)
            for field in allowed_fields
            if raw_sources.get(field)
        },
        "vix": _finite_number(state.get("vix")),
        "us_10y": _finite_number(state.get("us_10y")),
        "crude_oil": _finite_number(state.get("crude_oil")),
    }


def _build_market_context(overview: Any, risk_state: Any) -> dict[str, Any]:
    overview = overview if isinstance(overview, dict) else _empty_market_overview()
    market_as_of = _iso_date(overview.get("updated"))
    raw_current_signals = _as_list(overview.get("signals"))[:MAX_SIGNALS]
    raw_recent_signals = _as_list(overview.get("recent_signals"))[:MAX_SIGNALS]
    factors = [
        cleaned
        for cleaned in (
            _clean_factor(item)
            for item in _as_list(overview.get("factors"))[:MAX_FACTORS]
        )
        if cleaned is not None
    ]
    current_signals = [
        cleaned
        for cleaned in (_clean_signal(item) for item in raw_current_signals)
        if (
            cleaned is not None
            and bool(overview.get("available"))
            and cleaned["decision_date"] == market_as_of
        )
    ]
    recent_signals = [
        cleaned
        for cleaned in (_clean_signal(item) for item in raw_recent_signals)
        if cleaned is not None
    ]
    raw_latest_decision_date = overview.get("latest_decision_date")
    latest_decision_date = _iso_date(raw_latest_decision_date)
    if latest_decision_date is not None and (
        date.fromisoformat(latest_decision_date) > _today_shanghai()
    ):
        latest_decision_date = None
    inferred_latest_decision_date = max(
        (item["decision_date"] for item in (recent_signals + current_signals)),
        default=None,
    )
    signal_contract_invalid = (
        len(current_signals) != len(raw_current_signals)
        or len(recent_signals) != len(raw_recent_signals)
        or (bool(raw_latest_decision_date) and latest_decision_date is None)
        or (
            inferred_latest_decision_date is not None
            and latest_decision_date != inferred_latest_decision_date
        )
    )
    if inferred_latest_decision_date is not None:
        latest_decision_date = inferred_latest_decision_date
    daily_audit = (
        overview.get("daily_audit")
        if isinstance(overview.get("daily_audit"), dict)
        else None
    )
    risk = _clean_risk(risk_state)
    market_freshness = _market_freshness(overview.get("updated"))

    degraded_reasons = []
    research_pool = overview.get("research_pool")
    if isinstance(research_pool, dict) and research_pool.get("status") != "ok":
        degraded_reasons.append("etf_pool_evidence_incomplete")
    if not overview.get("available"):
        degraded_reasons.append("market_context_unavailable")
    elif not market_freshness["calendar_available"]:
        degraded_reasons.append("market_calendar_unavailable")
    elif market_freshness["is_stale"]:
        degraded_reasons.append("market_context_stale")
    if overview.get("available") and (
        not factors
        or any(
            item.get("value") is None
            or item.get("is_stale")
            or not item.get("collect_available")
            for item in factors
            if isinstance(item, dict)
        )
    ):
        degraded_reasons.append("market_factor_stale_or_unavailable")
    insufficient_factors = [item for item in factors if not item.get("history_ready")]
    if insufficient_factors:
        degraded_reasons.append("factor_history_insufficient")
    if signal_contract_invalid:
        degraded_reasons.append("signal_contract_invalid")
    if overview.get("daily_audit_invalid"):
        degraded_reasons.append("swing_audit_invalid")
    if daily_audit is not None:
        audit_run = daily_audit.get("run") or {}
        audit_status = audit_run.get("status")
        # Internal legacy objects predate this additive field. Rows loaded by
        # this version always include an explicit eligibility decision.
        audit_eligible = audit_run.get("decision_eligible") is not False
        audit_date = daily_audit.get("decision_date")
        if not audit_eligible:
            degraded_reasons.append("swing_audit_outside_decision_window")
        else:
            if audit_status == "running":
                degraded_reasons.append("swing_audit_running")
            elif audit_status == "partial":
                degraded_reasons.append("swing_audit_partial")
            elif audit_status == "failed":
                degraded_reasons.append("swing_audit_failed")
            expected_audit_date = market_freshness.get("expected_as_of")
            if expected_audit_date and audit_date and audit_date < expected_audit_date:
                degraded_reasons.append("swing_audit_stale")
    if overview.get("effectiveness_invalid"):
        degraded_reasons.append("swing_effectiveness_invalid")
    if not risk["available"]:
        degraded_reasons.append("risk_snapshot_unavailable")
    else:
        if not risk["data_complete"]:
            degraded_reasons.append("risk_snapshot_incomplete")
        if risk["is_future"]:
            degraded_reasons.append("risk_snapshot_from_future")
        if risk["is_stale"]:
            degraded_reasons.append("risk_snapshot_stale")

    strategy = _clean_strategy(overview.get("strategy"))
    factor_history_labels = {
        "total_amount": "成交额",
        "margin_growth_pct": "两融增速",
        "limit_ratio": "涨跌停比",
    }
    factor_history_messages = list(
        dict.fromkeys(
            f"{factor_history_labels.get(str(item.get('factor')), item.get('label') or item.get('factor') or '因子')}历史 "
            f"{int(item.get('sample_size') or 0)}/20"
            for item in insufficient_factors
        )
    )
    payload = {
        "schema_version": "quant.market-context.v1",
        "generated_at": datetime.now(tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "status": "degraded" if degraded_reasons else "ok",
        "market": {
            "available": bool(overview.get("available", False)),
            "as_of": market_as_of,
            "expected_as_of": market_freshness["expected_as_of"],
            "age_days": market_freshness["age_days"],
            "is_stale": market_freshness["is_stale"],
            "source": _short_text(overview.get("source"), 64),
            "breadth": {
                "limit_up": _finite_number(overview.get("limit_up")),
                "limit_down": _finite_number(overview.get("limit_down")),
            },
            "margin_balance": _finite_number(overview.get("margin_balance")),
            "factors": factors[:MAX_FACTORS],
            "strategy": strategy,
        },
        "signals": {
            "current": current_signals[:MAX_SIGNALS],
            "latest_decision_date": latest_decision_date,
            "latest_signal_date": latest_decision_date,
            "recent": recent_signals[:MAX_SIGNALS],
        },
        "risk": risk,
        "quality": {
            "degraded_reasons": degraded_reasons,
            "messages": factor_history_messages,
        },
    }
    if isinstance(research_pool, dict):
        payload["research_pool"] = research_pool
    if daily_audit is not None:
        payload["signals"]["daily_audit"] = daily_audit
    elif overview.get("daily_audit_invalid"):
        # Presence matters: consumers distinguish a malformed new-contract row
        # from a legacy producer that has no audit table at all.
        payload["signals"]["daily_audit"] = {"invalid": True}
    effectiveness = overview.get("effectiveness")
    if isinstance(effectiveness, dict):
        payload["signals"]["effectiveness"] = effectiveness
    elif overview.get("effectiveness_invalid"):
        payload["signals"]["effectiveness"] = {"invalid": True}
    return payload


@app.route("/health", methods=["GET"], provide_automatic_options=False)
def health() -> Response:
    return jsonify({"status": "ok", "service": "quant-integration-api"})


@app.route("/ready", methods=["GET"], provide_automatic_options=False)
def ready() -> Response:
    status = "ready" if _readiness_ok() else "not_ready"
    return jsonify({"status": status, "service": "quant-integration-api"}), (
        200 if status == "ready" else 503
    )


@app.route(
    "/integration/v1/market-context",
    methods=["GET"],
    provide_automatic_options=False,
)
def market_context() -> Response:
    try:
        overview = _load_market_overview()
    except Exception:
        logger.warning("Unable to build market context", exc_info=True)
        overview = _empty_market_overview()
    try:
        risk_state = _load_persisted_risk_state()
    except Exception:
        logger.warning("Unable to read risk context", exc_info=True)
        risk_state = {}
    return jsonify(_build_market_context(overview, risk_state))
