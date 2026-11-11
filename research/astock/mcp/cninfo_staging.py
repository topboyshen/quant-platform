"""Cutoff-safe CNINFO pre-collection staging (schema v2).

The 08:00 run used to query CNINFO live, so the recorded query instant landed
after the frozen cutoff and the report could not judge window filings. A 07:50
pre-collection queries the same fixed watch codes earlier and records the real
observation time; the 08:00 run reuses those rows only when the staging file is
intact, names the same target date, covers exactly the requested watchlist, and
was observed no later than the request cutoff.

Per-code query instants and the batch completion instant are stored separately:
using the batch completion time as every stock's coverage boundary would
overstate the earlier queries. Never backdate, and never present a previous
day's query as today's: a per-code instant must fall on the target date. A
missing, malformed, wrong-date, incomplete, future, or late file simply falls
back to the live query, which then reports its own later observation honestly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
import tempfile
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 2
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS_PER_CODE = 64
MAX_WINDOW_ROWS = 120
MAX_TITLE_CHARS = 400
ROW_KEYS = {"title", "date", "dateKind", "observedAt", "nextDayDisclosure", "url", "disclosureEvidence"}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
CODE_RE = re.compile(r"[0-9]{6}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
STAGING_DIRNAME = "cninfo-staging"


class StagingError(ValueError):
    """A staging artifact is unusable; the caller must fall back to a live query."""


def staging_directory(project_root: os.PathLike[str] | str) -> Path:
    return Path(project_root) / "work" / STAGING_DIRNAME


def staging_path(directory: os.PathLike[str] | str, target_date: str) -> Path:
    if not isinstance(target_date, str) or not DATE_RE.fullmatch(target_date):
        raise StagingError("invalid_target_date")
    return Path(directory) / f"{target_date}.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _instant(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise StagingError(f"invalid_{field}")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise StagingError(f"invalid_{field}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StagingError(f"invalid_{field}")
    return parsed


def _local_day(value: dt.datetime, target_date: str) -> bool:
    """The observation must belong to the target date in its own offset."""
    return value.date().isoformat() == target_date


def _validate_row(row: Any, target_date: str, queried: dt.datetime):
    if not isinstance(row, dict) or set(row) - ROW_KEYS:
        raise StagingError("staging_row_shape")
    title = row.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
        raise StagingError("staging_row_title")
    if row.get("dateKind") != "source_disclosure_date":
        raise StagingError("staging_row_date_kind")
    day = row.get("date")
    if not isinstance(day, str) or not DATE_RE.fullmatch(day) or day > target_date:
        raise StagingError("staging_row_date")
    if not isinstance(row.get("nextDayDisclosure"), bool):
        raise StagingError("staging_row_next_day")
    observed = _instant(row.get("observedAt"), "staging_row_observed_at")
    if observed > queried or not _local_day(observed, target_date):
        raise StagingError("staging_row_observed_outside_query")
    url = row.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
        raise StagingError("staging_row_url")
    evidence = row.get("disclosureEvidence")
    if evidence is not None:
        if (not isinstance(evidence, dict) or set(evidence) != {"orgId", "announcementId", "adjunctUrl"}
                or any(not isinstance(value, str) or not value for value in evidence.values())):
            raise StagingError("staging_row_disclosure_evidence")
    return {"title": title, "date": day, "dateKind": "source_disclosure_date",
            "observedAt": row["observedAt"], "nextDayDisclosure": row["nextDayDisclosure"],
            **({"url": url} if url is not None else {}),
            **({"disclosureEvidence": dict(evidence)} if evidence is not None else {})}


def _validate_document(document: Any, code: str, target_date: str, queried: dt.datetime):
    if not isinstance(document, dict):
        raise StagingError("staging_document_shape")
    if (document.get("status") != "available" or document.get("kind") != "financial_summary"
            or document.get("code") != code or not isinstance(document.get("title"), str)
            or not isinstance(document.get("pdfSha256"), str)
            or not HASH_RE.fullmatch(document["pdfSha256"])
            or not isinstance(document.get("url"), str)
            or not document["url"].startswith("https://static.cninfo.com.cn/")
            or not isinstance(document.get("disclosureDate"), str)
            or not DATE_RE.fullmatch(document["disclosureDate"])
            or not isinstance(document.get("reportDate"), str)
            or not DATE_RE.fullmatch(document["reportDate"])
            or not isinstance(document.get("metrics"), list)
            or not any(isinstance(m, dict) and m.get("field") == "operatingCashFlow"
                       for m in document["metrics"])):
        raise StagingError("staging_document_invalid")
    observed = _instant(document.get("observedAt"), "staging_document_observed_at")
    if observed > queried or not _local_day(observed, target_date):
        raise StagingError("staging_document_observed_outside_query")
    if document.get("dataAsOf") != document.get("observedAt"):
        raise StagingError("staging_document_time_mismatch")
    return document


def _validate_window_query(value: Any, target_date: str, queried: dt.datetime,
                           cutoff: dt.datetime) -> dict[str, Any]:
    """Optional new query proof; old schema-v2 first-page files remain readable."""
    if not isinstance(value, dict) or set(value) != {"rows", "queryCoverage"}:
        raise StagingError("staging_window_shape")
    coverage, rows = value["queryCoverage"], value["rows"]
    required = {"kind", "windowStartAt", "cutoffAt", "queriedAt", "observedThroughAt",
                "pageSize", "maxPages", "pagesFetched", "total", "returned", "complete", "reason"}
    if not isinstance(coverage, dict) or set(coverage) != required or coverage.get("kind") != "window":
        raise StagingError("staging_window_coverage_shape")
    start = _instant(coverage["windowStartAt"], "window_start_at")
    end = _instant(coverage["cutoffAt"], "window_cutoff_at")
    if start > end or end > cutoff or not _local_day(end, target_date):
        raise StagingError("staging_window_bounds")
    if (type(coverage["pageSize"]) is not int or not 1 <= coverage["pageSize"] <= 100
            or type(coverage["maxPages"]) is not int or not 1 <= coverage["maxPages"] <= 8
            or coverage["pageSize"] * coverage["maxPages"] > MAX_WINDOW_ROWS
            or type(coverage["pagesFetched"]) is not int
            or not 0 <= coverage["pagesFetched"] <= coverage["maxPages"]
            or type(coverage["returned"]) is not int
            or type(coverage["complete"]) is not bool
            or not isinstance(coverage["reason"], str)
            or coverage["reason"] not in {"query_not_started", "cutoff_reached", "query_failed",
                                           "observed_after_cutoff", "pagination_changed",
                                           "window_filter_mismatch", "pagination_duplicate",
                                           "all_query_pages_collected", "page_limit_reached"}
            or not isinstance(rows, list) or len(rows) > MAX_WINDOW_ROWS
            or coverage["returned"] != len(rows)):
        raise StagingError("staging_window_coverage_values")
    total = coverage["total"]
    if total is not None and (type(total) is not int or total < len(rows)):
        raise StagingError("staging_window_total")
    observed = None
    through = None
    if coverage["queriedAt"] is not None:
        observed = _instant(coverage["queriedAt"], "window_queried_at")
        through = _instant(coverage["observedThroughAt"], "window_observed_through_at")
        if (observed > queried or observed > end or not _local_day(observed, target_date)
                or through > observed or not _local_day(through, target_date)):
            raise StagingError("staging_window_observation")
    elif coverage["observedThroughAt"] is not None or rows or coverage["pagesFetched"]:
        raise StagingError("staging_window_missing_observation")
    if coverage["complete"] and (
            observed is None or coverage["reason"] != "all_query_pages_collected"
            or total != len(rows) or coverage["pagesFetched"] < 1
            or total > coverage["pageSize"] * coverage["pagesFetched"]):
        raise StagingError("staging_window_unproved_completeness")
    validated = []
    identities = set()
    for row in rows:
        item = _validate_row(row, target_date, observed)
        # New window proofs require a real calendar date, not merely a
        # lexically matching YYYY-MM-DD string in an otherwise valid hash.
        # Keep legacy first-page artifacts on their existing compatibility path.
        try:
            disclosure_day = dt.date.fromisoformat(item["date"])
        except ValueError:
            raise StagingError("staging_window_row_invalid_date") from None
        if through > _instant(item["observedAt"], "window_row_observed_at"):
            raise StagingError("staging_window_visibility_overstated")
        if not start.date() <= disclosure_day <= end.date():
            raise StagingError("staging_window_row_outside_bounds")
        identity = ((item["url"],) if item.get("url")
                    else (item["title"], item["date"]))
        if identity in identities:
            raise StagingError("staging_window_duplicate")
        identities.add(identity)
        validated.append(item)
    return {"rows": validated, "queryCoverage": dict(coverage)}


def load_staging(directory: os.PathLike[str] | str, target_date: str, cutoff: dt.datetime,
                 expected_codes: Iterable[str] | None = None) -> dict[str, Any] | None:
    """Return validated staging for this target date, or None to use a live query.

    ``expected_codes`` is the request watchlist. When given, the staged set must
    match it exactly: a missing code would silently mix pre-collected and live
    coverage, and an unknown code means the artifact belongs to another run.

    Every failure is a fallback, never an exception into the data bridge.
    """
    try:
        path = staging_path(directory, target_date)
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            return None
        info = path.lstat()
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            return None
        if info.st_size <= 0 or info.st_size > MAX_BYTES:
            return None
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(StagingError("nonfinite_json")))
        if not isinstance(document, dict) or set(document) != {
                "schemaVersion", "targetDate", "batchCompletedAt", "collectorSourceSha256",
                "codes", "documents", "payloadSha256"}:
            return None
        if document.get("schemaVersion") != SCHEMA_VERSION or document.get("targetDate") != target_date:
            return None
        if (not isinstance(document.get("collectorSourceSha256"), str)
                or not HASH_RE.fullmatch(document["collectorSourceSha256"])
                or not isinstance(document.get("payloadSha256"), str)
                or not HASH_RE.fullmatch(document["payloadSha256"])):
            return None
        payload = {key: document[key] for key in
                   ("targetDate", "batchCompletedAt", "collectorSourceSha256", "codes", "documents")}
        if _digest(payload) != document["payloadSha256"]:
            return None
        batch = _instant(document.get("batchCompletedAt"), "staging_batch_completed_at")
        if batch > cutoff or not _local_day(batch, target_date):
            return None
        codes, documents = document.get("codes"), document.get("documents")
        if not isinstance(codes, dict) or not codes or not isinstance(documents, dict):
            return None
        if set(documents) - set(codes):
            return None
        if expected_codes is not None:
            expected = set(expected_codes)
            if not expected or set(codes) != expected:
                return None
        validated: dict[str, Any] = {}
        for code, entry in codes.items():
            if not isinstance(code, str) or not CODE_RE.fullmatch(code):
                return None
            if (not isinstance(entry, dict) or not {"queriedAt", "rows"} <= set(entry)
                    or set(entry) - {"queriedAt", "rows", "windowQuery"}):
                return None
            queried = _instant(entry.get("queriedAt"), "staging_code_queried_at")
            if queried > cutoff or queried > batch or not _local_day(queried, target_date):
                return None
            rows = entry.get("rows")
            if not isinstance(rows, list) or len(rows) > MAX_ROWS_PER_CODE:
                return None
            validated[code] = {"queriedAt": queried,
                               "rows": [_validate_row(row, target_date, queried) for row in rows]}
            if "windowQuery" in entry:
                validated[code]["windowQuery"] = _validate_window_query(
                    entry["windowQuery"], target_date, batch, cutoff)
        validated_documents = {}
        for code, document_value in documents.items():
            # PDF evidence is fetched after that stock's announcement query, so it
            # is bounded by the batch completion time, never by the earlier query.
            validated_documents[code] = _validate_document(
                document_value, code, target_date, batch)
        return {"targetDate": target_date, "batchCompletedAt": batch, "codes": validated,
                "documents": validated_documents, "collectorSourceSha256": document["collectorSourceSha256"]}
    except (OSError, ValueError, StagingError, UnicodeError, RecursionError):
        return None


def write_staging(directory: os.PathLike[str] | str, target_date: str,
                  batch_completed_at: dt.datetime,
                  codes: Mapping[str, Mapping[str, Any]],
                  documents: Mapping[str, Any],
                  collector_source_sha256: str) -> Path:
    """Atomically store a bounded, digested staging file (private, no symlinks).

    ``codes`` maps each code to ``{"queriedAt": iso, "rows": [...]}`` so the
    per-stock coverage boundary stays distinct from the batch completion time.
    """
    if batch_completed_at.tzinfo is None or batch_completed_at.utcoffset() is None:
        raise StagingError("invalid_batch_completed_at")
    payload = {
        "targetDate": target_date,
        "batchCompletedAt": batch_completed_at.isoformat(),
        "collectorSourceSha256": collector_source_sha256,
        "codes": {code: {"queriedAt": entry["queriedAt"], "rows": list(entry["rows"]),
                         **({"windowQuery": entry["windowQuery"]} if "windowQuery" in entry else {})}
                  for code, entry in codes.items()},
        "documents": dict(documents),
    }
    body = {**payload, "schemaVersion": SCHEMA_VERSION, "payloadSha256": _digest(payload)}
    encoded = _canonical(body)
    if len(encoded) > MAX_BYTES:
        raise StagingError("staging_too_large")
    directory_path = Path(directory)
    directory_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory_path.is_symlink():
        raise StagingError("staging_directory_symlink")
    path = staging_path(directory_path, target_date)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise StagingError("staging_path_not_regular_file")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target_date}.", dir=directory_path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
