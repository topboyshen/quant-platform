"""Pure, fail-closed conversion for the chenditc Qlib daily-data format.

No network, filesystem, database, provider, or subprocess operations occur here.
The caller must obtain every raw row, previous-valid-row fact, source MIN date,
calendar and membership fact from ONE immutable Dolt AS OF commit. A release tag
or an observation of a mutable branch is not that proof.

Source semantics: investment_data/qlib/normalize.py and the Yahoo normalizer in
Qlib b87a2c294d364a33fb739359886acffe8ec907d1. In particular ``change`` is based
on unadjusted closes, ``adjclose`` and ``amount`` are not scaled, and source
VWAP is amount / raw_volume * 10. The upstream 100x correction heuristic is
deliberately unsupported: detecting it blocks this bounded incremental path.

Existing symbols require the LAST TWO valid stored anchors, plus the immediately
previous valid raw close for the first anchor. Completeness of these facts is a
caller obligation, not something two supplied rows can establish by themselves.
All ten fields of both anchors must agree; a raw-history adjustment/revision is
not repaired by rewriting the old feature bytes. The scale reconstructed from
float32 anchors is checked, not claimed to reproduce a full rebuild bit-for-bit.

Missing source prices are never fabricated. Explicit zero/null-volume source rows
use upstream suspension semantics (NaN values, carried factor), not stale prices.
Same-commit confirmed absence has separate history-tail and new-day contracts.
Both emit NaN, never prices or assertions of market suspension. The caller must
retain observed-active inventory and enforce per-day quote/prior-close coverage.
Calendar extension and CSI constituents must be separately checked at the same
commit. Historical inactive instruments must retain their old interval and bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import math
import re
import struct
from typing import Any, Mapping, Sequence


FIELDS = ("adjclose", "amount", "change", "close", "factor", "high", "low", "open", "volume", "vwap")
RAW_FIELDS = ("open", "high", "low", "close", "adjclose", "volume", "amount")
_RAW_UNSCALED = frozenset(("adjclose", "amount"))
_COMMIT = re.compile(r"[0-9a-v]{32}\Z")
_SYMBOL = re.compile(r"(?:SH|SZ|BJ)[0-9]{6}\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class ConversionError(ValueError):
    """Input or provenance is insufficient to append safely."""


@dataclass(frozen=True)
class ConversionResult:
    symbol: str
    source_commit: str
    dates: tuple[str, ...]
    values: Mapping[str, tuple[float, ...]]
    append_bytes: Mapping[str, bytes]
    scale: float
    overlap_dates: tuple[str, ...]
    source_absent_dates: tuple[str, ...] = ()


def _day(value: Any) -> str:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise ConversionError("invalid_date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise ConversionError("invalid_date") from exc
    return value


def _validate_days_tuple(values: tuple[str, ...], label: str, allow_empty: bool) -> tuple[str, ...]:
    result = tuple(_day(value) for value in values)
    if (not result and not allow_empty) or any(a >= b for a, b in zip(result, result[1:])):
        raise ConversionError(f"unordered_or_empty_{label}")
    return result


@lru_cache(maxsize=128)
def _validated_days_cached(values: tuple[str, ...], label: str, allow_empty: bool) -> tuple[str, ...]:
    """Only immutable tuples of exact strings enter this bounded calendar cache."""
    return _validate_days_tuple(values, label, allow_empty)


def _days(values: Sequence[str], label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ConversionError(f"invalid_{label}")
    # Copy mutable input on every call. Never cache by list identity or let
    # unhashable/custom string elements participate in an LRU cache key.
    snapshot = tuple(values)
    if all(type(value) is str for value in snapshot):
        return _validated_days_cached(snapshot, label, allow_empty)
    return _validate_days_tuple(snapshot, label, allow_empty)


def _number(value: Any, label: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConversionError(f"invalid_number:{label}")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ConversionError(f"invalid_number:{label}") from exc
    if not math.isfinite(result):
        raise ConversionError(f"nonfinite_number:{label}")
    return result


def _positive(value: Any, label: str) -> float:
    result = _number(value, label)
    if result is None or result <= 0:
        raise ConversionError(f"nonpositive_number:{label}")
    return result


def _f32(value: float, *, allow_nan: bool = False) -> float:
    if math.isnan(value) and allow_nan:
        return value
    if not math.isfinite(value):
        raise ConversionError("nonfinite_output")
    try:
        rounded = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise ConversionError("float32_overflow") from exc
    if not math.isfinite(rounded) or (value != 0 and rounded == 0):
        raise ConversionError("float32_overflow_or_underflow")
    return rounded


def _identity(symbol: str, source_commit: str, expected_source_commit: str) -> None:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ConversionError("invalid_symbol")
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise ConversionError("unfixed_source_commit")
    if source_commit != expected_source_commit:
        raise ConversionError("mixed_source_commit")


def _row_day(row: Mapping[str, Any]) -> str:
    if not isinstance(row, Mapping):
        raise ConversionError("invalid_row")
    value = row.get("date", row.get("tradedate"))
    if "date" in row and "tradedate" in row and row["date"] != row["tradedate"]:
        raise ConversionError("conflicting_row_dates")
    return _day(value)


def _raw(row: Mapping[str, Any], symbol: str, commit: str) -> dict[str, Any]:
    day = _row_day(row)
    if "symbol" in row and row["symbol"] != symbol:
        raise ConversionError("mixed_symbol")
    if "source_commit" in row and row["source_commit"] != commit:
        raise ConversionError("mixed_source_commit")
    if any(field not in row for field in RAW_FIELDS):
        raise ConversionError("missing_raw_field")
    result = {field: _number(row[field], field, nullable=True) for field in RAW_FIELDS}
    result["date"] = day
    volume = result["volume"]
    result["paused"] = volume is None or volume <= 0
    if result["paused"]:
        return result
    for field in ("open", "high", "low", "close", "adjclose"):
        result[field] = _positive(result[field], field)
    amount = result["amount"]
    if amount is None or amount < 0:
        raise ConversionError("invalid_amount")
    if not result["low"] <= min(result["open"], result["close"]) <= max(result["open"], result["close"]) <= result["high"]:
        raise ConversionError("inconsistent_ohlc")
    computed_vwap = amount / volume * 10
    if "vwap" in row and not math.isclose(_number(row["vwap"], "raw_vwap"), computed_vwap, rel_tol=1e-12, abs_tol=1e-12):
        raise ConversionError("source_vwap_mismatch")
    return result


def _ordered_raw(rows: Sequence[Mapping[str, Any]], symbol: str, commit: str, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    result = [_raw(row, symbol, commit) for row in rows]
    _days([row["date"] for row in result], "raw_rows", allow_empty=allow_empty)
    return result


def _align_proven_history(rows: Sequence[Mapping[str, Any]], days: tuple[str, ...], *, symbol: str, commit: str,
                         historical_missing_dates: Sequence[str] = (), history_boundary: str | None = None,
                         missing_source_commit: str | None = None,
                         missing_after: str | None = None, confirmed_missing_new_dates: Sequence[str] = (),
                         missing_new_source_commit: str | None = None,
                         stored_missing_dates: Sequence[str] = (), stored_history_dates: Sequence[str] = (),
                         stored_missing_source_commit: str | None = None) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Represent independently proven historical/new source absence as NaN.

    Absence is not a halt/delisting assertion and not a synthetic raw price row.
    The caller must have an exhaustive same-AS-OF range/count result; a missing
    item in an incomplete response is not proof. Separate history/new proofs
    cannot cross the old boundary. Existing stored dates are never replaced.
    """
    missing = _days(historical_missing_dates, "historical_missing_dates", allow_empty=True)
    if missing:
        if missing_source_commit != commit:
            raise ConversionError("missing_history_evidence_source_mismatch")
        boundary = _day(history_boundary)
        if any(day not in days or day > boundary or (missing_after is not None and day <= missing_after) for day in missing):
            raise ConversionError("missing_history_evidence_outside_boundary")
    new_missing = _days(confirmed_missing_new_dates, "confirmed_missing_new_dates", allow_empty=True)
    if new_missing:
        if missing_new_source_commit != commit:
            raise ConversionError("missing_new_evidence_source_mismatch")
        boundary = _day(history_boundary)
        if any(day not in days or day <= boundary or (missing_after is not None and day <= missing_after) for day in new_missing):
            raise ConversionError("missing_new_evidence_outside_boundary")
    stored_missing = _days(stored_missing_dates, "stored_missing_dates", allow_empty=True)
    if stored_missing:
        if stored_missing_source_commit != commit:
            raise ConversionError("stored_missing_evidence_source_mismatch")
        stored_days = _days(stored_history_dates, "stored_history_dates")
        if any(day not in stored_days or day == stored_days[0] for day in stored_missing):
            raise ConversionError("stored_missing_evidence_outside_old_history")
        if set(stored_missing) & (set(missing) | set(new_missing)):
            raise ConversionError("stored_missing_evidence_overlaps_append_proof")
    missing = tuple(sorted(missing + new_missing + stored_missing))
    parsed = _ordered_raw(rows, symbol, commit, allow_empty=bool(missing))
    lookup = {row["date"]: row for row in parsed}
    if set(lookup) & set(missing):
        raise ConversionError("missing_history_evidence_conflicts_with_source_row")
    if set(lookup) | set(missing) != set(days):
        raise ConversionError("missing_or_extra_source_dates")
    # _normalized treats this typed absence marker as a calendar NaN slot. No
    # prices, volume or amount are invented, and its previous valid state carries.
    return [lookup[day] if day in lookup else {"date": day, "paused": True, "source_absent": True}
            for day in days], missing


def _ordered_f32(value: float) -> int:
    bits = struct.unpack("<I", struct.pack("<f", _f32(value)))[0]
    return 0x80000000 - (bits & 0x7FFFFFFF) if bits & 0x80000000 else 0x80000000 + bits


def _matches(field: str, observed: float, expected: float) -> bool:
    # For raw stored fields, permit at most one float32 ULP; do not hide a
    # corporate-action rebase with a broad price-relative tolerance.
    ulps = abs(_ordered_f32(observed) - _ordered_f32(expected))
    if field in _RAW_UNSCALED:
        return ulps <= 1
    if field == "change":
        return abs(observed - expected) <= 1e-7 and (ulps <= 4 or abs(observed - expected) <= 1e-9)
    return ulps <= 8 and math.isclose(observed, expected, rel_tol=2e-6, abs_tol=0.0)


def _normalized(raw: Mapping[str, Any], *, scale: float, previous_close: float | None,
                previous_factor: float | None) -> tuple[dict[str, float], float | None, float]:
    if raw["paused"]:
        if previous_factor is None:
            raise ConversionError("paused_without_prior_factor")
        values = {field: float("nan") for field in FIELDS}
        values["factor"] = previous_factor
        return values, previous_close, previous_factor
    close = raw["close"]
    change = close / previous_close - 1 if previous_close is not None else float("nan")
    if math.isfinite(change) and 89 <= change <= 111:
        raise ConversionError("upstream_100x_correction_requires_full_normalization")
    factor = raw["adjclose"] / close * scale
    _positive(factor, "factor")
    values = {
        "adjclose": raw["adjclose"],
        "amount": raw["amount"],
        "change": change,
        "factor": factor,
        "volume": raw["volume"] / factor,
        "vwap": raw["amount"] / raw["volume"] * 10 * factor,
    }
    values.update({field: raw[field] * factor for field in ("open", "high", "low", "close")})
    return values, close, factor


def _result(symbol: str, commit: str, days: tuple[str, ...], rows: Sequence[Mapping[str, float]],
            scale: float, overlap_dates: tuple[str, ...], source_absent_dates: tuple[str, ...] = ()) -> ConversionResult:
    values = {field: tuple(_f32(row[field], allow_nan=True) for row in rows) for field in FIELDS}
    blobs = {field: b"".join(struct.pack("<f", value) for value in values[field]) for field in FIELDS}
    return ConversionResult(symbol, commit, days, values, blobs, scale, overlap_dates, source_absent_dates)


def convert_existing(*, symbol: str, source_commit: str, expected_source_commit: str,
                     old_anchors: Sequence[Mapping[str, Any]], raw_anchors: Sequence[Mapping[str, Any]],
                     raw_previous_valid: Mapping[str, Any], new_rows: Sequence[Mapping[str, Any]],
                     new_dates: Sequence[str], historical_missing_dates: Sequence[str] = (),
                     history_boundary: str | None = None,
                     missing_source_commit: str | None = None, confirmed_missing_new_dates: Sequence[str] = (),
                     missing_new_source_commit: str | None = None) -> ConversionResult:
    """Convert a complete appended date range after verifying two old valid days.

    old_anchors: exactly two chronological {date, <all ten FIELDS>} mappings
    raw_anchors/new_rows: {date (or tradedate), <all seven RAW_FIELDS>}
    raw_previous_valid: {date, close}, immediately before the first valid anchor.
    All are factual rows selected AS OF source_commit, not locally synthesized.
    Every new_dates entry requires a source row or confirmed same-commit absence.
    Missing prices stay NaN and do not provide a usable one-day return.
    """
    _identity(symbol, source_commit, expected_source_commit)
    days = _days(new_dates, "new_dates")
    if len(old_anchors) != 2 or len(raw_anchors) != 2:
        raise ConversionError("two_valid_overlap_anchors_required")
    old_days = _days([_row_day(row) for row in old_anchors], "old_anchors")
    anchors = _ordered_raw(raw_anchors, symbol, source_commit)
    if tuple(row["date"] for row in anchors) != old_days or any(row["paused"] for row in anchors):
        raise ConversionError("anchor_date_or_validity_mismatch")
    if old_days[-1] >= days[0]:
        raise ConversionError("new_dates_overlap_history")
    prev_day = _row_day(raw_previous_valid)
    if prev_day >= old_days[0]:
        raise ConversionError("invalid_previous_raw_date")
    if raw_previous_valid.get("source_commit", source_commit) != source_commit:
        raise ConversionError("mixed_source_commit")
    if raw_previous_valid.get("symbol", symbol) != symbol:
        raise ConversionError("mixed_symbol")
    previous_close = _positive(raw_previous_valid.get("close"), "previous_raw_close")
    old_values = []
    for row in old_anchors:
        if any(field not in row for field in FIELDS):
            raise ConversionError("missing_old_field")
        value = {field: _number(row[field], f"old_{field}") for field in FIELDS}
        for field in FIELDS:
            if field != "change" and value[field] <= 0:
                raise ConversionError(f"invalid_old_anchor:{field}")
        old_values.append(value)
    scale = old_values[-1]["close"] / anchors[-1]["adjclose"]
    _positive(scale, "scale")
    previous_factor = None
    for raw, observed in zip(anchors, old_values):
        expected, previous_close, previous_factor = _normalized(raw, scale=scale,
            previous_close=previous_close, previous_factor=previous_factor)
        for field in FIELDS:
            if not _matches(field, observed[field], expected[field]):
                raise ConversionError(f"overlap_mismatch:{raw['date']}:{field}")
    new, missing = _align_proven_history(new_rows, days, symbol=symbol, commit=source_commit,
        historical_missing_dates=historical_missing_dates, history_boundary=history_boundary,
        missing_source_commit=missing_source_commit, missing_after=old_days[-1],
        confirmed_missing_new_dates=confirmed_missing_new_dates, missing_new_source_commit=missing_new_source_commit)
    # The first missing slot carries the exact *stored* factor, not a nearby
    # float32 reconstruction. Later slots carry the preceding emitted factor.
    previous_factor = _f32(old_values[-1]["factor"])
    converted = []
    for raw in new:
        values, previous_close, previous_factor = _normalized(raw, scale=scale,
            previous_close=previous_close, previous_factor=previous_factor)
        converted.append(values)
    return _result(symbol, source_commit, days, converted, scale, old_days, missing)


def convert_new_symbol(*, symbol: str, source_commit: str, expected_source_commit: str,
                       authoritative_first_date: str, old_calendar_end: str,
                       new_rows: Sequence[Mapping[str, Any]], new_dates: Sequence[str],
                       confirmed_missing_new_dates: Sequence[str] = (),
                       missing_new_source_commit: str | None = None) -> ConversionResult:
    """Initialize only a genuinely newly appearing source instrument.

    authoritative_first_date must be MIN(tradedate) over the instrument's full
    history at this exact source commit. It must be after the old calendar end.
    new_dates are the appended calendar suffix starting at that first date, not
    an arbitrary excerpt of a pre-existing listing. First-day change is NaN.
    """
    _identity(symbol, source_commit, expected_source_commit)
    days = _days(new_dates, "new_dates")
    first = _day(authoritative_first_date)
    if first <= _day(old_calendar_end) or first != days[0]:
        raise ConversionError("new_symbol_requires_true_source_history_start")
    rows, missing = _align_proven_history(new_rows, days, symbol=symbol, commit=source_commit,
        confirmed_missing_new_dates=confirmed_missing_new_dates, missing_new_source_commit=missing_new_source_commit,
        history_boundary=old_calendar_end, missing_after=first)
    if rows[0]["paused"]:
        raise ConversionError("new_symbol_first_row_not_valid")
    scale = 1 / rows[0]["adjclose"]
    _positive(scale, "scale")
    converted = []
    previous_close = previous_factor = None
    for raw in rows:
        values, previous_close, previous_factor = _normalized(raw, scale=scale,
            previous_close=previous_close, previous_factor=previous_factor)
        converted.append(values)
    return _result(symbol, source_commit, days, converted, scale, (), missing)


def convert_short_existing(*, symbol: str, source_commit: str, expected_source_commit: str,
                           authoritative_first_date: str, old_rows: Sequence[Mapping[str, Any]],
                           raw_history: Sequence[Mapping[str, Any]],
                           new_dates: Sequence[str], historical_missing_dates: Sequence[str] = (),
                           history_boundary: str | None = None,
                           missing_source_commit: str | None = None, confirmed_missing_new_dates: Sequence[str] = (),
                           missing_new_source_commit: str | None = None,
                           stored_missing_dates: Sequence[str] = (),
                           stored_missing_source_commit: str | None = None) -> ConversionResult:
    """Validate a complete <=10-day IPO history, then return only new values.

    This is not an anchor fallback for mature instruments. The source's full
    MIN(tradedate) must equal the existing feature's actual first calendar date.
    Every old calendar row, including first-day NaN change and explicit pauses,
    is recalculated and checked. Previously stored source-absent slots require
    separate, freshly verified same-ref evidence; tail/new proofs cannot grant
    this authority. The caller retains the original old bytes.
    """
    _identity(symbol, source_commit, expected_source_commit)
    days = _days(new_dates, "new_dates")
    if not 1 <= len(old_rows) <= 10:
        raise ConversionError("short_existing_requires_complete_history_at_most_10_days")
    old_days = _days([_row_day(row) for row in old_rows], "old_rows")
    if _day(authoritative_first_date) != old_days[0] or old_days[-1] >= days[0]:
        raise ConversionError("short_existing_source_history_start_mismatch")
    try:
        raw, missing = _align_proven_history(raw_history, old_days + days, symbol=symbol, commit=source_commit,
            historical_missing_dates=historical_missing_dates, history_boundary=history_boundary,
            missing_source_commit=missing_source_commit, missing_after=old_days[-1],
            confirmed_missing_new_dates=confirmed_missing_new_dates, missing_new_source_commit=missing_new_source_commit,
            stored_missing_dates=stored_missing_dates, stored_history_dates=old_days,
            stored_missing_source_commit=stored_missing_source_commit)
    except ConversionError as exc:
        if str(exc) == "missing_or_extra_source_dates":
            raise ConversionError("short_existing_history_incomplete") from exc
        raise
    if raw[0]["paused"]:
        raise ConversionError("short_existing_first_row_not_valid")
    scale = 1 / raw[0]["adjclose"]
    _positive(scale, "scale")
    previous_close = previous_factor = None
    converted = []
    for index, row in enumerate(raw):
        expected, previous_close, previous_factor = _normalized(row, scale=scale,
            previous_close=previous_close, previous_factor=previous_factor)
        if index < len(old_rows):
            old = old_rows[index]
            if any(field not in old for field in FIELDS):
                raise ConversionError("missing_old_field")
            for field in FIELDS:
                observed = old[field]
                if isinstance(observed, float) and math.isnan(observed):
                    matches = math.isnan(expected[field])
                elif math.isnan(expected[field]):
                    matches = False
                else:
                    observed = _number(observed, f"old_{field}")
                    matches = _matches(field, observed, expected[field])
                if not matches:
                    raise ConversionError(f"short_history_mismatch:{row['date']}:{field}")
            if index == len(old_rows) - 1:
                previous_factor = _f32(old["factor"])
        else:
            converted.append(expected)
    return _result(symbol, source_commit, days, converted, scale, old_days, missing)


def append_feature_bytes(old_blob: bytes, *, old_calendar: Sequence[str], new_calendar: Sequence[str],
                         append_dates: Sequence[str], appended_bytes: bytes) -> bytes:
    """Return old bytes + values only when the old feature reaches the boundary.

    Old NaN bit patterns and header bytes are retained verbatim. This operation
    does not itself infer missing history or write to disk. For a source-proven
    historical tail gap, the caller must pass the actual feature-ending calendar
    prefix and prove every appended slot; the global old calendar is not edited.
    Call separately for all ten fields and retain the same header/start/end.
    """
    old_days = _days(old_calendar, "old_calendar")
    full_days = _days(new_calendar, "new_calendar")
    dates = _days(append_dates, "append_dates")
    if full_days[:len(old_days)] != old_days or full_days[len(old_days):] != dates:
        raise ConversionError("calendar_is_not_exact_append")
    if not isinstance(old_blob, bytes) or len(old_blob) < 8 or len(old_blob) % 4:
        raise ConversionError("invalid_old_binary")
    start = struct.unpack("<f", old_blob[:4])[0]
    if not math.isfinite(start) or not start.is_integer() or start < 0:
        raise ConversionError("invalid_old_start_index")
    if int(start) + (len(old_blob) // 4 - 1) != len(old_days):
        raise ConversionError("old_feature_does_not_reach_calendar_boundary")
    if not isinstance(appended_bytes, bytes) or len(appended_bytes) != 4 * len(dates):
        raise ConversionError("invalid_appended_binary_length")
    if any(math.isinf(value[0]) for value in struct.iter_unpack("<f", appended_bytes)):
        raise ConversionError("infinite_appended_binary")
    return old_blob + appended_bytes


def encode_new_feature(*, start_index: int, values: Sequence[float]) -> bytes:
    """Encode Qlib's one float32 calendar-index header followed by daily values."""
    if isinstance(start_index, bool) or not isinstance(start_index, int) or not 0 <= start_index <= 2**24:
        raise ConversionError("invalid_new_start_index")
    if not values:
        raise ConversionError("empty_new_feature")
    encoded = []
    for value in values:
        # NaN is intentional only for source-explicit pauses / first-day change.
        numeric = value if isinstance(value, float) and math.isnan(value) else _number(value, "feature")
        encoded.append(struct.pack("<f", _f32(numeric, allow_nan=True)))
    return struct.pack("<f", start_index) + b"".join(encoded)
