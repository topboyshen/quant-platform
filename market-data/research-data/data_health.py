#!/usr/bin/env python3
"""Read-only data coverage audit. Never imports collectors, locks, or sends messages."""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import sys


def _load_source_policy():
    """Load the sibling policy module by path.

    This file is executed both as a script and loaded by qlib's read-only
    consumer via ``spec_from_file_location``, which does not put this directory
    on ``sys.path``; a plain ``from source_policy import ...`` therefore raised
    ModuleNotFoundError inside the nightly qlib run (2026-09-19 22:00). Loading
    by path keeps the single shared policy without mutating ``sys.path``.
    """
    path = Path(__file__).resolve().with_name("source_policy.py")
    spec = importlib.util.spec_from_file_location("_nng8_source_policy", path)
    if spec is None or spec.loader is None:
        raise ImportError("source_policy module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_source_policy = _load_source_policy()
is_optional_source_error = _source_policy.is_optional_source_error

CST = dt.timezone(dt.timedelta(hours=8))
# 已证明源缺失的分级守卫：仅在“数量在既定有界容忍内、且影响日期数不超过上界”时记 information；
# 缺失扩大或持续期变长时重新升级为 warning（“原因可解释”不构成永久豁免）。
ABSENCE_TOLERANCE_MIN = 20
ABSENCE_TOLERANCE_RATIO = 0.005
ABSENCE_DATE_BOUND = 3
REFERENCES = ("sh600000", "sz000001", "sh600519", "sz300750")
FIELDS = ("close", "high", "low", "amount")
FAILURES = {"rejected", "deferred", "failed", "temporary_failure", "usage_error"}
INCREMENTAL_MANIFEST = ".qlib-dolt-incremental.json"


class EvidenceError(ValueError):
    pass


def strict_json(raw):
    def reject(_):
        raise EvidenceError("non_finite_json")
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise EvidenceError("non_finite_json")
        return result
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject, parse_float=finite, object_pairs_hook=unique)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def iso_date(value):
    if not isinstance(value, str):
        raise EvidenceError("invalid_date_type")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise EvidenceError("invalid_date") from None
    if parsed.isoformat() != value:
        raise EvidenceError("noncanonical_date")
    return parsed


def timestamp(value):
    if not isinstance(value, str):
        raise EvidenceError("invalid_timestamp_type")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvidenceError("timestamp_without_timezone")
    return parsed.astimezone(CST)


def read_file(path, limit=4 * 1024 * 1024, private=False):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise EvidenceError("invalid_file_type_or_size")
        if private and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
            raise EvidenceError("invalid_stage_permissions")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise EvidenceError("file_too_large")
        return raw


def incremental_quality(document):
    """Validate producer claims and their arithmetic, not underlying market rows."""
    absence, price = document["sourceAbsence"], document["priceValidation"]
    if (not isinstance(absence, dict) or set(absence) != {"sourceCommit", "dates"}
            or absence["sourceCommit"] != document["sourceCommit"]
            or not isinstance(absence["dates"], dict)):
        raise EvidenceError("incremental_source_absence_invalid")
    missing = {}
    for symbol, dates in absence["dates"].items():
        if (not isinstance(symbol, str) or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None
                or not isinstance(dates, list) or not dates):
            raise EvidenceError("incremental_source_absence_invalid")
        for day in dates:
            iso_date(day)
        if dates != sorted(set(dates)) or dates[-1] > document["targetDate"]:
            raise EvidenceError("incremental_source_absence_invalid")
        missing[symbol] = list(dates)
    fields = {"target_date", "eligible", "valid", "invalid", "coverage", "invalid_symbols",
              "invalid_reasons", "first_day_listings_checked", "reference_dates"}
    if (not isinstance(price, dict) or not fields | {"status", "daily_coverage", "sourceAbsentDates", "lastQuotedDate", "historical_tail_gaps"} <= set(price)
            or not isinstance(price["daily_coverage"], list)
            or len(price["daily_coverage"]) != len(document["calendarAdded"])):
        raise EvidenceError("incremental_price_claim_invalid")
    if (price.get("source_commit", document["sourceCommit"]) != document["sourceCommit"]
            or price.get("base_date", document["baseDate"]) != document["baseDate"]):
        raise EvidenceError("incremental_price_source_disagrees")
    if (price["sourceAbsentDates"] != missing or not isinstance(price["lastQuotedDate"], dict)
            or set(price["lastQuotedDate"]) != set(missing)):
        raise EvidenceError("incremental_source_absence_disagrees")
    for symbol, day in price["lastQuotedDate"].items():
        if not isinstance(symbol, str) or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None:
            raise EvidenceError("incremental_last_quote_claim_invalid")
        iso_date(day)
        if day > document["targetDate"] or day in missing.get(symbol, []):
            raise EvidenceError("incremental_last_quote_claim_invalid")
    gaps = price["historical_tail_gaps"]
    if not isinstance(gaps, dict):
        raise EvidenceError("incremental_historical_tail_claim_invalid")
    for symbol, dates in gaps.items():
        if (not isinstance(symbol, str) or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None
                or not isinstance(dates, list) or not 1 <= len(dates) <= 10):
            raise EvidenceError("incremental_historical_tail_claim_invalid")
        for day in dates:
            iso_date(day)
        if dates != sorted(set(dates)) or dates[-1] > document["baseDate"]:
            raise EvidenceError("incremental_historical_tail_claim_invalid")
    stored = {}
    if "storedShortHistoryEvidence" in document:
        evidence = document["storedShortHistoryEvidence"]
        if (not isinstance(evidence, dict) or set(evidence) != {"sourceCommit", "dates"}
                or evidence["sourceCommit"] != document["sourceCommit"] or not isinstance(evidence["dates"], dict)):
            raise EvidenceError("incremental_stored_short_history_invalid")
        for symbol, dates in evidence["dates"].items():
            if (symbol not in missing or not isinstance(dates, list) or not 1 <= len(dates) <= 10):
                raise EvidenceError("incremental_stored_short_history_invalid")
            for day in dates:
                iso_date(day)
            if (dates != sorted(set(dates)) or dates[-1] > document["baseDate"]
                    or not set(dates) <= set(missing[symbol]) or price["lastQuotedDate"][symbol] in dates):
                raise EvidenceError("incremental_stored_short_history_invalid")
            stored[symbol] = list(dates)
    days = []
    for expected_day, row in zip(document["calendarAdded"], price["daily_coverage"]):
        if (not isinstance(row, dict) or not fields | {"status"} <= set(row)
                or row["target_date"] != expected_day):
            raise EvidenceError("incremental_daily_coverage_invalid")
        for field in ("eligible", "valid", "invalid", "first_day_listings_checked"):
            if type(row[field]) is not int or row[field] < 0:
                raise EvidenceError("incremental_daily_coverage_invalid")
        if (row["eligible"] <= 0 or row["valid"] + row["invalid"] != row["eligible"]
                or type(row["coverage"]) not in (int, float) or not math.isfinite(row["coverage"])
                or not 0 <= row["coverage"] <= 1
                or not math.isclose(row["coverage"], row["valid"] / row["eligible"], rel_tol=0, abs_tol=1e-12)):
            raise EvidenceError("incremental_daily_coverage_invalid")
        symbols, reasons, refs = row["invalid_symbols"], row["invalid_reasons"], row["reference_dates"]
        if (not isinstance(symbols, list) or any(not isinstance(symbol, str)
                or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None for symbol in symbols)
                or len(set(symbols)) != len(symbols) or len(symbols) != row["invalid"]
                or not isinstance(reasons, dict) or set(reasons) != set(symbols)
                or row["status"] != ("partial" if symbols else "complete")
                or not isinstance(refs, dict) or not {symbol.upper() for symbol in REFERENCES} <= set(refs)):
            raise EvidenceError("incremental_daily_coverage_invalid")
        for symbol, date in refs.items():
            if (not isinstance(symbol, str) or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None
                    or date != expected_day or symbol in reasons):
                raise EvidenceError("incremental_reference_claim_invalid")
        for symbol, codes in reasons.items():
            if (not isinstance(codes, list) or not codes or any(not isinstance(code, str) or code not in {
                    "source_absent_today", "prior_close_source_absent"} for code in codes)
                    or len(set(codes)) != len(codes) or symbol not in missing):
                raise EvidenceError("incremental_invalid_reason_unproven")
            if "source_absent_today" in codes and expected_day not in missing[symbol]:
                raise EvidenceError("incremental_invalid_reason_unproven")
        days.append({key: row[key] for key in sorted(fields | {"status"})})
    if (any(type(price[key]) is not int for key in ("eligible", "valid", "invalid", "first_day_listings_checked"))
            or type(price["coverage"]) not in (int, float)
            or price["status"] != ("partial" if any(day["invalid"] for day in days) else "complete")
            or any(price[key] != days[-1][key] for key in fields)):
        raise EvidenceError("incremental_price_claim_disagrees")
    return {"status": price["status"], "dailyCoverage": days, "sourceAbsenceDates": missing,
            "lastQuotedDate": dict(price["lastQuotedDate"]), "historicalTailGaps": dict(gaps),
            "storedShortHistoryDates": stored}


def incremental_index_quality(document):
    """Cross-check CSI inventory claims without asserting an independent scan."""
    legacy, index = document["legacyUnquotedCSI"], document["indexPriceValidation"]
    if (not isinstance(legacy, dict) or set(legacy) != {"sourceCommit", "baseDate", "dates", "lastQuotedDates"}
            or legacy["sourceCommit"] != document["sourceCommit"] or legacy["baseDate"] != document["baseDate"]
            or not isinstance(legacy["dates"], dict) or not isinstance(legacy["lastQuotedDates"], dict)
            or set(legacy["dates"]) != set(legacy["lastQuotedDates"])
            or set(legacy["dates"]) & set(document["sourceAbsence"]["dates"])):
        raise EvidenceError("incremental_legacy_csi_claim_invalid")
    for symbol, dates in legacy["dates"].items():
        if (not isinstance(symbol, str) or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None
                or dates != document["calendarAdded"]):
            raise EvidenceError("incremental_legacy_csi_claim_invalid")
        end = legacy["lastQuotedDates"][symbol]
        iso_date(end)
        if end >= document["baseDate"]:
            raise EvidenceError("incremental_legacy_csi_claim_invalid")
    lists = {"missingQuoteMembers", "missingPreviousCloseMembers", "firstListingDayMembers", "legacyUnquotedMembers"}
    counts = {"uniqueMembers", "uniqueMembersWithPrices", "usableDailyReturnMembers", "sourceAbsenceSlotsVerified"}
    maps = {"indexMemberCounts", "missingQuoteCountsByIndex", "missingPreviousCloseCountsByIndex", "legacyUnquotedCountsByIndex"}
    fields = lists | counts | maps | {"status", "targetDate", "sourceAbsenceCommit", "absenceMeaning",
                                      "legacyUnquotedMeaning", "legacyUnquotedBaseDate"}
    if (not isinstance(index, dict) or not fields <= set(index)
            or index["targetDate"] != document["targetDate"] or index["sourceAbsenceCommit"] != document["sourceCommit"]
            or index["legacyUnquotedBaseDate"] != document["baseDate"]
            or index["absenceMeaning"] != "source_absent_not_confirmed_suspension"
            or index["legacyUnquotedMeaning"] != "unchanged_historical_feature_boundary_no_physical_target_quote"):
        raise EvidenceError("incremental_index_price_claim_invalid")
    for key in counts:
        if type(index[key]) is not int or index[key] < 0:
            raise EvidenceError("incremental_index_price_claim_invalid")
    members = {}
    for key in lists:
        values = index[key]
        if (not isinstance(values, list) or any(not isinstance(symbol, str)
                or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None for symbol in values)
                or values != sorted(set(values))):
            raise EvidenceError("incremental_index_price_claim_invalid")
        members[key] = set(values)
    quotes, previous = members["missingQuoteMembers"], members["missingPreviousCloseMembers"]
    old, first = members["legacyUnquotedMembers"], members["firstListingDayMembers"]
    missing = document["sourceAbsence"]["dates"]
    if (old != set(legacy["dates"]) or old & (quotes | previous | first) or first & (quotes | previous)
            or not quotes | previous <= set(missing)
            or index["uniqueMembers"] <= 0
            or index["uniqueMembersWithPrices"] != index["uniqueMembers"] - len(quotes | old)
            or index["usableDailyReturnMembers"] != index["uniqueMembers"] - len(quotes | previous | old | first)
            or index["status"] != ("partial" if quotes or previous or old else "complete")
            or any(document["targetDate"] not in missing[symbol] for symbol in quotes)
            or not sum(len(missing[symbol]) for symbol in quotes | previous) <= index["sourceAbsenceSlotsVerified"]
                <= sum(len(dates) for dates in missing.values())):
        raise EvidenceError("incremental_index_price_claim_disagrees")
    names = {"csi300", "csi500", "csi800", "csi1000", "csiall"}
    for key in maps:
        values = index[key]
        if (not isinstance(values, dict) or set(values) != names
                or any(type(value) is not int or value < 0 for value in values.values())):
            raise EvidenceError("incremental_index_counts_invalid")
    inventory = index["indexMemberCounts"]
    if not all(0 < value <= index["uniqueMembers"] for value in inventory.values()) or sum(inventory.values()) < index["uniqueMembers"]:
        raise EvidenceError("incremental_index_counts_invalid")
    for key, affected in (("missingQuoteCountsByIndex", quotes), ("missingPreviousCloseCountsByIndex", previous),
                          ("legacyUnquotedCountsByIndex", old)):
        if (sum(index[key].values()) < len(affected)
                or any(value > min(inventory[name], len(affected)) for name, value in index[key].items())):
            raise EvidenceError("incremental_index_counts_invalid")
    return {"validation": {key: index[key] for key in sorted(fields)},
            "legacyLastQuotedDates": dict(legacy["lastQuotedDates"])}


def incremental_identity(root, tag):
    """Check local generation claims, not source authenticity/full-market prices.

    Only a genuinely absent file is a legacy candidate. Malformed, dangling,
    symlinked, oversized, or incomplete manifests never downgrade to legacy.
    The caller must additionally match this identity to calendar/reference data.
    """
    try:
        raw = read_file(root / INCREMENTAL_MANIFEST, 65536)
    except FileNotFoundError:
        return None, None
    try:
        document = strict_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise EvidenceError("incremental_manifest_invalid_json") from None
    required = {"schemaVersion", "kind", "status", "sourceRepository", "sourceCommit",
                "generationId", "baseReleaseTag", "baseDate", "targetDate", "calendarAdded",
                "sourceRowsSha256", "historicalPrefixes", "sourceAbsence", "priceValidation",
                "legacyUnquotedCSI", "indexPriceValidation"}
    if not isinstance(document, dict) or not required <= set(document):
        raise EvidenceError("incremental_manifest_missing_fields")
    if (type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
            or document["kind"] != "same_source_dolt_incremental"
            or document["status"] != "validated"
            or document["sourceRepository"] != "chenditc/investment_data"
            or not isinstance(document["sourceCommit"], str)
            or re.fullmatch(r"[0-9a-v]{32}", document["sourceCommit"]) is None
            or not isinstance(document["generationId"], str)
            or re.fullmatch(r"[0-9a-f]{32}", document["generationId"]) is None
            or document["baseReleaseTag"] != tag):
        raise EvidenceError("incremental_manifest_identity_invalid")
    base, target = iso_date(document["baseDate"]), iso_date(document["targetDate"])
    added = document["calendarAdded"]
    if base >= target or not isinstance(added, list) or not added:
        raise EvidenceError("incremental_manifest_dates_invalid")
    for day in added:
        iso_date(day)
    if added != sorted(set(added)) or added[0] <= base.isoformat() or added[-1] != target.isoformat():
        raise EvidenceError("incremental_manifest_dates_invalid")
    validate_hash(document["sourceRowsSha256"])
    proof = document["historicalPrefixes"]
    if (not isinstance(proof, dict) or not {"unchanged", "files", "bytes", "sha256"} <= set(proof)
            or proof["unchanged"] is not True
            or type(proof["files"]) is not int or proof["files"] <= 0
            or type(proof["bytes"]) is not int or proof["bytes"] <= 0):
        raise EvidenceError("incremental_prefix_claim_invalid")
    validate_hash(proof["sha256"])
    quality = incremental_quality(document)
    quality["index"] = incremental_index_quality(document)
    quality["priceStatus"] = quality["status"]
    if quality["index"]["validation"]["status"] == "partial":
        quality["status"] = "partial"
    if document.get("dataQuality", quality["status"]) != quality["status"]:
        raise EvidenceError("incremental_quality_claim_disagrees")
    identity = (document["generationId"], document["sourceCommit"], base.isoformat(), target.isoformat(),
                tuple(added), document["sourceRowsSha256"], proof["sha256"], proof["files"], proof["bytes"], quality)
    return hashlib.sha256(raw).hexdigest(), identity


def generation(root):
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError("qlib_root_invalid")
    first = root.stat()
    tag = read_file(root.parent / ".tag", 128).decode().strip()
    embedded = read_file(root / ".qlib-release", 128).decode().strip()
    iso_date(tag)
    if tag != embedded:
        raise EvidenceError("qlib_tags_disagree")
    manifest_sha, incremental = incremental_identity(root, tag)
    last = root.stat()
    if (first.st_dev, first.st_ino) != (last.st_dev, last.st_ino):
        raise EvidenceError("qlib_generation_changed_or_invalid")
    # Keep release tag at index 1 for existing readers; hash exact manifest bytes
    # to detect in-place replacement/removal even when root inode/tag stay fixed.
    return (first.st_ino, tag, manifest_sha, incremental, first.st_dev)


def qlib_evidence(root):
    before = generation(root)
    incremental = before[3]
    calendar = read_file(root / "calendars/day.txt", 1024 * 1024).decode().splitlines()
    if not calendar or calendar != sorted(set(calendar)):
        raise EvidenceError("calendar_not_strictly_increasing")
    for day in calendar:
        iso_date(day)
    if incremental is not None:
        base, target, added = incremental[2], incremental[3], incremental[4]
        if (base not in calendar or target != calendar[-1]
                or tuple(calendar[calendar.index(base) + 1:]) != added):
            raise EvidenceError("incremental_calendar_disagrees")
        quality = incremental[9]
        missing = quality["sourceAbsenceDates"]
        if any(day not in calendar for dates in missing.values() for day in dates):
            raise EvidenceError("incremental_absence_outside_calendar")
        if any(day not in calendar for day in quality["lastQuotedDate"].values()):
            raise EvidenceError("incremental_last_quote_outside_calendar")
        if any(day not in calendar for day in quality["index"]["legacyLastQuotedDates"].values()):
            raise EvidenceError("incremental_legacy_csi_date_outside_calendar")
        if any(calendar[-2] not in missing[symbol]
               for symbol in quality["index"]["validation"]["missingPreviousCloseMembers"]):
            raise EvidenceError("incremental_index_previous_close_unproven")
        gaps = quality["historicalTailGaps"]
        if any(day not in calendar for dates in gaps.values() for day in dates):
            raise EvidenceError("incremental_historical_tail_outside_calendar")
        base_index = calendar.index(base)
        stored = quality["storedShortHistoryDates"]
        old_window = set(calendar[max(0, base_index - 9):base_index + 1])
        if any(day not in old_window for dates in stored.values() for day in dates):
            raise EvidenceError("incremental_stored_short_history_outside_window")
        recent = set(calendar[max(0, base_index - 2):])
        if any(day not in recent and day not in gaps.get(symbol, []) and day not in stored.get(symbol, [])
               for symbol, dates in missing.items() for day in dates):
            raise EvidenceError("incremental_old_absence_unproven")
        for row in quality["dailyCoverage"]:
            previous = calendar[calendar.index(row["target_date"]) - 1]
            for symbol, codes in row["invalid_reasons"].items():
                if "prior_close_source_absent" in codes and previous not in missing[symbol]:
                    raise EvidenceError("incremental_invalid_reason_unproven")
            for symbol, dates in missing.items():
                codes = row["invalid_reasons"].get(symbol, [])
                if ((row["target_date"] in dates and "source_absent_today" not in codes)
                        or (previous in dates and "prior_close_source_absent" not in codes)):
                    raise EvidenceError("incremental_source_absence_not_disclosed")
    dates = {}
    for symbol in REFERENCES:
        starts, features = [], {}
        for field in FIELDS:
            raw = read_file(root / "features" / symbol / (field + ".day.bin"))
            if len(raw) < 8 or len(raw) % 4:
                raise EvidenceError("invalid_reference_binary")
            values = struct.unpack("<" + str(len(raw) // 4) + "f", raw)
            start = values[0]
            if not math.isfinite(start) or start < 0 or not start.is_integer():
                raise EvidenceError("invalid_reference_start")
            starts.append(int(start))
            features[field] = values[1:]
        if len(set(starts)) != 1 or len({len(v) for v in features.values()}) != 1:
            raise EvidenceError("reference_fields_disagree")
        if incremental is not None and starts[0] + len(features["close"]) != len(calendar):
            raise EvidenceError("incremental_reference_physical_tail_disagrees")
        for index in range(len(features["close"]) - 1, 0, -1):
            position = starts[0] + index
            values = [features["close"][index - 1]] + [features[f][index] for f in FIELDS]
            if position < len(calendar) and all(math.isfinite(v) for v in values):
                dates[symbol] = calendar[position]
                break
        if symbol not in dates:
            raise EvidenceError("reference_complete_date_missing")
    if len(set(dates.values())) != 1:
        raise EvidenceError("reference_dates_disagree")
    complete = next(iter(dates.values()))
    if incremental is not None and complete != incremental[3]:
        raise EvidenceError("incremental_reference_date_disagrees")
    if (incremental is None and complete > before[1]) or generation(root) != before:
        raise EvidenceError("qlib_generation_changed_or_invalid")
    return calendar, complete, before


def has_source_error(value, path=""):
    """Detect stored source errors, skipping tolerated optional sources.

    The tolerated set is shared with the collector's write path
    (source_policy.OPTIONAL_SOURCE_ERRORS). A tolerated error stays in the
    payload as evidence; it just no longer marks an otherwise complete
    snapshot row invalid.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (key == "error" or key.endswith("_error")) and bool(child):
                if is_optional_source_error(child_path):
                    continue
                return True
            if has_source_error(child, child_path):
                return True
        return False
    return isinstance(value, list) and any(has_source_error(child, path) for child in value)


def absence_severity(proven, invalid_count, affected_dates, eligible):
    """已证明源缺失的分级：仅在数量与持续期都在上界内时记 information。

    `proven` 只说明“缺失声明与清单/计数相互一致”，既不证明停牌，也不证明对研究无影响；
    因此数量扩大或持续期变长时重新升级为 warning，不做永久豁免。
    返回 (severity, escalation_reason)。
    """
    if not proven:
        return "warning", "proof_incomplete"
    tolerance = max(ABSENCE_TOLERANCE_MIN, ABSENCE_TOLERANCE_RATIO * (eligible or 0))
    if invalid_count > tolerance:
        return "warning", f"count_over_tolerance:{invalid_count}>{tolerance:.1f}"
    if len(affected_dates) > ABSENCE_DATE_BOUND:
        return "warning", f"duration_over_bound:{len(affected_dates)}>{ABSENCE_DATE_BOUND}"
    return "information", None


def snapshot_rows(path):
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("database_missing_or_symlinked")
    result = {}
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise EvidenceError("database_quick_check_failed")
        for day, raw in conn.execute("SELECT date, payload FROM snapshot ORDER BY date"):
            iso_date(day)
            if not isinstance(raw, str):
                raise EvidenceError("non_text_snapshot_payload")
            row = {"payloadSha256": hashlib.sha256(raw.encode()).hexdigest(), "state": "valid"}
            try:
                payload = strict_json(raw)
                activity = payload["akshare"]["activity"]["统计日期"]
                if (payload["date"] != day or payload["qlib"]["date"] != day
                        or not isinstance(activity, str) or activity[:10] != day):
                    raise EvidenceError("internal_dates_disagree")
                if has_source_error(payload):
                    raise EvidenceError("stored_source_errors")
            except (ValueError, KeyError, TypeError) as exc:
                row["state"] = "invalid"
                row["reason"] = str(exc) if isinstance(exc, EvidenceError) else "stored_payload_invalid_schema"
            result[day] = row
    return result


def stage_evidence(root, now):
    if not root.exists():
        return {}, "missing"
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        return {}, "invalid_permissions"
    result = {}
    for path in sorted(root.glob("*.json")):
        try:
            iso_date(path.stem)
            document = strict_json(read_file(path, private=True))
            unsigned = dict(document)
            record_sha = unsigned.pop("recordSha256")
            payload = document["akshare"]
            window = document["collectionWindow"]
            start, end = timestamp(window["startedAt"]), timestamp(window["completedAt"])
            sectors, activity = payload["sectors"], payload["activity"]
            if (set(document) != {"schemaVersion", "tradeDate", "collectionWindow", "akshare", "payloadSha256", "recordSha256"}
                    or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
                    or set(window) != {"startedAt", "completedAt"}
                    or document["tradeDate"] != path.stem or str(activity["统计日期"])[:10] != path.stem
                    or not start <= end <= now + dt.timedelta(minutes=5)
                    or iso_date(path.stem) > now.date()
                    or canonical_hash(payload) != document["payloadSha256"]
                    or canonical_hash(unsigned) != record_sha
                    or not isinstance(sectors, list) or len(sectors) < 50 or has_source_error(payload)):
                raise EvidenceError("invalid_stage")
            for sector in sectors:
                if (not isinstance(sector, dict) or not {"name", "chg", "netInflow", "upCount", "downCount", "leader"} <= set(sector)
                        or not isinstance(sector["name"], str) or not sector["name"].strip()):
                    raise EvidenceError("invalid_sector")
            for field in ("上涨", "下跌", "平盘", "涨停", "跌停"):
                value = activity[field]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise EvidenceError("invalid_activity")
            if sum(activity[f] for f in ("上涨", "下跌", "平盘")) < 1000:
                raise EvidenceError("invalid_breadth")
            result[path.stem] = {"state": "valid", "payloadSha256": document["payloadSha256"],
                                 "recordSha256": record_sha, "observedAt": end.isoformat()}
        except (OSError, ValueError, KeyError, TypeError):
            result[path.stem] = {"state": "invalid", "reason": "stage_validation_failed"}
    return result, "checked"


def recent_run(path, now):
    try:
        record = strict_json(read_file(path))
        when = timestamp(record["timestamp"])
        if (when > now + dt.timedelta(minutes=5)
                or record.get("status") not in FAILURES | {"inserted", "skipped_existing", "idempotent", "success"}
                or (record.get("akshareStage") is not None and not isinstance(record["akshareStage"], dict))):
            raise EvidenceError("invalid_run_record")
        if record.get("target") is not None and iso_date(record["target"]) > now.date():
            raise EvidenceError("invalid_run_target")
        summary = {"status": record["status"], "timestamp": when.isoformat(),
                   "target": record.get("target"), "qlibTag": record.get("qlibTag"),
                   "hasWarnings": bool(record.get("warnings")),
                   "stageUnavailable": (record.get("akshareStage") or {}).get("status") == "unavailable"}
        stage = record.get("akshareStage") or {}
        for date_key, hash_key in (("tradeDate", "payloadSha256"), ("usedTradeDate", "usedPayloadSha256")):
            if (stage.get(date_key) is None) != (stage.get(hash_key) is None):
                raise EvidenceError("incomplete_stage_claim")
        if stage.get("tradeDate") is not None:
            iso_date(stage["tradeDate"])
            summary["stageTradeDate"] = stage["tradeDate"]
        if stage.get("payloadSha256") is not None:
            validate_hash(stage["payloadSha256"])
            summary["stagePayloadSha256"] = stage["payloadSha256"]
        if stage.get("usedTradeDate") is not None:
            iso_date(stage["usedTradeDate"])
            summary["usedStageTradeDate"] = stage["usedTradeDate"]
        if stage.get("usedPayloadSha256") is not None:
            validate_hash(stage["usedPayloadSha256"])
            summary["usedStagePayloadSha256"] = stage["usedPayloadSha256"]
        if record.get("payloadSha256") is not None:
            iso_date(record.get("target"))
            validate_hash(record["payloadSha256"])
            summary["payloadSha256"] = record["payloadSha256"]
        return summary, when
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unknown", "reason": "missing_or_invalid_last_run"}, None


def validate_hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise EvidenceError("invalid_sha256")


def audit(db, qlib, stages, last_run, mode, since, monitor_since, now, baseline=None):
    since, monitor_since = iso_date(since), iso_date(monitor_since)
    if since > monitor_since or monitor_since > now.date():
        raise EvidenceError("invalid_audit_window")
    calendar, complete, gen = qlib_evidence(qlib)
    if calendar[0] > since.isoformat() or iso_date(complete) > now.date():
        raise EvidenceError("calendar_coverage_or_complete_date_invalid")
    rows = snapshot_rows(db)
    stage_rows, stage_state = stage_evidence(stages, now)
    last, last_time = recent_run(last_run, now)
    business_mode = read_file(mode, 32).decode().strip()
    issues = []

    def issue(code, severity="warning", **details):
        issues.append({"code": code, "severity": severity, **details})

    if gen[3] is not None:
        quality = gen[3][9]
        index = quality["index"]["validation"]
        if quality["priceStatus"] == "partial" or index["missingQuoteMembers"] or index["missingPreviousCloseMembers"]:
            # 这里的缺失已被上游校验证明：invalid_reasons 只允许 source_absent_today /
            # prior_close_source_absent，符号必须出现在 sourceAbsence.dates 中，且槽位计数自洽
            # （否则 incremental_invalid_reason_unproven / incremental_index_price_claim_disagrees）。
            # 因此按“已证明的有界源缺失”记 information：保留日期与含义，不暗示需要处置，
            # 也不改变任何覆盖门禁；若证明不完整（防御性分支）才回落为 warning。
            absence_dates = quality.get("sourceAbsenceDates") or {}
            missing_members = set(index["missingQuoteMembers"]) | set(index["missingPreviousCloseMembers"])
            proven = (index["absenceMeaning"] == "source_absent_not_confirmed_suspension"
                      and sum(len(absence_dates.get(symbol, [])) for symbol in missing_members)
                      <= index["sourceAbsenceSlotsVerified"])
            # 分级守卫：数量与持续期超出上界时不再按 information 展示，重新升级为 warning。
            coverage_rows = quality.get("dailyCoverage") or []
            coverage_last = coverage_rows[-1] if coverage_rows and isinstance(coverage_rows[-1], dict) else {}
            eligible = coverage_last.get("eligible") if isinstance(coverage_last.get("eligible"), int) else 0
            tolerance = max(ABSENCE_TOLERANCE_MIN, ABSENCE_TOLERANCE_RATIO * eligible)
            affected = [row["target_date"] for row in coverage_rows if row["invalid"]]
            invalid_count = quality.get("invalid") if isinstance(quality.get("invalid"), int) else len(missing_members)
            severity, escalation = absence_severity(proven, invalid_count, affected, eligible)
            issue("qlib_source_absence", severity=severity,
                  dataQuality="partial", validationScope="local_manifest_calendar_and_reference_only",
                  affectedDates=[row["target_date"] for row in quality["dailyCoverage"] if row["invalid"]],
                  currentInventoryQuality=quality["priceStatus"], csiInventoryQuality=index["status"],
                  meaning=index["absenceMeaning"], proven=proven,
                  absenceTolerance=round(tolerance, 1), invalidCount=invalid_count,
                  escalation=escalation,
                  sourceAbsenceSlotsVerified=index["sourceAbsenceSlotsVerified"])
        if index["legacyUnquotedMembers"]:
            issue("qlib_legacy_unquoted_csi", dataQuality="partial", validationScope="local_manifest_calendar_and_reference_only",
                  symbols=index["legacyUnquotedMembers"], meaning=index["legacyUnquotedMeaning"])
    if business_mode != "FROZEN":
        issue("business_mode_not_frozen", "critical")
    if any(iso_date(d) > now.date() for d in rows):
        issue("future_snapshot_date", "critical")
    if any(d > complete for d in rows):
        issue("snapshot_ahead_of_qlib_reference")
    if stage_state != "checked" or any(v["state"] != "valid" for v in stage_rows.values()):
        issue("market_stage_missing_or_invalid")
    elif not stage_rows:
        issue("no_sealed_market_evidence")
    calendar_set = set(calendar)
    for kind, records in (("snapshot", rows), ("stage", stage_rows)):
        outside = [d for d in records if calendar[0] <= d <= calendar[-1] and d not in calendar_set]
        if outside:
            issue(kind + "_outside_known_trading_calendar", dates=sorted(outside))
            for day in outside:
                records[day]["state"] = "invalid"
                records[day]["reason"] = "outside_known_trading_calendar"
        uncovered = [d for d in records if d < calendar[0]]
        if uncovered:
            issue(kind + "_before_calendar_coverage", dates=sorted(uncovered))
    for date_key, hash_key in (("stageTradeDate", "stagePayloadSha256"),
                               ("usedStageTradeDate", "usedStagePayloadSha256")):
        if last.get(date_key):
            claimed = stage_rows.get(last[date_key], {})
            if (claimed.get("state") != "valid" or (last.get(hash_key)
                    and claimed.get("payloadSha256") != last[hash_key])):
                issue("last_run_claimed_stage_missing_or_mismatched", tradeDate=last[date_key])
    if last.get("payloadSha256") and last["status"] in {"inserted", "skipped_existing", "idempotent", "success"}:
        if rows.get(last["target"], {}).get("payloadSha256") != last["payloadSha256"]:
            issue("last_run_snapshot_missing_or_mismatched", "critical", tradeDate=last["target"])
    baseline_state = "not_provided"
    if baseline is not None:
        baseline_state = "matched"
        expected = strict_json(read_file(baseline))["rowHashes"]
        if not isinstance(expected, dict) or not expected:
            raise EvidenceError("invalid_row_baseline")
        for day, digest in expected.items():
            iso_date(day)
            validate_hash(digest)
        changed = [d for d, h in expected.items() if rows.get(d, {}).get("payloadSha256") != h]
        if changed:
            baseline_state = "mismatch"
            issue("historical_rows_changed_or_missing", "critical", dates=sorted(changed))
    deadline = dt.datetime.combine(now.date(), dt.time(22, 45), CST)
    due_date = now.date() if now >= deadline else now.date() - dt.timedelta(days=1)
    due_start = dt.datetime.combine(due_date, dt.time(22, 30), CST)
    if last_time is None or last_time < due_start:
        issue("scheduled_snapshot_missing", dueDate=due_date.isoformat())
    if last["status"] in FAILURES:
        issue("last_snapshot_not_successful", status=last["status"], target=last.get("target"))
    if last.get("hasWarnings") or last.get("stageUnavailable"):
        issue("last_snapshot_material_warning")
    ledger = []
    new_due_gaps = []
    for day in (d for d in calendar if since.isoformat() <= d <= complete):
        row = rows.get(day, {"state": "missing"})
        stage = stage_rows.get(day, {"state": "missing"})
        legacy = day < monitor_since.isoformat()
        pending = day == now.date().isoformat() and now < deadline
        item = {"tradeDate": day, "snapshotState": row["state"], "stageState": stage["state"],
                "classification": "legacy" if legacy else "monitored", "pendingWindow": pending,
                "qlibAvailability": "calendar_and_reference_evidence_only",
                "recoveryReadiness": "not_applicable" if row["state"] == "valid" else (
                    "partial_evidence_only" if stage["state"] == "valid" else "missing_or_invalid_market_evidence")}
        if row["state"] != "missing":
            item["payloadSha256"] = row["payloadSha256"]
        if row.get("reason"):
            item["reason"] = row["reason"]
        if row["state"] != "valid" and not legacy and not pending:
            new_due_gaps.append(day)
        ledger.append(item)
    legacy_bad = [row["tradeDate"] for row in ledger if row["classification"] == "legacy" and row["snapshotState"] != "valid"]
    if legacy_bad:
        issue("legacy_gaps_or_invalid_rows", dates=legacy_bad)
    if new_due_gaps:
        issue("unresolved_trading_day_gaps", "critical" if len(new_due_gaps) >= 2 else "warning", dates=new_due_gaps)
    latest_stage = max((d for d, v in stage_rows.items() if v["state"] == "valid"), default=None)
    pending_upstream = bool(latest_stage and latest_stage > complete
                            and latest_stage == now.date().isoformat() and now < deadline)
    overdue_stages = sorted(d for d, value in stage_rows.items() if value["state"] == "valid"
                            and d > complete and (d < now.date().isoformat() or now >= deadline))
    if overdue_stages:
        issue("qlib_behind_observed_market_stage", pendingWindow=False,
              overdueStageDates=overdue_stages, latestStageDate=latest_stage, qlibCompleteDate=complete)
    if generation(qlib) != gen:
        raise EvidenceError("qlib_generation_changed_during_audit")
    return {"schemaVersion": 1, "generatedAt": now.isoformat(), "readOnly": True,
            "status": "critical" if any(i["severity"] == "critical" for i in issues) else "warning" if issues else "ok",
            "scope": "local_evidence_only", "businessMode": business_mode,
            "auditSince": since.isoformat(), "monitorSince": monitor_since.isoformat(),
            "schedule": {"expectedLastRunDate": due_date.isoformat(), "todayDeadline": deadline.isoformat()},
            "qlib": {"releaseTag": gen[1], "calendarLastDate": calendar[-1], "referenceCompleteDate": complete,
                     "dataGeneration": "same_source_dolt_incremental" if gen[3] is not None else "release",
                     "dataQuality": gen[3][9]["status"] if gen[3] is not None else "unknown",
                     "incremental": ({"manifestSha256": gen[2], "generationId": gen[3][0],
                                      "sourceCommit": gen[3][1], "baseDate": gen[3][2], "targetDate": gen[3][3],
                                      "dailyCoverage": gen[3][9]["dailyCoverage"],
                                      "currentInventoryQuality": gen[3][9]["priceStatus"],
                                      "csiInventoryQuality": gen[3][9]["index"]["validation"]["status"],
                                      "indexPriceValidation": gen[3][9]["index"]["validation"],
                                      "validationScope": "local_manifest_calendar_and_reference_only"}
                                     if gen[3] is not None else None),
                     "upstreamFreshness": "not_independently_verified", "pendingObservedStage": pending_upstream,
                     "fullMarketCoverage": "not_scanned"},
            "snapshot": {"rowCount": len(rows), "latestValidDate": max((d for d, v in rows.items() if v["state"] == "valid"), default=None),
                         "quickCheck": "ok", "baseline": baseline_state, "rowHashes": {d: v["payloadSha256"] for d, v in rows.items()}},
            "lastRun": last, "stages": stage_rows, "issues": issues, "ledger": ledger,
            "automaticRecoveryAuthorized": False,
            "limitations": ["No independent exchange calendar or release API lookup.",
                            "Valid market stage alone cannot reconstruct historical observed_at sources.",
                            "Stored rows receive strict JSON/date/error checks, not a full market revalidation."]}


def write_audit_output(path, payload):
    """Atomically store the audit JSON for a networked alerter. Never follow a symlink."""
    destination = Path(path)
    parent = destination.parent
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EvidenceError("audit_output_not_regular_file")
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvidenceError("audit_output_parent_invalid")
    temporary = parent / f".{destination.name}.{os.getpid()}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise EvidenceError("audit_output_temp_exists")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--qlib", type=Path, required=True)
    parser.add_argument("--stages", type=Path, required=True)
    parser.add_argument("--last-run", type=Path, required=True)
    parser.add_argument("--mode", type=Path, required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--monitor-since", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.db, args.qlib, args.stages, args.last_run, args.mode,
                       args.since, args.monitor_since, dt.datetime.now(CST), args.baseline)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # No paths, raw database data, or arbitrary exception text in journal.
        result = {"schemaVersion": 1, "readOnly": True, "status": "unknown",
                  "reason": str(exc) if isinstance(exc, EvidenceError) else "evidence_read_or_validation_failed",
                  "errorType": type(exc).__name__}
        payload = json.dumps(result, sort_keys=True)
        print(payload)
        if args.output is not None:
            try:
                write_audit_output(args.output, payload + "\n")
            except (OSError, EvidenceError):
                return 2
        return 2
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None)
    print(payload)
    if args.output is not None:
        try:
            write_audit_output(args.output, payload + ("" if payload.endswith("\n") else "\n"))
        except (OSError, EvidenceError):
            return 2
    return 0  # Data warnings are payload state, not collector/process failure.


if __name__ == "__main__":
    raise SystemExit(main())
