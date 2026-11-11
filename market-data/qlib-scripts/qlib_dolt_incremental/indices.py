"""Fixed-snapshot CSI interval extension. Writes only the supplied staging tree.

All index plans are validated before any stage file changes. This function never
publishes a tree, changes a release tag, fills absent source members, or fetches
unbounded constituent history. Caller owns the production lock and promotion.
"""

from __future__ import annotations

from datetime import date
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid


INDEX_MAP = {
    "csi300": "399300.SZ", "csi500": "000905.SH", "csi800": "000906.SH",
    "csi1000": "000852.SH", "csiall": "000985.SH",
}
NOMINAL_COUNTS = {"csi300": 300, "csi500": 500, "csi800": 800, "csi1000": 1000}
MAX_FILE_BYTES = 16 * 1024 * 1024
REF_RE = re.compile(r"[0-9a-v]{32}\Z")


class IndicesError(ValueError):
    pass


def _day(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise IndicesError("invalid_date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise IndicesError("invalid_date") from None
    return value


def _symbol(value, *, source=False):
    if not isinstance(value, str):
        raise IndicesError("invalid_symbol")
    upper = value.upper()
    if source:
        match = re.fullmatch(r"([0-9]{6})\.(SH|SZ|BJ)", upper)
        if match:
            return match.group(2) + match.group(1)
    if re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", upper) is None:
        raise IndicesError("invalid_symbol")
    return upper


def interval_symbol(value, end, boundary):
    """Parse a local interval ID without inventing a modern symbol mapping.

    Legacy alphabetic IDs may be retained only in already-ended intervals.
    This parser must not be used for source snapshots: `_symbol(source=True)`
    deliberately continues to reject every nonnumeric source member.
    """
    end, boundary = _day(end), _day(boundary)
    if isinstance(value, str) and re.fullmatch(r"(?:SH|SZ|BJ)[A-Z][0-9]{5}", value.upper()):
        if end >= boundary:
            raise IndicesError("legacy_symbol_interval_not_closed")
        return value.upper()
    return _symbol(value)


def _directory(path):
    try:
        info = path.lstat()
    except OSError:
        raise IndicesError("directory_unavailable") from None
    if not stat.S_ISDIR(info.st_mode):
        raise IndicesError("unsafe_directory")


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise IndicesError("unsafe_index_file")
            raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise IndicesError("index_file_too_large")
            return raw
    except OSError:
        raise IndicesError("index_file_unavailable_or_unsafe") from None


def _parse(raw, base_date):
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise IndicesError("index_not_utf8") from None
    if not text or not text.endswith("\n"):
        raise IndicesError("index_empty_or_unterminated")
    intervals, by_symbol, cases = [], {}, set()
    for line in text.splitlines():
        cells = line.split()
        if len(cells) != 3:
            raise IndicesError("invalid_interval_row")
        spelling, start, end = cells
        start, end = _day(start), _day(end)
        symbol = interval_symbol(spelling, end, base_date)
        if start > end or end > base_date:
            raise IndicesError("invalid_or_future_interval")
        if spelling not in {spelling.upper(), spelling.lower()}:
            raise IndicesError("mixed_symbol_case")
        cases.add("upper" if spelling == spelling.upper() else "lower")
        intervals.append([symbol, start, end])
        by_symbol.setdefault(symbol, []).append((start, end))
    if len(cases) != 1:
        raise IndicesError("mixed_symbol_case")
    for periods in by_symbol.values():
        ordered = sorted(periods)
        if any(a[1] >= b[0] for a, b in zip(ordered, ordered[1:])):
            raise IndicesError("overlapping_index_intervals")
    if not intervals or max(row[2] for row in intervals) != base_date:
        raise IndicesError("index_not_at_base_date")
    active = {row[0] for row in intervals if row[2] == base_date}
    if not active:
        raise IndicesError("empty_base_membership")
    return intervals, active, max(row[1] for row in intervals), next(iter(cases))


def _count(value):
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,6}", value) is None:
        raise IndicesError("invalid_snapshot_count")
    return int(value)


def _snapshots(client, index_code, start, base, target, calendar):
    table = client.table("ts_index_weight")
    predicate = f"index_code='{index_code}' AND trade_date>='{start}' AND trade_date<='{target}'"
    groups = client.query(f"SELECT trade_date,COUNT(*) AS member_count FROM {table} WHERE {predicate} GROUP BY trade_date ORDER BY trade_date")
    if not groups:
        raise IndicesError("no_source_membership_snapshot")
    counts, prior = {}, None
    for row in groups:
        if not isinstance(row, dict) or set(row) != {"trade_date", "member_count"}:
            raise IndicesError("invalid_snapshot_metadata")
        day = _day(row["trade_date"])
        if day < start or day > target or (prior is not None and day <= prior):
            raise IndicesError("snapshot_date_out_of_range_or_unordered")
        counts[day] = _count(row["member_count"])
        prior = day
    candidates = [day for day in counts if day <= base]
    if not candidates:
        raise IndicesError("missing_baseline_snapshot")
    baseline = max(candidates)
    selected = [baseline] + [day for day in counts if day > base]
    if any(day not in calendar for day in selected):
        raise IndicesError("snapshot_not_in_trading_calendar")
    dates_sql = ",".join("'" + day + "'" for day in selected)
    rows = client.paginated_rows(
        "ts_index_weight", ("index_code", "stock_code", "trade_date"),
        f"index_code='{index_code}' AND trade_date IN ({dates_sql})",
        ("index_code", "trade_date", "stock_code"),
    )
    snapshots = {day: set() for day in selected}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"index_code", "stock_code", "trade_date"} or row["index_code"] != index_code:
            raise IndicesError("source_membership_identity_mismatch")
        day = _day(row["trade_date"])
        if day not in snapshots:
            raise IndicesError("unexpected_membership_date")
        symbol = _symbol(row["stock_code"], source=True)
        if symbol in snapshots[day]:
            raise IndicesError("duplicate_source_member")
        snapshots[day].add(symbol)
    if any(len(snapshots[day]) != counts[day] for day in selected):
        raise IndicesError("incomplete_membership_snapshot")
    return baseline, snapshots


def _historical(intervals, base):
    return sorted((symbol, start, min(end, base)) for symbol, start, end in intervals if start <= base)


def _render(intervals, style):
    lines = []
    for symbol, start, end in intervals:
        lines.append("\t".join((symbol.lower() if style == "lower" else symbol, start, end)))
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise IndicesError("output_index_file_too_large")
    return raw


def _replace_stage(path, raw, expected_directory):
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temp_name = None
    try:
        current = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != expected_directory:
            raise IndicesError("stage_directory_changed")
        candidate_name = "." + path.name + "." + uuid.uuid4().hex + ".new"
        fd = os.open(candidate_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory_fd)
        temp_name = candidate_name
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temp_name = None
        os.fsync(directory_fd)
    finally:
        try:
            if temp_name is not None:
                os.unlink(temp_name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)


def update_indices(old_root, stage_root, client, base_date, target_date, new_calendar):
    """Validate five CSI files, then extend only stage intervals after base_date.

    Between explicitly observed membership snapshots, the last observed set is
    carried forward, matching interval semantics. The summary exposes the last
    snapshot date; this does not claim a new constituent observation each day.
    """
    base, target = _day(base_date), _day(target_date)
    if target < base or isinstance(new_calendar, (str, bytes)):
        raise IndicesError("invalid_target_or_calendar")
    calendar = tuple(_day(day) for day in new_calendar)
    if not calendar or calendar[-1] != target or base not in calendar or any(a >= b for a, b in zip(calendar, calendar[1:])):
        raise IndicesError("calendar_not_aligned")
    new_days = [day for day in calendar if day > base]
    old_root, stage_root = Path(old_root), Path(stage_root)
    for directory in (old_root, stage_root, old_root / "instruments", stage_root / "instruments"):
        _directory(directory)
    old_real, stage_real = old_root.resolve(), stage_root.resolve()
    if old_real == stage_real or old_real in stage_real.parents or stage_real in old_real.parents:
        raise IndicesError("stage_overlaps_old_tree")
    old_info = (old_root / "instruments").stat()
    stage_info = (stage_root / "instruments").stat()
    stage_identity = (stage_info.st_dev, stage_info.st_ino)
    if stage_identity == (old_info.st_dev, old_info.st_ino):
        raise IndicesError("stage_aliases_old_instruments")
    ref = client.latest_ref()
    if not isinstance(ref, str) or REF_RE.fullmatch(ref) is None:
        raise IndicesError("invalid_source_commit")
    originals, outputs, reports = {}, {}, {}
    for name, index_code in INDEX_MAP.items():
        original = _read(old_root / "instruments" / (name + ".txt"))
        if _read(stage_root / "instruments" / (name + ".txt")) != original:
            raise IndicesError("stage_index_not_original_clone")
        originals[name] = original
        intervals, active_base, terminal_start, style = _parse(original, base)
        if name in NOMINAL_COUNTS and len(active_base) != NOMINAL_COUNTS[name]:
            raise IndicesError(f"old_membership_cardinality:{name}:expected={NOMINAL_COUNTS[name]}:actual={len(active_base)}")
        baseline, snapshots = _snapshots(client, index_code, terminal_start, base, target, set(calendar))
        if snapshots[baseline] != active_base:
            raise IndicesError("base_membership_mismatch:" + name)
        # Fixed-size CSI indices must not silently turn an incomplete source
        # group into constituent removals. csiall legitimately changes size.
        if name in NOMINAL_COUNTS and any(len(members) != NOMINAL_COUNTS[name] for members in snapshots.values()):
            raise IndicesError("membership_cardinality_changed:" + name)
        planned = [row[:] for row in intervals]
        open_rows = {row[0]: i for i, row in enumerate(planned) if row[2] == base}
        active, snapshot_date = set(active_base), baseline
        for day in new_days:
            if day in snapshots:
                snapshot_date = day
            desired = snapshots[snapshot_date]
            for symbol in sorted(active - desired):
                open_rows.pop(symbol)
            for symbol in sorted(active & desired):
                planned[open_rows[symbol]][2] = day
            for symbol in sorted(desired - active):
                open_rows[symbol] = len(planned)
                planned.append([symbol, day, day])
            active = set(desired)
        if _historical(planned, base) != _historical(intervals, base):
            raise IndicesError("historical_membership_rewrite")
        output = _render(planned, style)
        _parse(output, target)
        outputs[name] = output
        reports[name] = {
            "source_index": index_code, "terminal_start": terminal_start,
            "base_snapshot": baseline, "target_snapshot": snapshot_date,
            "source_snapshot_dates": sorted(snapshots), "base_members": len(active_base),
            "target_members": len(active), "changed": output != original,
            "sha256": hashlib.sha256(output).hexdigest(),
        }
    if client.latest_ref() != ref:
        raise IndicesError("source_commit_changed")
    # Recheck all originals before the first stage write. Parent owns the lock;
    # this also catches a clone or source file changing during a long API call.
    for name, original in originals.items():
        for root in (old_root, stage_root):
            if _read(root / "instruments" / (name + ".txt")) != original:
                raise IndicesError("index_changed_during_planning")
    for name, output in outputs.items():
        if output != originals[name]:
            _replace_stage(stage_root / "instruments" / (name + ".txt"), output, stage_identity)
    return {"source_commit": ref, "base_date": base, "target_date": target,
            "historical_membership_unchanged": True, "indices": reports}
