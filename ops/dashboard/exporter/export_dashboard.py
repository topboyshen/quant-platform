#!/usr/bin/env python3
"""Read-only dashboard exporter: fixed whitelist of sources -> sanitized JSON.

Design rules (see dashboard/README.md):
  * No database access, no subprocess, no network, no secrets.
  * Fixed file list and field whitelist; unknown fields are dropped, never passed through.
  * Markdown becomes structured plain-text blocks (no HTML), so the browser only
    ever assigns textContent and report content can never execute.
  * Missing evidence is never a pass: an incomplete seal or a delivery without a
    platform receipt stays 'insufficient'; only a real mismatch is 'fail'.
  * Atomic replace per file under a verified real output directory (no symlinked
    component may redirect the write), files 0640 / directories 0750 for the
    dedicated web account, optional dedicated group.
  * One malformed source degrades one panel, never the whole batch, and every
    panel records readErrors instead of silently publishing an "all good" page.
  * Every panel carries sourceTime / exportedAt / ageSeconds / stale so stale data
    can never be presented as current-normal; the page re-checks staleness at read
    time because an exporter that stops must not look fresh forever.

Usage:
  export_dashboard.py --source-root <root> --out <dir> [--health-json PATH]
                      [--services-json PATH] [--history 7] [--group NAME] [--now ISO]
"""

from __future__ import annotations

import argparse
import datetime as dt
import grp
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

CST = dt.timezone(dt.timedelta(hours=8))
SCHEMA_VERSION = 1
MAX_BLOCK_CHARS = 4000
MAX_BLOCKS = 240
MAX_SOURCES = 60
MAX_TEXT_CHARS = 200
REPORT_TEXT_CONTRACT = "nng8.report-text.v2"
MAX_REPORT_TEXT_CHARS = 180000
MAX_REPORT_TEXT_BYTES = 180000
MAX_REPORT_BODY_WIRE_BYTES = 200000
MAX_REPORT_BYTES = 1024 * 1024
MAX_HISTORY = 30
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REF_RE = re.compile(r"\[来源\s*([0-9]+(?:\s*、\s*[0-9]+)*)\]")
URL_RE = re.compile(r"https?://[^\s)\]，。；、]+")
MD_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\(([^)\s]{1,500})\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^([-*+]|\d{1,3}\.)\s+(.*)$")
QUOTE_RE = re.compile(r"^>\s?(.*)$")
STRONG_RE = re.compile(r"\*{1,2}([^*]{1,200})\*{1,2}")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")

THRESHOLDS_HOURS = {"today": 26.0, "progress": 26.0, "delivery": 30.0, "projects": 26.0,
                    "report": 26.0}
FILE_MODE = 0o640
DIR_MODE = 0o750
HISTORY_PAGES = "reports"

KNOWN_UNITS = (
    "astock-acceptance-night.timer", "astock-acceptance-monday.timer",
    "hermes-telegram-watchdog.timer",
    "astock-xhs-source.timer", "astock-xhs-source.service",
    "astock-xhs-draft.service", "astock-research-seal.service",
    "astock-morning-notify.service",
    "nng8-research-digest.timer", "nng8-research-digest.service",
    "research-chain-alert.timer", "research-chain-alert.service",
    "research-chain-alert-post.service", "research-chain-alert-generation.service",
    "astock-cninfo-precollect.timer", "astock-cninfo-precollect.service",
    "research-data-health.timer", "research-data-health.service",
    "research-data-health-alert.service", "research-ledger.service",
    "research-system-phase2.service",
    "hermes-telegram-watchdog.service",
    "nng8-dashboard-refresh.timer", "nng8-dashboard-refresh.service",
    "hermes-fd-monitor.timer", "hermes-fd-monitor.service",
    "hermes-fd-alert.timer", "hermes-fd-alert.service",
    "nginx.service", "mihomo.service", "nng8-operations.service",
    "jie-pi-worker.service", "hermes-gateway.service",
)
OK_SEVERITIES = ("critical", "warning", "information", "info")
# Original user-confirmed 8+10 scope. The 2026-09-19 22:45 audit was read on
# 2026-09-20. Its date, hash, and membership remain the original anchor.
RETAINED_BASELINE_DATE = "2026-09-20"
RETAINED_BASELINE_SHA256 = "79fc2927e988b227a5c1f2a18700c319a7c433889ce3b61cd268d54c3ee140bc"
RETAINED_ISSUES = {
    "unresolved_trading_day_gaps": ("critical", frozenset((
        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-11",
        "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"))),
    "legacy_gaps_or_invalid_rows": ("warning", frozenset((
        "2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
        "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"))),
}
# Explicit later registration, kept apart from the original scope. The first
# audit clock comes from the 2026-09-22 systemd journal; its complete JSON
# line's hash is a reconstruction, not a retained original file hash. The
# method was cross-checked against the still present 2026-09-23 audit file.
# No acceptance timestamp or per-day critical severity is asserted.
RETAINED_ADDENDA = ({
    "code": "unresolved_trading_day_gaps", "aggregateIssueSeverity": "critical",
    "date": "2026-09-22",
    "firstObservedAt": "2026-09-22T22:45:00.813661+08:00",
    "firstObservedSource": "research-data-health.service journal_reconstruction",
    "reconstructedJournalPayloadSha256": "226b85d0c84c683faeee39757ecdda6f9723441bf1379070a8f0613f6ed935f2",
},)
PENDING_AUTHORIZATION = (
    "signal-bus 远端仓库、可见性与推送授权",
    "research-data 远端仓库、可见性与推送授权",
    "公网发布（含认证、证书与最小暴露面）",
    "维护窗口：libc6 重启标记、可升级清单复核",
)
# Fixed pass-through for the service snapshot; unknown keys are dropped.
UNIT_FIELDS = ("unit", "scope", "kind", "health", "note", "loadState", "activeState", "subState",
               "unitFileState", "result", "execMainStatus", "restarts", "lastStart", "lastExit",
               "nextElapse", "nextElapseSource", "unitType", "invocationId")
UNIT_TEXT_LIMIT = 200


# Trusted CODE path, never inferred from --source-root or a receipt.
DELIVERY_CONTRACT_PATH = Path("/home/ubuntu/signal-bus/scripts/delivery_evidence.py")


def load_delivery_contract(module_path=None):
    """Controlled code-path hook for isolated tests / explicitly staged deployments."""
    path = Path(module_path) if module_path is not None else DELIVERY_CONTRACT_PATH
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("delivery_contract_unavailable")
    spec = importlib.util.spec_from_file_location("dashboard_delivery_evidence", path)
    if spec is None or spec.loader is None:
        raise ValueError("delivery_contract_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if type(getattr(module, "CONTRACT_VERSION", None)) is not int or module.CONTRACT_VERSION != 1 \
            or not all(callable(getattr(module, name, None)) for name in
                       ("relay_verdict", "telegram_verdict", "validate_send_receipt", "platform_receipt")):
        raise ValueError("delivery_contract_version_unsupported")
    return module


def parse_instant(value):
    """Parse an ISO instant, tolerating the trailing Z on Python 3.9 too."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None


def now_cst(override: str | None = None) -> dt.datetime:
    if override:
        parsed = parse_instant(override)
        if parsed is None:
            raise ValueError("--now must be an ISO instant")
        return parsed.astimezone(CST)
    return dt.datetime.now(CST)


def iso(value: dt.datetime) -> str:
    return value.astimezone(CST).isoformat()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def clean_text(value: object, limit: int = MAX_BLOCK_CHARS) -> str:
    text = str(value if value is not None else "")
    text = CONTROL_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return text[:limit]


def safe_url(value: object) -> str | None:
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")):
        return None
    if any(character.isspace() for character in text) or len(text) > 500:
        return None
    return text


def sha256_file(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def sha256_without_trailing_newline(path: Path) -> str | None:
    """Hash used by the archive for result.json.

    ``run-archive.mjs`` writes ``canonicalJson(result) + "\n"`` and records
    ``jsonHash(result)``, i.e. the hash of the canonical bytes *without* that
    trailing newline. Verified against the production archives: the recorded
    resultSha256 equals sha256(bytes minus one trailing newline) for
    2026-09-15/16/17/18.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        body = path.read_bytes()
    except OSError:
        return None
    if body.endswith(b"\n"):
        body = body[:-1]
    return sha256_bytes(body)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_telegram_event(path: Path):
    """Separate absent evidence from an existing but unreadable/bad JSON record."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if not stat.S_ISREG(mode):
        return None, "unreadable"
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeError:
        return None, "malformed"
    except OSError:
        return None, "unreadable"
    try:
        value = json.loads(body)
    except ValueError:
        return None, "malformed"
    return (value, None) if isinstance(value, dict) else (None, "malformed")


def read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ------------------------------------------------------------------ markdown

def markdown_blocks(markdown: str):
    """Line-based Markdown -> structured blocks. No HTML is produced or kept."""
    blocks, sources, section = [], [], None
    pending_source = None
    for raw in markdown.split("\n"):
        line = clean_text(raw, MAX_BLOCK_CHARS).rstrip()
        if not line.strip():
            continue
        if line.startswith("## 来源"):
            section = "sources"
            continue
        heading = HEADING_RE.match(line)
        if heading and not section:
            blocks.append({"kind": f"h{len(heading.group(1))}",
                           "text": clean_text(heading.group(2).strip(), MAX_TEXT_CHARS)})
            continue
        if section == "sources":
            if line.strip() == "---" or line.startswith("本晨报用于信息整理"):
                section = None
                continue
            entry = re.match(r"^(\d+)\.\s+(.*)$", line.strip())
            if entry:
                if pending_source is not None:
                    sources.append(pending_source)
                pending_source = {"id": int(entry.group(1)),
                                  "text": clean_text(entry.group(2), 600), "url": None}
            elif pending_source is not None:
                pending_source["text"] = clean_text(
                    f"{pending_source['text']} {line.strip()}", 600)
            continue
        if line.strip() == "---":
            continue
        quote = QUOTE_RE.match(line)
        if quote:
            blocks.append({"kind": "quote", "text": clean_text(quote.group(1), 600)})
            continue
        bullet = BULLET_RE.match(line.strip())
        text = bullet.group(2) if bullet else line.strip()
        link_urls = []
        for label, target in MD_LINK_RE.findall(text):
            text = text.replace(f"[{label}]({target})", label)
            safe_target = safe_url(target)
            if safe_target:
                link_urls.append(safe_target)
        text = STRONG_RE.sub(r"\1", text)
        refs = [int(value) for group in REF_RE.findall(text) for value in group.split("、")]
        urls = link_urls + [url for url in (safe_url(match.group(0))
                                            for match in URL_RE.finditer(text)) if url]
        body = REF_RE.sub("", text).strip()
        blocks.append({"kind": "bullet" if bullet else "paragraph",
                       "text": clean_text(body, MAX_TEXT_CHARS),
                       "refs": refs, "urls": urls})
        if len(blocks) >= MAX_BLOCKS:
            break
    if pending_source is not None:
        sources.append(pending_source)
    for source in sources:
        url = next((safe_url(match.group(0)) for match in URL_RE.finditer(source["text"])), None)
        source["url"] = url
    return blocks[:MAX_BLOCKS], sources[:MAX_SOURCES]


def section_blocks(blocks, title_contains):
    """Blocks following a heading whose text contains the marker, up to the next heading."""
    collected, inside = [], False
    for block in blocks:
        if block["kind"].startswith("h"):
            if inside:
                break
            inside = title_contains in block["text"]
            continue
        if inside:
            collected.append(block)
    return collected


def freshness(exported: dt.datetime, source_time: dt.datetime | None, page: str):
    threshold = THRESHOLDS_HOURS[page]
    if source_time is None or source_time.tzinfo is None:
        return {"sourceTime": None, "ageSeconds": None, "stale": True,
                "thresholdHours": threshold, "reason": "source_time_unavailable"}
    age = (exported - source_time).total_seconds()
    return {"sourceTime": iso(source_time), "ageSeconds": round(age),
            "stale": age > threshold * 3600 or age < -300,
            "thresholdHours": threshold, "reason": None}


def report_freshness(project: Path, date: str | None, exported: dt.datetime,
                     source_time: dt.datetime | None, page: str):
    """A sealed report is due again on its recorded next trading day, not in 26h.

    Only report-related panels use this policy. Daily health/Qlib and service
    clocks remain independent. No network calendar lookup or weekday guessing.
    The existing source unit starts at 08:00 CST, TimeoutStartSec=1h and timer
    AccuracySec=30s: 09:01 is the conservative dashboard overdue boundary, NOT
    an extension of the relay send window or a change to any business timer.
    """
    result = freshness(exported, source_time, page)
    result["freshnessPolicy"] = None

    def fallback(reason):
        result["freshnessPolicyError"] = reason
        return result

    if page not in ("today", "delivery", "report") or not is_iso_date(date):
        return fallback("report_date_unavailable")
    if source_time is None or source_time.tzinfo is None:
        return fallback("source_time_unavailable")
    if source_time.astimezone(CST).date().isoformat() != date:
        return fallback("source_report_date_mismatch")
    archive = project / "work" / "report-archives" / date
    input_path = archive / "input.json"
    published = read_json(archive / "published.json")
    try:
        if input_path.is_symlink():
            return fallback("calendar_input_symlink")
        input_bytes = input_path.read_bytes()
        frozen = json.loads(input_bytes)
    except (OSError, ValueError):
        return fallback("calendar_input_unavailable")
    input_hash = sha256_bytes(input_bytes)
    report_hash = sha256_file(project / "reports" / "daily" / f"{date}.md")
    if (not isinstance(published, dict) or published.get("status") != "sealed"
            or published.get("inputSha256") != input_hash or not report_hash
            or published.get("reportSha256") != report_hash):
        return fallback("calendar_seal_binding_failed")
    if not isinstance(frozen, dict) or frozen.get("targetDate") != date:
        return fallback("calendar_input_date_mismatch")
    context = frozen.get("compilerContext")
    data = context.get("data") if isinstance(context, dict) else None
    calendar = data.get("tradingCalendar") if isinstance(data, dict) else None
    if (not isinstance(calendar, dict) or calendar.get("targetDate") != date
            or calendar.get("status") != "trading_day"):
        return fallback("calendar_record_unavailable")
    following = calendar.get("nextTradingDate")
    if not is_iso_date(following):
        return fallback("next_trading_date_unavailable")
    gap = (dt.date.fromisoformat(following) - dt.date.fromisoformat(date)).days
    if not 0 < gap <= 31:
        return fallback("next_trading_date_invalid")
    observed = parse_instant(calendar.get("dataAsOf"))
    if (observed is None or observed.tzinfo is None
            or observed.astimezone(CST).date().isoformat() != date
            or (observed - exported).total_seconds() > 300):
        return fallback("calendar_observed_time_invalid")
    sources = calendar.get("sourceIds")
    if not isinstance(sources, list) or "akshare:sina:trade-calendar" not in sources:
        return fallback("calendar_source_unavailable")
    expected = dt.datetime.combine(dt.date.fromisoformat(following), dt.time(8), CST)
    deadline = expected + dt.timedelta(minutes=61)
    result["freshnessPolicy"] = {
        "kind": "sealed_trading_calendar_v1", "reportDate": date,
        "nextTradingDate": following, "expectedAt": iso(expected),
        "deadlineAt": iso(deadline), "calendarObservedAt": iso(observed),
        "calendarSourceIds": ["akshare:sina:trade-calendar"],
        "inputSha256": input_hash, "reportSha256": report_hash,
    }
    result["freshnessPolicyError"] = None
    future_source = (source_time - exported).total_seconds() > 300
    result["stale"] = future_source or exported >= deadline
    result["reason"] = ("source_time_in_future" if future_source else
                        "expected_report_overdue" if exported >= deadline else
                        "awaiting_scheduled_report" if exported >= expected else
                        "latest_scheduled_report")
    return result


MARKET_HOLIDAY_CONTEXT_RE = re.compile(r"work/[A-Za-z0-9._/-]{1,200}\.json\Z")


def holiday_skip_record(project: Path):
    """Validated trading-calendar skip record, without any export-time window.

    Same evidence rule as the AStock publish-branch gate: the latest run is a
    non-trading-day skip bound by sha256 to its frozen context. No weekday
    guessing; without that evidence this returns None.
    """
    last_run = read_json(project / "work" / "last-run.json")
    if not isinstance(last_run, dict):
        return None
    date = last_run.get("targetDate")
    if (not is_iso_date(date) or last_run.get("status") != "skipped"
            or last_run.get("phase") != "collect" or last_run.get("tradingStatus") != "non_trading_day"
            or last_run.get("publicationGate") != "not_applicable"):
        return None
    context_file = last_run.get("contextFile")
    if (not isinstance(context_file, str) or not MARKET_HOLIDAY_CONTEXT_RE.fullmatch(context_file)
            or ".." in context_file):
        return None
    context_path = project / context_file
    try:
        if context_path.is_symlink():
            return None
        raw = context_path.read_bytes()
        context = json.loads(raw)
    except (OSError, ValueError):
        return None
    if sha256_bytes(raw) != last_run.get("contextSha256"):
        return None
    data = context.get("data") if isinstance(context, dict) else None
    calendar = data.get("tradingCalendar") if isinstance(data, dict) else None
    if (not isinstance(calendar, dict) or calendar.get("targetDate") != date
            or calendar.get("status") != "non_trading_day"):
        return None
    sources = calendar.get("sourceIds")
    if not isinstance(sources, list) or "akshare:sina:trade-calendar" not in sources:
        return None
    previous, following = calendar.get("previousTradingDate"), calendar.get("nextTradingDate")
    if not is_iso_date(previous) or not previous < date:
        return None
    if not is_iso_date(following) or not following > date:
        return None
    return {"calendarDate": date, "latestTradingDate": previous, "nextTradingDate": following,
            "contextSha256": last_run.get("contextSha256")}


def validated_market_holiday(project: Path, exported: dt.datetime):
    """Holiday exemption: only until the next trading day actually starts.

    R04 (2026-09-27): the exemption used to run to the next trading day's 09:01
    deadline, so 2026-09-28 08:20 still said "no new report due". It now ends at
    00:00 of the next trading day: the holiday (and the weekend before it) still
    needs no new report, but the trading day itself is "awaiting" (see
    expected_run_day) and no panel may call it not_required.
    """
    record = holiday_skip_record(project)
    if record is None:
        return None
    following = record["nextTradingDate"]
    assert isinstance(following, str)  # holiday_skip_record validated it
    next_start = dt.datetime.combine(dt.date.fromisoformat(following), dt.time(0), CST)
    if exported.astimezone(CST) >= next_start:
        return None
    return {**record, "status": "not_required", "kind": "validated_market_holiday_v1",
            "calendarSourceIds": ["akshare:sina:trade-calendar"],
            "text": (f"无需新稿，最近有效交易日为 {record['latestTradingDate']}"
                     f"（下一交易日 {record['nextTradingDate']}）")}


def expected_run_day(project: Path, exported: dt.datetime):
    """Expected-report state machine shared by every report panel (R04).

    not_required -> the export is still on a validated market holiday.
    awaiting     -> the next trading day has started; its report is not due yet.
    overdue      -> the same day's deadline passed without a new report.
    The latest available report date stays a separate fact; this record never
    points at the holiday date once the holiday is over.
    """
    holiday = validated_market_holiday(project, exported)
    if holiday:
        return holiday
    record = holiday_skip_record(project)
    if record is None:
        return None
    today = exported.astimezone(CST).date().isoformat()
    if record["nextTradingDate"] != today:
        return None
    expected = dt.datetime.combine(dt.date.fromisoformat(today), dt.time(8), CST)
    deadline = expected + dt.timedelta(minutes=61)
    overdue = exported >= deadline
    return {"status": "overdue" if overdue else "awaiting",
            "kind": "validated_market_holiday_next_trading_day_v1",
            "calendarDate": record["calendarDate"], "expectedDate": today,
            "latestTradingDate": record["latestTradingDate"], "nextTradingDate": today,
            "expectedAt": iso(expected), "deadlineAt": iso(deadline),
            "calendarSourceIds": ["akshare:sina:trade-calendar"],
            "contextSha256": record["contextSha256"],
            "text": (f"本次逾期：{today} 的运行尚未产出（截止 {deadline.strftime('%H:%M')}）" if overdue
                     else f"等待 {today} 本次运行（预计 {expected.strftime('%H:%M')}）")}


# ------------------------------------------------------------- page builders

def complete_report_blocks(markdown):
    """Provide bounded whole plain-text blocks, with an explicit loss contract.

    A preview is a different field. Long paragraphs are split, never silently
    shortened to preview length. Headings, citations and source paragraphs stay
    in this text representation, so the content hash binds what readers get.
    """
    blocks, reasons, total = [], [], 0
    numbered_bullets = []  # (number, text) for \d+. prefixed bullets inside ## 来源
    in_sources_section = False
    for raw in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = CONTROL_RE.sub("", raw).strip()
        if not line or line == "---":
            continue
        heading = HEADING_RE.match(line)
        quote = QUOTE_RE.match(line)
        bullet = BULLET_RE.match(line)
        kind = (f"h{len(heading.group(1))}" if heading else "quote" if quote else
                "bullet" if bullet else "paragraph")
        # Track when we enter/exit the ## 来源 section
        if heading and heading.group(2).strip() == "来源":
            in_sources_section = True
        elif heading:
            in_sources_section = False
        # Capture numbered prefix before BULLET_RE strips it, plus URL extraction
        if in_sources_section and bullet and bullet.group(1).rstrip(".").isdigit():
            num = int(bullet.group(1).rstrip("."))
            btext = bullet.group(2)
            burl_match = re.search(r"https?://\S+", btext)
            burl = burl_match.group(0) if burl_match else None
            numbered_bullets.append((num, btext, burl))
        text = heading.group(2) if heading else quote.group(1) if quote else bullet.group(2) if bullet else line
        # Keep link destinations as plain text; no Markdown or HTML is executed.
        text = MD_LINK_RE.sub(
            lambda m: m.group(1) if m.group(2).lower().startswith(("javascript:", "data:"))
            else f"{m.group(1)} ({m.group(2)})", text)
        text = STRONG_RE.sub(r"\1", text)
        total += len(text)
        for offset in range(0, len(text), MAX_BLOCK_CHARS):
            part = text[offset:offset + MAX_BLOCK_CHARS]
            provided = sum(len(block["text"]) for block in blocks)
            if len(blocks) >= MAX_BLOCKS:
                if "block_limit" not in reasons:
                    reasons.append("block_limit")
                break
            if provided + len(part) > MAX_REPORT_TEXT_CHARS:
                if "text_limit" not in reasons:
                    reasons.append("text_limit")
                break
            if sum(len(block["text"].encode("utf-8")) for block in blocks) + len(part.encode("utf-8")) > MAX_REPORT_TEXT_BYTES:
                if "body_byte_limit" not in reasons:
                    reasons.append("body_byte_limit")
                break
            offered = [{"heading": "", "text": block["text"]} for block in blocks] + [{"heading": "", "text": part}]
            if len(json.dumps(offered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_REPORT_BODY_WIRE_BYTES:
                if "body_wire_limit" not in reasons:
                    reasons.append("body_wire_limit")
                break
            blocks.append({"kind": kind, "text": part})
    # Extract source references from numbered bullets captured during parsing
    sources = [{"id": num, "text": text, "url": url} for num, text, url in numbered_bullets]
    return blocks, sources, {"complete": not reasons, "reasons": reasons,
                        "blockCount": len(blocks), "textChars": total}

def list_report_dates(project: Path):
    try:
        return sorted((path.stem for path in (project / "reports" / "daily").glob("*.md")), reverse=True)
    except OSError:
        return []


def report_page(project: Path, date: str, exported: dt.datetime, last_run: dict):
    path = project / "reports" / "daily" / f"{date}.md"
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REPORT_BYTES:
            return None, ["report_source_limit_or_type"], None
        raw = b""
        while len(raw) <= MAX_REPORT_BYTES:
            chunk = os.read(fd, min(65536, MAX_REPORT_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(fd)
        if (len(raw) > MAX_REPORT_BYTES or before.st_size != len(raw)
                or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            return None, ["report_source_changed_or_limit"], None
        body = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None, ["report_unreadable"], None
    finally:
        if fd is not None:
            os.close(fd)
    blocks, sources, completeness = complete_report_blocks(body)
    cutoff_local = None
    for line in body.split("\n")[:12]:
        cutoff_match = re.match(r"^>\s*数据截止：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", line.strip())
        if cutoff_match:
            cutoff_local = cutoff_match.group(1)
            break
    run_for_date = last_run if last_run.get("targetDate") == date else {}
    page = {
        "targetDate": date,
        "cutoffLocal": cutoff_local,
        "dataStatus": run_for_date.get("dataStatus"),
        "publicationGate": run_for_date.get("publicationGate"),
        "publicationLevel": clean_text(run_for_date.get("publicationLevel"), 200) or None,
        "variant": run_for_date.get("reportVariant"),
        "sha256": sha256_bytes(raw),
        "sourceReportSha256": sha256_bytes(raw),
        "contentSha256": sha256_bytes(json.dumps(
            [{"heading": "", "text": block["text"]} for block in blocks],
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "contentContract": REPORT_TEXT_CONTRACT,
        "bodyComplete": completeness["complete"],
        "truncationReasons": completeness["reasons"],
        "bodyBlockCount": completeness["blockCount"],
        "bodyTextChars": completeness["textChars"],
        "preview": clean_text(" ".join(block["text"] for block in blocks), MAX_TEXT_CHARS),
        "generatedAt": iso(parsed) if (parsed := parse_instant(run_for_date.get("generatedAt")))
            is not None and parsed.tzinfo is not None else None,
        "dataCutoff": run_for_date.get("dataDate") if is_iso_date(run_for_date.get("dataDate")) else None,
        "conclusion": [{"text": clean_text(block["text"], 500)}
                       for block in section_blocks(blocks, "一句话结论")[:16]],
        "risks": [{"text": clean_text(block["text"], 500)}
                  for block in section_blocks(blocks, "风险提示")[:16]],
        "blocks": blocks,
        "sources": sources,
    }
    source_time = parse_instant(f"{cutoff_local}:00+08:00") if cutoff_local else None
    return page, ([] if cutoff_local else ["report_cutoff_missing"]), source_time


def build_today(root: Path, exported: dt.datetime, history: int):
    project = root / "AStock_AI_System"
    errors = []
    dates = list_report_dates(project)
    if not dates:
        errors.append("reports_directory_empty")
    panel = {"schemaVersion": SCHEMA_VERSION, "page": "today", "exportedAt": iso(exported),
             "readErrors": errors, "report": None, "focus": [],
             "availableDates": dates[:history]}
    source_time = None
    last_run = read_json(project / "work" / "last-run.json")
    if not isinstance(last_run, dict):
        errors.append("last_run_unreadable")
        last_run = {}
    if dates:
        page, page_errors, source_time = report_page(project, dates[0], exported, last_run)
        errors.extend(page_errors)
        panel["report"] = page
    panel.update(report_freshness(project, dates[0] if dates else None,
                                  exported, source_time, "today"))
    expected = expected_run_day(project, exported)
    if expected and expected["status"] != "not_required":
        panel["expectedReport"] = expected
    else:
        panel["expectedReport"] = (expected if expected and dates
                                   and expected["latestTradingDate"] == dates[0] else None)
    return panel


def split_retained_issues(issues):
    """Match original scope and explicit addenda; never accept other dates."""
    baseline = {code: set() for code in RETAINED_ISSUES}
    addenda = []
    current = []
    for issue in issues:
        code = issue["code"]
        if code not in RETAINED_ISSUES:
            current.append(dict(issue))
            continue
        severity, dates = RETAINED_ISSUES[code]
        observed = set(issue["dates"])
        matched_baseline = observed & dates if issue["severity"] == severity else set()
        baseline[code].update(matched_baseline)
        matched_addenda = []
        for addendum in RETAINED_ADDENDA:
            if (addendum["code"] == code
                    and addendum["aggregateIssueSeverity"] == issue["severity"]
                    and addendum["date"] in observed):
                matched_addenda.append(addendum)
        addenda.extend(matched_addenda)
        unclassified = observed - matched_baseline - {item["date"] for item in matched_addenda}
        if unclassified or not observed:
            current.append({**issue, "dates": sorted(unclassified),
                            "classification": "not_in_retained_baseline"})
    return ({code: sorted(dates) for code, dates in baseline.items()},
            sorted(addenda, key=lambda item: (item["date"], item["code"])), current)


def build_focus(root: Path, exported: dt.datetime, today_panel: dict, delivery_panel: dict,
                health, progress_panel=None):
    """Quote dated evidence, never promote old report wording into current verification."""
    focus = []
    report = today_panel.get("report") or {}
    report_date = report.get("targetDate")
    seal = (delivery_panel.get("layers") or {}).get("seal") or {}
    recomputed = (seal.get("evidence") or {}).get("recomputed") or {}
    seal_verified = (seal.get("status") == "pass"
                     and delivery_panel.get("targetDate") == report_date
                     and bool(report.get("sha256"))
                     and recomputed.get("report") == report.get("sha256"))
    for block in report.get("risks") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or ""
        # Only explicit data-limit statements belong here. Other narrative is
        # retained in the unchanged full report, not promoted to verified gaps.
        if (not text or re.search(r"\[(推断|分析|建议)\]|可能|预计|建议", text)
                or not re.search(r"缺失|不足|未核|尚未|不可用|不提供|缺口|不适用|未完成|未与.*核对", text)):
            continue
        focus.append({"kind": "report_limitation", "text": text,
                      "refs": block.get("refs") or [],
                      "source": f"report:{report_date}" if report_date else "report",
                      "reportDate": report_date, "sourceTime": today_panel.get("sourceTime"),
                      "reportSha256": report.get("sha256"), "sealVerified": seal_verified})
    for layer, label in (("seal", "本地密封"), ("telegram", "Telegram"), ("relay", "接收端 relay")):
        state = (delivery_panel.get("layers") or {}).get(layer, {})
        if state.get("status") in ("fail", "insufficient"):
            reason = "；".join(state.get("limitations") or []) or "证据不足"
            focus.append({"kind": "delivery", "text": f"{label}：{reason}", "source": "dashboard",
                          "layer": layer, "status": state.get("status"),
                          "targetDate": delivery_panel.get("targetDate"),
                          "sourceTime": delivery_panel.get("sourceTime"), "checkedAt": iso(exported),
                          "sourceFile": "delivery.json"})
    progress = progress_panel or {}
    gaps = progress.get("historicalGaps") or {}
    baseline = gaps.get("baseline") or {}
    for key, label in (("criticalDates", "原保留基线登记的缺口日"), ("legacyDates", "原保留基线登记的遗留问题日")):
        dates = baseline.get(key) or []
        if dates:
            focus.append({"kind": "historical_gap", "source": "dashboard", "dates": dates,
                          "scope": "original_baseline",
                          "auditTime": progress.get("sourceTime"), "sourceFile": "progress.json",
                          "text": f"{label}（历史来源记录；当前状态以最新审计为准）：" + "、".join(dates)})
    for addendum in gaps.get("addenda") or []:
        focus.append({"kind": "historical_gap", "source": "dashboard",
                      "scope": "later_addendum", "dates": [addendum["date"]],
                      "firstObservedAt": addendum["firstObservedAt"],
                      "auditTime": progress.get("sourceTime"), "sourceFile": "progress.json",
                      "text": f"后续登记的缺口日（历史来源记录）：{addendum['date']}；"
                              f"首次见于审计 {addendum['firstObservedAt']}（日志重建）。"})
    for issue in progress.get("currentIssues") or []:
        if issue.get("classification") == "not_in_retained_baseline":
            focus.append({"kind": "audit_unconfirmed", "source": "dashboard",
                          "sourceFile": "progress.json", "auditTime": progress.get("sourceTime"),
                          "issueCode": issue["code"], "issueSeverity": issue["severity"],
                          "issueDates": issue["dates"],
                          "text": (f"审计记录不在已确认保留范围：{issue['code']} / {issue['severity']}；"
                                   + ("、".join(issue["dates"]) or "未提供日期") + "。新增或变化原因待核。")})
    if not isinstance(progress.get("health"), dict):
        focus.append({"kind": "audit_unconfirmed", "source": "dashboard",
                      "sourceFile": "progress.json", "text": "健康审计不可用：历史缺口与当前问题无法确认。"})
    elif "currentIssues" in progress and not isinstance(progress.get("currentIssues"), list):
        focus.append({"kind": "audit_unconfirmed", "source": "dashboard",
                      "sourceFile": "progress.json", "text": "健康审计的问题列表不可读：当前缺口日期与数量无法确认。"})
    return focus


def build_coverage(health: dict, snapshot: dict, issues: list, issues_available: bool = True):
    """Pass through the audit's own coverage facts; never invent a ratio.

    Two rules matter here:

    * A row hash proves a record exists, not that the audit considers it valid.
      The audit writes `snapshotState` per date in its ledger (for example
      `invalid` with `reason=non_finite_json`), so validity is taken from that
      state. A hash without a ledger verdict is reported as `recorded` and is
      never counted as valid.
    * When the audit ships no ledger at all, the number of valid dates is
      unknown, not zero: `validSnapshotDates` stays null and the page draws no
      bar.
    """
    raw_ledger = health.get("ledger")
    ledger = raw_ledger if isinstance(raw_ledger, list) else []
    raw_stages = health.get("stages")
    # 缺失与空集合不同：没有这个字段＝未知（None），明确给了空字典＝真的 0。
    stages_present = isinstance(raw_stages, dict)
    stages = raw_stages if stages_present else {}
    raw_rows = snapshot.get("rowHashes")
    row_hashes_present = isinstance(raw_rows, dict)
    row_hashes = raw_rows if row_hashes_present else {}
    raw_qlib = health.get("qlib")
    qlib = raw_qlib if isinstance(raw_qlib, dict) else {}
    raw_incremental = qlib.get("incremental")
    incremental = raw_incremental if isinstance(raw_incremental, dict) else {}
    raw_daily = incremental.get("dailyCoverage")
    # dailyCoverage follows calendarAdded order. A recovery import can add
    # several days at once, so the newest day is the last row, not the first.
    dated = ([row for row in raw_daily if isinstance(row, dict)
              and is_iso_date(row.get("target_date"))]
             if isinstance(raw_daily, list) else [])
    daily = (max(dated, key=lambda row: row["target_date"]) if dated
             else raw_daily[0] if isinstance(raw_daily, list) and raw_daily
             and isinstance(raw_daily[0], dict) else {})
    gap_codes = {issue["code"]: issue["dates"] for issue in issues
                 if isinstance(issue, dict) and issue.get("code")
                 and isinstance(issue.get("dates"), list)}
    critical = {str(value) for value in gap_codes.get("unresolved_trading_day_gaps", [])}
    legacy = {str(value) for value in gap_codes.get("legacy_gaps_or_invalid_rows", [])}
    ledger_by_date = {str(item.get("tradeDate")): item for item in ledger
                      if isinstance(item, dict) and item.get("tradeDate")}
    known = set(row_hashes) | set(stages) | set(ledger_by_date) | critical | legacy
    dates_enumerated = bool(ledger_by_date) or stages_present or row_hashes_present \
        or bool(critical) or bool(legacy)
    verdicts = ("valid", "invalid", "missing")
    dates = []
    for date in sorted(known):
        item = ledger_by_date.get(date, {})
        staged = stages.get(date) if isinstance(stages.get(date), dict) else {}
        recorded = date in row_hashes
        verdict = str(item.get("snapshotState") or "")
        if verdict in verdicts:
            snapshot_state = verdict
        elif recorded:
            snapshot_state = "recorded"      # 记录存在，审计未判有效性
        else:
            snapshot_state = "unknown"
        dates.append({
            "date": date,
            "contract": FULL_CONTRACT, "fullSnapshot": True,
            "expectedUnderContract": item.get("expectedUnderContract") is not False,
            "snapshot": snapshot_state,
            "snapshotRecorded": (recorded if row_hashes_present else None),
            "snapshotReason": (str(item.get("reason")) if item.get("reason") else None),
            "stage": ("valid" if isinstance(staged, dict) and staged.get("state") == "valid"
                      else str(item.get("stageState") or "unknown")),
            "classification": str(item.get("classification") or "unclassified"),
            # 逐日证据：真实观察时间与哈希（缺失就是缺失，不补造）
            "stageObservedAt": (str(staged.get("observedAt")) if staged.get("observedAt") else None),
            "payloadSha256": (str(item.get("payloadSha256")) if item.get("payloadSha256")
                              else (str(row_hashes.get(date)) if date in row_hashes else None)),
            "recoveryReadiness": (str(item.get("recoveryReadiness")) if item.get("recoveryReadiness") else None),
            "gap": ("critical" if date in critical else "legacy" if date in legacy else None)})
    coverage_ratio = daily.get("coverage")
    ratio = (coverage_ratio if isinstance(coverage_ratio, (int, float))
             and not isinstance(coverage_ratio, bool) else None)
    has_ledger = bool(ledger_by_date)
    expected_dates = [item for item in dates if item["expectedUnderContract"]]
    return {"auditSince": health.get("auditSince"), "monitorSince": health.get("monitorSince"),
            "referenceCompleteDate": health.get("referenceCompleteDate"),
            "knownDates": len(expected_dates) if dates_enumerated else None,
            "observedDates": len(known) if dates_enumerated else None,
            "snapshotValiditySource": "audit_ledger" if has_ledger else None,
            "validSnapshotDates": (sum(1 for item in expected_dates if item["snapshot"] == "valid")
                                   if has_ledger else None),
            "recordedSnapshotDates": len(row_hashes) if row_hashes_present else None,
            "stagedDates": (sum(1 for value in stages.values()
                                if isinstance(value, dict) and value.get("state") == "valid")
                            if stages_present else None),
            "criticalDates": len(critical) if issues_available else None,
            "legacyDates": len(legacy) if issues_available else None,
            "qlibCoverage": None if ratio is None else {
                "ratio": ratio, "targetDate": daily.get("target_date"),
                "eligible": daily.get("eligible"), "valid": daily.get("valid"),
                "status": daily.get("status")},
            "dates": dates}


def legacy_issue_evidence(item, errors):
    """Whitelist audit evidence, without inferring missing dates or querying data.

    Target, baseline and last physical quote are different dates. The producer
    binds them to one validated manifest; the exporter only transports them.
    Legacy audits with no fields stay unknown, never borrow the audit clock.
    """
    result = {}
    for key in ("targetDate", "legacyUnquotedBaseDate"):
        value = item.get(key)
        if is_iso_date(value):
            result[key] = value
        elif value is not None:
            errors.append("legacy_issue_invalid_" + key)
    symbols = item.get("symbols")
    allowed = {symbol for symbol in symbols if isinstance(symbol, str)
               and re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", symbol)} if isinstance(symbols, list) else set()
    quotes = item.get("lastQuotedDates")
    if isinstance(quotes, dict):
        result["lastQuotedDates"] = {symbol: day for symbol, day in quotes.items()
                                     if symbol in allowed and is_iso_date(day)}
        if len(result["lastQuotedDates"]) != len(quotes):
            errors.append("legacy_issue_invalid_lastQuotedDates")
    elif quotes is not None:
        errors.append("legacy_issue_invalid_lastQuotedDates")
    for key in ("meaning", "validationScope", "escalation"):
        value = item.get(key)
        if value is None or (isinstance(value, str) and len(value) <= MAX_TEXT_CHARS
                             and not CONTROL_RE.search(value)):
            if key in item:
                result[key] = value
        else:
            errors.append("legacy_issue_invalid_" + key)
    if type(item.get("proven")) is bool:
        result["proven"] = item["proven"]
    manifest_sha = item.get("sourceManifestSha256")
    if isinstance(manifest_sha, str) and HASH_RE.fullmatch(manifest_sha):
        result["sourceManifestSha256"] = manifest_sha
    elif manifest_sha is not None:
        errors.append("legacy_issue_invalid_sourceManifestSha256")
    target, base = result.get("targetDate"), result.get("legacyUnquotedBaseDate")
    date_sets = {}
    for key in ("dates", "affectedDates"):
        raw = item.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list) or any(not is_iso_date(day) for day in raw):
            errors.append("legacy_issue_invalid_" + key)
            result["dates"] = []
        else:
            date_sets[key] = sorted(set(raw))
    dates = date_sets.get("dates", date_sets.get("affectedDates"))
    if "dates" not in result and dates is not None:
        result["dates"] = dates
    if ((target and base and base >= target)
            or (base and any(day >= base for day in result.get("lastQuotedDates", {}).values()))
            or (len(date_sets) == 2 and date_sets["dates"] != date_sets["affectedDates"])
            or (target and dates and max(dates) != target)
            or (base and dates and min(dates) <= base)):
        result["status"] = "fail"
        errors.append("legacy_issue_date_binding_conflict")
    return result


SNAPSHOT_MARKET_ENDPOINTS = frozenset({
    "stock_board_industry_summary_ths", "stock_market_activity_legu",
})
SNAPSHOT_FAILURE_TYPES = frozenset({
    "ValueError", "RuntimeError", "TypeError", "KeyError", "IndexError",
    "OSError", "ConnectionError", "TimeoutError", "HTTPError", "SSLError",
    "JSONDecodeError", "ParserError", "OtherError",
})
SNAPSHOT_RUN_STATUSES = frozenset({
    "deferred", "rejected", "failed", "temporary_failure", "usage_error",
    "inserted", "skipped_existing", "idempotent", "success",
})


def snapshot_failure_evidence(value):
    """Bounded audit pass-through; raw upstream error text is never exported."""
    if not isinstance(value, dict) or value.get("provider") != "akshare":
        return None
    count, attempts = value.get("attemptCount"), value.get("attempts")
    if (type(count) is not int or not 1 <= count <= 8 or not isinstance(attempts, list)
            or len(attempts) != count):
        return None
    clean = []
    for number, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict) or type(attempt.get("attempt")) is not int or attempt["attempt"] != number:
            return None
        endpoint, error_type = attempt.get("endpoint"), attempt.get("errorType")
        if endpoint is not None and (not isinstance(endpoint, str)
                                     or endpoint not in SNAPSHOT_MARKET_ENDPOINTS):
            return None
        if not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
            return None
        row = {"attempt": number,
               "errorType": error_type if error_type in SNAPSHOT_FAILURE_TYPES else "OtherError"}
        if endpoint is not None:
            row["endpoint"] = endpoint
        clean.append(row)
    return {"provider": "akshare", "attemptCount": count, "attempts": clean}


FULL_CONTRACT = "research.full-snapshot.v1"
CORE_CONTRACT = "research.core-market.v1"
CORE_ISSUES = frozenset({
    "unresolved_core_market_gaps", "scheduled_core_market_missing",
    "last_core_market_not_successful", "core_market_run_missing_or_mismatched",
    "core_market_evidence_unavailable", "core_market_material_warning",
    "legacy_full_rows_after_cutover", "core_market_outside_known_trading_calendar",
    "core_calendar_schedule_conflict",
})


def contract_run(value):
    """Whitelisted run identity only; never publish raw error/source payloads."""
    value = value if isinstance(value, dict) else {}
    result = {"status": value.get("status") if isinstance(value.get("status"), str) and value.get("status") in
              SNAPSHOT_RUN_STATUSES | {"unknown", "not_configured", "skipped_non_trading"} else None,
              "targetDate": value.get("target") if is_iso_date(value.get("target")) else None}
    stamp = parse_instant(value.get("timestamp"))
    result["timestamp"] = stamp.isoformat() if stamp and stamp.tzinfo else None
    started = parse_instant(value.get("startedAt"))
    result["startedAt"] = started.isoformat() if started and started.tzinfo else None
    result["payloadSha256"] = (value.get("payloadSha256")
        if isinstance(value.get("payloadSha256"), str) and HASH_RE.fullmatch(value["payloadSha256"]) else None)
    run_id = value.get("runId")
    result["runId"] = run_id if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id) else None
    for key in ("stageTradeDate", "usedStageTradeDate"):
        result[key] = value.get(key) if is_iso_date(value.get(key)) else None
    for key in ("stagePayloadSha256", "usedStagePayloadSha256"):
        result[key] = value.get(key) if isinstance(value.get(key), str) and HASH_RE.fullmatch(value[key]) else None
    result["stageUnavailable"] = value.get("stageUnavailable") if type(value.get("stageUnavailable")) is bool else None
    result["calendarClosedVerified"] = value.get("calendarClosedVerified") is True
    for key in ("calendarTargetDate", "previousTradingDate"):
        result[key] = value.get(key) if is_iso_date(value.get(key)) else None
    result["calendarEvidenceSha256"] = (value.get("calendarEvidenceSha256")
        if isinstance(value.get("calendarEvidenceSha256"), str) and HASH_RE.fullmatch(value["calendarEvidenceSha256"]) else None)
    return result


def audited_run_decision(health, errors):
    """Project the audit's verdict; transport checks do not repeat its business gates."""
    value = health.get("activeRunDecision")
    try:
        if not isinstance(value, dict) or value.get("schema") != "research.active-run-decision.v1":
            raise ValueError("schema")
        contract = value["contract"]
        core = contract == CORE_CONTRACT
        if (contract not in {FULL_CONTRACT, CORE_CONTRACT} or contract != health.get("activeContract")
                or value.get("scope") != ("core_market" if core else "full_snapshot")
                or value.get("fullSnapshot") is not (not core)
                or value.get("artifactKind") != ("qlib_market_summary" if core else "full_snapshot")):
            raise ValueError("contract identity")
        verified, run, artifact = value["verification"], value["run"], value["artifact"]
        if not all(isinstance(x, dict) for x in (verified, run, artifact)):
            raise ValueError("object fields")
        if (verified.get("status") not in {"pass", "fail", "insufficient", "pending", "not_required"}
                or verified.get("scope") != "local_run_artifact_calendar_identity"):
            raise ValueError("status")
        stamp = parse_instant(value.get("evaluatedAt"))
        audit_stamp = parse_instant(health.get("generatedAt"))
        if not stamp or not stamp.tzinfo or stamp != audit_stamp or value.get("scheduledDate") != stamp.astimezone(CST).date().isoformat():
            raise ValueError("audit identity")
        times = {key: parse_instant(value.get(key)) for key in ("scheduledFor", "deadlineAt")}
        if (any(not x or not x.tzinfo for x in times.values()) or times["scheduledFor"] >= times["deadlineAt"]
                or any(x.astimezone(CST).date().isoformat() != value["scheduledDate"] for x in times.values())):
            raise ValueError("schedule")
        codes = {}
        for key in ("reasons", "currentIssueCodes", "materialWarningCodes", "historicalIssueCodes"):
            entries = verified.get(key)
            if not isinstance(entries, list) or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,150}", x) for x in entries):
                raise ValueError("reason codes")
            codes[key] = entries
        cleaned_run = {"runId": run.get("runId"), "status": run.get("status"),
                       "targetDate": run.get("targetDate"), "startedAt": run.get("startedAt"),
                       "completedAt": run.get("completedAt")}
        if cleaned_run["runId"] is not None and (not isinstance(cleaned_run["runId"], str) or not re.fullmatch(r"[0-9a-f]{32}", cleaned_run["runId"])):
            raise ValueError("run id")
        if not isinstance(cleaned_run["status"], str) or not re.fullmatch(r"[a-z_]{1,60}", cleaned_run["status"]):
            raise ValueError("run status")
        for key in ("targetDate",):
            if cleaned_run[key] is not None and not is_iso_date(cleaned_run[key]): raise ValueError("run date")
        for key in ("startedAt", "completedAt"):
            if cleaned_run[key] is not None and (not parse_instant(cleaned_run[key]) or not parse_instant(cleaned_run[key]).tzinfo):
                raise ValueError("run timestamp")
        if value.get("expectedTargetDate") is not None and not is_iso_date(value["expectedTargetDate"]):
            raise ValueError("expected target")
        payload = artifact.get("payloadSha256")
        if payload is not None and (not isinstance(payload, str) or not HASH_RE.fullmatch(payload)):
            raise ValueError("artifact hash")
        last = health.get("activeLastRun") or {}
        for source_key, target_key in (("runId", "runId"), ("status", "status"), ("target", "targetDate")):
            if last.get(source_key) != cleaned_run[target_key]:
                raise ValueError("run binding")
        for source_key, target_key in (("timestamp", "completedAt"), ("startedAt", "startedAt")):
            if parse_instant(last.get(source_key)) != parse_instant(cleaned_run[target_key]):
                raise ValueError("run time binding")
        if last.get("payloadSha256") != payload:
            raise ValueError("artifact binding")
        quality = artifact.get("quality")
        safe_quality = None
        if quality is not None and (not core or not isinstance(quality, dict)):
            raise ValueError("quality scope")
        if isinstance(quality, dict):
            safe_quality = {key: quality.get(key) for key in ("scope", "targetDate", "payloadSha256", "priceStatus", "valid", "invalid", "eligible", "ratio", "generationTag")}
            if (any(type(safe_quality[k]) is not int or safe_quality[k] < 0 for k in ("valid", "invalid", "eligible"))
                    or type(safe_quality["ratio"]) not in (int, float) or not math.isfinite(safe_quality["ratio"])
                    or safe_quality["scope"] != "sealed_core_qlib_summary"
                    or safe_quality["priceStatus"] not in {"partial", "complete"}
                    or not isinstance(safe_quality["generationTag"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", safe_quality["generationTag"])
                    or safe_quality["targetDate"] != cleaned_run["targetDate"]
                    or safe_quality["payloadSha256"] != payload
                    or not is_iso_date(safe_quality["targetDate"]) or not isinstance(safe_quality["payloadSha256"], str)
                    or not HASH_RE.fullmatch(safe_quality["payloadSha256"])):
                raise ValueError("quality schema")
        row_state = artifact.get("rowState")
        if row_state not in {"valid", "invalid", "missing"}:
            raise ValueError("row state")
        calendar = value.get("calendar")
        safe_calendar = None
        if calendar is not None:
            if not isinstance(calendar, dict): raise ValueError("calendar schema")
            safe_calendar = {key: calendar.get(key) for key in ("status", "targetDate", "previousTradingDate", "nextTradingDate", "evidenceSha256", "sourceEvidenceSha256", "sourceDate", "observedAt", "proofKind")}
            if (safe_calendar["status"] not in {"open", "closed"}
                    or safe_calendar["targetDate"] != value["scheduledDate"]
                    or any(not is_iso_date(safe_calendar[k]) for k in ("targetDate", "previousTradingDate", "nextTradingDate", "sourceDate"))
                    or any(not isinstance(safe_calendar[k], str) or not HASH_RE.fullmatch(safe_calendar[k]) for k in ("evidenceSha256", "sourceEvidenceSha256"))
                    or not parse_instant(safe_calendar["observedAt"]) or not parse_instant(safe_calendar["observedAt"]).tzinfo
                    or safe_calendar["proofKind"] not in {"sealed_archive", "validated_terminal_context", "validated_non_trading_skip", "validated_closed_interval"}):
                raise ValueError("calendar identity")
        return {"schema": value["schema"], "contract": contract, "scope": value["scope"],
                "fullSnapshot": not core, "artifactKind": value["artifactKind"],
                "scheduledDate": value["scheduledDate"], "scheduledFor": times["scheduledFor"].isoformat(),
                "deadlineAt": times["deadlineAt"].isoformat(), "evaluatedAt": stamp.isoformat(),
                "expectedTargetDate": value.get("expectedTargetDate"), "run": cleaned_run,
                "verification": {"status": verified["status"], "scope": "local_run_artifact_calendar_identity", **codes},
                "artifact": {"payloadSha256": payload, "rowState": row_state, "quality": safe_quality},
                "calendar": safe_calendar}
    except (KeyError, ValueError, TypeError, AttributeError):
        errors.append("active_run_decision_invalid")
        return None


def audited_contract_evidence(health, errors):
    decision = audited_run_decision(health, errors)
    declared = health.get("activeContract")
    active = declared if isinstance(declared, str) and declared in {FULL_CONTRACT, CORE_CONTRACT} else None
    effective = health.get("effectiveTradeDate") if is_iso_date(health.get("effectiveTradeDate")) else None
    legacy = health.get("legacyFullSnapshot") if isinstance(health.get("legacyFullSnapshot"), dict) else {}
    result = {"activeContract": active, "contractSource": "audit_decision",
              "effectiveTradeDate": effective,
              "activeLastRun": contract_run(health.get("activeLastRun")),
              "activeRunDecision": decision, "legacyFullSnapshot": {"contract": FULL_CONTRACT,
                  "status": "historical_only" if active == CORE_CONTRACT else "active",
                  "scheduledBefore": legacy.get("scheduledBefore") if is_iso_date(legacy.get("scheduledBefore")) else None}, "coreMarket": None}
    if "intervals" in legacy:
        intervals = legacy["intervals"]
        if (not isinstance(intervals, list) or len(intervals) > 17 or any(not isinstance(row, dict)
                or any(key not in row or row[key] is not None and not is_iso_date(row[key])
                       for key in ("fromInclusive", "untilExclusive")) for row in intervals)):
            errors.append("full_contract_intervals_projection_invalid")
            result["legacyFullSnapshot"]["intervals"] = None
        else:
            result["legacyFullSnapshot"]["intervals"] = [{key: row[key] for key in
                ("fromInclusive", "untilExclusive")} for row in intervals]
    # Project the auditor's interval evidence, never select a contract here.
    intervals = health.get("contractIntervals")
    if intervals is not None:
        if not isinstance(intervals, list) or len(intervals) > 16 or any(
                not isinstance(row, dict) or not isinstance(row.get("activeContract"), str)
                or row["activeContract"] not in {CORE_CONTRACT, FULL_CONTRACT}
                or "untilExclusive" not in row
                or not is_iso_date(row.get("fromInclusive"))
                or row.get("untilExclusive") is not None and not is_iso_date(row.get("untilExclusive"))
                for row in intervals):
            errors.append("contract_intervals_projection_invalid")
            result["contractIntervals"] = None
        else:
            result["contractIntervals"] = [{key: row.get(key) for key in
                ("activeContract", "fromInclusive", "untilExclusive")} for row in intervals]
    raw = health.get("coreMarket") if isinstance(health.get("coreMarket"), dict) else {}
    historical = active == FULL_CONTRACT and effective is not None
    if historical:
        # A core contract whose every start is after the audit day has not run
        # yet: it is scheduled, not history. Unknown audit time keeps old behaviour.
        stamp = parse_instant(health.get("generatedAt"))
        as_of = stamp.astimezone(CST).date().isoformat() if stamp and stamp.tzinfo else None
        rows = result.get("contractIntervals")
        starts = ([row["fromInclusive"] for row in rows if row["activeContract"] == CORE_CONTRACT]
                  if rows is not None else [effective])
        if as_of is not None and starts and all(start > as_of for start in starts):
            result["coreContractSchedule"] = {"contract": CORE_CONTRACT, "fromInclusive": min(starts),
                                              "status": "not_started"}
            return result
    if active != CORE_CONTRACT and not historical:
        return result
    raw_ledger = raw.get("ledger") if isinstance(raw.get("ledger"), list) else []
    ledger = [{"date": row["tradeDate"], "state": row["coreState"], "pendingWindow": row["pendingWindow"],
               "payloadSha256": row.get("payloadSha256"), "contract": CORE_CONTRACT, "fullSnapshot": False}
              for row in raw_ledger if isinstance(row, dict) and is_iso_date(row.get("tradeDate"))
              and isinstance(row.get("coreState"), str) and row["coreState"] in {"valid", "invalid", "missing"}
              and type(row.get("pendingWindow")) is bool
              and (row.get("payloadSha256") is None or isinstance(row["payloadSha256"], str) and HASH_RE.fullmatch(row["payloadSha256"]))]
    ledger_ok = isinstance(raw.get("ledger"), list) and len(ledger) == len(raw_ledger)
    if not ledger_ok:
        errors.append("core_ledger_projection_invalid")
    due = [row for row in ledger if row["pendingWindow"] is False]
    result["contractPolicy"] = {"activeContract": active, "effectiveTradeDate": result["effectiveTradeDate"]}
    # The current full-run decision cannot certify a historical core run.
    core_decision = decision if active == CORE_CONTRACT else None
    result["coreMarket"] = {"contract": CORE_CONTRACT, "fullSnapshot": False, "artifactKind": "qlib_market_summary",
        "includesCompletePriceArchive": False, "historicalOnly": historical,
        "readState": raw.get("readState") if isinstance(raw.get("readState"), str) and raw["readState"] in {"checked", "missing", "invalid", "not_configured"} else None,
        "status": core_decision["verification"]["status"] if core_decision else "insufficient",
        "expectedFrom": result["effectiveTradeDate"], "rowCount": raw.get("rowCount") if type(raw.get("rowCount")) is int and raw["rowCount"] >= 0 else None,
        "latestValidDate": raw.get("latestValidDate") if is_iso_date(raw.get("latestValidDate")) else None,
        "lastRun": contract_run(raw.get("lastRun")) if historical else result["activeLastRun"],
        "quality": core_decision["artifact"]["quality"] if core_decision else None,
        "knownDates": len(due) if ledger_ok else None,
        "validDates": sum(row["state"] == "valid" for row in due) if ledger_ok else None,
        "ledger": ledger, "evidenceProblems": (["historical_core_not_current_run"] if historical
            else [] if core_decision else ["active_run_decision_invalid"]),
        "blockingReasons": core_decision["verification"]["reasons"] if core_decision else []}
    return result


def legacy_full_contract_verified(health):
    """A schema-less payload must prove its own full-audit identity.

    data_health writes a read-only unknown record (reason/errorType only) when
    evidence cannot be read. That record, or any other partial object, must
    never be promoted to the original full contract by default.
    """
    if health.get("schemaVersion") != 1 or health.get("readOnly") is not True:
        return False
    if health.get("status") not in {"ok", "warning", "critical"}:
        return False
    stamp = parse_instant(health.get("generatedAt"))
    if stamp is None or stamp.tzinfo is None:
        return False
    snapshot = health.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    count = snapshot.get("rowCount")
    if type(count) is not int or count < 0:
        return False
    latest = snapshot.get("latestValidDate")
    return latest is None or is_iso_date(latest)


def contract_evidence(health, errors):
    """Full-snapshot history and core-market observations must never be merged."""
    if "activeRunDecision" in health:
        return audited_contract_evidence(health, errors)
    declared = health.get("activeContract")
    # Older audit schemas are explicitly interpreted as the original full
    # contract only when their own fields establish a full audit. Merely
    # encountering coreMarket never activates it, and an unknown/error record
    # stays unknown instead of claiming a legacy snapshot.
    if declared is None:
        active = FULL_CONTRACT if legacy_full_contract_verified(health) else None
        source = "legacy_schema" if active else "unknown"
    elif isinstance(declared, str) and declared in {FULL_CONTRACT, CORE_CONTRACT}:
        active, source = declared, "audit"
    else:
        active, source = None, "audit"
    if active is None:
        errors.append("active_contract_invalid" if declared is not None else "legacy_full_contract_unverified")
    effective = health.get("effectiveTradeDate") if is_iso_date(health.get("effectiveTradeDate")) else None
    legacy = health.get("legacyFullSnapshot")
    legacy = legacy if isinstance(legacy, dict) else {}
    result = {"activeContract": active, "contractSource": source,
              "effectiveTradeDate": effective, "activeLastRun": contract_run(health.get("activeLastRun")),
              "legacyFullSnapshot": {"contract": FULL_CONTRACT,
                  "status": "historical_only" if active == CORE_CONTRACT else "active" if active == FULL_CONTRACT else "unknown",
                  "scheduledBefore": effective}, "coreMarket": None}
    if active != CORE_CONTRACT:
        return result
    problems = []
    policy = health.get("contractPolicy")
    if (not isinstance(policy, dict) or set(policy) != {"activeContract", "effectiveTradeDate"}
            or policy.get("activeContract") != CORE_CONTRACT or policy.get("effectiveTradeDate") != effective):
        problems.append("core_contract_policy_missing_or_invalid")
    result["contractPolicy"] = {"activeContract": CORE_CONTRACT, "effectiveTradeDate": effective} if not problems else None
    audit_stamp = parse_instant(health.get("generatedAt"))
    if not effective or not audit_stamp or not audit_stamp.tzinfo or effective > audit_stamp.astimezone(CST).date().isoformat():
        problems.append("core_contract_effective_date_invalid")
    if legacy.get("contract") != FULL_CONTRACT or legacy.get("status") != "historical_only" or legacy.get("scheduledBefore") != effective:
        problems.append("legacy_contract_scope_invalid")
    core = health.get("coreMarket")
    core = core if isinstance(core, dict) else {}
    if (core.get("contract") != CORE_CONTRACT or core.get("fullSnapshot") is not False
            or core.get("artifactKind") != "qlib_market_summary" or core.get("includesCompletePriceArchive") is not False
            or core.get("expectedFrom") != effective):
        problems.append("core_contract_scope_invalid")
    rows, ledger = core.get("rowHashes"), core.get("ledger")
    if (not isinstance(rows, dict) or any(not is_iso_date(day) or not isinstance(digest, str)
            or not HASH_RE.fullmatch(digest) for day, digest in rows.items())
            or type(core.get("rowCount")) is not int or core["rowCount"] != len(rows or {})):
        problems.append("core_market_rows_invalid")
    if not isinstance(ledger, list):
        problems.append("core_market_ledger_unavailable")
        ledger = []
    clean, seen = [], set()
    for item in ledger:
        if (not isinstance(item, dict) or not is_iso_date(item.get("tradeDate"))
                or item["tradeDate"] in seen or not effective or item["tradeDate"] < effective
                or item.get("contract") != CORE_CONTRACT or item.get("fullSnapshot") is not False
                or not isinstance(item.get("coreState"), str) or item.get("coreState") not in {"valid", "invalid", "missing"}
                or type(item.get("pendingWindow")) is not bool):
            problems.append("core_market_ledger_invalid")
            continue
        day = item["tradeDate"]
        seen.add(day)
        digest = item.get("payloadSha256")
        if item["coreState"] == "valid" and (not isinstance(digest, str) or not HASH_RE.fullmatch(digest)
                or not isinstance(rows, dict) or rows.get(day) != digest):
            problems.append("core_market_valid_row_hash_mismatch")
        clean.append({"date": day, "state": item["coreState"], "pendingWindow": item["pendingWindow"],
                      "payloadSha256": digest if isinstance(digest, str) and HASH_RE.fullmatch(digest) else None,
                      "contract": CORE_CONTRACT, "fullSnapshot": False})
    run = contract_run(core.get("lastRun"))
    if result["activeLastRun"] != run:
        problems.append("core_market_active_run_mismatch")
    latest = core.get("latestValidDate")
    if latest is not None and (not is_iso_date(latest) or not isinstance(rows, dict) or latest not in rows):
        problems.append("core_market_latest_date_invalid")
    successes = {"inserted", "skipped_existing", "idempotent", "success"}
    target = next((item for item in clean if item["date"] == run["targetDate"]), None)
    if run["status"] in successes and (not target or target["state"] != "valid"
            or not run["timestamp"] or not run["payloadSha256"] or target["payloadSha256"] != run["payloadSha256"]):
        problems.append("core_market_run_missing_or_mismatched")
    if run["status"] is None:
        problems.append("core_market_run_unavailable")
    closed = run["status"] == "skipped_non_trading"
    run_stamp = parse_instant(run["timestamp"])
    skip_proven = closed and not (not run["calendarClosedVerified"] or not run["calendarTargetDate"]
            or run["previousTradingDate"] != run["targetDate"] or not run["targetDate"]
            or run["targetDate"] >= run["calendarTargetDate"] or not run["runId"]
            or not run["calendarEvidenceSha256"] or run["payloadSha256"] is not None
            or not run_stamp or run_stamp.astimezone(CST).date().isoformat() != run["calendarTargetDate"]
            or not audit_stamp or audit_stamp.astimezone(CST).date().isoformat() != run["calendarTargetDate"])
    if closed and not skip_proven:
        problems.append("core_non_trading_skip_unproven")
    empty_closed = (skip_proven and core.get("readState") == "missing" and rows == {} and ledger == []
                    and type(core.get("rowCount")) is int and core["rowCount"] == 0 and latest is None)
    if core.get("readState") != "checked" and not empty_closed:
        problems.append("core_market_evidence_unavailable")
    quality = core.get("quality")
    safe_quality = None
    if run["status"] in successes:
        quality = quality if isinstance(quality, dict) else {}
        counts = [quality.get(key) for key in ("valid", "invalid", "eligible")]
        ratio = quality.get("ratio")
        if (quality.get("scope") != "sealed_core_qlib_summary"
                or quality.get("targetDate") != run["targetDate"] or quality.get("payloadSha256") != run["payloadSha256"]
                or not all(type(value) is int and value >= 0 for value in counts) or counts[2] <= 0
                or counts[0] + counts[1] != counts[2]
                or quality.get("priceStatus") != ("partial" if counts[1] else "complete")
                or type(ratio) not in (int, float) or not 0 <= ratio <= 1
                or abs(ratio - counts[0] / counts[2]) > 0.000001):
            problems.append("core_market_quality_missing_or_mismatched")
        else:
            safe_quality = {key: quality[key] for key in ("scope", "targetDate", "payloadSha256", "priceStatus", "valid", "invalid", "eligible", "ratio")}
            tag = quality.get("generationTag")
            safe_quality["generationTag"] = tag if isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", tag) else None
    due = [item for item in clean if not item["pendingWindow"]]
    raw_issues = health.get("issues")
    if not isinstance(raw_issues, list):
        problems.append("core_market_issues_unavailable")
        raw_issues = []
    blockers = sorted({item["code"] for item in raw_issues if isinstance(item, dict)
        and isinstance(item.get("code"), str) and item["code"] in CORE_ISSUES
        and item["code"] != "core_market_material_warning"})
    if any(item["state"] != "valid" for item in due):
        blockers = sorted(set(blockers + ["core_market_due_gap"]))
    state = ("insufficient" if problems else "fail" if blockers else "not_required" if closed else "pass" if run["status"] in successes
             else "pending" if run["status"] == "unknown" else "fail")
    result["coreMarket"] = {"contract": CORE_CONTRACT, "fullSnapshot": False,
        "artifactKind": "qlib_market_summary", "includesCompletePriceArchive": False,
        "readState": core.get("readState") if isinstance(core.get("readState"), str) and core.get("readState") in {"checked", "missing", "invalid", "not_configured"} else "unknown",
        "status": state, "expectedFrom": effective, "rowCount": core.get("rowCount") if not problems else None,
        "latestValidDate": latest if not problems else None, "lastRun": run,
        "quality": safe_quality,
        "knownDates": len(due) if not problems else None,
        "validDates": sum(item["state"] == "valid" for item in due) if not problems else None,
        "ledger": sorted(clean, key=lambda item: item["date"]), "evidenceProblems": sorted(set(problems)),
        "blockingReasons": blockers}
    errors.extend(sorted(set(problems)))
    return result


def absence_scope_evidence(value, errors):
    """Preserve two audited populations without copying arbitrary source text."""
    scopes = value.get("absenceScopes")
    if scopes is None:
        return {}
    if not isinstance(scopes, dict) or set(scopes) != {"full_market", "csi_members"}:
        errors.append("absence_scope_evidence_invalid")
        return {"absenceScopeEvidence": "unavailable"}
    clean = {}
    for name in ("full_market", "csi_members"):
        item = scopes[name]
        if not isinstance(item, dict) or item.get("scope") != name:
            errors.append("absence_scope_evidence_invalid")
            return {"absenceScopeEvidence": "unavailable"}
        count, eligible = item.get("invalidCount"), item.get("eligibleCount")
        tolerance = item.get("absenceTolerance")
        if (type(count) is not int or type(eligible) is not int or not 0 <= count <= eligible or eligible <= 0
                or type(tolerance) not in (int, float) or not 0 <= tolerance < float("inf")
                or type(item.get("proven")) is not bool):
            errors.append("absence_scope_evidence_invalid")
            return {"absenceScopeEvidence": "unavailable"}
        reason = item.get("escalation")
        if reason is not None and (not isinstance(reason, str) or not re.fullmatch(
                r"proof_incomplete|count_over_tolerance:\d+>\d+(?:\.\d+)?|duration_over_bound:\d+>\d+", reason)):
            errors.append("absence_scope_evidence_invalid")
            return {"absenceScopeEvidence": "unavailable"}
        streaks = item.get("consecutiveMissingTradingDates")
        symbols = item.get("symbols")
        longest = item.get("maxConsecutiveMissingTradingDays")
        if (not isinstance(streaks, dict) or not isinstance(symbols, list)
                or type(longest) is not int or longest < 0
                or any(not isinstance(s, str) or not re.fullmatch(r"(?:SH|SZ)\d{6}", s) for s in symbols)
                or any(not isinstance(s, str) or s not in symbols or not isinstance(days, list)
                       or any(not is_iso_date(day) for day in days) for s, days in streaks.items())):
            errors.append("absence_scope_evidence_invalid")
            return {"absenceScopeEvidence": "unavailable"}
        clean[name] = {"scope": name, "invalidCount": count, "eligibleCount": eligible,
                       "absenceTolerance": tolerance, "proven": item["proven"],
                       "severity": item.get("severity") if item.get("severity") in {"warning", "information"} else None,
                       "escalation": reason, "maxConsecutiveMissingTradingDays": longest,
                       "symbols": symbols[:200],
                       "consecutiveMissingTradingDates": {s: streaks.get(s, [])[:60] for s in symbols[:200]},
                       "detailTruncated": len(symbols) > 200 or any(len(days) > 60 for days in streaks.values())}
    return {"absenceScopes": clean, "countScope": "two_separate_populations"}


def build_progress(root: Path, exported: dt.datetime, health_path: Path):
    errors = []
    panel = {"schemaVersion": SCHEMA_VERSION, "page": "progress", "exportedAt": iso(exported),
             "readErrors": errors, "health": None, "qlib": None, "snapshot": None,
             "criticalDates": [], "legacyDates": [], "coverage": None,
             "historicalGaps": None, "currentIssues": []}
    source_time = None
    health = read_json(health_path)
    if health is None:
        errors.append("health_json_unreadable")
    elif not isinstance(health, dict):
        errors.append("health_json_not_object")
    else:
        generated = health.get("generatedAt")
        source_time = parse_instant(generated)
        if source_time is None:
            errors.append("health_generated_at_unreadable")
        raw_issues = health.get("issues")
        issues_available = isinstance(raw_issues, list)
        if not issues_available:
            errors.append("health_issues_unavailable" if raw_issues is None else "health_issues_not_list")
        issues = list(raw_issues) if issues_available else []
        parsed_issues = []
        for item in issues:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                errors.append("health_issue_shape")
                continue
            severity = item.get("severity") if item.get("severity") in OK_SEVERITIES else "unknown"
            if severity == "unknown":
                # Unknown is not informational: keep it actionable in every UI.
                errors.append("health_issue_severity_unrecognized")
            # Production uses `dates` for gap issues and `affectedDates` for
            # qlib_source_absence, so accept both spellings.
            raw_dates = item.get("dates")
            if not isinstance(raw_dates, list):
                raw_dates = item.get("affectedDates")
            raw_overdue = item.get("overdueStageDates")
            raw_symbols = item.get("symbols")
            parsed_issues.append({
                "code": item["code"], "severity": severity,
                "dates": sorted(set(str(value) for value in raw_dates))[:60]
                if isinstance(raw_dates, list) else [],
                "overdueStageDates": sorted(str(value) for value in raw_overdue)[:30]
                if isinstance(raw_overdue, list) else [],
                "symbols": sorted(str(value) for value in raw_symbols)[:20]
                if isinstance(raw_symbols, list) else []})
            # Preserve an explicit failure even if severity says information.
            # The homepage must not reclassify contradictory failure evidence.
            if item.get("status") == "fail":
                parsed_issues[-1]["status"] = "fail"
            if item["code"] == "qlib_source_absence":
                parsed_issues[-1].update(absence_scope_evidence(item, errors))
            if item["code"] in {"last_snapshot_not_successful", "last_snapshot_material_warning",
                                "last_core_market_not_successful", "core_market_material_warning"}:
                # The audit describes the last run, not necessarily a run for
                # the audit date. Keep its own outcome and target separately;
                # never manufacture a date from generatedAt.
                run_status = (item.get("status") if item["code"] in {"last_snapshot_not_successful", "last_core_market_not_successful"}
                              else item.get("runStatus"))
                parsed_issues[-1]["runStatus"] = (run_status if isinstance(run_status, str)
                                                    and run_status in SNAPSHOT_RUN_STATUSES else None)
                parsed_issues[-1]["targetDate"] = (item.get("target")
                                                      if is_iso_date(item.get("target")) else None)
                run_at = parse_instant(item.get("runAt"))
                if run_at is not None and run_at.tzinfo is not None:
                    parsed_issues[-1]["runAt"] = run_at.isoformat()
                run_id = item.get("runId")
                if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id):
                    parsed_issues[-1]["runId"] = run_id
                failure_key = ("sourceFailure" if item["code"] == "last_snapshot_not_successful"
                               else "stageSourceFailure")
                failure = snapshot_failure_evidence(item.get(failure_key))
                if failure is not None:
                    parsed_issues[-1][failure_key] = failure
                if item["code"] == "last_snapshot_material_warning" and type(item.get("stageUnavailable")) is bool:
                    parsed_issues[-1]["stageUnavailable"] = item["stageUnavailable"]
            if item["code"] == "qlib_legacy_unquoted_csi":
                parsed_issues[-1].update(legacy_issue_evidence(item, errors))
            if item["code"] in CORE_ISSUES:
                parsed_issues[-1]["contract"] = (FULL_CONTRACT if item["code"] == "legacy_full_rows_after_cutover" else CORE_CONTRACT)
                for key in ("target", "tradeDate", "dueDate"):
                    if is_iso_date(item.get(key)):
                        parsed_issues[-1]["targetDate"] = item[key]
                        break
        raw_snapshot = health.get("snapshot")
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        panel["health"] = {"status": str(health.get("status") or "unknown"),
                           "generatedAt": generated if isinstance(generated, str) else None,
                           "issues": parsed_issues if issues_available else None}
        panel["snapshot"] = {"rowCount": snapshot.get("rowCount"),
                             "latestValidDate": snapshot.get("latestValidDate"),
                             "contract": FULL_CONTRACT, "fullSnapshot": True}
        panel.update(contract_evidence(health, errors))
        panel["criticalDates"] = (next((issue["dates"] for issue in parsed_issues
                                        if issue["code"] == "unresolved_trading_day_gaps"), [])
                                  if issues_available else None)
        panel["legacyDates"] = (next((issue["dates"] for issue in parsed_issues
                                      if issue["code"] == "legacy_gaps_or_invalid_rows"), [])
                                if issues_available else None)
        panel["coverage"] = build_coverage(health, snapshot, parsed_issues, issues_available)
        panel["coverage"].update({"contract": FULL_CONTRACT, "fullSnapshot": True})
        # 历史缺口（有意保留、不补造）与当前问题分开呈现，避免混报。
        current_baseline, current_addenda, current = split_retained_issues(parsed_issues)
        retained_critical = (sorted(set(current_baseline["unresolved_trading_day_gaps"]) |
                                    {item["date"] for item in current_addenda
                                     if item["code"] == "unresolved_trading_day_gaps"})
                             if issues_available else None)
        retained_legacy = current_baseline["legacy_gaps_or_invalid_rows"] if issues_available else None
        retained_dates = set(retained_critical or []) | set(retained_legacy or [])
        gap_evidence = ([item for item in (panel["coverage"] or {}).get("dates", [])
                         if item.get("date") in retained_dates] if issues_available else None)
        panel["historicalGaps"] = {
            "count": len(gap_evidence) if gap_evidence is not None else None,
            "criticalDates": retained_critical,
            "legacyDates": retained_legacy,
            "evidence": gap_evidence,
            "current": {"criticalDates": retained_critical, "legacyDates": retained_legacy,
                        "count": len(gap_evidence) if gap_evidence is not None else None},
            "baseline": {"criticalDates": sorted(RETAINED_ISSUES["unresolved_trading_day_gaps"][1]),
                         "legacyDates": sorted(RETAINED_ISSUES["legacy_gaps_or_invalid_rows"][1]),
                         "baselineDate": RETAINED_BASELINE_DATE,
                         "baselineSha256": RETAINED_BASELINE_SHA256},
            "addenda": [dict(item) for item in RETAINED_ADDENDA],
            "observedAt": generated, "baselineDate": RETAINED_BASELINE_DATE,
            "baselineSha256": RETAINED_BASELINE_SHA256,
            "note": (f"原保留基线（{RETAINED_BASELINE_DATE}）："
                     f"{len(RETAINED_ISSUES['unresolved_trading_day_gaps'][1])} 个缺口日"
                     f" + {len(RETAINED_ISSUES['legacy_gaps_or_invalid_rows'][1])} 个遗留问题日；"
                     f"后续逐项登记 {len(RETAINED_ADDENDA)} 个缺口日。"
                     + (f"本次审计在保留范围内仍有 {len(retained_critical)} 个未解决缺口日"
                        f"和 {len(retained_legacy)} 个遗留问题日，"
                        f"总计 {len(gap_evidence)} 条日期记录；" if issues_available
                        else "本次审计问题列表不可读，当前缺口日期与数量未知；")
                     + "审计整体级别见 health.status。"
                     "不回填、不补造；仅列审计已记录的快照/暂存状态、原因、观察时间与哈希；"
                     "未提供的字段保持未记录，基线外日期或级别变化另列待核。")}
        panel["currentIssues"] = current if issues_available else None
    manifest = read_json(root / "qlib" / "data" / "cn_data" / ".qlib-dolt-incremental.json")
    if manifest is None:
        errors.append("qlib_manifest_unreadable")
    elif not isinstance(manifest, dict):
        errors.append("qlib_manifest_not_object")
    else:
        panel["qlib"] = {"installTargetDate": manifest.get("targetDate"),
                         "dataQuality": manifest.get("dataQuality")}
    calendar = read_text(root / "qlib" / "data" / "cn_data" / "calendars" / "day.txt")
    if calendar and panel["qlib"] is not None:
        panel["qlib"]["calendarLast"] = calendar.strip().splitlines()[-1]
    panel.update(freshness(exported, source_time, "progress"))
    return panel


def acceptance_latest(root: Path):
    directory = root / "ops" / "acceptance" / "out"
    latest = {}
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return latest
    for path in files:
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        phase = payload.get("phase")
        if phase not in ("night", "monday"):
            continue
        raw_stamp = payload.get("generatedAt")
        stamp = raw_stamp if isinstance(raw_stamp, str) else ""
        previous = latest.get(phase)
        if isinstance(previous, dict) and str(previous.get("generatedAt") or "") > stamp:
            continue
        raw_checks = payload.get("checks")
        checks = raw_checks if isinstance(raw_checks, dict) else {}
        limitations = []
        for value in checks.values():
            if isinstance(value, dict) and isinstance(value.get("limitations"), list):
                limitations.extend(value["limitations"])
        latest[phase] = {"generatedAt": stamp,
                         "statuses": {name: (value.get("status") if isinstance(value, dict) else None)
                                      for name, value in checks.items()},
                         "limitations": limitations[:12]}
    return latest


# Timestamp syntax for a held-attempt observation (not delivery proof).
ZONED_INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


def read_send_receipt(project: Path, date: str, contract=None):
    event_id = f"astock-{date}-success-telegram-v1"
    record, issue = read_telegram_event(project / "work/notifications/receipts/send" / f"{event_id}.json")
    if issue is not None:
        return None, f"receipt_{issue}"
    return (contract or load_delivery_contract()).validate_send_receipt(record, date)


def platform_receipt(record):
    return load_delivery_contract().platform_receipt(record)


def receiver_content_hash(record):
    """Canonical persisted v2 content hash, without alias fallback."""
    if isinstance(record, dict):
        value = record.get("receiver_content_sha256")
        if isinstance(value, str) and HASH_RE.fullmatch(value):
            return "receiver_content_sha256", value
    return None, None


def is_iso_date(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def verified_delivery_data_date(project: Path, date: str, report_sha: str, published):
    """Expected data day comes from independently hash-bound archived evidence.

    Never derive this expectation from the receipt being checked. Legacy title
    fallback reads the actual bound report bytes, not another day's report.
    """
    if not isinstance(published, dict) or published.get("status") != "sealed" \
            or published.get("reportSha256") != report_sha:
        return None
    archive = project / "work/report-archives" / date
    frozen = archive / "input.json"
    try:
        if frozen.is_symlink() or not frozen.is_file():
            return None
        body = frozen.read_bytes()
        if sha256_bytes(body) != published.get("inputSha256"):
            return None
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("targetDate") != date:
            return None
        data = value.get("compilerContext", {}).get("data", {})
        session = data.get("previousAShareSession", {}) if isinstance(data, dict) else {}
        expected = session.get("sessionDate") if isinstance(session, dict) else None
        if expected is not None:
            return expected if is_iso_date(expected) and expected <= date else None
        report = project / "reports/daily" / f"{date}.md"
        if report.is_symlink() or not report.is_file():
            return None
        report_body = report.read_bytes()
        if sha256_bytes(report_body) != report_sha:
            return None
        days = set(re.findall(r"上一交易日 A 股行情[（(](\d{4}-\d{2}-\d{2})[）)]",
                              report_body.decode("utf-8")))
        if len(days) == 1:
            expected = days.pop()
            if is_iso_date(expected) and expected <= date:
                return expected
    except (OSError, ValueError, UnicodeError, AttributeError):
        return None
    return None


def build_delivery(root: Path, exported: dt.datetime, today_panel: dict):
    errors = []
    project, bus = root / "AStock_AI_System", root / "signal-bus"
    last_run = read_json(project / "work" / "last-run.json")
    if not isinstance(last_run, dict):
        errors.append("last_run_unreadable")
        last_run = {}
    date = last_run.get("targetDate") if isinstance(last_run.get("targetDate"), str) else None
    if date is None:
        dates = list_report_dates(project)
        date = dates[0] if dates else None
    source_time = parse_instant(last_run.get("completedAt"))
    expected = expected_run_day(project, exported)
    holiday = expected if expected and expected["status"] == "not_required" else None
    if holiday:
        # A validated market holiday is not a missing report: judge the latest
        # valid trading day's delivery, with that report's own cutoff clock.
        date = holiday["latestTradingDate"]
        report = (today_panel or {}).get("report") or {}
        source_time = (parse_instant((today_panel or {}).get("sourceTime"))
                       if report.get("targetDate") == date else None)
    elif holiday_skip_record(project) is not None:
        # R04: the holiday is over. Never point the layers at the holiday date;
        # judge the latest available report while the expected day is displayed
        # separately (awaiting / overdue).
        dates = list_report_dates(project)
        if dates:
            date = dates[0]
        report = (today_panel or {}).get("report") or {}
        source_time = (parse_instant((today_panel or {}).get("sourceTime"))
                       if report.get("targetDate") == date else None)
    panel = {"schemaVersion": SCHEMA_VERSION, "page": "delivery", "exportedAt": iso(exported),
             "readErrors": errors, "targetDate": date, "layers": {}, "acceptance": acceptance_latest(root),
             "expectedReport": expected}
    if not date:
        errors.append("no_report_date")
        for layer in ("seal", "telegram", "relay"):
            panel["layers"][layer] = {"status": "insufficient", "evidence": {},
                                      "limitations": ["无目标日，无法判定"]}
        panel.update(freshness(exported, source_time, "delivery"))
        return panel

    # --- 本地密封：先判证据完整性，缺失为 insufficient，只有真实不一致才 fail
    archive = project / "work" / "report-archives" / date
    published = read_json(archive / "published.json")
    report_path = project / "reports" / "daily" / f"{date}.md"
    result_path = archive / "result" / "result.json"
    recomputed: dict[str, str | None] = {"input": sha256_file(archive / "input.json"),
                                         "report": sha256_file(report_path),
                                         "result": sha256_without_trailing_newline(result_path)}
    declared: dict[str, object] = {}
    if isinstance(published, dict):
        declared = {key: published.get(f"{key}Sha256") for key in ("input", "report", "result")}
    else:
        errors.append("published_json_missing")
    complete = (all(isinstance(value, str) and HASH_RE.fullmatch(value) for value in recomputed.values())
                and all(isinstance(value, str) and HASH_RE.fullmatch(value) for value in declared.values()))
    # Trailing-newline convention: the archive writes canonicalJson + "\n" and
    # records the hash of the canonical bytes, so accept that (or a raw byte
    # hash) and record which convention matched.
    result_raw = sha256_file(result_path)
    result_convention = None
    if complete and isinstance(declared.get("result"), str):
        if declared["result"] == recomputed.get("result"):
            result_convention = "canonical-json-without-trailing-newline"
        elif declared["result"] == result_raw:
            result_convention = "raw-bytes"
            recomputed["result"] = result_raw
    matches = ({key: recomputed.get(key) == declared.get(key) for key in ("input", "report", "result")}
               if complete else None)
    if not complete:
        seal_status = "insufficient"
        limitations = ["密封证据不完整：缺少文件或记录值，不能判通过"]
    elif matches is not None and all(matches.values()):
        seal_status = "pass"
        limitations = []
    else:
        seal_status = "fail"
        limitations = ["密封哈希与记录值不一致"]
    # R02 (2026-09-27): hash equality alone is not a sealed report. The published
    # record must explicitly say "sealed"; a missing status is insufficient and an
    # explicit failure is a failure, regardless of matching hashes.
    published_status = published.get("status") if isinstance(published, dict) else None
    if seal_status == "pass" and published_status != "sealed":
        seal_status = "fail" if published_status else "insufficient"
        limitations = ([f"发布记录状态为 {published_status!r}，不是已封存；哈希一致不足以判通过"]
                       if published_status else ["发布记录缺少 status，哈希一致不足以判通过"])
    panel["layers"]["seal"] = {"status": seal_status,
                               "evidence": {"matches": matches,
                                            "resultHashConvention": result_convention,
                                            "declared": {key: str(declared.get(key) or "")
                                                         for key in ("input", "report", "result")},
                                            "recomputed": {key: str(recomputed.get(key) or "")
                                                           for key in ("input", "report", "result")},
                                            "publishedStatus": published.get("status")
                                            if isinstance(published, dict) else None},
                               "limitations": limitations}
    sealed_report = recomputed["report"]

    # A missing/invalid code dependency degrades only delivery proof, never PASS.
    try:
        contract = load_delivery_contract()
    except Exception:
        errors.append("delivery_contract_unavailable")
        for name in ("telegram", "relay"):
            panel["layers"][name] = {"status": "insufficient", "evidence": {},
                "limitations": ["共享送达证据模块缺失或不可用，不能确认送达"],
                "contractReason": "delivery_contract_unavailable"}
        panel.update(report_freshness(project, date, exported, source_time, "delivery"))
        return panel

    notification, notification_issue = read_telegram_event(
        project / "work/notifications/sent" / f"astock-{date}-success-telegram-v1.json")
    receipt_path = project / "work/notifications/receipts/send" / f"astock-{date}-success-telegram-v1.json"
    send_receipt, receipt_read_issue = read_telegram_event(receipt_path)
    if receipt_read_issue in {"unreadable", "malformed", "not_object"}:
        errors.append(f"telegram_receipt_{receipt_read_issue}")
    if notification_issue not in (None, "missing"):
        errors.append(f"telegram_record_{notification_issue}")
    notify_root = project / "work/notifications"
    payload_path = notify_root / "payloads" / f"astock-{date}-success-telegram-v1.md"
    safe = not any((notify_root / relative).is_symlink()
                   for relative in ("", "sent", "receipts", "receipts/send", "payloads"))
    safe = safe and not payload_path.is_symlink()
    payload_sha = sha256_file(payload_path) if safe else None
    payload_bytes = payload_path.stat().st_size if payload_sha is not None else None
    decision = contract.telegram_verdict(notification, send_receipt,
        date=date, report_sha256=sealed_report, payload_sha256=payload_sha,
        payload_bytes=payload_bytes, paths_safe=safe,
        receipt_issue=f"receipt_{receipt_read_issue}" if receipt_read_issue else None)
    panel["layers"]["telegram"] = {key: decision[key] for key in ("status", "evidence", "limitations")}
    panel["layers"]["telegram"]["contractReason"] = decision["reason"]
    if notification_issue not in (None, "missing"):
        panel["layers"]["telegram"]["evidence"]["recordReadStatus"] = notification_issue
        panel["layers"]["telegram"]["limitations"] = ["当日发送记录存在但不可读取或格式无效；无法确认实际发送或送达"]

    # --- relay：无内容哈希最高 insufficient
    relay, relay_issue = read_telegram_event(bus / "out" / "relay-delivery" / f"{date}.json")
    if relay_issue == "missing":
        panel["layers"]["relay"] = {"status": "insufficient", "evidence": {},
                                    "limitations": ["尚无当日 relay 记录"]}
        observation, observation_issue = read_telegram_event(
            bus / "out/relay-attempts" / f"{date}.json")
        if observation_issue not in (None, "missing"):
            errors.append(f"relay_attempt_{observation_issue}")
        if isinstance(observation, dict):
            observed_at = observation.get("observedAt")
            reason = observation.get("reason")
            if observation.get("schemaVersion") == "signal-bus.relay-attempt.v1" \
                    and observation.get("target_date") == date \
                    and observation.get("status") == "held" \
                    and type(observation.get("sent")) is int and observation["sent"] == 0 \
                    and isinstance(observed_at, str) and ZONED_INSTANT_RE.fullmatch(observed_at) \
                    and parse_instant(observed_at) is not None:
                safe_reason = reason if isinstance(reason, str) and re.fullmatch(
                    r"[a-z0-9_]{1,100}", reason) else "held_reason_unavailable"
                panel["layers"]["relay"]["evidence"] = {
                    "attemptStatus": "held", "attemptObservedAt": observed_at,
                    "attemptReason": safe_reason, "sent": 0}
                panel["layers"]["relay"]["limitations"] = [
                    f"relay 被门禁阻断（{safe_reason}），本次未发送；尚无接收回执"]
            else:
                errors.append("relay_attempt_binding_invalid")
    elif relay_issue is not None:
        errors.append(f"relay_record_{relay_issue}")
        panel["layers"]["relay"] = {"status": "insufficient",
                                    "evidence": {"recordReadStatus": relay_issue},
                                    "limitations": ["当日 relay 记录存在但不可读取或格式无效"]}
    else:
        relay_status, relay_reason = relay.get("status"), relay.get("reason")
        stored = relay.get("receiver_storage")
        relay_target, relay_as_of = relay.get("target_date"), relay.get("as_of")
        expected_data_date = verified_delivery_data_date(project, date, sealed_report, published)
        hash_key, receiver_hash = receiver_content_hash(relay)
        panel["layers"]["relay"] = {
            "status": None,
            "evidence": {"status": relay_status, "reason": relay_reason, "as_of": relay_as_of,
                         "target_date": relay_target, "ack_scope": relay.get("ack_scope"),
                         "receiver_storage": stored,
                         "expectedDataDate": expected_data_date,
                         "localReportSha256": relay.get("report_sha256"),
                         # 同一对象：我们实际发出的 POST 字节（发送端持久化）
                         "sentPayloadSha256": relay.get("payload_sha256"),
                         "contentMatch": relay.get("content_match"),
                         "receiverRecordId": relay.get("receiver_record_id"),
                         "receiverHashField": hash_key, "receiverContentSha256": receiver_hash,
                         "sealedReportSha256": sealed_report},
            "limitations": []}
        entry = panel["layers"]["relay"]
        decision = contract.relay_verdict(relay, report_date=date,
            report_sha256=sealed_report, data_date=expected_data_date)
        entry["status"] = decision["status"]
        entry["limitations"] = decision["limitations"]
        entry["contractReason"] = decision["reason"]
    panel.update(report_freshness(project, date, exported, source_time, "delivery"))
    return panel


def read_git_head(repo: Path):
    try:
        head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head.split(" ", 1)[1].strip()
        try:
            return (repo / ".git" / ref).read_text(encoding="utf-8").strip()
        except OSError:
            try:
                for line in (repo / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0]
            except OSError:
                return None
        return None
    return head or None


REPAIR_UNIT = "astock-xhs-source.service"
REPAIR_FILES = ("src/report.mjs", "src/xhs-draft.mjs")


def verified_service_repair(root, unit, services, exported, source_time):
    """Display a bounded repair, never change the recorded execution verdict.

    Only the reviewed source-service incident is in scope. Every export checks
    the exact failed invocation and both deployed code bytes. A new attempt,
    code drift, missing evidence, disabled timer or expiry removes this label.
    No service operation, Git command, collection, or network access.
    """
    if (unit.get("unit") != REPAIR_UNIT or unit.get("scope") != "user"
            or unit.get("kind") != "service" or unit.get("unitType") != "oneshot"
            or unit.get("health") != "failed" or unit.get("activeState") != "failed"):
        return None
    path = root / "ops/dashboard/service-repairs.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            return None
        raw = path.read_bytes()
        record = json.loads(raw)
        expected = {"schemaVersion", "unit", "scope", "failedInvocationId", "failedAt",
                    "recordedAt", "expiresAt", "deployedCommit", "files"}
        if not isinstance(record, dict) or set(record) != expected or type(record["schemaVersion"]) is not int or record["schemaVersion"] != 1:
            return None
        invocation = record["failedInvocationId"]
        if (record["unit"] != REPAIR_UNIT or record["scope"] != "user"
                or not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation)
                or invocation != unit.get("invocationId")):
            return None
        failed, recorded, expires = (parse_instant(record[key]) for key in ("failedAt", "recordedAt", "expiresAt"))
        if (any(value is None or value.tzinfo is None for value in (failed, recorded, expires, source_time, exported))
                or failed != parse_instant(unit.get("lastExit"))
                or not failed < recorded <= source_time <= exported < expires
                or exported - source_time > dt.timedelta(minutes=15)
                or expires - recorded > dt.timedelta(hours=36)):
            return None
        commit = record["deployedCommit"]
        project = root / "AStock_AI_System"
        if (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or commit != read_git_head(project)):
            return None
        files = record["files"]
        if not isinstance(files, dict) or set(files) != set(REPAIR_FILES):
            return None
        for relative in REPAIR_FILES:
            file = project / relative
            value = files[relative]
            if (not isinstance(value, str) or not HASH_RE.fullmatch(value)
                    or project.is_symlink() or file.is_symlink() or file.parent.is_symlink() or file.stat().st_size > 2_000_000
                    or sha256_bytes(file.read_bytes()) != value):
                return None
        timer = next((item for item in services if item.get("unit") == "astock-xhs-source.timer"
                      and item.get("scope") == "user"), {})
        next_run = parse_instant(timer.get("nextElapse"))
        if (timer.get("health") != "ok" or timer.get("activeState") != "active"
                or timer.get("unitFileState") != "enabled" or next_run is None
                or not exported < next_run < expires):
            return None
        return {"state": "deployed_pending_verification", "failedInvocationId": invocation,
                "failedAt": iso(failed), "recordedAt": iso(recorded), "verifiedAt": iso(exported),
                "expiresAt": iso(expires), "deployedCommit": commit, "codeHashesVerified": True,
                "proofSha256": sha256_bytes(raw), "nextRunAt": iso(next_run)}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def build_projects(root: Path, exported: dt.datetime, services_path: Path | None):
    errors, versions, services = [], {}, []
    for name in ("AStock_AI_System", "signal-bus", "research-data"):
        head = read_git_head(root / name)
        if head is None:
            errors.append(f"{name}_head_unreadable")
        versions[name] = {"head": head}
    source_time = None
    if services_path is None:
        errors.append("services_snapshot_not_provided")
    else:
        payload = read_json(services_path)
        if not isinstance(payload, dict):
            errors.append("services_snapshot_unreadable")
        else:
            source_time = parse_instant(payload.get("checkedAt"))
            if source_time is None:
                errors.append("services_checked_at_unreadable")
            raw_units = payload.get("units")
            if not isinstance(raw_units, list):
                errors.append("services_units_not_list")
                raw_units = []
            for unit in raw_units:
                if isinstance(unit, dict) and unit.get("unit") in KNOWN_UNITS:
                    entry = {}
                    for key in UNIT_FIELDS:
                        value = unit.get(key)
                        if isinstance(value, bool) or not isinstance(value, (str, int)) and value is not None:
                            value = None
                        elif isinstance(value, str):
                            value = value[:UNIT_TEXT_LIMIT]
                        entry[key] = value
                    services.append(entry)
    for unit in services:
        repair = verified_service_repair(root, unit, services, exported, source_time)
        if repair is not None:
            unit["repair"] = repair
    panel = {"schemaVersion": SCHEMA_VERSION, "page": "projects", "exportedAt": iso(exported),
             "readErrors": errors, "versions": versions, "services": services,
             "pendingAuthorization": list(PENDING_AUTHORIZATION)}
    panel.update(freshness(exported, source_time, "projects"))
    return panel


# --------------------------------------------------------------------- output

def ensure_within_root(root: Path, target: Path, *, allow_missing_last: bool = True,
                       allow_symlinked_ancestors: bool = False) -> Path:
    """Refuse symlinked components so no link can redirect the write.

    With ``allow_symlinked_ancestors=False`` (the default, used in production) the
    output root must also be its own real location: if any directory above it is a
    symlink, the configured path is not where bytes actually land, and the web
    server alias could point somewhere else. Local/dev trees under a symlinked
    system prefix (for example macOS /var) may opt out explicitly, and the mode is
    recorded in the manifest.
    """
    if root.is_symlink():
        raise ValueError("export_root_symlink")
    if not allow_symlinked_ancestors:
        lexical = Path(os.path.abspath(root))
        if root.resolve(strict=False) != lexical:
            raise ValueError("export_root_ancestor_symlink")
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target != resolved_root and not str(resolved_target).startswith(str(resolved_root) + os.sep):
        raise ValueError("export_path_outside_root")
    current = root
    for index, part in enumerate(target.relative_to(root).parts):
        current = current / part
        last = index == len(target.relative_to(root).parts) - 1
        if current.is_symlink():
            raise ValueError("export_path_symlink_component")
        if current.exists() and not last and not current.is_dir():
            raise ValueError("export_path_not_directory")
        if current.exists() and last and allow_missing_last and not current.is_file():
            raise ValueError("export_path_not_regular_file")
    return target


def prepare_directory(root: Path, directory: Path, *, group_gid: int | None,
                      allow_symlinked_ancestors: bool = False):
    ensure_within_root(root, directory, allow_missing_last=False,
                       allow_symlinked_ancestors=allow_symlinked_ancestors)
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    os.chmod(directory, DIR_MODE)
    if group_gid is not None:
        os.chown(directory, -1, group_gid)
    return directory


def write_atomic(root: Path, relative: str, body: bytes, *, group_gid: int | None = None,
                 allow_symlinked_ancestors: bool = False, _publisher=None):
    target = ensure_within_root(root, root / relative,
                                allow_symlinked_ancestors=allow_symlinked_ancestors)
    prepare_directory(root, target.parent, group_gid=group_gid,
                      allow_symlinked_ancestors=allow_symlinked_ancestors)
    # Separate reviewed permission candidate; only these four targets opt in.
    if relative in {"today.json", "progress.json", "delivery.json", "projects.json"}:
        if _publisher is not None:
            return _publisher(root, target, body, group_gid=group_gid,
                             allow_symlinked_ancestors=allow_symlinked_ancestors)
        permission_spec = importlib.util.spec_from_file_location(
            "nng8_four_file_permissions", "/opt/nng8-permissions/nng8_publish_permissions.py")
        permission_module = importlib.util.module_from_spec(permission_spec)
        permission_spec.loader.exec_module(permission_module)
        return permission_module.publish_dashboard(
            root, relative, body, group_gid=group_gid,
            allow_symlinked_ancestors=allow_symlinked_ancestors)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, FILE_MODE)
        if group_gid is not None:
            os.chown(temporary, -1, group_gid)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def degraded_page(name: str, exported: dt.datetime, error: Exception):
    return {"schemaVersion": SCHEMA_VERSION, "page": name, "exportedAt": iso(exported),
            "sourceTime": None, "ageSeconds": None, "stale": True,
            "thresholdHours": THRESHOLDS_HOURS[name], "reason": "builder_failed",
            "readErrors": [f"builder_failed:{type(error).__name__}"],
            "report": None, "focus": [], "availableDates": [], "health": None, "qlib": None,
            "snapshot": None, "criticalDates": [], "legacyDates": [], "layers": {},
            "acceptance": {}, "targetDate": None, "versions": {}, "services": [],
            "pendingAuthorization": []}


def export_all(root: Path, out: Path, exported: dt.datetime, health_path: Path,
               services_path: Path | None = None, history: int = 7, group: str | None = None,
               allow_symlinked_ancestors: bool = False, _publisher=None):
    group_gid = None
    if group:
        group_gid = grp.getgrnam(group).gr_gid
    history = max(1, min(MAX_HISTORY, int(history)))
    builders = {
        "today": lambda: build_today(root, exported, history),
        "progress": lambda: build_progress(root, exported, health_path),
        "projects": lambda: build_projects(root, exported, services_path),
    }
    pages, failures = {}, {}
    for name, builder in builders.items():
        try:
            pages[name] = builder()
        except Exception as error:  # one bad source must not drag the batch down
            pages[name] = degraded_page(name, exported, error)
            failures[name] = type(error).__name__
    try:
        pages["delivery"] = build_delivery(root, exported, pages.get("today", {}))
    except Exception as error:
        pages["delivery"] = degraded_page("delivery", exported, error)
        failures["delivery"] = type(error).__name__
    health = read_json(health_path)
    try:
        pages["today"]["focus"] = build_focus(root, exported, pages.get("today", {}),
                                              pages.get("delivery", {}), health, pages.get("progress", {}))
    except Exception as error:
        pages["today"]["readErrors"] = (pages["today"].get("readErrors") or []) + \
            [f"focus_failed:{type(error).__name__}"]

    manifest = {"schemaVersion": SCHEMA_VERSION, "exportedAt": iso(exported), "files": {},
                "builderFailures": failures, "history": {"count": history, "pages": {}},
                "pathCheck": "resolved-symlink-allowed" if allow_symlinked_ancestors else "strict"}
    for name, payload in pages.items():
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        write_atomic(out, f"{name}.json", body, group_gid=group_gid,
                     allow_symlinked_ancestors=allow_symlinked_ancestors, _publisher=_publisher)
        manifest["files"][f"{name}.json"] = {"sha256": sha256_bytes(body), "bytes": len(body),
                                             "stale": payload.get("stale"),
                                             "readErrors": payload.get("readErrors") or []}
    # Per-date report pages so 历史日期 can actually switch reports.
    project = root / "AStock_AI_System"
    last_run = read_json(project / "work" / "last-run.json")
    if not isinstance(last_run, dict):
        last_run = {}
    for date in list_report_dates(project)[:history]:
        try:
            page, page_errors, source_time = report_page(project, date, exported, last_run)
            if page is None:
                continue
            payload = {"schemaVersion": SCHEMA_VERSION, "page": "report", "date": date,
                       "exportedAt": iso(exported), "report": page,
                       "readErrors": page_errors}
            payload.update(report_freshness(project, date, exported, source_time, "report"))
        except Exception as error:
            payload = degraded_page("report", exported, error)
            payload["date"] = date
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        write_atomic(out, f"{HISTORY_PAGES}/{date}.json", body, group_gid=group_gid,
                     allow_symlinked_ancestors=allow_symlinked_ancestors, _publisher=_publisher)
        manifest["history"]["pages"][date] = {"sha256": sha256_bytes(body), "bytes": len(body),
                                             "readErrors": payload.get("readErrors") or []}
    write_atomic(out, "manifest.json",
                 json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n",
                 group_gid=group_gid, allow_symlinked_ancestors=allow_symlinked_ancestors,
                 _publisher=_publisher)
    return manifest


def default_root() -> Path:
    return Path("/home/ubuntu")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(default_root()))
    parser.add_argument("--out", default="/var/lib/nng8-dashboard")
    parser.add_argument("--health-json", default="/var/lib/research-data-health/last.json")
    parser.add_argument("--services-json", default=None)
    parser.add_argument("--history", type=int, default=7)
    parser.add_argument("--group", default=None)
    parser.add_argument("--allow-symlinked-ancestors", action="store_true",
                        help="local/dev only: permit a symlinked prefix above the output root")
    parser.add_argument("--now")
    parser.add_argument("--delivery-contract-module", type=Path,
                        help="Trusted code file; never a source-root evidence path")
    return parser


def main(argv=None) -> int:
    global DELIVERY_CONTRACT_PATH
    args = build_parser().parse_args(argv)
    if args.delivery_contract_module is not None:
        DELIVERY_CONTRACT_PATH = args.delivery_contract_module
    exported = now_cst(args.now)
    manifest = export_all(Path(args.source_root), Path(args.out), exported, Path(args.health_json),
                          Path(args.services_json) if args.services_json else None,
                          history=args.history, group=args.group,
                          allow_symlinked_ancestors=args.allow_symlinked_ancestors)
    stale = [name for name, value in manifest["files"].items() if value["stale"]]
    print(json.dumps({"exportedAt": manifest["exportedAt"], "files": sorted(manifest["files"]),
                      "historyPages": sorted(manifest["history"]["pages"]),
                      "stale": stale, "builderFailures": manifest["builderFailures"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
