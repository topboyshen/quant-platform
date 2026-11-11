#!/usr/bin/env python3
"""Commit-pinned, validated Qlib market-data append. No business task imports."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import struct
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from atomic import LockBusy, Transaction, read_small
from source import CurlTransport, DoltClient, SourceError, TABLE_KEYS
from alignment import validate_index_market_alignment

RAW_COLUMNS = ("tradedate", "symbol", "open", "high", "low", "close", "adjclose", "volume", "amount")
SYMBOL = re.compile(r"(?:SH|SZ|BJ)[0-9]{6}\Z")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def source_client():
    # The real pinned final COUNT took 23.35 s. A 20 s attempt limit cut off
    # legitimate slow responses; the total job/query/byte limits remain bounded.
    return DoltClient(transport=CurlTransport(timeout=40), page_size=500,
                      max_rows=100000, max_requests=320, max_seconds=1000)


class InterruptedRun(BaseException):
    def __init__(self, signum):
        self.signum = signum
        # Capture arrival before Transaction.__exit__ cleans the abandoned
        # generation; its IO must not be attributed to the interrupted phase.
        self.observed_monotonic = time.monotonic()
        self.observed_at = datetime.now(SHANGHAI).isoformat()


def iso(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid_iso_date")
    return value


def scalar(client, sql, key):
    rows = client.query(sql)
    if len(rows) != 1 or set(rows[0]) != {key}:
        raise ValueError("invalid_scalar_response")
    return rows[0][key]


def read_calendar(root):
    raw = read_small(root / "calendars/day.txt", 1024 * 1024)
    days = raw.decode("ascii").splitlines()
    if not raw.endswith(b"\n") or not days or days != sorted(set(days)):
        raise ValueError("invalid_base_calendar")
    for day in days:
        iso(day)
    return days


def read_raw(rows, ref):
    output = []
    for row in rows:
        if set(row) != set(RAW_COLUMNS) or not SYMBOL.fullmatch(row["symbol"]):
            raise ValueError("unexpected_market_row_schema_or_symbol")
        converted = {"date": iso(row["tradedate"]), "symbol": row["symbol"], "source_commit": ref}
        for key in RAW_COLUMNS[2:]:
            raw = row[key]
            if raw is None:
                if key != "amount":
                    raise ValueError("unexpected_null_market_field")
                converted[key] = None
            else:
                try:
                    value = float(raw)
                except (ValueError, TypeError):
                    raise ValueError("invalid_market_number") from None
                if not math.isfinite(value):
                    raise ValueError("nonfinite_market_number")
                converted[key] = value
        output.append(converted)
    return output


class ClosedIndexQuarantine:
    """Bounded exception for four audited rows, never a general calendar bypass."""

    POLICY = "qlib-closed-index-20260912-v1"
    CLOSED_DATE = "2026-09-12"
    SYMBOLS = frozenset(("SH000300", "SH000852", "SH000905", "SH000906"))

    def __init__(self, client, ref, calendar, emit=None):
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-v]{32}", ref):
            raise ValueError("invalid_quarantine_source_commit")
        self.client, self.ref = client, ref
        self.calendar = frozenset(iso(day) for day in calendar)
        self.emit = emit
        self._proof = None
        self._rows = {}

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()

    def _closed_proof(self):
        if self.client.latest_ref() != self.ref:
            raise ValueError("quarantine_source_commit_changed")
        if self._proof is None:
            rows = self.client.query(
                f"SELECT exchange,date,is_open FROM {self.client.table('ts_trade_day_calendar')} "
                f"WHERE exchange = 'SSE' AND date = '{self.CLOSED_DATE}' LIMIT 2")
            if self.client.latest_ref() != self.ref:
                raise ValueError("quarantine_source_commit_changed")
            expected = {"exchange": "SSE", "date": self.CLOSED_DATE, "is_open": "0"}
            if rows != [expected] or self.CLOSED_DATE in self.calendar:
                raise ValueError("quarantine_closed_calendar_not_proven")
            self._proof = {**expected, "sourceCommit": self.ref}

    def filter_rows(self, rows, *, start, end, symbol=None):
        start, end = iso(start), iso(end)
        if start > end or (symbol is not None and
                           (not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol))):
            raise ValueError("invalid_quarantine_request")
        kept, pending, seen = [], {}, set()
        # Validate the complete response before dropping or recording anything.
        # Pagination/count checks have already completed in fetch_prices.
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or not SYMBOL.fullmatch(row["symbol"]):
                raise ValueError("unexpected_market_row_schema_or_symbol")
            day = iso(row.get("tradedate"))
            if not start <= day <= end or (symbol is not None and row["symbol"] != symbol):
                raise ValueError("market_response_outside_requested_range")
            key = (day, row["symbol"])
            if key in seen:
                raise ValueError("duplicate_market_row")
            seen.add(key)
            if day == self.CLOSED_DATE and row["symbol"] in self.SYMBOLS:
                if day in self.calendar:
                    raise ValueError("quarantine_calendar_conflicts_with_known_closed_date")
                pending[key] = dict(row)
            elif day in self.calendar:
                kept.append(row)
            else:
                raise ValueError(f"market_row_outside_exchange_calendar:{day}:{row['symbol']}")
        if pending:
            # Malformed/non-finite payloads remain failures, even for an allowed pair.
            read_raw(list(pending.values()), self.ref)
            for key, row in pending.items():
                if key in self._rows and self._rows[key] != row:
                    raise ValueError("fixed_source_row_disagrees")
            self._closed_proof()
            additions = set(pending) - self._rows.keys()
            self._rows.update(pending)
            if additions and self.emit is not None:
                self.emit(self.evidence())
        return kept

    def evidence(self):
        if not self._rows:
            return None
        ordered = [self._rows[key] for key in sorted(self._rows)]
        return {"policyId": self.POLICY, "sourceCommit": self.ref,
                "calendarEvidence": dict(self._proof), "count": len(ordered),
                "records": [{"date": row["tradedate"], "symbol": row["symbol"],
                             "rowSha256": self._digest(row)} for row in ordered],
                "rowsSha256": self._digest(ordered)}


class TailGapException:
    """Exchange-announced suspensions longer than ten days; not a general bypass.

    The generic ten-day tail-gap gate is unchanged. Each record is pinned by
    SHA-256, names one symbol with its exact gap dates, binds the official
    announcements by file hash and the probed source rows by row hash, and is
    active only while the installed base date is the last gap date. Every run
    re-proves at its own pinned commit that the gap has zero source rows and the
    resume day has a real row. Gap days keep the existing representation (NaN
    prices, carried factor); nothing is filled, excluded or rewritten.

    Multiple records may be registered (RECORD_REGISTRY); each must satisfy the
    same schema and is hash-pinned independently.
    """

    POLICY = "qlib-tail-gap-SZ301139-20260831-v1"
    RECORD = Path(__file__).resolve().with_name("exceptions") / "tail-gap-SZ301139-20260831.json"
    RECORD_SHA256 = "3ed73aa02e5f207eedc250c41cd6a6964d9ac72d9143080e11bfa481984afbb2"
    POLICY_FORMAT = re.compile(r"qlib-tail-gap-(?:SH|SZ)\d{6}-\d{8}-v\d+")
    RECORD_REGISTRY = (
        (RECORD, RECORD_SHA256),
        (RECORD.with_name("tail-gap-SZ002731-20260901.json"),
         "bddd338b6bf08de2ff0a4b30f46300818f923404c0987879727ff6af61ce8049"),
    )

    def __init__(self, old_calendar, *, record_path=None, record_sha256=None):
        if record_path is not None:
            registry = ((Path(record_path), record_sha256 or self.RECORD_SHA256),)
        else:
            registry = self.RECORD_REGISTRY
        self.records = {}
        self.digests = {}
        self.allowances = {}
        for path, expected_sha in registry:
            raw = read_small(path, 64 * 1024)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != (expected_sha or ""):
                raise ValueError("tail_gap_exception_record_changed")
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("tail_gap_exception_record_invalid")
            symbol = record.get("symbol")
            gap = record.get("gapDates")
            if (not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol)
                    or not isinstance(record.get("policyId"), str)
                    or not self.POLICY_FORMAT.fullmatch(record["policyId"])
                    or symbol in self.records
                    or not isinstance(gap, list) or len(gap) <= 10 or gap != sorted(set(gap))
                    or any(iso(day) != day for day in gap)
                    or not iso(record.get("lastStoredDate")) < gap[0] or not gap[-1] < iso(record.get("resumeDate"))
                    or record.get("requiredBaseDate") != gap[-1]):
                raise ValueError("tail_gap_exception_record_invalid")
            probe = record.get("sourceProbe")
            if (not isinstance(probe, dict) or probe.get("gapRowsReturned") != 0
                    or any(not isinstance(probe.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", probe[key])
                           for key in ("lastStoredRowSha256", "resumeRowSha256"))):
                raise ValueError("tail_gap_exception_record_invalid")
            documents = record.get("officialEvidence")
            if not isinstance(documents, list) or len(documents) < 2:
                raise ValueError("tail_gap_exception_evidence_missing")
            for document in documents:
                name = document.get("file") if isinstance(document, dict) else None
                if not isinstance(name, str) or not re.fullmatch(r"evidence/[A-Za-z0-9._-]+\.pdf", name):
                    raise ValueError("tail_gap_exception_evidence_missing")
                if hashlib.sha256(read_small(path.parent / name, 4 * 1024 * 1024)).hexdigest() != document.get("sha256"):
                    raise ValueError("tail_gap_exception_evidence_changed")
            self.records[symbol] = record
            self.digests[symbol] = digest
            last = record["lastStoredDate"]
            active = (old_calendar[-1] == record["requiredBaseDate"] and last in old_calendar
                      and list(old_calendar[old_calendar.index(last) + 1:]) == gap)
            if active:
                self.allowances[symbol] = tuple(gap)
        # Backward-compatible single-record views (registry order = priority).
        last_symbol = next(reversed(self.records))
        self.record = self.records[last_symbol]
        self.digest = self.digests[last_symbol]

    def confirm(self, inspections, confirmed_missing, raw_map, new_days, ref):
        """Return manifest evidence when an allowance was actually exercised."""
        if not self.allowances:
            return None
        for symbol, gap in self.allowances.items():
            record = self.records[symbol]
            info = inspections.get(symbol)
            if info is None or tuple(info.get("gap_dates", ())) != gap:
                continue
            if (info.get("last_date") != record["lastStoredDate"]
                    or tuple(confirmed_missing.get(symbol, ())) != gap
                    or any((symbol, day) in raw_map for day in gap)):
                raise ValueError("tail_gap_exception_source_rows_present")
            resume, probe = record["resumeDate"], record["sourceProbe"]
            last_row, row = raw_map.get((symbol, info["last_date"])), raw_map.get((symbol, resume))
            if resume not in new_days or row is None or last_row is None:
                raise ValueError("tail_gap_exception_resume_row_missing")
            if read_raw([row], ref)[0]["volume"] <= 0:
                raise ValueError("tail_gap_exception_resume_row_invalid")
            digest = ClosedIndexQuarantine._digest
            if digest(row) != probe["resumeRowSha256"] or digest(last_row) != probe["lastStoredRowSha256"]:
                raise ValueError("tail_gap_exception_source_row_differs_from_record")
            return {"policyId": record["policyId"], "recordSha256": self.digests[symbol], "symbol": symbol,
                    "lastStoredDate": info["last_date"], "gapDates": list(gap), "resumeDate": resume,
                    "sourceCommit": ref, "resumeRowSha256": digest(row), "lastStoredRowSha256": digest(last_row),
                    "officialEvidenceSha256": [document["sha256"] for document in record["officialEvidence"]],
                    "meaning": record["meaning"]}
        return None


def completed_cutoff(now):
    now = now.astimezone(SHANGHAI)
    # Conservative end-of-day cut-off. Never admit same-day intraday rows.
    return (now.date() if now.hour >= 18 else now.date() - timedelta(days=1)).isoformat()


def fetch_calendar(client, cutoff):
    rows = client.paginated_rows("ts_trade_day_calendar", ("exchange", "date", "is_open"),
        f"exchange = 'SSE' AND is_open = 1 AND date >= '2000-01-04' AND date <= '{iso(cutoff)}'",
        ("exchange", "date"))
    if not rows or any(row["exchange"] != "SSE" or row["is_open"] != "1" for row in rows):
        raise ValueError("invalid_exchange_calendar")
    days = [iso(row["date"]) for row in rows]
    if days != sorted(set(days)) or days[0] < "2000-01-04" or days[-1] > cutoff:
        raise ValueError("duplicate_exchange_calendar")
    return days



REQUIRED_MARKET_SYMBOLS = ("SH600000", "SZ000001", "SH600519", "SZ300750",
                            "SH000300", "SH000905", "SH000852", "SH000906", "SZ399300")


def require_symbols_at_target(client, target):
    """Fail before the market download unless the pinned commit has that exact day.

    MAX(tradedate) is not enough: a later row must not hide a missing target day.
    A missing row is not a suspension and is not filled in.
    """
    listed = ", ".join("'" + symbol + "'" for symbol in REQUIRED_MARKET_SYMBOLS)
    rows = client.query(
        "SELECT symbol, tradedate FROM " + client.table("final_a_stock_eod_price")
        + " WHERE tradedate = '" + target + "' AND symbol IN (" + listed + ")")
    if not isinstance(rows, list) or len(rows) > len(REQUIRED_MARKET_SYMBOLS):
        raise ValueError("required_symbol_check_invalid")
    found = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("tradedate") != target:
            raise ValueError("required_symbol_check_invalid")
        symbol = row.get("symbol")
        if symbol not in REQUIRED_MARKET_SYMBOLS or symbol in found:
            raise ValueError("required_symbol_check_invalid")
        found.add(symbol)
    missing = [symbol for symbol in REQUIRED_MARKET_SYMBOLS if symbol not in found]
    if missing:
        raise ValueError("required_reference_or_index_not_at_target:" + missing[0])


def fetch_prices(client, where):
    return client.paginated_rows("final_a_stock_eod_price", RAW_COLUMNS, where, ("tradedate", "symbol"))


def fetch_historical_gaps(client, inspections, old_calendar, raw_map, quarantine=None, tail_gap_allowances=None):
    """Prove absent old-tail rows by complete pinned queries, never by inference.

    New-day absence uses a separate full-market proof. A gap remains explicit
    missing data, not a claim that the exchange officially suspended a stock.
    """
    confirmed, proof = {}, {}
    for symbol, info in inspections.items():
        gaps = tuple(info.get("gap_dates", ()))
        if not gaps:
            continue
        end = info["last_date"]
        expected = tuple(day for day in old_calendar if day > end)
        allowed = (tail_gap_allowances or {}).get(symbol)
        bounded = 0 < len(gaps) <= 10 or (len(gaps) > 10 and allowed is not None and tuple(allowed) == gaps)
        if not SYMBOL.fullmatch(symbol) or gaps != expected or not bounded:
            raise ValueError("invalid_historical_gap_request")
        extra = fetch_prices(client, f"symbol = '{symbol}' AND tradedate >= '{gaps[0]}' AND tradedate <= '{gaps[-1]}'")
        if quarantine is not None:
            extra = quarantine.filter_rows(extra, start=gaps[0], end=gaps[-1], symbol=symbol)
        seen = set()
        for row in extra:
            day = iso(row["tradedate"])
            if row["symbol"] != symbol or day not in gaps or day in seen:
                raise ValueError("historical_gap_response_outside_request")
            seen.add(day)
            key = (symbol, day)
            if key in raw_map and raw_map[key] != row:
                raise ValueError("fixed_source_row_disagrees")
            raw_map[key] = row
        absent = tuple(day for day in gaps if day not in seen)
        if any((symbol, day) in raw_map for day in absent):
            raise ValueError("historical_gap_absence_disagrees")
        if absent:
            confirmed[symbol] = absent
        proof[symbol] = {"from": gaps[0], "through": gaps[-1], "returnedDates": sorted(seen),
                         "absentDates": list(absent), "meaning": "source_absent_not_confirmed_suspension"}
    return confirmed, proof


def verify_anchor_intervals(client, inspections, old_calendar, raw_map, quarantine=None):
    """The latest raw valid close must really precede the appended segment."""
    bulk_start = old_calendar[-3]
    for symbol, info in inspections.items():
        if info.get("new") or info.get("short_history"):
            continue
        expected = tuple(info["last3_valid_dates"])
        end = info["last_date"]
        if len(expected) != 3 or expected != tuple(sorted(set(expected))) or expected[-1] > end:
            raise ValueError("invalid_old_anchor_interval")
        if len([d for d in old_calendar if expected[0] <= d <= end]) > 180:
            raise ValueError("old_anchor_interval_requires_manual_review")
        if expected[0] < bulk_start:
            extra = fetch_prices(client, f"symbol = '{symbol}' AND tradedate >= '{expected[0]}' AND tradedate <= '{end}'")
            if quarantine is not None:
                extra = quarantine.filter_rows(extra, start=expected[0], end=end, symbol=symbol)
            seen = set()
            for row in extra:
                day = iso(row["tradedate"])
                if row["symbol"] != symbol or not expected[0] <= day <= end or day not in old_calendar or day in seen:
                    raise ValueError("anchor_response_outside_request")
                seen.add(day)
                key = (symbol, day)
                if key in raw_map and raw_map[key] != row:
                    raise ValueError("fixed_source_row_disagrees")
                raw_map[key] = row
            if any(s == symbol and expected[0] <= d <= end and d not in seen for s, d in raw_map):
                raise ValueError("anchor_range_absence_disagrees")
        rows = [raw_map[(symbol, day)] for day in old_calendar
                if expected[0] <= day <= end and (symbol, day) in raw_map]
        # Full schema/numeric validation precedes interpreting a row as valid.
        converted = read_raw(rows, client.latest_ref())
        actual = tuple(row["date"] for row in converted if row["volume"] > 0)
        if actual != expected:
            raise ValueError(f"source_revises_old_valid_dates:{symbol}")


def read_price_inventory(root):
    records = {}
    for line in read_small(root / "instruments/all.txt", 16 * 1024 * 1024).decode("ascii").splitlines():
        cells = line.split()
        if len(cells) != 3 or not SYMBOL.fullmatch(cells[0]) or cells[0] in records:
            raise ValueError("invalid_price_inventory")
        start, end = iso(cells[1]), iso(cells[2])
        if start > end:
            raise ValueError("inverted_price_interval")
        records[cells[0]] = (start, end)
    return records


def legacy_index_absence(root, base, new_days, raw_map, source_symbols, ref):
    """Disclose already-unquoted CSI inventory; never create new price slots."""
    from indices import INDEX_MAP, interval_symbol
    intervals = read_price_inventory(root)
    members = set()
    for name in INDEX_MAP:
        for line in read_small(root / "instruments" / (name + ".txt"), 16 * 1024 * 1024).decode("ascii").splitlines():
            cells = line.split()
            if len(cells) != 3:
                raise ValueError("invalid_index_member_row")
            start, end = iso(cells[1]), iso(cells[2])
            if start > end:
                raise ValueError("invalid_index_member_row")
            symbol = interval_symbol(cells[0], end, base)
            if start <= base <= end:
                members.add(symbol)
    candidates = sorted(s for s in members if s in intervals and intervals[s][1] < base and s not in source_symbols)
    if any((symbol, day) in raw_map for symbol in candidates for day in new_days):
        raise ValueError("legacy_absence_conflicts_with_source")
    return {"sourceCommit": ref, "baseDate": base,
            "dates": {symbol: list(new_days) for symbol in candidates},
            "lastQuotedDates": {symbol: intervals[symbol][1] for symbol in candidates}}


def derive_source_absence(inspections, first_dates, raw_map, bulk_days, new_days, confirmed_history, ref):
    """Called only after the full fixed-source range/count checks succeeded."""
    dates, missing_new = {}, {}
    for symbol, info in inspections.items():
        first = first_dates[symbol] if info.get("new") else info["first_date"]
        absent = {day for day in bulk_days if day >= first and (symbol, day) not in raw_map}
        if info.get("short_history"):
            # The caller separately fetched this whole <=10-day history with
            # pinned pagination/count verification, not just the bulk overlap.
            absent.update(row["date"] for row in info["old_rows"]
                          if (symbol, row["date"]) not in raw_map)
        absent.update(confirmed_history.get(symbol, ()))
        if any((symbol, day) in raw_map for day in absent):
            raise ValueError("absence_conflicts_with_source_row")
        if absent:
            dates[symbol] = sorted(absent)
        fresh = tuple(day for day in new_days if day in absent)
        if fresh:
            missing_new[symbol] = fresh
    return {"sourceCommit": ref, "dates": dates}, missing_new


def preflight_indices(root, base):
    from indices import INDEX_MAP, NOMINAL_COUNTS, _parse, _read
    result = {}
    for name in INDEX_MAP:
        intervals, members, _, _ = _parse(_read(root / "instruments" / (name + ".txt")), base)
        if name in NOMINAL_COUNTS and len(members) != NOMINAL_COUNTS[name]:
            raise ValueError("old_membership_cardinality:" + name)
        result[name] = {"historicalIntervals": len(intervals), "activeMembers": len(members)}
    return result


def run_update(data_dir, *, dry_run=False, through=None, client=None, now=None, emit=print, candidate_check=None,
               consumer_factory=None, min_eligible=5000, min_coverage=0.999, lifecycle=None):
    if candidate_check is not None and consumer_factory is not None:
        raise ValueError("candidate_check_ambiguous")
    # Domain/tree modules have no import-time IO; imports are kept out of the
    # argument-only --help path and the source-only unit tests.
    from tree import active_update_symbols, inspect_symbols, apply_prices, validate_tree
    from indices import update_indices

    if lifecycle is not None:
        lifecycle["stage"] = "run_setup"
    started = time.monotonic()
    now = now or datetime.now(SHANGHAI)
    cutoff = completed_cutoff(now)
    if lifecycle is not None:
        lifecycle["cutoff"] = cutoff
    if through is not None:
        if iso(through) > cutoff:
            raise ValueError("target_not_completed_eod")
        cutoff = through
    if lifecycle is not None:
        lifecycle["cutoff"] = cutoff
        lifecycle["stage"] = "source_client_init"
    client = client or source_client()

    def mark(stage):
        if lifecycle is not None:
            lifecycle["stage"] = stage
            lifecycle["stageStartedMonotonic"] = time.monotonic()

    def event(name, **values):
        record = {"time": datetime.now(SHANGHAI).isoformat(), "event": name, **values}
        if lifecycle is not None and lifecycle.get("runId") is not None:
            record["runId"] = lifecycle["runId"]
        emit(json.dumps(record, sort_keys=True))

    if lifecycle is not None:
        lifecycle["stage"] = "transaction_open"
    status_path, status_mode = status_destination(data_dir, dry_run=dry_run)
    planned = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date().isoformat()
    identity = {"schemaVersion": 1, "runId": (lifecycle or {}).get("runId"), "generation": (lifecycle or {}).get("runId"),
                "mode": status_mode, "dataRoot": str(Path(data_dir).resolve()), "plannedDate": planned}
    if lifecycle is not None:
        lifecycle["plannedDate"] = planned
        lifecycle["mode"] = status_mode
        lifecycle["dataRoot"] = identity["dataRoot"]
    published = {"done": False}

    def publish(status, **fields):
        payload = {**identity, "status": status, "event": fields.pop("event", status), "time": datetime.now(SHANGHAI).isoformat()}
        payload.update(fields)
        payload["cutoff"] = (lifecycle or {}).get("cutoff")
        payload["sourceCommit"] = (lifecycle or {}).get("sourceCommit")
        payload["failureStage"] = (lifecycle or {}).get("stage")
        write_update_status(status_path, payload)
        published["done"] = status != "running"

    def before_unlock(kind, value):
        if published["done"] or kind is None:
            return
        error = str(value)[:300] if isinstance(value, (ValueError, SourceError)) else type(value).__name__
        publish("interrupted" if isinstance(value, InterruptedRun) else "failed", error=error)

    txn = Transaction(data_dir)
    txn.before_unlock = before_unlock
    txn.on_lock_busy = lambda: record_lock_busy(status_path, identity, lifecycle)
    with txn:
        publish("running")
        if consumer_factory is not None:
            # Capture the reviewed consumer identity only after this run owns
            # the lock and already has a terminal-status publisher installed.
            mark("candidate_check_init")
            candidate_check = consumer_factory()
        mark("base_calendar_read")
        old_calendar = read_calendar(txn.active)
        base = old_calendar[-1]
        mark("local_index_layout_validate")
        event("local_index_layout_validated", indices=preflight_indices(txn.active, base))
        mark("source_pin")
        ref = client.latest_ref()
        if lifecycle is not None:
            lifecycle["sourceCommit"] = ref
        event("source_pinned", sourceCommit=ref, baseDate=base, cutoff=cutoff)
        mark("calendar_fetch")
        calendar = fetch_calendar(client, cutoff)
        mark("calendar_validate")
        if calendar[:len(old_calendar)] != old_calendar:
            raise ValueError("source_calendar_revises_existing_history")
        target = calendar[-1]
        if target < base:
            raise ValueError("source_calendar_behind_active")
        mark("market_freshness_check")
        source_max = iso(scalar(client,
            f"SELECT MAX(tradedate) AS latest FROM {client.table('final_a_stock_eod_price')}", "latest"))
        if source_max > now.astimezone(SHANGHAI).date().isoformat():
            raise ValueError("source_contains_future_market_date")
        if source_max < target:
            raise ValueError(f"upstream_market_stale:{source_max}:expected:{target}")
        mark("required_symbol_check")
        require_symbols_at_target(client, target)
        event("calendar_and_source_dates_validated", targetDate=target, sourceMaxDate=source_max,
              calendarRows=len(calendar), requests=client.requests)
        if target == base:
            mark("installed_manifest_read")
            try:
                installed = json.loads(read_small(txn.active / ".qlib-dolt-incremental.json"))
                has_installed_manifest = True
            except FileNotFoundError:
                installed = {}
                has_installed_manifest = False
                if base > iso(txn.origin_tag.decode().strip()):
                    raise ValueError("incremental_manifest_missing_cannot_downgrade")
            if has_installed_manifest and candidate_check is not None:
                mark("installed_consumer_validation")
                event("installed_consumer_validation_passed", **candidate_check(txn.active, installed))
            installed_ref = installed.get("sourceCommit")
            source_absence = installed.get("sourceAbsence")
            legacy_unquoted = installed.get("legacyUnquotedCSI")
            mark("installed_tree_validation")
            coverage = validate_tree(txn.active, calendar, target, min_eligible=min_eligible, min_coverage=min_coverage,
                source_absence=source_absence, expected_source_commit=installed_ref)
            index_prices = validate_index_market_alignment(txn.active, calendar,
                source_absence=source_absence, expected_source_commit=installed_ref, legacy_unquoted=legacy_unquoted)
            quality = "partial" if (coverage.get("status") == "partial" or index_prices.get("status") == "partial"
                                      or installed.get("dataQuality") == "partial") else "complete"
            result = {"status": "already_current", "dataQuality": quality, "targetDate": target,
                "installedSourceCommit": installed_ref, "observedSourceCommit": ref,
                "priceValidation": coverage, "indexPriceValidation": index_prices}
            if installed.get("sourceRowQuarantine"):
                result["sourceRowQuarantine"] = installed["sourceRowQuarantine"]
                result["sourceIntegrity"] = "partial"
            event("current", releaseTag=txn.origin_tag.decode().strip(), **result)
            publish("current", targetDate=result.get("targetDate"))
            return result
        new_days = calendar[len(old_calendar):]
        if len(new_days) > 10 or len(old_calendar) < 3:
            raise ValueError("incremental_gap_requires_bounded_backfill")

        event("market_download_start", overlapFrom=old_calendar[-3], targetDate=target)
        mark("market_fetch")
        raw_source = fetch_prices(client, f"tradedate >= '{old_calendar[-3]}' AND tradedate <= '{target}'")
        downloaded_rows = len(raw_source)
        mark("market_validate")
        quarantine = ClosedIndexQuarantine(client, ref, calendar,
            emit=lambda proof: event("closed_index_rows_quarantined", **proof))
        raw_source = quarantine.filter_rows(raw_source, start=old_calendar[-3], end=target)
        if any(not old_calendar[-3] <= iso(row["tradedate"]) <= target for row in raw_source):
            raise ValueError("market_response_outside_requested_range")
        raw_map = {(row["symbol"], row["tradedate"]): row for row in raw_source}
        if len(raw_map) != len(raw_source):
            raise ValueError("duplicate_market_row")
        day_counts = Counter(row["tradedate"] for row in raw_source)
        if any(day_counts[day] < min_eligible for day in new_days):
            raise ValueError("market_day_population_incomplete")
        off_calendar = sorted({
            (row["tradedate"], row["symbol"]) for row in raw_source if row["tradedate"] not in calendar
        })
        if off_calendar:
            sample = ",".join(f"{day}:{symbol}" for day, symbol in off_calendar[:8])
            raise ValueError(f"market_row_outside_exchange_calendar:{sample}")
        source_symbols = sorted({row["symbol"] for row in raw_source if row["tradedate"] > base})
        if any(SYMBOL.fullmatch(symbol) is None for symbol in source_symbols):
            raise ValueError("invalid_source_symbol")
        symbols = active_update_symbols(txn.active, base, source_symbols)
        tail_gap = TailGapException(old_calendar)
        # Keyword passed only while the exact record is active (call shape unchanged otherwise).
        gap_allowance = {"tail_gap_allowances": tail_gap.allowances} if tail_gap.allowances else {}
        inspections = inspect_symbols(txn.active, symbols, old_calendar, **gap_allowance)
        event("market_rows_downloaded", sourceSymbols=len(source_symbols), inventorySymbols=len(symbols),
              rows=len(raw_source), downloadedRows=downloaded_rows,
              quarantinedRows=downloaded_rows - len(raw_source),
              dateCounts=dict(sorted(day_counts.items())), requests=client.requests)
        mark("historical_source_validation")
        confirmed_missing, gap_proof = fetch_historical_gaps(client, inspections, old_calendar, raw_map, quarantine,
                                                             **gap_allowance)
        if gap_proof:
            event("historical_tail_gaps_verified", gaps=gap_proof, requests=client.requests)

        first_dates = {}
        for symbol, info in inspections.items():
            if info.get("new") or info.get("short_history"):
                first = iso(scalar(client, f"SELECT MIN(tradedate) AS first_date FROM {client.table('final_a_stock_eod_price')} WHERE symbol = '{symbol}'", "first_date"))
                first_dates[symbol] = first
                if info.get("new"):
                    if first <= base:
                        raise ValueError(f"new_symbol_has_unimported_history:{symbol}")
                else:
                    if first not in old_calendar or len([d for d in old_calendar if d >= first]) > 10:
                        raise ValueError(f"short_history_requires_manual_review:{symbol}")
                    extra = fetch_prices(client, f"symbol = '{symbol}' AND tradedate >= '{first}' AND tradedate <= '{target}'")
                    extra = quarantine.filter_rows(extra, start=first, end=target, symbol=symbol)
                    for row in extra:
                        key = (row["symbol"], row["tradedate"])
                        if key in raw_map and raw_map[key] != row:
                            raise ValueError("fixed_source_row_disagrees")
                        raw_map[key] = row
        verify_anchor_intervals(client, inspections, old_calendar, raw_map, quarantine)
        tail_gap_evidence = tail_gap.confirm(inspections, confirmed_missing, raw_map, new_days, ref)
        if tail_gap_evidence:
            event("historical_tail_gap_exception_applied", **tail_gap_evidence)
        source_absence, missing_new = derive_source_absence(inspections, first_dates, raw_map,
            calendar[len(old_calendar) - 3:], new_days, confirmed_missing, ref)
        stored_short_absence = {"sourceCommit": ref, "dates": {
            symbol: sorted(set(source_absence["dates"].get(symbol, ())) &
                           {row["date"] for row in info["old_rows"]})
            for symbol, info in inspections.items() if info.get("short_history") and
            set(source_absence["dates"].get(symbol, ())) & {row["date"] for row in info["old_rows"]}}}
        legacy_unquoted = legacy_index_absence(txn.active, base, new_days, raw_map, source_symbols, ref)
        event("source_absence_proven", sourceAbsence=source_absence, legacyUnquotedCSI=legacy_unquoted,
              meaning="missing_observations_not_automatic_suspension_claim")
        # The caller verifies no intervening valid close has been silently
        # skipped: stored anchors are taken from full local histories, and raw
        # market rows covering the overlap interval are fetched in full.
        source_rows = sorted(raw_map.values(), key=lambda r: (r["symbol"], r["tradedate"]))
        raw_digest = hashlib.sha256(json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        raw = read_raw(source_rows, ref)

        mark("candidate_copy")
        stage = txn.copy()
        event("candidate_copied", baseDate=base, targetDate=target)
        # Finish all fixed-ref network work before the expensive local price
        # conversion. The source deadline is unchanged and is never reset.
        mark("index_update")
        index_result = update_indices(txn.active, stage, client, base, target, calendar)
        mark("future_calendar_fetch")
        future_rows = client.paginated_rows("ts_trade_day_calendar", ("exchange", "date", "is_open"),
            f"exchange = 'SSE' AND is_open = 1 AND date > '{target}'", ("exchange", "date"))
        if not future_rows or any(row["exchange"] != "SSE" or row["is_open"] != "1" for row in future_rows):
            raise ValueError("future_calendar_unavailable")
        future = [iso(row["date"]) for row in future_rows]
        if future != sorted(set(future)) or future[0] <= target:
            raise ValueError("invalid_future_calendar")
        event("source_downloads_complete", requests=client.requests, futureCalendarRows=len(future))
        mark("price_conversion")
        phase_started = time.monotonic()
        price_result = apply_prices(txn.active, stage, old_calendar, calendar,
            source_commit=ref, expected_source_commit=ref, raw_rows=raw, first_dates=first_dates,
            confirmed_missing_history=confirmed_missing,
            confirmed_missing_history_source_commit=ref,
            confirmed_missing_new_days=missing_new, confirmed_missing_new_days_source_commit=ref,
            source_absence=source_absence,
            min_eligible=min_eligible, min_coverage=min_coverage,
            **(gap_allowance if tail_gap_evidence else {}))
        event("price_validation_passed", phaseElapsedMs=round((time.monotonic() - phase_started) * 1000), **price_result)
        mark("candidate_index_alignment")
        phase_started = time.monotonic()
        index_prices = validate_index_market_alignment(stage, calendar,
            source_absence=source_absence, expected_source_commit=ref, legacy_unquoted=legacy_unquoted)
        event("candidate_index_alignment_passed", phaseElapsedMs=round((time.monotonic() - phase_started) * 1000))
        quarantine_evidence = quarantine.evidence()
        quality = "partial" if (price_result.get("status") == "partial" or
            index_prices.get("status") == "partial") else "complete"
        # All production-file mutations occur in the candidate generation.
        mark("candidate_calendar_write")
        with (stage / "calendars/day.txt").open("ab") as stream:
            stream.write(("\n".join(new_days) + "\n").encode())
        (stage / "calendars/day_future.txt").write_text("\n".join(future) + "\n", encoding="ascii")
        if read_calendar(stage) != calendar:
            raise ValueError("candidate_calendar_mismatch")
        manifest = {
            "schemaVersion": 1, "kind": "same_source_dolt_incremental", "status": "validated",
            "sourceRepository": "chenditc/investment_data", "sourceCommit": ref,
            "baseReleaseTag": txn.origin_tag.decode().strip(), "baseDate": base, "targetDate": target,
            "dataQuality": quality, "sourceAbsence": source_absence, "legacyUnquotedCSI": legacy_unquoted,
            "storedShortHistoryEvidence": stored_short_absence,
            "calendarAdded": new_days, "sourceRowsSha256": raw_digest,
            "historicalTailGapEvidence": gap_proof,
            "sourceRows": len(source_rows), "sourceRequests": client.requests,
            "priceValidation": price_result, "indexValidation": index_result,
            "indexPriceValidation": index_prices,
            "verifiedAt": datetime.now(SHANGHAI).isoformat(),
            "scope": "market_data_only_no_snapshot_no_trading_no_delivery",
        }
        if tail_gap_evidence:
            manifest["historicalTailGapException"] = tail_gap_evidence
        if quarantine_evidence:
            manifest["sourceRowQuarantine"] = quarantine_evidence
            manifest["sourceIntegrity"] = "partial"
        # Dry-runs exercise the exact embedded identity and consumer read path
        # too. The public CLI always supplies the read-only consumer check.
        mark("candidate_manifest_prepare")
        phase_started = time.monotonic()
        manifest_operation = "prepare"

        def manifest_progress(phase, *, state, elapsedMs):
            # Observation only: no new deadline and no cached admission proof.
            # Keep the parent phase clock intact if a signal interrupts a step.
            if lifecycle is not None:
                if state == "start":
                    lifecycle["substage"] = phase
                    lifecycle["substageStartedMonotonic"] = time.monotonic()
                elif state == "completed":
                    lifecycle.pop("substage", None)
                    lifecycle.pop("substageStartedMonotonic", None)
            event("candidate_manifest_step", operation=manifest_operation,
                  phase=phase, state=state, elapsedMs=elapsedMs)

        txn.progress = manifest_progress
        # prepare_manifest computes the authoritative complete prefix proof;
        # commit independently recomputes it immediately before publication.
        manifest = txn.prepare_manifest(manifest)
        proof = manifest["historicalPrefixes"]
        event("candidate_manifest_prepared", phaseElapsedMs=round((time.monotonic() - phase_started) * 1000))
        if candidate_check is not None:
            mark("candidate_consumer_validation")
            phase_started = time.monotonic()
            consumer_result = candidate_check(stage, manifest)
            event("candidate_consumer_validation_passed", phaseElapsedMs=round((time.monotonic() - phase_started) * 1000), **consumer_result)
        if not dry_run:
            mark("candidate_publish")
            phase_started = time.monotonic()
            manifest_operation = "commit"
            manifest = txn.commit(manifest)
            proof = manifest["historicalPrefixes"]
            event("candidate_published", phaseElapsedMs=round((time.monotonic() - phase_started) * 1000))
        result = {"status": "dry_run_validated" if dry_run else "updated_with_source_absence" if quality == "partial" else "updated", "targetDate": target,
                  "dataQuality": quality,
                  "sourceCommit": ref, "priceValidation": price_result, "indexValidation": index_result,
                  "indexPriceValidation": index_prices,
                  "historicalPrefixes": proof, "elapsedSeconds": round(time.monotonic() - started, 2)}
        if tail_gap_evidence:
            result["historicalTailGapException"] = tail_gap_evidence
        if quarantine_evidence:
            result["sourceRowQuarantine"] = quarantine_evidence
            result["sourceIntegrity"] = "partial"
        event("finished", **result)
        publish("current", targetDate=result.get("targetDate"))
        return result


def safe_last_events(events, fields, limit):
    """Keep only the updater's fixed transport metadata, never SQL or body fields."""
    if not isinstance(events, list):
        return []
    safe_codes = CurlTransport._SAFE_EXCEPTION_CODES | {"transport_internal_error"}
    result = []
    for event in events[-limit:]:
        if not isinstance(event, dict):
            continue
        selected = {}
        for key in fields:
            if key not in event:
                continue
            value = event.get(key)
            if key == "queryKind":
                if isinstance(value, str) and value in {"head", "count", "page", "select", "schema"}:
                    selected[key] = value
            elif key == "table":
                if isinstance(value, str) and value in TABLE_KEYS:
                    selected[key] = value
            elif key in {"pageIndex", "elapsedMs"}:
                if type(value) is int and value >= 0:
                    selected[key] = value
            elif key in {"attemptTimeoutMs", "remainingBudgetMs", "attemptElapsedMs",
                         "nameLookupMs", "connectMs", "appConnectMs", "startTransferMs", "transferTotalMs"}:
                if type(value) is int and 0 <= value <= 86400000:
                    selected[key] = value
            elif key == "numRedirects":
                if type(value) is int and 0 <= value <= 99:
                    selected[key] = value
            elif key == "diagnosticStatus":
                if isinstance(value, str) and value in {"available", "unavailable", "invalid"}:
                    selected[key] = value
            elif type(value) is int or value is None:
                selected[key] = value
            elif key == "status" and value in ("failed", "Success"):
                selected[key] = value
            elif key == "transport" and value in ("direct", "proxy"):
                selected[key] = value
            elif key == "code" and isinstance(value, str) and value in safe_codes:
                selected[key] = value
        result.append(selected)
    return result



PRODUCTION_DATA_ROOT = Path("/home/ubuntu/qlib/data")
PRODUCTION_STATUS = Path("/home/ubuntu/qlib/logs/update-status.json")


def status_destination(data_dir, *, dry_run):
    """Production status is writable only for the production data root, not dry-run."""
    data = Path(data_dir).resolve()
    production_data = PRODUCTION_DATA_ROOT.resolve()
    production_status = PRODUCTION_STATUS.resolve()
    override = os.environ.get("QLIB_UPDATE_STATUS")
    if dry_run or data != production_data:
        if not override:
            return data / "update-status.json", "dry-run" if dry_run else "isolated"
        path = Path(override).resolve()
        if path == production_status:
            raise ValueError("production_status_write_forbidden")
        return path, "dry-run" if dry_run else "isolated"
    if override and Path(override).resolve() != production_status:
        raise ValueError("production_status_path_override_forbidden")
    return production_status, "production"


def write_update_status(path, payload):
    """Unique temp file. Refuses to publish a non-production record onto the production path."""
    path = Path(path)
    if path.resolve() == PRODUCTION_STATUS.resolve() and payload.get("mode") != "production":
        raise ValueError("production_status_write_forbidden")
    if payload.get("mode") == "production" and Path(payload.get("dataRoot", "")).resolve() != PRODUCTION_DATA_ROOT.resolve():
        raise ValueError("production_status_data_root_mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def record_lock_busy(path, identity, lifecycle):
    """Append an independent contention record; the shared status stays untouched."""
    path = Path(path)
    attempts = path.with_name(path.stem + ".attempts.jsonl")
    record = {**identity, "status": "locked", "event": "locked",
              "time": datetime.now(SHANGHAI).isoformat(),
              "cutoff": (lifecycle or {}).get("cutoff"),
              "sourceCommit": (lifecycle or {}).get("sourceCommit"),
              "failureStage": (lifecycle or {}).get("stage"),
              "error": "update_lock_busy"}
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    attempts.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(attempts, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe_attempt_record")
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="/home/ubuntu/qlib/data")
    parser.add_argument("--through", help="Optional completed date cap, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Validate candidate but never promote it")
    parser.add_argument("--health-file", type=Path, default=Path("/home/ubuntu/research-data/data_health.py"),
                        help="Reviewed read-only Qlib consumer, checked before promotion")
    args = parser.parse_args()
    def stop(signum, _frame):
        raise InterruptedRun(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_id = str(uuid.uuid4())
    lifecycle = {"runId": run_id, "sourceCommit": None, "cutoff": None,
                 "stage": "source_client_init"}
    client = None

    def terminal(status, *, observed_monotonic=None, **values):
        observed = time.monotonic() if observed_monotonic is None else observed_monotonic
        stage_started = lifecycle.get("stageStartedMonotonic")
        if type(stage_started) in (int, float):
            values["stageElapsedMs"] = max(0, round((observed - stage_started) * 1000))
        substage_started = lifecycle.get("substageStartedMonotonic")
        if lifecycle.get("substage") is not None and type(substage_started) in (int, float):
            values["failureSubstage"] = lifecycle["substage"]
            values["substageElapsedMs"] = max(0, round((observed - substage_started) * 1000))
        payload = {"time": datetime.now(SHANGHAI).isoformat(), "event": status,
                   "runId": run_id, "sourceCommit": lifecycle["sourceCommit"],
                   "cutoff": lifecycle["cutoff"], "failureStage": lifecycle["stage"],
                   "status": status, **values}
        print(json.dumps(payload), flush=True)

    try:
        client = source_client()
        lifecycle["stage"] = "candidate_check_init"

        def consumer_factory():
            # Import and identity capture happen under the update lock, after
            # this run's terminal-status publisher exists. A missing, unsafe
            # or unreadable consumer can therefore stop core waiting once.
            from candidate import consumer_check
            return consumer_check(args.health_file)

        run_update(args.data_dir, through=args.through, dry_run=args.dry_run,
                   client=client,
                   consumer_factory=consumer_factory,
                   emit=lambda value: print(value, flush=True), lifecycle=lifecycle)
        return 0
    except InterruptedRun as exc:
        terminal("interrupted", signal=exc.signum, interruptedAt=exc.observed_at,
                 observed_monotonic=exc.observed_monotonic,
                 postInterruptionElapsedMs=max(0, round((time.monotonic() - exc.observed_monotonic) * 1000)))
        return 128 + exc.signum
    except BlockingIOError:
        terminal("locked", action="no_update")
        return 75
    except Exception as exc:
        # Our domain exceptions use fixed codes and market symbols/dates only.
        # Do not expose remote payloads, proxy URLs or arbitrary stack locals.
        code = str(exc) if isinstance(exc, (ValueError, SourceError)) else type(exc).__name__
        # Transport events contain only route/status/count metadata, never URL,
        # SQL, response body, environment or credentials.
        details = {"error": code[:300], "sourceRequests": getattr(client, "requests", 0)}
        if isinstance(exc, SourceError) and client is not None:
            details["lastSourceEvent"] = safe_last_events(
                [getattr(client, "last_source_event", None)],
                ("request_i", "http", "rows", "status", "queryKind", "table", "pageIndex", "elapsedMs"), 1)
            details["lastTransportEvents"] = safe_last_events(
                getattr(client, "last_transport_events", []),
                ("request_i", "queryKind", "table", "pageIndex", "transport", "attempt", "status", "code",
                 "curl_rc", "http", "bytes", "diagnosticStatus", "attemptTimeoutMs", "remainingBudgetMs",
                 "attemptElapsedMs", "nameLookupMs", "connectMs", "appConnectMs", "startTransferMs",
                 "transferTotalMs", "numRedirects"), 4)
        terminal("failed", **details)
        return 1


if __name__ == "__main__":
    sys.exit(main())
