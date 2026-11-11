"""Local staged-tree price conversion. No network or production activation.

The caller owns source AS OF proof, transaction locks/generation CAS, calendars,
CSI memberships, cleanup and final atomic activation. Only stage_root/features
and stage_root/instruments/all.txt are written here. old_root is never written.
All existing staged feature bytes must initially equal old_root, including
historical instruments not present in the new market rows. Same/overlapping
roots and symlinked feature paths are rejected; replacements do not modify a
hard-linked old inode. A failed operation may leave an unusable *stage*, never an
approved candidate: the caller must not publish unless this function succeeds.
The caller must fetch the complete stored-anchor-through-feature-tail source
range and reject added/removed valid history dates; selecting only three isolated
anchors cannot prove that an old NaN slot has not since acquired a source price.
Previously observed-active instruments stay in each day's denominator. With
proven absence, all.txt end is an observation boundary, not a last-quote date.
Missing slots contain nine NaNs and an exactly carried factor. Affected symbols'
last quoted dates are separate; partial data is not an assertion of suspension.
"""

from __future__ import annotations

from collections import defaultdict
import importlib.util
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
from typing import Any, Generator, Iterable, Mapping, Sequence


# Load the sibling module without modifying sys.path or importing site packages.
_spec = importlib.util.spec_from_file_location("_qlib_tree_conversion", Path(__file__).with_name("conversion.py"))
conversion = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = conversion
_spec.loader.exec_module(conversion)
FIELDS = conversion.FIELDS
ConversionError = conversion.ConversionError
REFERENCE_SYMBOLS = ("SH600000", "SZ000001", "SH600519", "SZ300750")
ACTIVE_INDEX_SYMBOLS = ("SH000300", "SH000905", "SH000852", "SH000906", "SZ399300")
_LINE = re.compile(r"([^\s]+)([ \t]+)([^\s]+)([ \t]+)([^\s]+)(\r?\n)?\Z")


class TreeValidationError(ValueError):
    pass


def _calendar(values: Sequence[str], label: str) -> tuple[str, ...]:
    return conversion._days(values, label)


def _root(value: os.PathLike[str] | str) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise TreeValidationError("root_must_be_real_directory")
    return path.resolve(strict=True)


def _path(root: Path, *parts: str, must_exist: bool = True, directory: bool = False) -> Path:
    current = root
    for index, part in enumerate(parts):
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise TreeValidationError("unsafe_relative_path")
        current = current / part
        if current.is_symlink():
            raise TreeValidationError("symlink_in_data_path")
        if index < len(parts) - 1 and not current.is_dir():
            raise TreeValidationError("missing_parent_directory")
    if must_exist and (not current.is_dir() if directory else not current.is_file()):
        raise TreeValidationError("missing_data_path")
    return current


def _instruments(root: Path) -> tuple[dict[str, dict[str, Any]], list[str], bytes]:
    data = _path(root, "instruments", "all.txt").read_bytes()
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TreeValidationError("invalid_instrument_encoding") from exc
    lines = text.splitlines(keepends=True)
    records = {}
    for index, line in enumerate(lines):
        match = _LINE.fullmatch(line)
        if match is None:
            raise TreeValidationError("malformed_instrument_line")
        symbol, start, end = match.group(1, 3, 5)
        if not conversion._SYMBOL.fullmatch(symbol) or symbol in records:
            raise TreeValidationError("invalid_or_duplicate_instrument_symbol")
        start, end = conversion._day(start), conversion._day(end)
        if start > end:
            raise TreeValidationError("inverted_instrument_interval")
        records[symbol] = {"start": start, "end": end, "line_index": index}
    if not records:
        raise TreeValidationError("empty_instruments")
    return records, lines, data


def _field_paths(root: Path, symbol: str) -> dict[str, Path]:
    if not isinstance(symbol, str) or not conversion._SYMBOL.fullmatch(symbol):
        raise TreeValidationError("invalid_symbol")
    directory = _path(root, "features", symbol.lower(), directory=True)
    expected = {f"{field}.day.bin" for field in FIELDS}
    if {path.name for path in directory.iterdir()} != expected:
        raise TreeValidationError(f"unknown_or_missing_field:{symbol}")
    return {field: _path(root, "features", symbol.lower(), f"{field}.day.bin") for field in FIELDS}


def _load(root: Path, symbol: str, calendar: Sequence[str]) -> tuple[int, int, dict[str, bytes], dict[str, tuple[float, ...]]]:
    blobs = {field: path.read_bytes() for field, path in _field_paths(root, symbol).items()}
    starts, lengths, arrays = set(), set(), {}
    for field, blob in blobs.items():
        if len(blob) < 8 or len(blob) % 4:
            raise TreeValidationError(f"invalid_feature_binary:{symbol}:{field}")
        start = struct.unpack("<f", blob[:4])[0]
        if not math.isfinite(start) or not start.is_integer() or start < 0:
            raise TreeValidationError(f"invalid_feature_header:{symbol}:{field}")
        length = len(blob) // 4 - 1
        if int(start) + length > len(calendar):
            raise TreeValidationError(f"feature_exceeds_calendar:{symbol}:{field}")
        starts.add(int(start))
        lengths.add(length)
        arrays[field] = struct.unpack_from(f"<{length}f", blob, 4)
    if len(starts) != 1 or len(lengths) != 1:
        raise TreeValidationError(f"misaligned_fields:{symbol}")
    return starts.pop(), lengths.pop(), blobs, arrays


def inspect_symbols(root: os.PathLike[str] | str, symbols: Iterable[str], calendar: Sequence[str], *,
                    tail_gap_allowances: Mapping[str, Sequence[str]] | None = None) -> dict[str, dict[str, Any]]:
    """Inspect only requested symbols; return the exact raw dates to fetch.

    New entries have new=True. Short IPO histories (<=10 old calendar rows and
    insufficient complete anchors) have short_history=True and all old_rows.
    At most ten old-calendar dates beyond the physical feature tail are returned
    as gap_dates, never automatically filled. The all.txt interval must agree.
    Mature entries expose two all-ten-field anchors and their preceding valid
    close date in last3_valid_dates / previous_date. No arbitrary older anchors
    are substituted for an incomplete latest close-bearing row.
    The ten-day bound is unchanged. tail_gap_allowances may only name a symbol
    whose gap equals an exact, separately evidenced date tuple (run.TailGapException);
    any other longer gap, including a near match, still fails.
    """
    root = _root(root)
    calendar = _calendar(calendar, "calendar")
    allowances = _tail_gap_allowances(tail_gap_allowances)
    records, _, _ = _instruments(root)
    result = {}
    for symbol in sorted(set(symbols)):
        if not isinstance(symbol, str) or not conversion._SYMBOL.fullmatch(symbol):
            raise TreeValidationError("invalid_symbol")
        feature = _path(root, "features", symbol.lower(), must_exist=False)
        if symbol not in records:
            if feature.exists():
                raise TreeValidationError(f"unlisted_existing_feature:{symbol}")
            result[symbol] = dict(new=True, short_history=False, start_index=None, length=0,
                                  last3_valid_dates=[], old_anchors=[], previous_date=None, gap_dates=[])
            continue
        start, length, _, arrays = _load(root, symbol, calendar)
        first, last = calendar[start], calendar[start + length - 1]
        if records[symbol]["start"] != first or records[symbol]["end"] != last:
            raise TreeValidationError(f"instrument_feature_interval_mismatch:{symbol}")
        gap_dates = list(calendar[start + length:])
        if len(gap_dates) > 10 and allowances.get(symbol) != tuple(gap_dates):
            raise TreeValidationError(f"historical_tail_gap_exceeds_10_days:{symbol}")
        if length <= 10:
            # A short history needs the full completeness count to decide
            # whether it can supply mature anchors or needs full replay.
            close_valid = [index for index, value in enumerate(arrays["close"])
                           if math.isfinite(value) and value > 0]
            complete = [index for index in close_valid
                        if all(math.isfinite(arrays[field][index]) for field in FIELDS)]
        else:
            # Mature admission uses only the last three close-bearing rows.
            # Do not search for older *complete* substitutes: an incomplete
            # latest anchor must still fail below.
            close_valid = []
            for index in range(length - 1, -1, -1):
                value = arrays["close"][index]
                if math.isfinite(value) and value > 0:
                    close_valid.append(index)
                    if len(close_valid) == 3:
                        break
            close_valid.reverse()
            complete = [index for index in close_valid[-2:]
                        if all(math.isfinite(arrays[field][index]) for field in FIELDS)]
        info = dict(new=False, start_index=start, length=length, first_date=first, last_date=last,
                    old_anchors=[], previous_date=None, last3_valid_dates=[], gap_dates=gap_dates)
        if len(complete) < 3 and length <= 10:
            info.update(short_history=True, old_rows=[{"date": calendar[start + index],
                **{field: arrays[field][index] for field in FIELDS}} for index in range(length)])
        else:
            if len(close_valid) < 3:
                raise TreeValidationError(f"insufficient_mature_valid_history:{symbol}")
            previous, first_anchor, last_anchor = close_valid[-3:]
            if first_anchor not in complete or last_anchor not in complete:
                raise TreeValidationError(f"latest_old_anchors_incomplete:{symbol}")
            info.update(short_history=False, previous_date=calendar[start + previous],
                last3_valid_dates=[calendar[start + index] for index in (previous, first_anchor, last_anchor)],
                old_anchors=[{"date": calendar[start + index],
                    **{field: arrays[field][index] for field in FIELDS}} for index in (first_anchor, last_anchor)])
        result[symbol] = info
    return result


def _tail_gap_allowances(value: Mapping[str, Sequence[str]] | None) -> dict[str, tuple[str, ...]]:
    """Exact per-symbol date tuples only; a short or malformed entry is rejected."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TreeValidationError("invalid_tail_gap_allowances")
    result = {}
    for symbol, days in value.items():
        if not isinstance(symbol, str) or not conversion._SYMBOL.fullmatch(symbol):
            raise TreeValidationError("invalid_tail_gap_allowance_symbol")
        days = tuple(conversion._days(days, "tail_gap_allowance"))
        if len(days) <= 10:
            raise TreeValidationError("tail_gap_allowance_not_needed")
        result[symbol] = days
    return result


def _feature_symbols(root: Path) -> set[str]:
    directory = _path(root, "features", directory=True)
    result = set()
    for path in directory.iterdir():
        symbol = path.name.upper()
        if path.name != symbol.lower() or not conversion._SYMBOL.fullmatch(symbol) or path.is_symlink() or not path.is_dir():
            raise TreeValidationError("invalid_feature_directory")
        result.add(symbol)
    return result


def active_update_symbols(root: os.PathLike[str] | str, base: str, source_symbols: Iterable[str]) -> list[str]:
    """Keep every previously observed-active instrument, including no-row days."""
    root = _root(root)
    base = conversion._day(base)
    records, _, _ = _instruments(root)
    symbols = set(source_symbols)
    if any(not isinstance(symbol, str) or not conversion._SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise TreeValidationError("invalid_source_symbol")
    return sorted(symbols | {symbol for symbol, interval in records.items() if interval["end"] == base})


def _absence_map(source_absence: Mapping[str, Any] | None, calendar: Sequence[str],
                 expected_source_commit: str | None) -> dict[str, tuple[str, ...]]:
    if source_absence is None:
        return {}
    if not isinstance(source_absence, Mapping) or set(source_absence) != {"sourceCommit", "dates"}:
        raise TreeValidationError("invalid_source_absence_envelope")
    ref = source_absence["sourceCommit"]
    conversion._identity("SH600000", ref, expected_source_commit)
    if not isinstance(source_absence["dates"], Mapping):
        raise TreeValidationError("invalid_source_absence_dates")
    result = {}
    for symbol, values in source_absence["dates"].items():
        if not isinstance(symbol, str) or not conversion._SYMBOL.fullmatch(symbol):
            raise TreeValidationError("invalid_source_absence_symbol")
        days = conversion._days(values, "source_absence_dates", allow_empty=True)
        if not set(days) <= set(calendar):
            raise TreeValidationError("source_absence_outside_calendar")
        result[symbol] = days
    return result


def _verify_absence_slots(root: Path, calendar: Sequence[str], absence: Mapping[str, Sequence[str]]) -> None:
    for symbol, days in absence.items():
        if not days:
            continue
        start, length, _, values = _load(root, symbol, calendar)
        for day in days:
            index = calendar.index(day) - start
            if not 1 <= index < length:
                raise TreeValidationError(f"source_absence_before_listing_or_out_of_bounds:{symbol}:{day}")
            if any(not math.isnan(values[field][index]) for field in FIELDS if field != "factor"):
                raise TreeValidationError(f"source_absence_conflicts_with_stored_values:{symbol}:{day}")
            factor, previous = values["factor"][index], values["factor"][index - 1]
            if not math.isfinite(factor) or factor <= 0 or not math.isfinite(previous) or previous <= 0 or factor != previous:
                raise TreeValidationError(f"source_absence_factor_not_carried:{symbol}:{day}")


def _validate_quote(symbol: str, current: Mapping[str, float], *, first_day: bool) -> None:
    for field, value in current.items():
        if not math.isfinite(value) and not (first_day and field == "change" and math.isnan(value)):
            raise TreeValidationError(f"invalid_current_field:{symbol}:{field}")
    for field in ("open", "high", "low", "close", "factor", "volume", "adjclose"):
        if current[field] <= 0:
            raise TreeValidationError(f"nonpositive_current_field:{symbol}:{field}")
    for field in ("amount", "vwap"):
        if current[field] < 0:
            raise TreeValidationError(f"negative_current_field:{symbol}:{field}")
    if not current["low"] <= min(current["open"], current["close"]) <= max(current["open"], current["close"]) <= current["high"]:
        raise TreeValidationError(f"inconsistent_current_ohlc:{symbol}")


def _same_bytes(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as a, second.open("rb") as b:
        while True:
            left, right = a.read(1024 * 1024), b.read(1024 * 1024)
            if left != right:
                return False
            if not left:
                return True


def _verify_initial_copy(old: Path, stage: Path, records: Mapping[str, Any], old_all: bytes) -> int:
    old_symbols, stage_symbols = _feature_symbols(old), _feature_symbols(stage)
    if old_symbols != set(records) or stage_symbols != old_symbols:
        raise TreeValidationError("stage_or_instrument_feature_inventory_mismatch")
    if _path(stage, "instruments", "all.txt").read_bytes() != old_all:
        raise TreeValidationError("stage_instruments_not_exact_copy")
    count = 0
    for symbol in sorted(old_symbols):
        old_paths, stage_paths = _field_paths(old, symbol), _field_paths(stage, symbol)
        for field in FIELDS:
            if not _same_bytes(old_paths[field], stage_paths[field]):
                raise TreeValidationError(f"stage_feature_not_exact_copy:{symbol}:{field}")
            count += 1
    return count


def _replace_stage_file(stage: Path, relative: tuple[str, ...], data: bytes, mode: int = 0o644) -> None:
    path = _path(stage, *relative, must_exist=False)
    descriptor, temporary = tempfile.mkstemp(prefix=".qlib-price-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        # Replacement, never truncation: a copied hard link cannot mutate old.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _coverage_steps(root: os.PathLike[str] | str, calendar: Sequence[str], *, min_eligible: int = 5000,
                      min_coverage: float = 0.999,
                      required_symbols: Sequence[str] = REFERENCE_SYMBOLS + ACTIVE_INDEX_SYMBOLS,
                      target_date: str | None = None, source_absence: Mapping[str, Any] | None = None,
                      expected_source_commit: str | None = None) -> Generator[
                          str, tuple[int, int, dict[str, bytes], dict[str, tuple[float, ...]]], dict[str, Any]]:
    """Match the snapshot consumer: four target fields plus prior-day close.

    First listing days are excluded from the market denominator, not synthesized.
    Required reference stocks and active indices must all be complete at target.
    Proven source absence remains in inventory/denominator as invalid, not a
    fabricated quote or a suspension assertion. Reported coverage is still
    valid/eligible. Production-scale gates (min_eligible >= 5000) do not block
    the calendar for a bounded number of proven-absence invalids; unproven holes
    and tiny fixtures still use the raw coverage threshold. Current prices are
    validated even when the prior close is absent. Unexpected nonfinite fields
    are fatal.
    """
    root = _root(root)
    calendar = _calendar(calendar, "calendar")
    if isinstance(min_eligible, bool) or not isinstance(min_eligible, int) or min_eligible < 1:
        raise TreeValidationError("invalid_min_eligible")
    if isinstance(min_coverage, bool) or not isinstance(min_coverage, (int, float)) or not 0 < min_coverage <= 1:
        raise TreeValidationError("invalid_min_coverage")
    records, _, _ = _instruments(root)
    target = calendar[-1] if target_date is None else conversion._day(target_date)
    if target not in calendar:
        raise TreeValidationError("target_outside_calendar")
    target_index = calendar.index(target)
    absence = _absence_map(source_absence, calendar, expected_source_commit)
    _verify_absence_slots(root, calendar, absence)
    eligible = valid = first_day_checked = 0
    invalid_symbols, invalid_reasons, reference_dates, last_quoted = [], {}, {}, {}
    required = set(required_symbols)
    if not required <= set(records):
        raise TreeValidationError("required_reference_or_index_missing")
    for symbol, interval in records.items():
        included = interval["start"] < target <= interval["end"]
        first_day = interval["start"] == target <= interval["end"]
        if not included and not first_day and symbol not in required:
            continue
        # The driver supplies this symbol's read-only feature snapshot. The
        # single-date driver reads exactly as before; the batch driver shares
        # only this one symbol between independent per-day validators.
        start = length = values = _ = None
        start, length, _, values = yield symbol
        index = target_index - start
        in_bounds = index >= (0 if first_day else 1) and index < len(values["close"])
        if not in_bounds:
            if symbol in required:
                raise TreeValidationError(f"required_reference_or_index_not_at_target:{symbol}")
            raise TreeValidationError(f"eligible_feature_out_of_bounds:{symbol}")
        current = {field: values[field][index] for field in FIELDS}
        reasons = []
        if target in absence.get(symbol, ()):
            if first_day:
                raise TreeValidationError(f"first_listing_day_incomplete:{symbol}")
            reasons.append("source_absent_today")
        else:
            _validate_quote(symbol, current, first_day=first_day)
        if not first_day:
            previous_day = calendar[target_index - 1]
            previous_close = values["close"][index - 1]
            if previous_day in absence.get(symbol, ()):
                reasons.append("prior_close_source_absent")
            elif not math.isfinite(previous_close) or previous_close <= 0:
                raise TreeValidationError(f"unproven_or_invalid_previous_close:{symbol}")
        complete = not reasons
        if absence.get(symbol):
            quotes = [offset for offset in range(min(index + 1, length))
                      if math.isfinite(values["close"][offset]) and values["close"][offset] > 0]
            if not quotes:
                raise TreeValidationError(f"missing_initial_quote:{symbol}")
            last_quoted[symbol] = calendar[start + quotes[-1]]
        if first_day:
            if not complete:
                raise TreeValidationError(f"first_listing_day_incomplete:{symbol}")
            first_day_checked += 1
        if symbol in required:
            if not included or not complete:
                raise TreeValidationError(f"required_reference_or_index_not_at_target:{symbol}")
            reference_dates[symbol] = target
        if included:
            eligible += 1
            if complete:
                valid += 1
            else:
                invalid_symbols.append(symbol)
                invalid_reasons[symbol] = reasons
    coverage = valid / eligible if eligible else 0.0
    proven_reasons = {"source_absent_today", "prior_close_source_absent"}
    proven_only = bool(invalid_symbols) and all(
        set(invalid_reasons[symbol]) <= proven_reasons for symbol in invalid_symbols)
    absence_bound = max(20, int(eligible * 0.005)) if min_eligible >= 5000 else 0
    within_absence_bound = proven_only and len(invalid_symbols) <= absence_bound
    if eligible < min_eligible or (coverage < min_coverage and not within_absence_bound):
        sample = ",".join(sorted(invalid_symbols)[:8])
        suffix = f":invalid={sample}" if sample else ""
        if proven_only and min_eligible >= 5000 and len(invalid_symbols) > absence_bound:
            raise TreeValidationError(
                f"source_absence_exceeds_bound:invalid={len(invalid_symbols)}:bound={absence_bound}{suffix}")
        raise TreeValidationError(
            f"market_coverage_incomplete:eligible={eligible}:valid={valid}:coverage={coverage:.9f}{suffix}")
    return dict(target_date=target, status="partial" if invalid_symbols else "complete",
                eligible=eligible, valid=valid, invalid=len(invalid_symbols), invalid_reasons=invalid_reasons,
                first_day_listings_checked=first_day_checked,
                coverage=coverage, invalid_symbols=invalid_symbols, reference_dates=reference_dates,
                sourceAbsentDates={symbol: list(dates) for symbol, dates in absence.items()}, lastQuotedDate=last_quoted)


def validate_coverage(root: os.PathLike[str] | str, calendar: Sequence[str], *, min_eligible: int = 5000,
                      min_coverage: float = 0.999,
                      required_symbols: Sequence[str] = REFERENCE_SYMBOLS + ACTIVE_INDEX_SYMBOLS,
                      target_date: str | None = None, source_absence: Mapping[str, Any] | None = None,
                      expected_source_commit: str | None = None) -> dict[str, Any]:
    """Validate one date with the same admission gates and fresh file reads."""
    root = _root(root)
    calendar = _calendar(calendar, "calendar")
    steps = _coverage_steps(root, calendar, min_eligible=min_eligible, min_coverage=min_coverage,
        required_symbols=required_symbols, target_date=target_date, source_absence=source_absence,
        expected_source_commit=expected_source_commit)
    try:
        symbol = next(steps)
        while True:
            symbol = steps.send(_load(root, symbol, calendar))
    except StopIteration as done:
        return done.value
    finally:
        steps.close()


def validate_coverage_days(root: os.PathLike[str] | str, calendar: Sequence[str], *,
                           target_dates: Sequence[str], min_eligible: int = 5000,
                           min_coverage: float = 0.999,
                           required_symbols: Sequence[str] = REFERENCE_SYMBOLS + ACTIVE_INDEX_SYMBOLS,
                           source_absence: Mapping[str, Any] | None = None,
                           expected_source_commit: str | None = None) -> list[dict[str, Any]]:
    """Independently validate all appended dates, reading each symbol once.

    This is a streaming batch, not a feature cache: only one symbol's immutable
    blobs and float tuples are live at a time. Every day executes the unchanged
    per-day validator and coverage threshold. Exceptions are retained in day
    order, so a later day's failure cannot hide an earlier day's first failure.
    No snapshot survives this call or is reused by consumers/publication checks.
    """
    root = _root(root)
    calendar = _calendar(calendar, "calendar")
    targets = conversion._days(target_dates, "coverage_target_dates")
    records, _, _ = _instruments(root)
    order = {symbol: index for index, symbol in enumerate(records)}
    steps, pending, results, errors = [], {}, {}, {}

    def remember_error(index: int, exc: Exception) -> None:
        # Keep the original exception type/arguments and day priority, but not
        # failed generator/load frames containing full feature histories.
        # Chained exceptions can retain the same frames, including through
        # implicit context or a cycle, so clear tracebacks throughout the chain.
        # BaseException is deliberately not caught here or by either driver.
        chain, seen = [exc], set()
        while chain:
            error = chain.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            error.__traceback__ = None
            for linked in (error.__cause__, error.__context__):
                if linked is not None:
                    chain.append(linked)
        errors[index] = exc
        # A known day failure makes every later day irrelevant. Earlier days
        # must still find their own first error, preserving sequential order.
        earliest = min(errors)
        for later in tuple(pending):
            if later > earliest:
                pending.pop(later)
                steps[later].close()

    def advance(index: int, value: Any = None, *, first: bool = False) -> None:
        try:
            symbol = next(steps[index]) if first else steps[index].send(value)
            if symbol not in order:
                raise TreeValidationError("coverage_inventory_changed_during_batch")
            pending[index] = symbol
        except StopIteration as done:
            pending.pop(index, None)
            results[index] = done.value
        except Exception as exc:
            pending.pop(index, None)
            remember_error(index, exc)

    try:
        for index, target in enumerate(targets):
            steps.append(_coverage_steps(root, calendar, min_eligible=min_eligible,
                min_coverage=min_coverage, required_symbols=required_symbols,
                target_date=target, source_absence=source_absence,
                expected_source_commit=expected_source_commit))
            advance(index, first=True)
            if errors:
                break
        while pending:
            symbol = min(pending.values(), key=order.__getitem__)
            waiting = [index for index, requested in pending.items() if requested == symbol]
            try:
                loaded = _load(root, symbol, calendar)
            except Exception as exc:
                for index in waiting:
                    if index not in pending:
                        continue
                    pending.pop(index, None)
                    remember_error(index, exc)
            else:
                for index in waiting:
                    if index not in pending:
                        continue
                    advance(index, loaded)
                # Do not accumulate histories for other symbols/days.
                del loaded
        for index in range(len(targets)):
            if index in errors:
                raise errors[index]
        return [results[index] for index in range(len(targets))]
    finally:
        for step in steps:
            step.close()


def validate_tree(root: os.PathLike[str] | str, calendar: Sequence[str], target_date: str,
                  min_eligible: int = 5000, min_coverage: float = 0.999, *,
                  required_symbols: Sequence[str] = REFERENCE_SYMBOLS + ACTIVE_INDEX_SYMBOLS,
                  source_absence: Mapping[str, Any] | None = None,
                  expected_source_commit: str | None = None) -> dict[str, Any]:
    """Read-only admission gate, including the updater's already-current branch."""
    calendar = _calendar(calendar, "calendar")
    if conversion._day(target_date) != calendar[-1]:
        raise TreeValidationError("target_is_not_calendar_price_boundary")
    return validate_coverage(root, calendar, min_eligible=min_eligible, min_coverage=min_coverage,
                             required_symbols=required_symbols, source_absence=source_absence,
                             expected_source_commit=expected_source_commit)


def apply_prices(old_root: os.PathLike[str] | str, stage_root: os.PathLike[str] | str,
                 old_calendar: Sequence[str], new_calendar: Sequence[str], *,
                 source_commit: str, expected_source_commit: str,
                 raw_rows: Sequence[Mapping[str, Any]], first_dates: Mapping[str, str],
                 confirmed_missing_history: Mapping[str, Sequence[str]] | None = None,
                 confirmed_missing_history_source_commit: str | None = None,
                 confirmed_missing_new_days: Mapping[str, Sequence[str]] | None = None,
                 confirmed_missing_new_days_source_commit: str | None = None,
                 source_absence: Mapping[str, Any] | None = None,
                 min_eligible: int = 5000, min_coverage: float = 0.999,
                 required_symbols: Sequence[str] = REFERENCE_SYMBOLS + ACTIVE_INDEX_SYMBOLS,
                 tail_gap_allowances: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """Append verified prices in an exact staged copy; return validated summary.

    raw_rows includes the complete new-day market, old raw anchor/previous rows,
    and, for short IPO histories, the complete <=10-day old raw range. Each row
    must declare the same source_commit and a canonical uppercase symbol/date.
    first_dates is an AS OF full-history MIN(tradedate), required only for new
    symbols and short histories. confirmed_missing_history is independently
    counted complete-AS-OF absence, bound to confirmed_missing_history_source_commit;
    it may cover only inspected <=10-day old tail gaps, never stored/new dates.
    confirmed_missing_new_days separately proves absence on appended days, never
    an IPO's first quote or earlier dates. source_absence is the shared envelope,
    including already-stored NaN dates needed to validate a missing prior close.
    Every appended day must independently pass the unchanged coverage threshold.
    No missing raw price or absent first date is guessed.
    """
    old, stage = _root(old_root), _root(stage_root)
    if old == stage or old in stage.parents or stage in old.parents:
        raise TreeValidationError("stage_must_be_distinct_nonoverlapping_real_directory")
    old_days, days = _calendar(old_calendar, "old_calendar"), _calendar(new_calendar, "new_calendar")
    if days[:len(old_days)] != old_days or len(days) <= len(old_days):
        raise TreeValidationError("calendar_is_not_exact_append")
    append_days = days[len(old_days):]
    conversion._identity("SH600000", source_commit, expected_source_commit)
    records, lines, old_all = _instruments(old)
    grouped: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise TreeValidationError("invalid_raw_row")
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not conversion._SYMBOL.fullmatch(symbol):
            raise TreeValidationError("invalid_raw_symbol")
        if row.get("source_commit") != source_commit:
            raise TreeValidationError("raw_source_commit_missing_or_mixed")
        day = conversion._row_day(row)
        if day not in days:
            raise TreeValidationError("raw_day_outside_calendar")
        if day in grouped[symbol]:
            raise TreeValidationError("duplicate_raw_symbol_day")
        grouped[symbol][day] = row
    source_symbols = sorted(symbol for symbol, rows in grouped.items() if any(day > old_days[-1] for day in rows))
    symbols = active_update_symbols(old, old_days[-1], source_symbols)
    if not symbols:
        raise TreeValidationError("no_observed_active_symbols")
    inspections = inspect_symbols(old, symbols, old_days, tail_gap_allowances=tail_gap_allowances)
    if confirmed_missing_history is None:
        confirmed_missing_history = {}
    if not isinstance(confirmed_missing_history, Mapping):
        raise TreeValidationError("invalid_missing_history_evidence")
    if confirmed_missing_history and confirmed_missing_history_source_commit != source_commit:
        raise TreeValidationError("missing_history_evidence_source_mismatch")
    missing_history = {}
    for symbol, missing in confirmed_missing_history.items():
        if symbol not in inspections or inspections[symbol]["new"]:
            raise TreeValidationError("missing_history_evidence_unknown_or_new_symbol")
        dates = conversion._days(missing, "confirmed_missing_history", allow_empty=True)
        if not dates:
            continue
        if not set(dates) <= set(inspections[symbol]["gap_dates"]):
            raise TreeValidationError("missing_history_evidence_outside_old_tail_gap")
        if set(dates) & set(grouped[symbol]):
            raise TreeValidationError("missing_history_evidence_conflicts_with_source_row")
        missing_history[symbol] = dates
    # An allowed long gap must be wholly source-absent: no partial fills.
    for symbol, allowed in _tail_gap_allowances(tail_gap_allowances).items():
        if symbol in inspections and tuple(inspections[symbol]["gap_dates"]) == allowed:
            if tuple(missing_history.get(symbol, ())) != allowed or set(grouped[symbol]) & set(allowed):
                raise TreeValidationError(f"tail_gap_allowance_requires_complete_absence:{symbol}")
    if confirmed_missing_new_days is None:
        confirmed_missing_new_days = {}
    if not isinstance(confirmed_missing_new_days, Mapping):
        raise TreeValidationError("invalid_missing_new_evidence")
    if confirmed_missing_new_days and confirmed_missing_new_days_source_commit != source_commit:
        raise TreeValidationError("missing_new_evidence_source_mismatch")
    missing_new = {}
    for symbol, missing in confirmed_missing_new_days.items():
        if symbol not in inspections:
            raise TreeValidationError("missing_new_evidence_unknown_symbol")
        dates = conversion._days(missing, "confirmed_missing_new_days", allow_empty=True)
        if not dates:
            continue
        if not set(dates) <= set(append_days):
            raise TreeValidationError("missing_new_evidence_outside_new_days")
        if inspections[symbol]["new"]:
            first = conversion._day(first_dates.get(symbol))
            if any(day <= first for day in dates):
                raise TreeValidationError("missing_new_evidence_before_first_quote")
        if set(dates) & set(grouped[symbol]):
            raise TreeValidationError("missing_new_evidence_conflicts_with_source_row")
        missing_new[symbol] = dates
    merged_absence = {symbol: tuple(sorted(set(missing_history.get(symbol, ())) | set(missing_new.get(symbol, ()))))
                      for symbol in sorted(set(missing_history) | set(missing_new))}
    if source_absence is None:
        source_absence = {"sourceCommit": source_commit, "dates": {symbol: list(dates) for symbol, dates in merged_absence.items()}}
    absence = _absence_map(source_absence, days, source_commit)
    for symbol, proven_dates in merged_absence.items():
        if not set(proven_dates) <= set(absence.get(symbol, ())):
            raise TreeValidationError("source_absence_envelope_missing_conversion_proof")
    for symbol, proven_dates in absence.items():
        if symbol not in inspections:
            raise TreeValidationError("source_absence_outside_observed_inventory")
        first = first_dates.get(symbol) if inspections[symbol]["new"] else inspections[symbol]["first_date"]
        first = conversion._day(first)
        if any(day <= first for day in proven_dates):
            raise TreeValidationError("source_absence_before_first_quote")
        if set(proven_dates) & set(grouped[symbol]):
            raise TreeValidationError("source_absence_conflicts_with_source_row")
    initial_files = _verify_initial_copy(old, stage, records, old_all)
    converted = {}
    new_count = short_count = 0
    # Validate EVERY conversion before making the first staged write.
    for symbol in symbols:
        info, rows = inspections[symbol], grouped[symbol]
        source_new_days = sorted(day for day in rows if day > old_days[-1])
        if not source_new_days and not missing_new.get(symbol):
            raise TreeValidationError(f"active_symbols_missing_new_source:{symbol}")
        if days[-1] not in rows and days[-1] not in missing_new.get(symbol, ()):
            raise TreeValidationError(f"source_missing_target_day:{symbol}")
        new_proof = dict(confirmed_missing_new_dates=missing_new.get(symbol, ()),
                         missing_new_source_commit=confirmed_missing_new_days_source_commit)
        if info["new"]:
            first = first_dates.get(symbol)
            if first is None:
                raise TreeValidationError(f"new_symbol_missing_source_min:{symbol}")
            selected_days = tuple(day for day in append_days if first <= day)
            result = conversion.convert_new_symbol(symbol=symbol, source_commit=source_commit,
                expected_source_commit=expected_source_commit, authoritative_first_date=first,
                old_calendar_end=old_days[-1], new_rows=[rows[day] for day in selected_days if day in rows],
                new_dates=selected_days, **new_proof)
            new_count += 1
        else:
            selected_days = tuple(info["gap_dates"]) + append_days
            new = [rows[day] for day in selected_days if day in rows]
            gap_proof = dict(historical_missing_dates=missing_history.get(symbol, ()),
                history_boundary=old_days[-1], missing_source_commit=confirmed_missing_history_source_commit, **new_proof)
            if info["short_history"]:
                if symbol not in first_dates:
                    raise TreeValidationError(f"short_history_missing_source_min:{symbol}")
                history_days = [row["date"] for row in info["old_rows"]] + list(selected_days)
                result = conversion.convert_short_existing(symbol=symbol, source_commit=source_commit,
                    expected_source_commit=expected_source_commit, authoritative_first_date=first_dates[symbol],
                    old_rows=info["old_rows"], raw_history=[rows[day] for day in history_days if day in rows],
                    new_dates=selected_days,
                    stored_missing_dates=tuple(day for day in absence.get(symbol, ())
                        if info["first_date"] < day <= info["last_date"]),
                    stored_missing_source_commit=source_commit, **gap_proof)
                short_count += 1
            else:
                missing = [day for day in info["last3_valid_dates"] if day not in rows]
                if missing:
                    raise TreeValidationError(f"missing_raw_overlap:{symbol}:{','.join(missing)}")
                result = conversion.convert_existing(symbol=symbol, source_commit=source_commit,
                    expected_source_commit=expected_source_commit, old_anchors=info["old_anchors"],
                    raw_anchors=[rows[row["date"]] for row in info["old_anchors"]],
                    raw_previous_valid=rows[info["previous_date"]], new_rows=new, new_dates=selected_days, **gap_proof)
        converted[symbol] = result
    prefix_files = 0
    for symbol, result in converted.items():
        info = inspections[symbol]
        if info["new"]:
            directory = _path(stage, "features", symbol.lower(), must_exist=False)
            directory.mkdir(mode=0o755)
            for field in FIELDS:
                blob = conversion.encode_new_feature(start_index=days.index(result.dates[0]), values=result.values[field])
                _replace_stage_file(stage, ("features", symbol.lower(), f"{field}.day.bin"), blob)
        else:
            _, _, old_blobs, _ = _load(old, symbol, old_days)
            old_paths = _field_paths(old, symbol)
            feature_calendar_prefix = old_days[:info["start_index"] + info["length"]]
            for field in FIELDS:
                blob = conversion.append_feature_bytes(old_blobs[field], old_calendar=feature_calendar_prefix,
                    new_calendar=days, append_dates=result.dates, appended_bytes=result.append_bytes[field])
                _replace_stage_file(stage, ("features", symbol.lower(), f"{field}.day.bin"), blob,
                    stat.S_IMODE(old_paths[field].stat().st_mode))
                prefix_files += 1
    appended = []
    for symbol, result in converted.items():
        if inspections[symbol]["new"]:
            appended.append(f"{symbol}\t{result.dates[0]}\t{result.dates[-1]}\n")
        else:
            index = records[symbol]["line_index"]
            match = _LINE.fullmatch(lines[index])
            lines[index] = "".join(match.group(i) for i in (1, 2, 3, 4)) + result.dates[-1] + (match.group(6) or "")
    existing_text = "".join(lines)
    if appended and not existing_text.endswith("\n"):
        existing_text += "\n"
    all_bytes = (existing_text + "".join(appended)).encode("ascii")
    _replace_stage_file(stage, ("instruments", "all.txt"), all_bytes,
                        stat.S_IMODE(_path(old, "instruments", "all.txt").stat().st_mode))
    after, _, _ = _instruments(stage)
    for symbol, interval in records.items():
        if after[symbol]["start"] != interval["start"] or after[symbol]["end"] < interval["end"]:
            raise TreeValidationError("old_instrument_history_rewritten")
        if symbol not in converted and after[symbol]["end"] != interval["end"]:
            raise TreeValidationError("untouched_instrument_interval_changed")
    daily = []
    target_result = None
    for target_result in validate_coverage_days(stage, days, target_dates=append_days,
            min_eligible=min_eligible, min_coverage=min_coverage, required_symbols=required_symbols,
            source_absence=source_absence, expected_source_commit=source_commit):
        daily.append({key: value for key, value in target_result.items() if key not in ("sourceAbsentDates", "lastQuotedDate")})
    result = dict(source_commit=source_commit, base_date=old_days[-1], **target_result,
                daily_coverage=daily,
                updated_symbols=len(converted) - new_count, new_symbols=new_count,
                short_history_symbols=short_count, untouched_symbols=len(records) - (len(converted) - new_count),
                historical_tail_gaps={symbol: info["gap_dates"] for symbol, info in inspections.items() if info["gap_dates"]},
                source_absent_history={symbol: list(dates) for symbol, dates in missing_history.items()},
                updated_symbols_ending_before_target=[symbol for symbol, result in converted.items() if result.dates[-1] < days[-1]],
                initial_feature_files_verified=initial_files, old_prefix_files_preserved=prefix_files)
    result["status"] = "partial" if any(day["invalid"] for day in daily) else "complete"
    return result
