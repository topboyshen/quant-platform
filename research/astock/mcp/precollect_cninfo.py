"""07:50 CNINFO pre-collection for the 08:00 AStock run.

Queries the same fixed watch codes with the same CninfoClient the bridge uses,
retains each stock's real query instant and the batch completion instant
separately, and stages the rows plus half-year-summary PDF evidence so the
08:00 run can treat window filings as cutoff-known facts.

All-or-nothing: any code failure removes today's staging file and exits nonzero,
so the 08:00 run falls back to its own live query instead of using a partial
artifact. The file is re-read through the same validator the bridge uses,
including the watchlist-completeness check. Nothing here rewrites past
observations or invents an earlier time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cninfo_source import CninfoClient, CninfoError  # noqa: E402
from cninfo_evidence import collect_summary  # noqa: E402
from cninfo_staging import StagingError, load_staging, staging_directory, staging_path, write_staging  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))
SUMMARY_TITLE = re.compile(r"20\d{2}年半年度报告摘要\Z")
PAGE_SIZE = 30
DOCUMENT_BUDGET_SECONDS = 180.0
# collect_summary enforces a hard process-boundary cap: timeout_seconds must be
# positive and <= 10, matching the live bridge call.
PER_DOCUMENT_SECONDS = 10.0
WINDOW_LOOKBACK_DAYS = 14


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def watchlist() -> list[dict]:
    with open(PROJECT_ROOT / "config" / "stocks.json", encoding="utf-8") as handle:
        config = json.load(handle)
    stocks = []
    for item in config.get("corePool", []):
        code = str(item.get("code", ""))
        name = str(item.get("name", ""))
        if not re.fullmatch(r"[0-9]{6}", code) or not name:
            raise SystemExit(f"invalid watchlist entry: {code}")
        stocks.append({"code": code, "name": name})
    if not stocks:
        raise SystemExit("watchlist is empty")
    return stocks


def scheduled_cutoff(target_day: dt.date) -> dt.datetime:
    """Use the configured future run boundary, never backdate observations."""
    with open(PROJECT_ROOT / "config" / "workflow.json", encoding="utf-8") as handle:
        config = json.load(handle)
    value = config.get("scheduledTime")
    if (config.get("timezone") != "Asia/Shanghai" or not isinstance(value, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)):
        raise ValueError("invalid_scheduled_cutoff")
    hour, minute = map(int, value.split(":"))
    return dt.datetime.combine(target_day, dt.time(hour, minute), CST)


def summary_candidates(rows: list[dict], target_date: str) -> list[dict]:
    return [row for row in rows
            if isinstance(row, dict)
            and SUMMARY_TITLE.fullmatch(str(row.get("title", "")).strip())
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date", "")))
            and str(row["date"]) <= target_date]


def collector_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def discard(directory: Path, target_date: str) -> None:
    """A failed or unreadable pre-collection must never leave today's file behind."""
    try:
        path = staging_path(directory, target_date)
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def remaining_budget(cutoff: dt.datetime, deadline: float, *, observed_at=None) -> float:
    """No new operation may start beyond either the wall-clock or run budget."""
    observed = observed_at if observed_at is not None else now_cst()
    return min((cutoff - observed).total_seconds(), deadline - time.monotonic())


def main() -> int:
    now = now_cst()
    target_date = now.date().isoformat()
    directory = staging_directory(PROJECT_ROOT)
    if now.weekday() >= 5:
        print(json.dumps({"status": "skipped_non_weekday", "targetDate": target_date}, ensure_ascii=False))
        return 0

    stocks = watchlist()
    expected_codes = [stock["code"] for stock in stocks]
    collected: dict[str, dict] = {}
    documents: dict[str, dict] = {}
    deadline = time.monotonic() + DOCUMENT_BUDGET_SECONDS
    try:
        cutoff = scheduled_cutoff(now.date())
        if now >= cutoff:
            print(json.dumps({"status": "held_after_cutoff", "targetDate": target_date}, ensure_ascii=False))
            return 1
        window_start = now.date() - dt.timedelta(days=WINDOW_LOOKBACK_DAYS)
        with CninfoClient() as client:
            for stock in stocks:
                code = stock["code"]
                if remaining_budget(cutoff, deadline) <= 0:
                    raise CninfoError("announcements", "CutoffReached", code)
                rows = client.announcements(code, page_size=PAGE_SIZE,
                                            not_after=cutoff, deadline=deadline)
                historical_completed_at = now_cst()
                if remaining_budget(cutoff, deadline, observed_at=historical_completed_at) <= 0:
                    raise CninfoError("announcements", "CutoffReached", code)
                # The explicit date-window query is separate from the legacy
                # first page, which still supplies older PDF evidence.
                window_query = client.announcement_window(
                    code, window_start=window_start, cutoff=cutoff,
                    page_size=PAGE_SIZE, max_pages=4, deadline=deadline)
                # Each stock keeps its own real query instant; the batch time is
                # recorded after every stock and PDF is done.
                collected[code] = {"queriedAt": historical_completed_at.isoformat(), "rows": rows,
                                   "windowQuery": window_query}
                candidates = sorted(summary_candidates(rows, target_date), key=lambda row: row["date"])
                if not candidates:
                    continue
                remaining = remaining_budget(cutoff, deadline)
                if remaining <= 0:
                    raise CninfoError("announcements", "CutoffReached", code)
                if remaining <= 1:
                    continue
                row = candidates[-1]
                documents[code] = collect_summary(
                    row, stock, PROJECT_ROOT / "data" / "inbox" / "cninfo-pdf",
                    timeout_seconds=min(PER_DOCUMENT_SECONDS, remaining))
    except (CninfoError, ValueError, OSError, StagingError) as error:
        discard(directory, target_date)
        print(json.dumps({"status": "failed", "targetDate": target_date,
                          "errorType": type(error).__name__}, ensure_ascii=False))
        return 1

    batch_completed_at = now_cst()
    if remaining_budget(cutoff, deadline, observed_at=batch_completed_at) <= 0:
        discard(directory, target_date)
        print(json.dumps({"status": "held_after_cutoff_or_budget", "targetDate": target_date}, ensure_ascii=False))
        return 1
    try:
        path = write_staging(directory, target_date, batch_completed_at, collected, documents,
                             collector_source_sha256())
    except (OSError, StagingError) as error:
        discard(directory, target_date)
        print(json.dumps({"status": "write_failed", "targetDate": target_date,
                          "errorType": type(error).__name__}, ensure_ascii=False))
        return 1
    # Read back through the same validator the 08:00 run uses, including that the
    # staged watchlist matches the configured pool exactly.
    staged = load_staging(directory, target_date, cutoff, expected_codes=expected_codes)
    if staged is None:
        discard(directory, target_date)
        print(json.dumps({"status": "readback_failed", "targetDate": target_date}, ensure_ascii=False))
        return 1
    # Everything printed must be JSON-serializable: the staged times are real
    # datetimes and a bare json.dumps would raise after a valid file was written.
    per_code = {code: entry["queriedAt"].isoformat() for code, entry in staged["codes"].items()}
    print(json.dumps({"status": "staged", "targetDate": target_date,
                      "batchCompletedAt": staged["batchCompletedAt"].isoformat(),
                      "queriedAt": per_code,
                      "rows": {code: len(entry["rows"]) for code, entry in staged["codes"].items()},
                      "documents": sorted(staged["documents"]), "file": str(path)},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
