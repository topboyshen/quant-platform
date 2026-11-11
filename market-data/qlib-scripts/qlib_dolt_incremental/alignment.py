"""Read-only CSI/price alignment with precise same-source absence exceptions.

A source-absent slot is not a price, usable daily return, or suspension claim.
The caller proves complete AS OF source queries. This module binds that proof
to the generation and verifies the installed bytes; it does not infer absence.
"""
from datetime import date
import math
from pathlib import Path
import re
import struct

from atomic import read_small
from conversion import FIELDS
from indices import INDEX_MAP, IndicesError, interval_symbol

SYMBOL = re.compile(r"(?:SH|SZ|BJ)[0-9]{6}\Z")
REF = re.compile(r"[0-9a-v]{32}\Z")


def _day(value):
    if not isinstance(value, str):
        raise ValueError("invalid_alignment_date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("invalid_alignment_date") from None
    return value


def _absence(evidence, expected_commit, calendar):
    if evidence is None:
        return {}
    if (not isinstance(evidence, dict) or not {"sourceCommit", "dates"} <= set(evidence)
            or not isinstance(expected_commit, str) or REF.fullmatch(expected_commit) is None
            or evidence["sourceCommit"] != expected_commit or not isinstance(evidence["dates"], dict)):
        raise ValueError("index_source_absence_identity_invalid")
    result = {}
    for symbol, days in evidence["dates"].items():
        if not isinstance(symbol, str) or SYMBOL.fullmatch(symbol) is None or not isinstance(days, list):
            raise ValueError("index_source_absence_dates_invalid")
        days = [_day(day) for day in days]
        if not days or days != sorted(set(days)) or not set(days) <= set(calendar):
            raise ValueError("index_source_absence_dates_invalid")
        result[symbol] = set(days)
    return result


def _legacy(evidence, expected_commit, calendar):
    if evidence is None:
        return {}, None
    if (not isinstance(evidence, dict) or not {"sourceCommit", "baseDate", "dates", "lastQuotedDates"} <= set(evidence)
            or not isinstance(expected_commit, str) or REF.fullmatch(expected_commit) is None
            or evidence["sourceCommit"] != expected_commit or not isinstance(evidence["dates"], dict)
            or not isinstance(evidence["lastQuotedDates"], dict)
            or set(evidence["dates"]) != set(evidence["lastQuotedDates"])):
        raise ValueError("legacy_unquoted_identity_invalid")
    base = _day(evidence["baseDate"])
    if base not in calendar or base >= calendar[-1]:
        raise ValueError("legacy_unquoted_base_invalid")
    new_days = calendar[calendar.index(base) + 1:]
    result = {}
    for symbol, days in evidence["dates"].items():
        if not isinstance(symbol, str) or SYMBOL.fullmatch(symbol) is None or days != new_days:
            raise ValueError("legacy_unquoted_dates_invalid")
        end = _day(evidence["lastQuotedDates"][symbol])
        if end >= base or end not in calendar:
            raise ValueError("legacy_unquoted_last_quote_not_before_base")
        result[symbol] = end
    return result, base


def _load(root, symbol, calendar, period, *, target_required=True):
    directory = root / "features" / symbol.lower()
    if ((root / "features").is_symlink() or directory.is_symlink() or not directory.is_dir()
            or {path.name for path in directory.iterdir()} != {field + ".day.bin" for field in FIELDS}):
        raise ValueError(f"index_member_feature_inventory_invalid:{symbol}")
    starts, lengths, values = set(), set(), {}
    for field in FIELDS:
        raw = read_small(directory / (field + ".day.bin"), 4 * 1024 * 1024)
        if len(raw) < 8 or len(raw) % 4:
            raise ValueError(f"index_member_bad_feature:{symbol}:{field}")
        header = struct.unpack_from("<f", raw)[0]
        if not math.isfinite(header) or header < 0 or not header.is_integer():
            raise ValueError("index_member_bad_feature_header")
        start, length = int(header), len(raw) // 4 - 1
        if start + length > len(calendar) or (target_required and start + length != len(calendar)):
            raise ValueError(f"index_member_feature_not_at_target:{symbol}")
        starts.add(start)
        lengths.add(length)
        values[field] = struct.unpack_from(f"<{length}f", raw, 4)
    if len(starts) != 1 or len(lengths) != 1:
        raise ValueError(f"index_member_fields_misaligned:{symbol}")
    start, length = starts.pop(), lengths.pop()
    if (calendar[start], calendar[start + length - 1]) != period:
        raise ValueError(f"index_member_inventory_feature_interval_disagrees:{symbol}")
    return start, values


def _missing_slot(values, index, symbol):
    if index <= 0 or index >= len(values["close"]):
        raise ValueError(f"index_missing_quote_has_no_prior_factor:{symbol}")
    if any(not math.isnan(values[field][index]) for field in FIELDS if field != "factor"):
        raise ValueError(f"index_source_absence_bytes_disagree:{symbol}")
    current, previous = values["factor"][index], values["factor"][index - 1]
    if (not math.isfinite(current) or not math.isfinite(previous) or current <= 0 or previous <= 0
            or current != previous):
        raise ValueError(f"index_missing_quote_factor_not_carried:{symbol}")


def _real_quote(values, index, symbol, first_day):
    current = {field: values[field][index] for field in FIELDS}
    for field, value in current.items():
        if not math.isfinite(value) and not (first_day and field == "change" and math.isnan(value)):
            raise ValueError(f"index_member_invalid_target_value:{symbol}:{field}")
    for field in ("open", "high", "low", "close", "factor", "volume", "adjclose"):
        if current[field] <= 0:
            raise ValueError(f"index_member_invalid_target_value:{symbol}:{field}")
    for field in ("amount", "vwap"):
        if current[field] < 0:
            raise ValueError(f"index_member_invalid_target_value:{symbol}:{field}")
    if not current["low"] <= min(current["open"], current["close"]) <= max(current["open"], current["close"]) <= current["high"]:
        raise ValueError(f"index_member_inconsistent_ohlc:{symbol}")


def validate_index_market_alignment(root, calendar, *, source_absence=None, expected_source_commit=None,
                                    legacy_unquoted=None):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or (root / "instruments").is_symlink():
        raise ValueError("unsafe_index_alignment_root")
    root = root.resolve(strict=True)
    if isinstance(calendar, (str, bytes)):
        raise ValueError("invalid_alignment_calendar")
    calendar = [_day(day) for day in calendar]
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError("invalid_alignment_calendar")
    target = calendar[-1]
    proven = _absence(source_absence, expected_source_commit, calendar)
    legacy, legacy_base = _legacy(legacy_unquoted, expected_source_commit, calendar)
    if set(proven) & set(legacy):
        raise ValueError("legacy_and_current_absence_evidence_overlap")
    intervals = {}
    for line in read_small(root / "instruments/all.txt", 16 * 1024 * 1024).decode("ascii").splitlines():
        cells = line.split()
        if len(cells) != 3 or not SYMBOL.fullmatch(cells[0]) or cells[0] in intervals:
            raise ValueError("invalid_all_inventory_for_indices")
        period = (_day(cells[1]), _day(cells[2]))
        if period[0] > period[1]:
            raise ValueError("invalid_all_inventory_for_indices")
        intervals[cells[0]] = period
    members, per_index = set(), {}
    for name in INDEX_MAP:
        active, historical = set(), {}
        for line in read_small(root / "instruments" / (name + ".txt"), 16 * 1024 * 1024).decode("ascii").splitlines():
            cells = line.split()
            if len(cells) != 3:
                raise ValueError("invalid_index_member_row")
            start, end = _day(cells[1]), _day(cells[2])
            if start > end or end > target:
                raise ValueError("invalid_index_member_row")
            try:
                symbol = interval_symbol(cells[0], end, target)
            except IndicesError:
                raise ValueError("invalid_index_member_row") from None
            historical.setdefault(symbol, []).append((start, end))
            if start <= target <= end:
                if symbol in active:
                    raise ValueError("duplicate_active_index_member")
                active.add(symbol)
        for periods in historical.values():
            ordered = sorted(periods)
            if any(a[1] >= b[0] for a, b in zip(ordered, ordered[1:])):
                raise ValueError("overlapping_index_intervals")
        if not active:
            raise ValueError(f"index_membership_not_at_target:{name}")
        members.update(active)
        per_index[name] = active
    if not set(legacy) <= members:
        raise ValueError("legacy_unquoted_not_active_csi_member")
    missing_quotes, missing_previous, first_listings, legacy_members = set(), set(), set(), set()
    slots_verified = quotes = usable = 0
    for symbol in sorted(members):
        period = intervals.get(symbol)
        if symbol in legacy:
            if period is None or period[1] != legacy[symbol] or not period[1] < legacy_base:
                raise ValueError(f"legacy_unquoted_inventory_disagrees:{symbol}")
            _, values = _load(root, symbol, calendar, period, target_required=False)
            _real_quote(values, len(values["close"]) - 1, symbol, period[0] == period[1])
            legacy_members.add(symbol)
            continue
        if period is None or not period[0] <= target <= period[1]:
            raise ValueError(f"index_member_outside_price_inventory:{symbol}")
        start, values = _load(root, symbol, calendar, period)
        index = len(calendar) - 1 - start
        for day in sorted(proven.get(symbol, ())):
            position = calendar.index(day) - start
            _missing_slot(values, position, symbol)
            slots_verified += 1
        target_missing = target in proven.get(symbol, ())
        first_day = period[0] == target
        if target_missing:
            missing_quotes.add(symbol)
        else:
            _real_quote(values, index, symbol, first_day)
            quotes += 1
        prior_missing = False
        if first_day:
            first_listings.add(symbol)
        else:
            if index < 1:
                raise ValueError(f"index_member_previous_close_missing:{symbol}")
            prior_missing = calendar[-2] in proven.get(symbol, ())
            if prior_missing:
                missing_previous.add(symbol)
            elif not math.isfinite(values["close"][index - 1]) or values["close"][index - 1] <= 0:
                raise ValueError(f"index_member_previous_close_missing:{symbol}")
        if not target_missing and not prior_missing and not first_day:
            usable += 1
    return {
        "status": "partial" if missing_quotes or missing_previous or legacy_members else "complete",
        "targetDate": target, "uniqueMembers": len(members), "uniqueMembersWithPrices": quotes,
        "usableDailyReturnMembers": usable, "indexMemberCounts": {name: len(items) for name, items in per_index.items()},
        "missingQuoteMembers": sorted(missing_quotes), "missingPreviousCloseMembers": sorted(missing_previous),
        "firstListingDayMembers": sorted(first_listings), "sourceAbsenceSlotsVerified": slots_verified,
        "legacyUnquotedMembers": sorted(legacy_members),
        "legacyUnquotedMeaning": "unchanged_historical_feature_boundary_no_physical_target_quote",
        "legacyUnquotedBaseDate": legacy_base,
        "sourceAbsenceCommit": expected_source_commit if source_absence is not None else None,
        "missingQuoteCountsByIndex": {name: len(items & missing_quotes) for name, items in per_index.items()},
        "missingPreviousCloseCountsByIndex": {name: len(items & missing_previous) for name, items in per_index.items()},
        "legacyUnquotedCountsByIndex": {name: len(items & legacy_members) for name, items in per_index.items()},
        "absenceMeaning": "source_absent_not_confirmed_suspension",
    }
