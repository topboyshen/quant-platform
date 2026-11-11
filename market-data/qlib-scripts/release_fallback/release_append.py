#!/usr/bin/env python3
"""Append a verified upstream qlib_bin release to an existing QLib tree.

This module is network-free. The caller must have downloaded, hash-verified and
safely extracted the release tree first. It preserves every byte of the active
historical feature files, appends only the release calendar's new trading days,
carries factor values across proven source-absence slots, and builds the same
embedded manifest shape consumed by research-data/data_health.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("QLIB_SCRIPTS_DIR", "/home/ubuntu/qlib/scripts/qlib_dolt_incremental"))
sys.path.insert(0, str(REPO))

import atomic  # noqa: E402
import candidate  # noqa: E402
import postcheck  # noqa: E402
import run as qrun  # noqa: E402
from alignment import validate_index_market_alignment  # noqa: E402
from tree import FIELDS, validate_coverage_days  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
NAN = struct.pack("<f", float("nan"))
PRICE_FIELDS = {
    "target_date", "eligible", "valid", "invalid", "coverage",
    "invalid_symbols", "invalid_reasons", "first_day_listings_checked",
    "reference_dates",
}


def read_calendar(root: Path) -> list[str]:
    return (root / "calendars/day.txt").read_text().splitlines()


def read_instruments(root: Path) -> list[str]:
    return (root / "instruments/all.txt").read_text().splitlines()


def parse_intervals(lines: list[str]) -> dict[str, tuple[str, str]]:
    result = {}
    for line in lines:
        cells = line.split()
        if len(cells) != 3:
            raise ValueError(f"invalid_all_line:{line[:80]}")
        result[cells[0]] = (cells[1], cells[2])
    return result


def feature(path: Path) -> tuple[bytes, int, int]:
    raw = path.read_bytes()
    if len(raw) < 8 or len(raw) % 4:
        raise ValueError(f"invalid_feature_binary:{path}")
    start = struct.unpack_from("<f", raw, 0)[0]
    if not math.isfinite(start) or not start.is_integer() or start < 0:
        raise ValueError(f"invalid_feature_header:{path}")
    return raw, int(start), len(raw) // 4 - 1


def value_bytes(raw: bytes, start: int, index: int) -> bytes | None:
    offset = index - start
    if offset < 0 or offset >= (len(raw) // 4 - 1):
        return None
    return raw[4 + offset * 4: 8 + offset * 4]


def value_float(raw: bytes, start: int, index: int) -> float | None:
    piece = value_bytes(raw, start, index)
    return None if piece is None else struct.unpack("<f", piece)[0]


def last_factor(raw: bytes) -> float:
    for offset in range(len(raw) // 4 - 1, 0, -1):
        value = struct.unpack_from("<f", raw, offset * 4)[0]
        if math.isfinite(value) and value > 0:
            return value
    raise ValueError("factor_history_missing")


def missing_bytes(field: str, carried_factor: float) -> bytes:
    if field == "factor":
        return struct.pack("<f", carried_factor)
    return NAN


def merge_symbol(stage: Path, old: Path, release: Path, symbol: str,
                 old_calendar_len: int, new_calendar_len: int,
                 release_calendar_len: int, missing_days: set[str],
                 calendar: list[str], digest) -> int:
    """Append new-day bytes to every field, preserving the old byte prefix."""
    rows = 0
    old_dir = old / "features" / symbol.lower()
    release_dir = release / "features" / symbol.lower()
    stage_dir = stage / "features" / symbol.lower()
    if not stage_dir.exists():
        stage_dir.mkdir(mode=0o755)

    # Validate the release slice before touching the staged tree.
    release_fields = {}
    for field in FIELDS:
        rel_path = release_dir / f"{field}.day.bin"
        if not rel_path.exists():
            raise ValueError(f"release_field_missing:{symbol}:{field}")
        raw, start, length = feature(rel_path)
        release_fields[field] = (raw, start, length)
    starts = {item[1] for item in release_fields.values()}
    lengths = {item[2] for item in release_fields.values()}
    if len(starts) != 1 or len(lengths) != 1:
        raise ValueError(f"release_fields_misaligned:{symbol}")
    rel_start = starts.pop()
    rel_len = lengths.pop()
    if rel_start + rel_len > release_calendar_len:
        raise ValueError(f"release_exceeds_calendar:{symbol}")

    for index in range(old_calendar_len, new_calendar_len):
        day = calendar[index]
        close = value_float(release_fields["close"][0], rel_start, index)
        if day in missing_days:
            if close is not None and math.isfinite(close) and close > 0:
                raise ValueError(f"missing_day_has_release_quote:{symbol}:{day}")
            continue
        if close is None or not math.isfinite(close) or close <= 0:
            raise ValueError(f"valid_day_has_no_release_quote:{symbol}:{day}")
        for field in FIELDS:
            value = value_float(release_fields[field][0], rel_start, index)
            if value is None or not math.isfinite(value):
                # A new listing's first day may legitimately have no change.
                if field == "change" and index == rel_start:
                    continue
                raise ValueError(f"release_field_nonfinite:{symbol}:{day}:{field}")
            if field == "factor" and value <= 0:
                raise ValueError(f"release_factor_nonpositive:{symbol}:{day}")
        rows += 1
        digest.update(f"{symbol}\t{day}".encode())
        for field in FIELDS:
            digest.update(value_bytes(release_fields[field][0], rel_start, index))

    for field in FIELDS:
        old_path = old_dir / f"{field}.day.bin"
        stage_path = stage_dir / f"{field}.day.bin"
        if not old_path.exists():
            raise ValueError(f"old_field_missing:{symbol}:{field}")
        old_raw, old_start, old_len = feature(old_path)
        rel_raw, rel_start, rel_len = release_fields[field]
        blob = bytearray(old_raw)
        carried = last_factor(old_raw) if field == "factor" else 0.0
        # A mature active symbol always ends at the old calendar boundary. If a
        # stored tail gap exists, pad it before appending the new calendar days.
        for index in range(old_start + old_len, old_calendar_len):
            blob += missing_bytes(field, carried)
        for index in range(old_calendar_len, new_calendar_len):
            day = calendar[index]
            if day in missing_days:
                blob += missing_bytes(field, carried)
                continue
            piece = value_bytes(rel_raw, rel_start, index)
            if piece is None:
                raise ValueError(f"release_slice_missing:{symbol}:{day}:{field}")
            blob += piece
            if field == "factor":
                carried = struct.unpack("<f", piece)[0]
        stage_path.write_bytes(bytes(blob))
        shutil.copymode(old_path, stage_path)
    return rows


def update_all(stage: Path, active: list[str], target: str, new_symbols: list[tuple[str, str, str]]) -> None:
    lines = read_instruments(stage)
    records = parse_intervals(lines)
    for index, line in enumerate(lines):
        symbol = line.split()[0]
        if symbol in active:
            start = records[symbol][0]
            lines[index] = f"{symbol}\t{start}\t{target}"
    for symbol, start, end in new_symbols:
        if symbol in records:
            raise ValueError(f"new_symbol_already_present:{symbol}")
        lines.append(f"{symbol}\t{start}\t{end}")
    content = "\n".join(lines) + "\n"
    path = stage / "instruments/all.txt"
    mode = path.stat().st_mode
    path.write_text(content, encoding="ascii")
    path.chmod(mode)


def copy_new_symbol(stage: Path, release: Path, symbol: str,
                    new_calendar_len: int, calendar: list[str], digest) -> int:
    """Copy a release-only symbol without inventing old bytes."""
    rows = 0
    source_dir = release / "features" / symbol.lower()
    target_dir = stage / "features" / symbol.lower()
    target_dir.mkdir(mode=0o755)
    blobs = {}
    for field in FIELDS:
        raw, start, length = feature(source_dir / f"{field}.day.bin")
        if start + length > new_calendar_len:
            raise ValueError(f"new_symbol_exceeds_calendar:{symbol}:{field}")
        blobs[field] = (raw, start, length)
    starts = {item[1] for item in blobs.values()}
    lengths = {item[2] for item in blobs.values()}
    if len(starts) != 1 or len(lengths) != 1:
        raise ValueError(f"new_symbol_fields_misaligned:{symbol}")
    start = starts.pop()
    length = lengths.pop()
    for field in FIELDS:
        raw, _, _ = blobs[field]
        (target_dir / f"{field}.day.bin").write_bytes(raw)
        (target_dir / f"{field}.day.bin").chmod(0o644)
    for offset in range(length):
        index = start + offset
        close = value_float(blobs["close"][0], start, index)
        if close is None or not math.isfinite(close) or close <= 0:
            continue
        rows += 1
        digest.update(f"{symbol}\t{calendar[index]}".encode())
        for field in FIELDS:
            digest.update(value_bytes(blobs[field][0], start, index))
    return rows


def build_current_status(*, run_id: str, status_mode: str, data_root: str,
                         planned_date: str | None, target: str,
                         source_commit: str, source_mode: str,
                         source_failure: dict | None) -> dict:
    """Build the success status with the same identity invariant as run.py."""
    payload = {
        "schemaVersion": 1,
        "runId": run_id,
        "generation": run_id,
        "mode": status_mode,
        "dataRoot": data_root,
        "plannedDate": planned_date or datetime.now(SHANGHAI).date().isoformat(),
        "status": "current",
        "event": "current",
        "time": datetime.now(SHANGHAI).isoformat(),
        "cutoff": target,
        "sourceCommit": source_commit,
        "targetDate": target,
        "sourceMode": source_mode,
    }
    if source_failure:
        payload["sourceFailure"] = source_failure
    return payload


def publish_release(*, data_dir, release_dir, source_commit: str, target: str,
                    health_file, backup_dir=None, publish: bool = False,
                    planned_date: str | None = None,
                    source_mode: str = "release_fallback",
                    source_failure: dict | None = None) -> dict:
    """Append a verified release tree, validate it, and optionally promote it.

    ``release_dir`` must point at the extracted ``qlib_bin`` root (the
    directory containing ``calendars``, ``features`` and ``instruments``).
    No network or subprocess is used here. ``planned_date`` is the run day for
    the shared status record; ``target`` remains the data date.
    """
    if not isinstance(source_mode, str) or not source_mode:
        raise ValueError("invalid_source_mode")
    data = Path(data_dir).resolve()
    release = Path(release_dir).resolve()
    health_file = Path(health_file)
    old_calendar = read_calendar(data / "cn_data")
    release_calendar = read_calendar(release)
    if release_calendar[:len(old_calendar)] != old_calendar:
        raise ValueError("release_calendar_is_not_an_append")
    added = release_calendar[len(old_calendar):]
    if not added or added[-1] != target:
        raise ValueError("release_target_mismatch")
    if old_calendar[-1] >= target:
        raise ValueError("active_tree_already_at_or_beyond_target")

    old_lines = read_instruments(data / "cn_data")
    old_records = parse_intervals(old_lines)
    release_records = parse_intervals(read_instruments(release))
    old_last = old_calendar[-1]
    active = sorted(symbol for symbol, (_, end) in old_records.items() if end == old_last)

    # A release file for every active symbol, and the exact new-day gaps.
    missing_new: dict[str, set[str]] = {}
    for symbol in active:
        rel_close = release / "features" / symbol.lower() / "close.day.bin"
        if not rel_close.exists():
            raise ValueError(f"active_release_missing:{symbol}")
        raw, start, length = feature(rel_close)
        gaps = set()
        for offset, day in enumerate(added):
            index = len(old_calendar) + offset
            value = value_float(raw, start, index)
            if value is None or not math.isfinite(value) or value <= 0:
                gaps.add(day)
        if gaps:
            missing_new[symbol] = gaps

    # A release-only symbol is admitted only when it actually has a new quote.
    new_symbols: list[tuple[str, str, str]] = []
    for symbol, (start, end) in release_records.items():
        if symbol in old_records:
            continue
        rel_close = release / "features" / symbol.lower() / "close.day.bin"
        if not rel_close.exists():
            continue
        raw, rel_start, rel_len = feature(rel_close)
        if any((value := value_float(raw, rel_start, len(old_calendar) + offset)) is not None
               and math.isfinite(value) and value > 0 for offset in range(len(added))):
            new_symbols.append((symbol, start, end))
    new_symbols.sort()

    old_manifest = json.loads((data / "cn_data/.qlib-dolt-incremental.json").read_text())
    absence_dates: dict[str, list[str]] = {
        symbol: list(dates) for symbol, dates in old_manifest.get("sourceAbsence", {}).get("dates", {}).items()
    }
    for symbol, gaps in missing_new.items():
        merged = sorted(set(absence_dates.get(symbol, ())) | gaps)
        absence_dates[symbol] = merged
    absence_dates = {symbol: dates for symbol, dates in sorted(absence_dates.items()) if dates}
    absence = {"sourceCommit": source_commit, "dates": absence_dates}

    # Old absence dates before the post-update health window must remain
    # explicitly evidenced as historical tail gaps.
    recent_start = old_calendar[max(0, len(old_calendar) - 3)]
    gaps = {}
    for symbol, dates in absence_dates.items():
        old_dates = [day for day in dates if day <= old_last and day < recent_start]
        if old_dates:
            gaps[symbol] = old_dates

    tag = (data / ".tag").read_text().strip()
    digest = hashlib.sha256()
    row_count = 0

    with atomic.Transaction(data) as txn:
        stage = txn.copy()
        # Calendar is append-only and already proven identical on the prefix.
        with (stage / "calendars/day.txt").open("a", encoding="ascii") as handle:
            for day in added:
                handle.write(day + "\n")
        future = [line for line in (release / "calendars/day_future.txt").read_text().splitlines()
                  if line > target]
        (stage / "calendars/day_future.txt").write_text("\n".join(future) + "\n", encoding="ascii")

        for symbol in active:
            row_count += merge_symbol(stage, data / "cn_data", release, symbol,
                                      len(old_calendar), len(release_calendar),
                                      len(release_calendar), missing_new.get(symbol, set()),
                                      release_calendar, digest)
        for symbol, start, end in new_symbols:
            row_count += copy_new_symbol(stage, release, symbol,
                                         len(release_calendar), release_calendar, digest)
        update_all(stage, active, target, new_symbols)
        # Index membership files are snapshot intervals, not feature history;
        # the release snapshot is the authoritative membership through target.
        for source in sorted((release / "instruments").glob("*.txt")):
            if source.name == "all.txt":
                continue
            shutil.copy2(source, stage / "instruments" / source.name)

        # The active tree's last quoted date for every proven-absence symbol.
        last_quoted = {}
        for symbol in absence_dates:
            raw, start, length = feature(stage / "features" / symbol.lower() / "close.day.bin")
            for offset in range(length - 1, -1, -1):
                value = struct.unpack_from("<f", raw, 4 + offset * 4)[0]
                if math.isfinite(value) and value > 0:
                    last_quoted[symbol] = release_calendar[start + offset]
                    break
            else:
                raise ValueError(f"absence_symbol_without_quote:{symbol}")

        daily_rows = validate_coverage_days(
            stage, release_calendar, target_dates=added,
            source_absence=absence, expected_source_commit=source_commit)
        daily = [{key: value for key, value in row.items()
                  if key not in ("sourceAbsentDates", "lastQuotedDate")} for row in daily_rows]
        last = daily[-1]
        price = {field: last[field] for field in PRICE_FIELDS}
        price.update(
            status="partial" if any(row["invalid"] for row in daily) else "complete",
            daily_coverage=daily,
            sourceAbsentDates={symbol: list(dates) for symbol, dates in absence_dates.items()},
            lastQuotedDate=last_quoted,
            historical_tail_gaps=gaps,
            source_commit=source_commit,
            base_date=old_last,
        )
        legacy = {"sourceCommit": source_commit, "baseDate": old_last,
                  "dates": {}, "lastQuotedDates": {}}
        index = validate_index_market_alignment(
            stage, release_calendar, source_absence=absence,
            expected_source_commit=source_commit, legacy_unquoted=legacy)
        index["legacyUnquotedBaseDate"] = old_last

        manifest = {
            "schemaVersion": 1,
            "kind": "same_source_dolt_incremental",
            "status": "validated",
            "sourceRepository": "chenditc/investment_data",
            "sourceCommit": source_commit,
            "baseReleaseTag": tag,
            "baseDate": old_last,
            "targetDate": target,
            "calendarAdded": added,
            "sourceRows": row_count,
            "sourceRowsSha256": digest.hexdigest(),
            "sourceAbsence": absence,
            "priceValidation": price,
            "legacyUnquotedCSI": legacy,
            "indexPriceValidation": index,
            "dataQuality": "partial" if price["status"] == "partial" or index["status"] == "partial" else "complete",
            "verifiedAt": datetime.now(SHANGHAI).isoformat(),
            "scope": "market_data_only_no_snapshot_no_trading_no_delivery",
            "sourceAbsenceEvidence": "release_feature_arrays_padded_nan_factor_carried",
        }
        prepared = txn.prepare_manifest(manifest)
        candidate.consumer_check(health_file)(stage, prepared)

        if not publish:
            return {"status": "dry_run_validated", "target": target,
                    "rows": row_count, "manifest": prepared}

        if backup_dir:
            backup = Path(backup_dir).resolve()
            if backup.exists():
                raise ValueError("backup_dir_exists")
            backup.mkdir(parents=True)
            shutil.copytree(txn.active, backup / "cn_data", copy_function=shutil.copy2)
            shutil.copy2(data / ".tag", backup / ".tag")

        txn.commit(prepared)

    # postcheck asserts that no .qlib-incremental.* workspace remains, so it
    # must run only after Transaction.__exit__ has completed cleanup.
    result = postcheck.verify(data, target, health_file)
    status_path, status_mode = qrun.status_destination(data, dry_run=False)
    payload = build_current_status(
        run_id=str(uuid.uuid4()), status_mode=status_mode, data_root=str(data),
        planned_date=planned_date, target=target, source_commit=source_commit,
        source_mode=source_mode, source_failure=source_failure)
    qrun.write_update_status(status_path, payload)
    return {"status": "published", "postcheck": result,
            "manifest": prepared, "rows": row_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--health-file", type=Path, default=Path("/home/ubuntu/research-data/data_health.py"))
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--planned-date")
    parser.add_argument("--source-mode", default="release_fallback")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    result = publish_release(
        data_dir=args.data_dir, release_dir=args.release_dir,
        source_commit=args.source_commit, target=args.target,
        health_file=args.health_file, backup_dir=args.backup_dir,
        publish=args.publish, planned_date=args.planned_date,
        source_mode=args.source_mode)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
