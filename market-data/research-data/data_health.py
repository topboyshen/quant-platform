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


def _load_source_policy(filename="source_policy.py"):
    """Load the sibling policy module by path.

    This file is executed both as a script and loaded by qlib's read-only
    consumer via ``spec_from_file_location``, which does not put this directory
    on ``sys.path``; a plain ``from source_policy import ...`` therefore raised
    ModuleNotFoundError inside the nightly qlib run (2026-09-19 22:00). Loading
    by path keeps the single shared policy without mutating ``sys.path``.
    """
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location("_nng8_source_policy", path)
    if spec is None or spec.loader is None:
        raise ImportError("source_policy module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_source_policy = _load_source_policy()
is_optional_source_error = _source_policy.is_optional_source_error
_core_contract = _load_source_policy("core_contract.py")
_contract_selector = _load_source_policy("snapshot_contract.py")
_absence_policy = _load_source_policy("absence_policy.py")
_calendar_evidence = _load_source_policy("calendar_evidence.py")

CST = dt.timezone(dt.timedelta(hours=8))
# 已证明源缺失的分级守卫：仅在“数量在既定有界容忍内、且影响日期数不超过上界”时记 information；
# 缺失扩大或持续期变长时重新升级为 warning（“原因可解释”不构成永久豁免）。
ABSENCE_TOLERANCE_MIN = 20
ABSENCE_TOLERANCE_RATIO = 0.005
ABSENCE_DATE_BOUND = 3
# 已证明的“历史特征边界”分级守卫：成员数扩大或触及必需参考符号时重新升级为 warning。
LEGACY_UNQUOTED_BOUND = 3
LEGACY_UNQUOTED_MEANING = "unchanged_historical_feature_boundary_no_physical_target_quote"
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


# Exact producer exceptions to the ten-day historical tail-gap claim bound.
# Mirrors qlib_dolt_incremental run.TailGapException (record pinned by SHA-256,
# exchange suspension 2026-08-31..2026-09-29 per SZSE announcements 2026-089 and
# 2026-097). Any other long tail gap, or a near match, is still rejected.
QLIB_TAIL_GAP_EXCEPTIONS = {
    "qlib-tail-gap-SZ301139-20260831-v1": {
        "symbol": "SZ301139",
        "recordSha256": "3ed73aa02e5f207eedc250c41cd6a6964d9ac72d9143080e11bfa481984afbb2",
        "lastStoredDate": "2026-08-28",
        "resumeDate": "2026-09-30",
        "gapDates": (
            "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07",
            "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15",
            "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23",
            "2026-09-24", "2026-09-28", "2026-09-29",
        ),
        "resumeRowSha256": "b41cd898ded75dc783e4e71c2baee482642bf6d83a64c846fbe3b6385aacedb1",
        "lastStoredRowSha256": "cbb9686194d6f788a04ebff79aafe611e973d07eb859c34e37c3a2df5d513eaf",
        "officialEvidenceSha256": ("eb93005104bd94d8896c4c4fcb27ecbd4478ba6d7c4b3a03b134a586aadc50d6", "cef3ef06550bf6e942a29ee39bf4f11b91dc8b6b8e228ba3c5cb0b91285e4aae"),
        "meaning": "exchange_announced_full_day_suspension_source_absent_nan_slots_no_prices_fabricated",
    },
}


def tail_gap_exception(document, gaps, missing, last_quotes):
    """Authorize a >10-day tail gap only for an exact allowlisted, exercised claim."""
    claim = document.get("historicalTailGapException")
    if claim is None:
        return {}
    keys = {"policyId", "recordSha256", "symbol", "lastStoredDate", "gapDates", "resumeDate", "sourceCommit",
            "resumeRowSha256", "lastStoredRowSha256", "officialEvidenceSha256", "meaning"}
    if not isinstance(claim, dict) or set(claim) != keys or claim["policyId"] not in QLIB_TAIL_GAP_EXCEPTIONS:
        raise EvidenceError("incremental_tail_gap_exception_invalid")
    expected = QLIB_TAIL_GAP_EXCEPTIONS[claim["policyId"]]
    symbol, dates = claim["symbol"], claim["gapDates"]
    if (any(claim[key] != expected[key] for key in ("symbol", "recordSha256", "lastStoredDate", "resumeDate",
                                                    "resumeRowSha256", "lastStoredRowSha256", "meaning"))
            or dates != list(expected["gapDates"])
            or claim["officialEvidenceSha256"] != list(expected["officialEvidenceSha256"])
            or claim["sourceCommit"] != document["sourceCommit"]
            or document["baseDate"] != dates[-1]
            or claim["resumeDate"] not in document["calendarAdded"]
            or gaps.get(symbol) != dates
            or not set(dates) <= set(missing.get(symbol, []))
            or not isinstance(last_quotes.get(symbol), str) or last_quotes[symbol] < claim["resumeDate"]):
        raise EvidenceError("incremental_tail_gap_exception_invalid")
    return {symbol: list(dates)}


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
    allowed_long = tail_gap_exception(document, gaps, missing, price["lastQuotedDate"])
    for symbol, dates in gaps.items():
        if (not isinstance(symbol, str) or re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol) is None
                or not isinstance(dates, list)
                or not (1 <= len(dates) <= 10 or (len(dates) > 10 and allowed_long.get(symbol) == dates))):
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
            "legacyAffectedDates": {symbol: list(dates) for symbol, dates in legacy["dates"].items()},
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
    return isinstance(value, list) and any(
        has_source_error(child, f"{path}[{index}]") for index, child in enumerate(value)
    )


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


def legacy_unquoted_severity(meaning_ok, members, references=()):
    """历史特征边界（无目标日实物报价）的分级。

    上游已证明：该符号的最后报价早于基线日、声明与清单/计数一致、且与源缺失集合互斥
    （否则 incremental_legacy_csi_claim_invalid / incremental_index_price_claim_invalid 直接失败）。
    这仍**不证明停牌**，也不证明对研究无影响；成员数扩大或触及必需参考符号时必须重新升级。
    """
    if not meaning_ok:
        return "warning", "meaning_unexpected"
    if any(symbol in set(references) for symbol in members):
        return "warning", "reference_symbol_affected"
    if len(members) > LEGACY_UNQUOTED_BOUND:
        return "warning", f"count_over_bound:{len(members)}>{LEGACY_UNQUOTED_BOUND}"
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


SNAPSHOT_MARKET_ENDPOINTS = frozenset({
    "stock_board_industry_summary_ths", "stock_market_activity_legu",
})
SNAPSHOT_FAILURE_TYPES = frozenset({
    "ValueError", "RuntimeError", "TypeError", "KeyError", "IndexError",
    "OSError", "ConnectionError", "TimeoutError", "HTTPError", "SSLError",
    "JSONDecodeError", "ParserError", "OtherError",
})


def snapshot_source_failure(record):
    """Keep only bounded endpoint evidence; never publish raw upstream errors."""
    if not isinstance(record, dict) or record.get("provider") != "akshare":
        return None
    count, attempts = record.get("attemptCount"), record.get("attempts")
    if (type(count) is not int or not 1 <= count <= 8 or not isinstance(attempts, list)
            or len(attempts) != count):
        return None
    kept = []
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict) or type(attempt.get("attempt")) is not int or attempt["attempt"] != index:
            return None
        endpoint, error_type = attempt.get("endpoint"), attempt.get("errorType")
        if endpoint is not None and (not isinstance(endpoint, str)
                                     or endpoint not in SNAPSHOT_MARKET_ENDPOINTS):
            return None
        if not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
            return None
        item = {"attempt": index,
                "errorType": error_type if error_type in SNAPSHOT_FAILURE_TYPES else "OtherError"}
        if endpoint is not None:
            item["endpoint"] = endpoint
        kept.append(item)
    return {"provider": "akshare", "attemptCount": count, "attempts": kept}


def recent_run(path, now):
    try:
        record = strict_json(read_file(path))
        when = timestamp(record["timestamp"])
        if (when > now + dt.timedelta(minutes=5)
                or record.get("status") not in FAILURES | {"inserted", "skipped_existing", "idempotent", "success", "skipped_non_trading"}
                or (record.get("akshareStage") is not None and not isinstance(record["akshareStage"], dict))):
            raise EvidenceError("invalid_run_record")
        if record.get("target") is not None and iso_date(record["target"]) > now.date():
            raise EvidenceError("invalid_run_target")
        summary = {"status": record["status"], "timestamp": when.isoformat(),
                   "target": record.get("target"), "qlibTag": record.get("qlibTag"),
                   "hasWarnings": bool(record.get("warnings")),
                   "stageUnavailable": (record.get("akshareStage") or {}).get("status") == "unavailable"}
        if record.get("startedAt") is not None:
            started = timestamp(record["startedAt"])
            if started > when:
                raise EvidenceError("run_start_after_completion")
            summary["startedAt"] = started.isoformat()
        if record["status"] == "skipped_non_trading":
            closed = _core_contract.validate_calendar_evidence(record.get("calendarEvidence"), when)
            if closed["status"] != "closed" or record.get("target") != closed["previousTradingDate"] or record.get("payloadSha256") is not None:
                raise EvidenceError("unproven_non_trading_skip")
            summary["calendarClosedVerified"] = True
            summary["calendarTargetDate"] = closed["targetDate"]
            summary["previousTradingDate"] = closed["previousTradingDate"]
            summary["calendarEvidenceSha256"] = record["calendarEvidence"]["sha256"]
            summary["calendarSourceEvidenceSha256"] = calendar_source(record["calendarEvidence"])["sha256"]
        run_id = record.get("runId")
        if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id):
            summary["runId"] = run_id
        if record["status"] in FAILURES:
            failure = snapshot_source_failure(record.get("sourceFailure"))
            if failure is not None:
                summary["sourceFailure"] = failure
        stage = record.get("akshareStage") or {}
        if summary["stageUnavailable"]:
            failure = snapshot_source_failure(stage.get("sourceFailure"))
            if failure is not None:
                summary["stageSourceFailure"] = failure
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


def read_contract_policy(path):
    """Default is legacy. Presence of a core database never switches contracts."""
    try:
        return _contract_selector.read_policy(path)
    except (OSError, ValueError, TypeError):
        raise EvidenceError("invalid_contract_policy") from None


def core_market_rows(path, now):
    """Pure read-only contract checks; does not import the networked collector."""
    if path is None or not path.exists():
        return {}, "missing"
    if path.is_symlink() or not path.is_file():
        return {}, "invalid"
    rows = {}
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise EvidenceError("core_database_quick_check_failed")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables & {"snapshot", "research_appendix"}:
                raise EvidenceError("mixed_snapshot_contract_database")
            for day, schema, raw, claimed in conn.execute("SELECT date,schema,payload,payload_sha256 FROM core_market ORDER BY date"):
                iso_date(day)
                if not isinstance(raw, str):
                    raise EvidenceError("non_text_core_payload")
                actual = hashlib.sha256(raw.encode()).hexdigest()
                row = {"payloadSha256": actual, "state": "valid"}
                try:
                    value = strict_json(raw)
                    _core_contract.validate_core(value)
                    if value["date"] != day or schema != _core_contract.SCHEMA or claimed != actual:
                        raise EvidenceError("core_identity_or_hash_mismatch")
                    if timestamp(value["generatedAt"]) > now + dt.timedelta(minutes=5) or iso_date(day) > now.date():
                        raise EvidenceError("core_evidence_in_future")
                    coverage = value["qlib"]["coverage"]
                    row["quality"] = {"targetDate": day, "payloadSha256": actual,
                                      "priceStatus": "partial" if coverage["invalid"] else "complete",
                                      "valid": coverage["valid"], "invalid": coverage["invalid"],
                                      "eligible": coverage["eligible"], "ratio": coverage["ratio"],
                                      "generationTag": value["qlib"]["generation"]["tag"],
                                      "scope": "sealed_core_qlib_summary"}
                except (ValueError, KeyError, TypeError, AttributeError):
                    row.update(state="invalid", reason="invalid_core_contract_or_binding")
                rows[day] = row
    except (sqlite3.Error, OSError, ValueError):
        return {}, "invalid"
    return rows, "checked"


def recent_core_run(path, now):
    if path is None:
        return {"status": "unknown", "reason": "core_last_run_not_configured"}, None
    try:
        value = strict_json(read_file(path))
        if (value.get("schema") != _core_contract.SCHEMA or value.get("scope") != "core_market"
                or value.get("fullSnapshot") is not False):
            raise EvidenceError("wrong_run_contract")
        if value.get("status") == "skipped_non_trading":
            when = timestamp(value["timestamp"])
            calendar = _core_contract.validate_calendar_evidence(value.get("calendarEvidence"), when)
            if (when > now or calendar["status"] != "closed"
                    or value.get("target") != calendar["previousTradingDate"]
                    or not isinstance(value.get("runId"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["runId"])
                    or value.get("payloadSha256") is not None):
                raise EvidenceError("unproven_non_trading_skip")
            result = {"status": "skipped_non_trading", "timestamp": when.isoformat(), "target": value["target"],
                    "runId": value["runId"], "calendarTargetDate": calendar["targetDate"],
                    "calendarClosedVerified": True, "previousTradingDate": calendar["previousTradingDate"],
                    "calendarEvidenceSha256": value["calendarEvidence"]["sha256"],
                    "calendarSourceEvidenceSha256": calendar_source(value["calendarEvidence"])["sha256"],
                    "hasWarnings": False, "stageUnavailable": False}
            if value.get("startedAt") is not None:
                started = timestamp(value["startedAt"])
                if started > when:
                    raise EvidenceError("run_start_after_completion")
                result["startedAt"] = started.isoformat()
            return result, when
        result, when = recent_run(path, now)
        if value.get("status") in {"inserted", "skipped_existing", "idempotent", "success"}:
            if when is None:
                raise EvidenceError("invalid_core_run_time")
            calendar = _core_contract.validate_calendar_evidence(value.get("calendarEvidence"), when)
            if calendar["status"] != "open" or value.get("target") != calendar["targetDate"]:
                raise EvidenceError("core_success_target_not_current_calendar_day")
            result["calendarTargetDate"] = calendar["targetDate"]
            result["calendarEvidenceSha256"] = value["calendarEvidence"]["sha256"]
        if value.get("sourceWarning"):
            result["hasWarnings"] = True
        return result, when
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unknown", "reason": "missing_or_invalid_core_last_run"}, None


def calendar_source(evidence):
    """Original validated identity; an interval's derivation time is not observation time."""
    return evidence["binding"].get("sourceEvidence", evidence)


def active_run_decision(*, contract, last, rows, stages, now, calendar, evidence, issues, known_trading_dates):
    """One audit verdict for today's scheduled occurrence, preserving historical issues.

    The existing run record remains execution evidence. This result is a pure
    view over already-validated audit inputs; it creates no additional state.
    """
    day = now.astimezone(CST).date()
    scheduled = dt.datetime.combine(day, dt.time(22, 30), CST)
    deadline = dt.datetime.combine(day, dt.time(22, 45), CST)
    target = last.get("target")
    row = rows.get(target, {})
    run_id = last.get("runId")
    reasons = set()
    current_codes, historical_codes = set(), set()
    direct = {"independent_expected_day_unavailable", "qlib_not_current_for_expected_trading_day",
              "scheduled_core_market_missing" if contract == _core_contract.SCHEMA else "scheduled_snapshot_missing",
              "last_core_market_not_successful" if contract == _core_contract.SCHEMA else "last_snapshot_not_successful",
              "core_market_run_missing_or_mismatched" if contract == _core_contract.SCHEMA else "last_run_snapshot_missing_or_mismatched"}
    integrity = {"business_mode_not_frozen", "historical_rows_changed_or_missing", "future_snapshot_date",
                 "legacy_full_rows_after_cutover", "core_calendar_schedule_conflict", "core_rows_outside_contract_interval",
                 "core_history_policy_missing_or_conflicting"}
    for item in issues:
        code = item["code"]
        dates = set(item.get("dates") or item.get("affectedDates") or item.get("overdueStageDates") or [])
        dates.update(x for x in (item.get("tradeDate"), item.get("target"), item.get("dueDate")) if x)
        current = code in integrity or (code in direct and (not dates or day.isoformat() in dates))
        current = current or (calendar is not None and calendar["status"] == "open" and calendar["targetDate"] in dates)
        (current_codes if current else historical_codes).add(code)
        if dates - {day.isoformat()}:
            historical_codes.add(code)
    try:
        completed = timestamp(last["timestamp"])
    except (ValueError, KeyError, TypeError):
        completed = None
    try:
        started = timestamp(last["startedAt"])
    except (ValueError, KeyError, TypeError):
        started = None
    current_run = (started is not None and completed is not None and isinstance(run_id, str)
                   and re.fullmatch(r"[0-9a-f]{32}", run_id) is not None
                   and scheduled <= started <= completed <= now and completed.date() == day)
    expected = (calendar["targetDate"] if calendar and calendar["status"] == "open"
                else calendar["previousTradingDate"] if calendar else None)
    source = calendar_source(evidence) if evidence is not None else None
    calendar_identity = ({"status": calendar["status"], "targetDate": calendar["targetDate"],
                          "previousTradingDate": calendar["previousTradingDate"], "nextTradingDate": calendar["nextTradingDate"],
                          "evidenceSha256": evidence["sha256"], "sourceEvidenceSha256": source["sha256"],
                          "sourceDate": source["document"]["targetDate"], "observedAt": source["document"]["observedAt"],
                          "proofKind": evidence["binding"].get("kind", "sealed_archive")} if calendar else None)
    status = last.get("status", "unknown")
    if not current_run:
        state = "pending" if now < deadline else "insufficient"
        reasons.add("scheduled_run_pending" if now < deadline else "current_run_identity_missing_or_outside_window")
    elif status in FAILURES:
        state = "fail"
        reasons.add("current_run_not_successful")
    elif calendar is None:
        state = "insufficient"
        reasons.add("calendar_evidence_unavailable")
    elif completed > deadline:
        state = "fail"
        reasons.add("current_run_completed_after_deadline")
    elif current_codes & integrity:
        state = "fail"
        reasons.update(current_codes & integrity)
    elif status == "skipped_non_trading":
        closed = (calendar["status"] == "closed" and last.get("calendarClosedVerified") is True
                  and last.get("calendarTargetDate") == day.isoformat() and target == expected
                  and last.get("calendarSourceEvidenceSha256") == source["sha256"]
                  and day.isoformat() not in known_trading_dates
                  and last.get("payloadSha256") is None)
        state = "not_required" if closed else "fail"
        if not closed:
            reasons.add("current_closed_skip_binding_mismatch")
    elif status in {"inserted", "skipped_existing", "idempotent", "success"}:
        if calendar["status"] != "open" or target != expected:
            reasons.add("current_run_target_calendar_mismatch")
        if row.get("state") != "valid" or not last.get("payloadSha256") or row.get("payloadSha256") != last["payloadSha256"]:
            reasons.add("current_run_artifact_missing_or_mismatched")
        if contract == _core_contract.LEGACY_SCHEMA and stages.get(target, {}).get("state") != "valid":
            reasons.add("current_full_stage_missing_or_invalid")
        reasons.update(current_codes & direct)
        state = "fail" if reasons else "pass"
    else:
        state = "insufficient"
        reasons.add("current_run_terminal_unconfirmed")
    warning_code = "core_market_material_warning" if contract == _core_contract.SCHEMA else "last_snapshot_material_warning"
    material_warnings = [warning_code] if current_run and (last.get("hasWarnings") or last.get("stageUnavailable")) else []
    current_codes.update(material_warnings)
    return {"schema": "research.active-run-decision.v1", "contract": contract,
            "scope": "core_market" if contract == _core_contract.SCHEMA else "full_snapshot",
            "fullSnapshot": contract == _core_contract.LEGACY_SCHEMA,
            "artifactKind": "qlib_market_summary" if contract == _core_contract.SCHEMA else "full_snapshot",
            "scheduledDate": day.isoformat(), "scheduledFor": scheduled.isoformat(), "deadlineAt": deadline.isoformat(),
            "evaluatedAt": now.isoformat(), "expectedTargetDate": expected,
            "run": {"runId": run_id, "status": status, "targetDate": target,
                    "startedAt": started.isoformat() if started else None,
                    "completedAt": completed.isoformat() if completed else None},
            "verification": {"status": state, "reasons": sorted(reasons),
                             "scope": "local_run_artifact_calendar_identity", "currentIssueCodes": sorted(current_codes),
                             "materialWarningCodes": material_warnings,
                             "historicalIssueCodes": sorted(historical_codes)},
            "artifact": {"payloadSha256": last.get("payloadSha256"), "rowState": row.get("state", "missing"),
                         "quality": row.get("quality") if last.get("payloadSha256")
                         and last["payloadSha256"] == row.get("payloadSha256") else None}, "calendar": calendar_identity}


def audit(db, qlib, stages, last_run, mode, since, monitor_since, now, baseline=None,
          contract_policy=None, core_db=None, core_last_run=None, calendar_project=None):
    since, monitor_since = iso_date(since), iso_date(monitor_since)
    if since > monitor_since or monitor_since > now.date():
        raise EvidenceError("invalid_audit_window")
    calendar, complete, gen = qlib_evidence(qlib)
    if calendar[0] > since.isoformat() or iso_date(complete) > now.date():
        raise EvidenceError("calendar_coverage_or_complete_date_invalid")
    rows = snapshot_rows(db)
    policy = read_contract_policy(contract_policy)
    effective = _contract_selector.first_core_date(policy)
    core_active = _contract_selector.select(policy, now) == _core_contract.SCHEMA
    def full_expected_on(day):
        return _contract_selector.contract_for_day(policy, day) == _core_contract.LEGACY_SCHEMA
    has_core_storage = core_db is not None and (core_db.exists() or core_db.is_symlink())
    core_rows, core_read_state = (core_market_rows(core_db, now) if effective is not None or has_core_storage
                                 else ({}, "not_configured"))
    core_last, core_last_time = (recent_core_run(core_last_run, now) if effective is not None
                                 else ({"status": "not_configured"}, None))
    stage_rows, stage_state = stage_evidence(stages, now)
    last, last_time = recent_run(last_run, now)
    business_mode = read_file(mode, 32).decode().strip()
    issues = []

    def issue(code, severity="warning", **details):
        issues.append({"code": code, "severity": severity, **details})

    if effective is None and (core_rows or core_read_state == "invalid"):
        issue("core_history_policy_missing_or_conflicting", "critical", dates=sorted(core_rows))

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
            # Counts and threshold refer to the same full-market coverage row.
            # CSI union is disclosed separately, never paired with eligible.
            invalid_count = coverage_last.get("invalid")
            if type(invalid_count) is not int or invalid_count < 0 or type(eligible) is not int or eligible <= 0:
                proven, invalid_count = False, 0
            scoped = _absence_policy.scopes(calendar, complete, coverage_last, index,
                                             absence_dates, quality.get("lastQuotedDate") or {}, proven)
            failures = {scope: value["escalation"] for scope, value in scoped.items() if value["escalation"]}
            severity = "warning" if failures else "information"
            escalation = ";".join(f"{scope}:{reason}" for scope, reason in sorted(failures.items())) or None
            proven = all(value["proven"] for value in scoped.values())
            issue("qlib_source_absence", severity=severity,
                  dataQuality="partial", validationScope="local_manifest_calendar_and_reference_only",
                  affectedDates=[row["target_date"] for row in quality["dailyCoverage"] if row["invalid"]],
                  currentInventoryQuality=quality["priceStatus"], csiInventoryQuality=index["status"],
                  meaning=index["absenceMeaning"], proven=proven,
                  absenceTolerance=round(tolerance, 1), invalidCount=invalid_count,
                  countScope="full_market_daily_coverage", eligibleCount=eligible,
                  csiMissingMemberCount=len(missing_members),
                  csiMissingQuoteMembers=index["missingQuoteMembers"],
                  csiMissingPreviousCloseMembers=index["missingPreviousCloseMembers"],
                  absenceScopes=scoped,
                  escalation=escalation,
                  sourceAbsenceSlotsVerified=index["sourceAbsenceSlotsVerified"])
        if index["legacyUnquotedMembers"]:
            legacy_severity, legacy_escalation = legacy_unquoted_severity(
                index["legacyUnquotedMeaning"] == LEGACY_UNQUOTED_MEANING,
                index["legacyUnquotedMembers"], REFERENCES)
            issue("qlib_legacy_unquoted_csi", severity=legacy_severity,
                  dataQuality="partial", validationScope="local_manifest_calendar_and_reference_only",
                  symbols=index["legacyUnquotedMembers"], meaning=index["legacyUnquotedMeaning"],
                  proven=legacy_severity == "information", escalation=legacy_escalation,
                  # Dates describe different objects. Use only this validated
                  # generation, never the audit clock or a later market read.
                  targetDate=index["targetDate"],
                  affectedDates=sorted({day for dates in quality["index"]["legacyAffectedDates"].values()
                                        for day in dates}),
                  legacyUnquotedBaseDate=index["legacyUnquotedBaseDate"],
                  lastQuotedDates=quality["index"]["legacyLastQuotedDates"],
                  sourceManifestSha256=gen[2])
    if business_mode != "FROZEN":
        issue("business_mode_not_frozen", "critical")
    if any(iso_date(d) > now.date() for d in rows):
        issue("future_snapshot_date", "critical")
    if any(d > complete for d in rows):
        issue("snapshot_ahead_of_qlib_reference")
    # AkShare market stage is mandatory only for the old full contract. On
    # cutover, keep its historical evidence and anomalies without making a
    # missing optional current stage a new core failure.
    mandatory_stages = {d: v for d, v in stage_rows.items() if full_expected_on(d)}
    if ((not core_active and stage_state != "checked")
            or any(v["state"] != "valid" for v in mandatory_stages.values())):
        issue("market_stage_missing_or_invalid", contract=_core_contract.LEGACY_SCHEMA,
              historicalOnly=core_active)
    elif not core_active and not stage_rows:
        issue("no_sealed_market_evidence")
    calendar_set = set(calendar)
    for kind, records in (("snapshot", rows), ("stage", mandatory_stages)):
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
    # CLI always supplies the independent, already-collected calendar. Library
    # callers without a calendar retain a historical-only view, never a claim
    # that Qlib alone establishes today's expected target.
    expected = None
    expected_evidence = expected_calendar = None
    if calendar_project is not None:
        try:
            expected_evidence = _calendar_evidence.load_calendar_evidence(calendar_project, now)
            expected_calendar = _core_contract.validate_calendar_evidence(expected_evidence, now)
            expected = (expected_calendar["targetDate"] if expected_calendar["status"] == "open"
                        else expected_calendar["previousTradingDate"])
        except (ValueError, OSError, TypeError, KeyError):
            expected_evidence = expected_calendar = None
            if now >= deadline:
                issue("independent_expected_day_unavailable", dueDate=due_date.isoformat())
        else:
            if now >= deadline and complete != expected:
                issue("qlib_not_current_for_expected_trading_day", dueDate=due_date.isoformat(),
                      expectedTarget=expected, actualTarget=complete,
                      calendarEvidenceSha256=expected_evidence["sha256"])
    full_run_due = full_expected_on(due_date.isoformat())
    if full_run_due and (last_time is None or last_time < due_start):
        issue("scheduled_snapshot_missing", dueDate=due_date.isoformat())
    if full_run_due and last["status"] in FAILURES:
        details = {"status": last["status"], "target": last.get("target"), "dueDate": due_date.isoformat(),
                   "runAt": last.get("timestamp")}
        if last.get("runId"):
            details["runId"] = last["runId"]
        if last.get("sourceFailure"):
            details["sourceFailure"] = last["sourceFailure"]
        issue("last_snapshot_not_successful", **details)
    if full_run_due and (last.get("hasWarnings") or last.get("stageUnavailable")):
        details = {"runStatus": last["status"], "target": last.get("target"),
                   "runAt": last.get("timestamp"),
                   "stageUnavailable": bool(last.get("stageUnavailable"))}
        if last.get("runId"):
            details["runId"] = last["runId"]
        if last.get("stageSourceFailure"):
            details["stageSourceFailure"] = last["stageSourceFailure"]
        issue("last_snapshot_material_warning", **details)
    ledger = []
    new_due_gaps = []
    ledger_calendar = sorted(set(calendar) | ({expected} if expected is not None else set()))
    ledger_end = max(complete, expected or complete)
    for day in (d for d in ledger_calendar if since.isoformat() <= d <= ledger_end):
        row = rows.get(day, {"state": "missing"})
        stage = stage_rows.get(day, {"state": "missing"})
        legacy = day < monitor_since.isoformat()
        pending = day == now.date().isoformat() and now < deadline
        full_expected = full_expected_on(day)
        item = {"tradeDate": day, "snapshotState": row["state"], "stageState": stage["state"],
                "contract": _core_contract.LEGACY_SCHEMA, "fullSnapshot": True,
                "expectedUnderContract": full_expected,
                "classification": "legacy" if legacy else "monitored", "pendingWindow": pending,
                "qlibAvailability": "not_current" if day > complete else "calendar_and_reference_evidence_only",
                "recoveryReadiness": "not_applicable" if row["state"] == "valid" else (
                    "partial_evidence_only" if stage["state"] == "valid" else "missing_or_invalid_market_evidence")}
        if row["state"] != "missing":
            item["payloadSha256"] = row["payloadSha256"]
        if row.get("reason"):
            item["reason"] = row["reason"]
        if row["state"] != "valid" and not legacy and not pending and full_expected:
            new_due_gaps.append(day)
        ledger.append(item)
    legacy_bad = [row["tradeDate"] for row in ledger if row["classification"] == "legacy" and row["snapshotState"] != "valid" and row["expectedUnderContract"]]
    if legacy_bad:
        issue("legacy_gaps_or_invalid_rows", dates=legacy_bad)
    if new_due_gaps:
        issue("unresolved_trading_day_gaps", "critical" if len(new_due_gaps) >= 2 else "warning", dates=new_due_gaps)
    core_ledger = []
    core_gaps = []
    if effective is not None:
        unexpected_full = sorted(d for d in rows if not full_expected_on(d))
        if unexpected_full:
            issue("legacy_full_rows_after_cutover", "critical", dates=unexpected_full)
        for day in (d for d in ledger_calendar if max(effective, since.isoformat()) <= d <= ledger_end and not full_expected_on(d)):
            row = core_rows.get(day, {"state": "missing"})
            pending = day == now.date().isoformat() and now < deadline
            core_ledger.append({"tradeDate": day, "coreState": row["state"], "pendingWindow": pending,
                                "contract": _core_contract.SCHEMA, "fullSnapshot": False,
                                **{k: row[k] for k in ("payloadSha256", "reason") if k in row}})
            if row["state"] != "valid" and not pending:
                core_gaps.append(day)
        if core_read_state != "checked" and (core_ledger or core_read_state == "invalid"):
            issue("core_market_evidence_unavailable", readState=core_read_state)
        if not full_run_due:
            if core_last_time is None or core_last_time < due_start:
                issue("scheduled_core_market_missing", dueDate=due_date.isoformat())
            if core_last["status"] in FAILURES:
                issue("last_core_market_not_successful", status=core_last["status"],
                      target=core_last.get("target"), runAt=core_last.get("timestamp"), dueDate=due_date.isoformat())
            if (core_last["status"] == "skipped_non_trading"
                    and core_last.get("calendarTargetDate") in calendar_set):
                issue("core_calendar_schedule_conflict", "critical", dates=[core_last["calendarTargetDate"]])
            if core_last.get("hasWarnings") or core_last.get("stageUnavailable"):
                issue("core_market_material_warning", target=core_last.get("target"), runAt=core_last.get("timestamp"))
            if core_last["status"] in {"inserted", "skipped_existing", "idempotent", "success"}:
                claimed = core_rows.get(core_last.get("target"), {})
                if (claimed.get("state") != "valid" or not core_last.get("payloadSha256")
                        or claimed.get("payloadSha256") != core_last["payloadSha256"]):
                    issue("core_market_run_missing_or_mismatched", "critical", tradeDate=core_last.get("target"))
        if core_gaps:
            issue("unresolved_core_market_gaps", "critical" if len(core_gaps) >= 2 else "warning", dates=core_gaps)
        wrong_contract = sorted(d for d in core_rows if full_expected_on(d))
        if wrong_contract:
            issue("core_rows_outside_contract_interval", "critical", dates=wrong_contract)
        outside = sorted(d for d in core_rows if calendar[0] <= d <= calendar[-1] and d not in calendar_set)
        if outside:
            issue("core_market_outside_known_trading_calendar", dates=outside)
    latest_stage = max((d for d, v in mandatory_stages.items() if v["state"] == "valid"), default=None)
    pending_upstream = bool(latest_stage and latest_stage > complete
                            and latest_stage == now.date().isoformat() and now < deadline)
    overdue_stages = sorted(d for d, value in mandatory_stages.items() if value["state"] == "valid"
                            and d > complete and (d < now.date().isoformat() or now >= deadline))
    if overdue_stages:
        issue("qlib_behind_observed_market_stage", pendingWindow=False,
              contract=_core_contract.LEGACY_SCHEMA, historicalOnly=core_active,
              overdueStageDates=overdue_stages, latestStageDate=latest_stage, qlibCompleteDate=complete)
    if generation(qlib) != gen:
        raise EvidenceError("qlib_generation_changed_during_audit")
    active_contract = _core_contract.SCHEMA if core_active else _core_contract.LEGACY_SCHEMA
    decision = active_run_decision(contract=active_contract, last=core_last if core_active else last,
                                   rows=core_rows if core_active else rows, stages=stage_rows, now=now,
                                   calendar=expected_calendar, evidence=expected_evidence, issues=issues,
                                   known_trading_dates=calendar_set)
    return {"schemaVersion": 1, "generatedAt": now.isoformat(), "readOnly": True,
            "activeContract": _core_contract.SCHEMA if core_active else _core_contract.LEGACY_SCHEMA,
            "effectiveTradeDate": effective, "contractPolicy": policy,
            "contractIntervals": _contract_selector.intervals(policy),
            "activeLastRun": core_last if core_active else last,
            "activeRunDecision": decision,
            "legacyFullSnapshot": {"contract": _core_contract.LEGACY_SCHEMA,
                                   "scheduledBefore": effective if len(_contract_selector.transitions(policy)) <= 1 else None,
                                   "intervals": ([{"fromInclusive": None, "untilExclusive": effective}] if effective else []) +
                                       [{"fromInclusive": i["fromInclusive"], "untilExclusive": i["untilExclusive"]}
                                        for i in _contract_selector.intervals(policy) if i["activeContract"] == _core_contract.LEGACY_SCHEMA],
                                   "status": "historical_only" if core_active else "active"},
            "coreMarket": {"contract": _core_contract.SCHEMA, "fullSnapshot": False,
                           "artifactKind": "qlib_market_summary", "includesCompletePriceArchive": False,
                           "readState": core_read_state, "rowCount": len(core_rows),
                           "latestValidDate": max((d for d, v in core_rows.items() if v["state"] == "valid"), default=None),
                           "rowHashes": {d: v["payloadSha256"] for d, v in core_rows.items()},
                           "lastRun": core_last, "ledger": core_ledger, "expectedFrom": effective,
                           "quality": (core_rows.get(core_last.get("target"), {}).get("quality")
                                       if core_last.get("payloadSha256") == core_rows.get(core_last.get("target"), {}).get("payloadSha256")
                                       and core_last.get("status") in {"inserted", "skipped_existing", "idempotent", "success"}
                                       else None)},
            "status": "critical" if any(i["severity"] == "critical" for i in issues) else "warning" if issues else "ok",
            "scope": "local_evidence_only", "businessMode": business_mode,
            "auditSince": since.isoformat(), "monitorSince": monitor_since.isoformat(),
            "schedule": {"expectedLastRunDate": due_date.isoformat(), "todayDeadline": deadline.isoformat()},
            "independentExpectedMarketDate": expected,
            "independentCalendarStatus": "verified" if expected is not None else "unavailable",
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
    parser.add_argument("--contract-policy", type=Path)
    parser.add_argument("--core-db", type=Path)
    parser.add_argument("--core-last-run", type=Path)
    parser.add_argument("--calendar-project", type=Path,
                        default=Path("/home/ubuntu/AStock_AI_System"))
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.db, args.qlib, args.stages, args.last_run, args.mode,
                       args.since, args.monitor_since, dt.datetime.now(CST), args.baseline,
                       args.contract_policy, args.core_db, args.core_last_run, args.calendar_project)
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
