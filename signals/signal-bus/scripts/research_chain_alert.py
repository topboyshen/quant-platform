#!/usr/bin/env python3
"""Research-chain failure alerting v2: classify, describe, deduplicate.

Scope: failure detection, accurate wording, one alert per stable fault transition. It never
generates research, never relays, never approves, never resends a digest and
never widens a delivery window. Everything it reads is read-only.
The automatic core chain covers generation, seal, Telegram and relay. XHS is
on demand; its command result does not participate in this chain's observations.

Design points that v1 got wrong and this version fixes:
  * A send is only "confirmed" with a platform acknowledgement. No ack means
    "uncertain" and is never retried automatically.
  * Research delivery evidence must bind date + report hash + receiver basis;
    a bare status=confirmed is not evidence.
  * Early relay rejections live in the unit's journal output, not in the
    delivery-state file, so both sources are read.
  * The seal is selected by the report hash and validated, never by filename
    ordering.
  * One incident per date ("research chain"), aggregating causes, so a
    generation failure and the compensations it blocks are a single alert.
    Later new/worsened faults and positively proven recoveries notify once per
    change; timestamps/run IDs do not change the deduplication identity.
  * Lock, read, persist "sending", send, then persist the outcome.
  * Corrupt state pauses automatic sending instead of re-notifying blind.
  * --dry-run performs no writes at all: no directories, no lock file.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(os.environ.get("RESEARCH_CHAIN_ROOT", "/home/ubuntu/AStock_AI_System"))
BUS = Path(os.environ.get("RESEARCH_CHAIN_BUS", "/home/ubuntu/signal-bus"))
STATE_DIR = Path(os.environ.get("RESEARCH_CHAIN_STATE", str(Path.home() / ".local/state/signal-bus")))

import importlib.util as _importlib_util
_RELAY_SPEC = _importlib_util.spec_from_file_location(
    "research_relay_ack", Path(__file__).resolve().parent / "research_relay.py")
_RELAY = _importlib_util.module_from_spec(_RELAY_SPEC)
assert _RELAY_SPEC and _RELAY_SPEC.loader
_RELAY_SPEC.loader.exec_module(_RELAY)
local_delivery_is_confirmed = _RELAY.local_delivery_is_confirmed
HERMES_PYTHON = Path(os.environ.get(
    "RESEARCH_CHAIN_HERMES_PYTHON", "/home/ubuntu/.hermes/hermes-agent/venv/bin/python"))
EXPECTED_CHAT = os.environ.get("RESEARCH_CHAIN_TELEGRAM_CHAT", "")
RELAY_UNIT = os.environ.get("RESEARCH_CHAIN_RELAY_UNIT", "nng8-research-digest")

PRE_DEADLINE = (8, 12)
RELAY_FIRST_RUN = (8, 5)
RELAY_LAST_RUN = (8, 10)
WINDOW_END = (8, 14)
IN_FLIGHT_SECONDS = 300
MAX_ATTEMPTS = 2

BENIGN_REASONS = {"delivery_busy", "already_confirmed"}
HASH_RE = re.compile(r"[a-f0-9]{64}\Z")


class Sender:
    """Injected in tests. Real implementation queries the platform ack."""

    def send(self, message: str) -> tuple[str, str, dict]:  # pragma: no cover - exercised live
        return "uncertain", "not_configured", {}


class HermesSender(Sender):
    """Send through the existing alert channel and require a platform ack."""

    def __init__(self, python: Path = HERMES_PYTHON, timeout: int = 60):
        self.python = python
        self.timeout = timeout

    def send(self, message: str) -> tuple[str, str, dict]:
        if not EXPECTED_CHAT:
            # Fail closed: without a configured target no ack can be validated.
            return "uncertain", "expected_chat_not_configured", {}
        try:
            result = subprocess.run(
                [str(self.python), "-m", "hermes_cli.main", "send", "--to", "telegram",
                 "--file", "-", "--json"],
                input=message + "\n", capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return "uncertain", "send_timeout", {"timeout": self.timeout}
        except OSError as exc:
            return "uncertain", f"spawn_failed:{type(exc).__name__}", {}
        return classify_send(result.returncode, result.stdout, EXPECTED_CHAT)


def classify_send(returncode: int, output: str, expected_chat: str = "") -> tuple[str, str, dict]:
    """Parse the CLI's whole stdout as JSON; only a validated ack confirms.

    The installed CLI prints a formatted multi-line JSON document, so the entire
    output is parsed instead of scanning lines. Anything without a valid ack --
    non-zero exit, error payload, unparseable output, missing or mismatched
    target -- stays "uncertain" and is never retried automatically: an error
    code alone cannot prove the whole message was not delivered.
    """
    text = (output or "").strip()
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return "uncertain", "unparseable_output", {"exit": returncode}
    success = payload.get("success") is True
    platform = str(payload.get("platform", "")).lower()
    chat_id = str(payload.get("chat_id", ""))
    message_id = payload.get("message_id")
    if success and platform == "telegram" and message_id not in (None, "") and str(message_id).isdigit():
        if not expected_chat:
            return "uncertain", "expected_chat_not_configured", {"chat_id": chat_id, "exit": returncode}
        if not chat_id:
            return "uncertain", "ack_missing_chat_id", {"exit": returncode}
        if chat_id != expected_chat:
            return "uncertain", "unexpected_chat", {"chat_id": chat_id, "exit": returncode}
        return "confirmed", "ack", {"message_id": str(message_id), "chat_id": chat_id, "exit": returncode}
    reason = payload.get("error") or payload.get("error_code")
    return "uncertain", f"no_ack_exit_{returncode}" + (f"_{reason}" if reason else ""), {"exit": returncode}


def _read_json(path: Path, limit: int = 512 * 1024) -> dict | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        with path.open("rb") as handle:
            body = handle.read(limit + 1)
        if len(body) > limit:
            return None
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def file_sha256(path: Path, limit: int = 2 * 1024 * 1024) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        with path.open("rb") as handle:
            body = handle.read(limit + 1)
        return hashlib.sha256(body).hexdigest() if len(body) <= limit else None
    except OSError:
        return None


def seal_for_report(seal_dir: Path, report_sha256: str | None) -> tuple[dict | None, Path | None]:
    """Bind the seal to the current report hash; validate instead of trusting JSON."""
    if not report_sha256 or not seal_dir.is_dir():
        return None, None
    candidate = seal_dir / f"{report_sha256}.json"
    seal = _read_json(candidate)
    if not isinstance(seal, dict):
        return None, None
    source = seal.get("source")
    checks = (seal.get("schemaVersion") == "astock.research-seal.v1",
              seal.get("kind") == "research_seal",
              seal.get("state") == "SEALED",
              isinstance(source, dict),
              isinstance(source, dict) and source.get("reportSha256") == report_sha256,
              isinstance(source, dict) and source.get("publicationGate") == "passed",
              isinstance(source, dict) and source.get("evidenceReview") == "passed")
    if not all(checks):
        return None, None
    return seal, candidate


def valid_seal(seal: dict | None, report_sha256: str | None, date: str = "") -> bool:
    if not isinstance(seal, dict) or not report_sha256:
        return False
    if date and seal.get("targetDate") != date:
        return False
    source = seal.get("source")
    return bool(seal.get("schemaVersion") == "astock.research-seal.v1"
                and seal.get("kind") == "research_seal"
                and seal.get("state") == "SEALED"
                and isinstance(source, dict)
                and source.get("reportSha256") == report_sha256
                and source.get("publicationGate") == "passed"
                and source.get("evidenceReview") == "passed")


def parse_relay_runs(text: str) -> list[dict]:
    """Parse short-iso journal lines into runs: start time, then its result.

    Each result binds to the most recent preceding start, so a finished 08:05 run
    can never mask an 08:10 run that started and has not reported yet.
    """
    runs: list[dict] = []
    for line in text.splitlines():
        stamp = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2})", line.strip())
        if "Starting" in line and RELAY_UNIT in line:
            started = None
            if stamp:
                offset = stamp.group(3)
                if ":" not in offset:
                    offset = f"{offset[:3]}:{offset[3:]}"
                started = datetime.fromisoformat(f"{stamp.group(1)}T{stamp.group(2)}{offset}")
            runs.append({"started": started, "status": None, "reason": None})
            continue
        if '"status"' not in line:
            continue
        blob = re.search(r"\{.*\}", line)
        payload = None
        if blob:
            try:
                payload = json.loads(blob.group(0))
            except ValueError:
                payload = None
        payload = payload if isinstance(payload, dict) else {"status": "held", "reason": "unparseable_output"}
        target = runs[-1] if runs else None
        if target is None or target.get("status") is not None:
            target = {"started": None, "status": None, "reason": None}
            runs.append(target)
        target["status"] = str(payload.get("status", "unknown"))
        target["reason"] = str(payload.get("reason", "")) or None
    return runs


def collect(root: Path = ROOT, bus: Path = BUS, date: str = "", now: datetime | None = None,
            relay_log: str | None = None) -> dict:
    """Read-only fact collection; never writes and never contacts the network."""
    now = now or datetime.now(TZ)
    occurrence = _read_json(root / "work/orchestrator/states" / f"morning-report-{date}.json")
    last_run = _read_json(root / "work/last-run.json")
    last_run = last_run if last_run and last_run.get("targetDate") == date else None
    report = root / "reports/daily" / f"{date}.md"
    report_sha = file_sha256(report)
    seal, seal_path = seal_for_report(root / "work/research-seal" / date, report_sha)
    seal_mtime = datetime.fromtimestamp(seal_path.stat().st_mtime, TZ) if seal_path else None
    delivery = _read_json(bus / "out/relay-delivery" / f"{date}.json")
    if relay_log is None:
        relay_log = ""
        try:
            relay_log = subprocess.run(
                ["journalctl", "--user", "-u", RELAY_UNIT, "--since", f"{date} 07:30",
                 "--until", f"{date} 09:30", "--no-pager", "-o", "short-iso"],
                capture_output=True, text=True, timeout=20, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            relay_log = ""
    calendar_skip = None
    if occurrence and occurrence.get("status") == "skipped":
        try:
            prepared = _RELAY.prepare_research(root, now)
            if prepared.get("kind") == "skip" and prepared.get("target_date") == date:
                calendar_skip = prepared
        except Exception:
            calendar_skip = None
    telegram_state, telegram_wording, telegram_evidence_value = telegram_delivery_evidence(
        root, date, report_sha)
    data_date = expected_data_date(root, date, report_sha)
    return {"date": date, "now": now, "occurrence": occurrence, "last_run": last_run,
            "report_exists": report.is_file() and not report.is_symlink(),
            "report_sha256": report_sha, "seal": seal, "seal_mtime": seal_mtime,
            "delivery": delivery, "relay_runs": parse_relay_runs(relay_log),
            "telegram_state": telegram_state, "telegram_wording": telegram_wording,
            "telegram_evidence": telegram_evidence_value, "data_date": data_date,
            "calendar_skip": calendar_skip}


MARKET_DATE_RE = re.compile(r"上一交易日 A 股行情[（(](\d{4}-\d{2}-\d{2})[）)]")
ISO_INSTANT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


def expected_data_date(root: Path, date: str, report_sha: str | None) -> str | None:
    """Independent expected date; a declared digest must satisfy its seal binding.

    Old seals without a digest may use their report heading. A missing, corrupt
    or conflicting declared digest never falls back to that legacy mechanism.
    """
    if not isinstance(report_sha, str) or not _RELAY.HASH_RE.fullmatch(report_sha):
        return None
    seal, _ = seal_for_report(root / "work/research-seal" / date, report_sha)
    source = seal.get("source", {}) if isinstance(seal, dict) else {}
    if "digestFile" in source or "digestSha256" in source:
        expected_path = f"work/research-seal/{date}/{report_sha}.digest.json"
        declared = source.get("digestSha256")
        if (not valid_seal(seal, report_sha, date) or source.get("digestFile") != expected_path
                or not isinstance(declared, str) or not _RELAY.HASH_RE.fullmatch(declared)):
            return None
        artifact = root / expected_path
        if file_sha256(artifact, _RELAY.MAX_DIGEST_BYTES) != declared:
            return None
        digest = _read_json(artifact, _RELAY.MAX_DIGEST_BYTES)
        if (not isinstance(digest, dict) or digest.get("schemaVersion") != _RELAY.DIGEST_SCHEMA
                or digest.get("kind") != _RELAY.DIGEST_KIND or digest.get("reportDate") != date
                or digest.get("reportSha256") != report_sha):
            return None
        data = digest.get("dataDate")
        try:
            valid = isinstance(data, str) and datetime.fromisoformat(data).date().isoformat() == data
        except ValueError:
            valid = False
        return data if valid and data <= date else None
    report = root / "reports/daily" / f"{date}.md"
    if report.is_file() and not report.is_symlink():
        try:
            text = report.read_text(encoding="utf-8")[:65536]
        except OSError:
            text = ""
        found = list(dict.fromkeys(MARKET_DATE_RE.findall(text)))
        if len(found) == 1 and found[0] < date:
            return found[0]
    return None


def _valid_iso_instant(value) -> bool:
    if not isinstance(value, str) or not ISO_INSTANT_RE.fullmatch(value):
        return False
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _exact_nonneg_int(value) -> bool:
    return type(value) is int and value >= 0


def telegram_delivery_evidence(root: Path, date: str, report_sha: str | None) -> tuple[str, str, dict]:
    """Expected Telegram send + receipt, bound to date, route, payload file, bytes and hashes."""
    event_id = f"astock-{date}-success-telegram-v1"
    notify_root = root / "work/notifications"
    sent_path = notify_root / "sent" / f"{event_id}.json"
    receipt_path = notify_root / "receipts/send" / f"{event_id}.json"
    evidence = {"event_id": event_id}
    try:
        _RELAY.checked_date(date)
    except _RELAY.Held:
        return "unconfirmed", "尚未确认（报告日期非法）", evidence
    if not isinstance(report_sha, str) or not _RELAY.HASH_RE.fullmatch(report_sha):
        return "unconfirmed", "尚未确认（密封报告哈希缺失或非法）", evidence
    for relative in ("", "sent", "receipts", "receipts/send", "payloads"):
        if (notify_root / relative).is_symlink():
            return "unconfirmed", "尚未确认（投递证据路径非法）", evidence
    if not sent_path.is_file() or sent_path.is_symlink():
        return "missing", "尚未确认（无 Telegram 发送记录）", evidence
    try:
        sent = json.loads(sent_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "unconfirmed", "尚未确认（发送记录无法核验）", evidence
    if not isinstance(sent, dict):
        return "unconfirmed", "尚未确认（发送记录无法核验）", evidence
    embedded = sent.get("receipt") if isinstance(sent.get("receipt"), dict) else {}
    if sent.get("outcome") != "success" or embedded.get("status") in ("failed", "rejected", "ambiguous") \
            or embedded.get("success") is False or embedded.get("confirmed") is False:
        return "mismatch", "结果不确定（发送记录含失败或未确认状态）", evidence
    if sent.get("schemaVersion") != "1.0" or sent.get("source") != "AStock_AI_System":
        return "unconfirmed", "尚未确认（发送记录类型非法）", evidence
    report = sent.get("report") if isinstance(sent.get("report"), dict) else {}
    source = report.get("sourceSha256")
    payload = report.get("sha256") or sent.get("payloadSha256")
    payload_rel = report.get("payloadFile")
    evidence.update({"sent_source_sha256": source, "sent_payload_sha256": payload,
                     "payload_file": payload_rel})
    if sent.get("eventId") != event_id or sent.get("targetDate") != date or sent.get("route") != "telegram":
        return "mismatch", "结果不确定（发送记录日期或路由与当日研究不一致）", evidence
    raw_original = ("presentation" not in report and "presentation" not in sent
                    and source is None and payload == report_sha)
    if source != report_sha and not raw_original:
        return "mismatch", "结果不确定（发送记录未绑定当日报告哈希）", evidence
    if payload_rel != f"payloads/{event_id}.md":
        return "unconfirmed", "尚未确认（发送记录未指向合法附件）", evidence
    payload_path = notify_root / payload_rel
    if not payload_path.is_file() or payload_path.is_symlink():
        return "unconfirmed", "尚未确认（Telegram 附件缺失）", evidence
    payload_bytes = payload_path.stat().st_size
    payload_hash = hashlib.sha256(payload_path.read_bytes()).hexdigest()
    evidence.update({"payload_bytes": payload_bytes, "payload_file_sha256": payload_hash})
    declared_bytes = report.get("bytes")
    if not _exact_nonneg_int(declared_bytes) or declared_bytes != payload_bytes or payload_hash != payload:
        return "mismatch", "结果不确定（发送记录与附件哈希或字节数冲突）", evidence
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return "unconfirmed", "尚未确认（已发送但无 Telegram 回执）", evidence
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "unconfirmed", "尚未确认（回执无法核验）", evidence
    if not isinstance(receipt, dict):
        return "unconfirmed", "尚未确认（回执无法核验）", evidence
    allowed = {"schemaVersion", "eventId", "targetDate", "route", "payloadSha256", "payloadBytes",
               "sourceSha256", "platformMessageId", "ackKind", "confirmedAt"}
    if (set(receipt) - allowed or receipt.get("schemaVersion") != "1.0"
            or receipt.get("ackKind") != "hermes_cli_json_message_id"):
        return "unconfirmed", "尚未确认（回执类型或字段非法）", evidence
    message_id = receipt.get("platformMessageId")
    evidence.update({"message_id": message_id, "receipt_target_date": receipt.get("targetDate"),
                     "receipt_source_sha256": receipt.get("sourceSha256"),
                     "receipt_payload_sha256": receipt.get("payloadSha256"),
                     "receipt_payload_bytes": receipt.get("payloadBytes")})
    if receipt.get("eventId") != event_id or receipt.get("targetDate") != date or receipt.get("route") != "telegram":
        return "mismatch", "结果不确定（回执日期或路由与当日研究不一致）", evidence
    if receipt.get("sourceSha256") != report_sha and not (raw_original and "sourceSha256" not in receipt):
        return "mismatch", "结果不确定（回执未绑定当日报告哈希）", evidence
    if receipt.get("payloadSha256") != payload or receipt.get("payloadSha256") != payload_hash:
        return "mismatch", "结果不确定（回执载荷哈希与发送记录或附件冲突）", evidence
    if not _exact_nonneg_int(receipt.get("payloadBytes")) or receipt.get("payloadBytes") != payload_bytes:
        return "mismatch", "结果不确定（回执字节数与附件冲突）", evidence
    if not isinstance(message_id, str) or not re.fullmatch(r"[1-9][0-9]*", message_id):
        return "ambiguous", "结果不确定（回执缺少平台消息 ID）", evidence
    if not _valid_iso_instant(receipt.get("confirmedAt")):
        return "unconfirmed", "尚未确认（回执确认时间非法）", evidence
    return "confirmed", f"已确认（Telegram message_id={message_id}，日期、附件哈希与字节数匹配）", evidence


def delivery_evidence(facts: dict) -> tuple[str, str, dict]:
    """Delivery state from the local receipt using the same strict confirmation rules."""
    record, date = facts["delivery"], facts["date"]
    if not isinstance(record, dict):
        return "unconfirmed", "尚未确认（无投递回执）", {}
    skip = facts.get("calendar_skip")
    if skip:
        ok, state, evidence = local_delivery_is_confirmed(
            record, report_date=date, payload_kind="skip",
            skip_sha256=skip.get("proof", {}).get("skip_sha256"), data_date=date)
    else:
        ok, state, evidence = local_delivery_is_confirmed(
            record, report_date=date, report_sha256=facts.get("report_sha256"),
            data_date=facts.get("data_date"))
    reason = str(record.get("reason", ""))
    status = str(record.get("status", ""))
    if ok:
        return "confirmed", f"已确认（reason={reason or 'accepted'}，日期、接收 ID、存储与内容哈希匹配）", evidence
    wording = {
        "unverified": "未核验（当前报告哈希不可用，不能确认投递对应本日研究）",
        "uncertain": "结果不确定（回执日期、报告哈希、接收 ID 或内容哈希不完整/冲突）",
        "failed": f"明确失败（reason={reason or 'unknown'}）",
        "unconfirmed": "尚未确认（缺接收 ID、存储证据或内容哈希，status 字符串不能代替接收证据）",
    }.get(state, f"结果不确定（status={status or 'unknown'}）")
    if state == "uncertain" and status in ("sending", "ambiguous"):
        wording = f"结果不确定（status={status}，reason={reason or 'unknown'}），不盲目重发"
    return state, wording, evidence


def classify(facts: dict) -> list[dict]:
    """One incident per date, aggregating every detected cause."""
    now, date = facts["now"], facts["date"]
    deadline = now.replace(hour=PRE_DEADLINE[0], minute=PRE_DEADLINE[1], second=0, microsecond=0)
    window_end = now.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    state, wording, evidence = delivery_evidence(facts)
    causes: list[dict] = []

    def add(code: str, detail: str, reason: str = "") -> None:
        safe_reason = reason if re.fullmatch(r"[a-z0-9_]{1,96}", reason) else ""
        if not any(item["code"] == code and item.get("reason", "") == safe_reason for item in causes):
            causes.append({"code": code, "reason": safe_reason, "detail": detail})

    occurrence, last_run = facts["occurrence"], facts["last_run"]
    calendar_skip = bool(facts.get("calendar_skip"))
    if isinstance(occurrence, dict) and occurrence.get("status") == "skipped" and not calendar_skip:
        add("calendar_skip_invalid", "跳过记录缺少当日哈希绑定的有效交易日历证明，不能认定今日无计划研究。")
    if isinstance(occurrence, dict) and (occurrence.get("status") == "failed"
                                         or occurrence.get("childExitCode") not in (0, None)):
        add("generation_failed", "研究生成任务失败（occurrence status=failed 或 childExitCode≠0），研究未产出。")
    elif isinstance(last_run, dict) and last_run.get("status") == "failed":
        add("generation_failed", "last-run 记录 status=failed，研究未产出。")
    elif not isinstance(occurrence, dict) and now >= deadline and now.weekday() < 5:
        add("generation_not_run", "截止前没有该日的 occurrence 记录；不据此断言当天必然缺报。")

    if not calendar_skip and facts["report_exists"] and isinstance(last_run, dict) and last_run.get("status") == "success" \
            and not valid_seal(facts["seal"], facts["report_sha256"], facts["date"]):
        add("seal_failed", "正式报告存在且 last-run 成功，但没有与该报告哈希绑定的有效研究密封"
                           "（以研究密封为准，不用 XHS 草稿判断）。")
    if facts["seal_mtime"] and window_end < facts["seal_mtime"] <= now:
        add("post_window_seal", f"研究密封生成于 {facts['seal_mtime'].strftime('%H:%M')}，已过投递窗口；"
                                "不追发当日日报，也不抹掉当日已缺报的事实。")

    latest = facts["relay_runs"][-1] if facts["relay_runs"] else None
    in_flight = bool(latest and latest.get("status") is None and latest.get("started")
                     and timedelta(0) <= (now - latest["started"]) <= timedelta(seconds=IN_FLIGHT_SECONDS))
    real_runs = [] if in_flight else [run for run in facts["relay_runs"] if run.get("status")]
    scheduled_gap = now < now.replace(hour=RELAY_FIRST_RUN[0], minute=RELAY_FIRST_RUN[1],
                                      second=0, microsecond=0)
    # A confirmed relay receipt supersedes old relay failures even while a
    # separate Telegram fault remains current.
    relay_ok = state == "confirmed"
    for run in ([] if relay_ok else real_runs):
        reason = str(run.get("reason") or "")
        if run["status"] in ("held", "rejected", "not_sent", "failed", "already_failed") and reason not in BENIGN_REASONS:
            add("relay_blocked", f"relay 结果 status={run['status']} reason={reason or 'unparseable_output'}；"
                                 f"投递状态：{wording}。", reason or "unparseable_output")
        elif run["status"] in ("ambiguous", "accepted", "already_accepted", "unknown"):
            add("relay_unconfirmed", f"relay 结果未确认：status={run['status']} reason={reason or 'unknown'}；{wording}。",
                reason or "unknown")
    if not relay_ok and isinstance(facts["delivery"], dict):
        reason = str(facts["delivery"].get("reason", ""))
        status = str(facts["delivery"].get("status", ""))
        if status in ("held", "rejected", "not_sent", "failed") and reason not in BENIGN_REASONS \
                and not any(item["code"] == "relay_blocked" for item in causes):
            add("relay_blocked", f"relay 状态文件 status={status} reason={reason or 'unknown'}；投递状态：{wording}。", reason or "unknown")
        elif status in ("ambiguous", "accepted"):
            add("relay_unconfirmed", f"relay 状态文件 status={status} reason={reason or 'unknown'}；投递状态：{wording}。",
                reason or "unknown")

    expected_telegram = (not calendar_skip and
        isinstance(occurrence, dict) and occurrence.get("status") == "success"
        and facts["report_exists"] and isinstance(last_run, dict) and last_run.get("status") == "success"
    )
    if expected_telegram:
        t_state = facts.get("telegram_state") or "missing"
        t_wording = facts.get("telegram_wording") or "尚未确认（无 Telegram 发送记录）"
        if t_state != "confirmed":
            code = {"missing": "telegram_missing", "unconfirmed": "telegram_unconfirmed",
                    "ambiguous": "telegram_ambiguous", "mismatch": "telegram_mismatch"}.get(
                t_state, "telegram_unconfirmed")
            if now >= window_end:
                add(code, f"Telegram 日报：{t_wording}。Hermes 连通性不能替代日报送达检查。")

    latest = facts["relay_runs"][-1] if facts["relay_runs"] else None
    in_flight = bool(latest and latest.get("status") is None and latest.get("started")
                     and timedelta(0) <= (now - latest["started"]) <= timedelta(seconds=IN_FLIGHT_SECONDS))
    ok, success_evidence = _success_evidence(facts)
    if not causes and not ok and now >= deadline and now < window_end and not in_flight and not scheduled_gap:
        add("predeadline_not_ready", ("距离日报窗口结束仅剩几分钟，休市证明尚未确认送达；" if calendar_skip else
                                      "距离日报窗口结束仅剩几分钟，研究尚未确认就绪；")
                                     + "不提前断言当天必然缺报。")
    if not causes and not ok and now >= window_end and now.weekday() < 5:
        add("delivery_unconfirmed", f"窗口结束后仍无确认证据；投递状态：{wording}。")

    if not causes:
        return []
    return [{"key": f"{date}:research_chain", "date": date, "causes": causes,
             "delivery_state": state, "delivery_wording": wording, "delivery_evidence": evidence,
             "success_evidence": success_evidence, "in_flight": in_flight}]


def _success_evidence(facts: dict) -> tuple[bool, dict]:
    """Recovery requires a hash-bound seal, a complete relay receipt, and Telegram."""
    state, _, evidence = delivery_evidence(facts)
    seal_ok = valid_seal(facts["seal"], facts["report_sha256"], facts["date"])
    calendar_ok = bool(facts.get("calendar_skip"))
    if calendar_ok:
        confirmed = state == "confirmed"
        return confirmed, {"seal_bound": False, "calendar_bound": True,
                           "delivery_state": state, "receipt": evidence,
                           "telegram_state": "not_required",
                           "stages": {"generation": True, "calendar": True,
                                      "relay": confirmed, "chain": confirmed}}
    telegram_state = facts.get("telegram_state")
    telegram_ok = telegram_state == "confirmed"
    occurrence, run = facts.get("occurrence"), facts.get("last_run")
    generation_ok = bool(isinstance(occurrence, dict) and occurrence.get("status") == "success"
                         and isinstance(run, dict) and run.get("status") == "success")
    stages = {"generation": generation_ok, "seal": seal_ok, "relay": state == "confirmed",
              "telegram": telegram_ok}
    ok = bool(generation_ok and state == "confirmed" and seal_ok and telegram_ok)
    return ok, {"seal_bound": seal_ok, "delivery_state": state, "receipt": evidence,
                "telegram_state": telegram_state,
                "telegram": facts.get("telegram_evidence") or {}, "stages": stages}


# --------------------------------------------------------------------- state

def _state_path(state_dir: Path, date: str) -> Path:
    return state_dir / f"research-chain-alert-{date}.json"


def _load_state(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {"version": 2, "incident": None}, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 2:
            raise ValueError("shape")
        incident = value.get("incident")
        if incident is not None:
            if not isinstance(incident, dict) or not isinstance(incident.get("key"), str):
                raise ValueError("incident")
            if "attempts" not in incident or not isinstance(incident["attempts"], list):
                raise ValueError("attempts")
            for attempt in incident["attempts"]:
                if not isinstance(attempt, dict) or attempt.get("outcome") not in (
                        "sending", "confirmed", "failed", "uncertain"):
                    raise ValueError("attempt")
            if "causes" not in incident or not isinstance(incident["causes"], list):
                raise ValueError("causes")
            for cause in incident["causes"]:
                if not isinstance(cause, dict) or not isinstance(cause.get("code"), str):
                    raise ValueError("cause")
        if "businessAtoms" in value and (not isinstance(value["businessAtoms"], list)
                or any(not isinstance(atom, str) or len(atom.split("|")) != 3
                       for atom in value["businessAtoms"])):
            raise ValueError("business atoms")
        if "atomDetails" in value and (not isinstance(value["atomDetails"], dict)
                or any(not isinstance(item, dict) or not isinstance(item.get("code"), str)
                       for item in value["atomDetails"].values())):
            raise ValueError("atom details")
        if "transitionCount" in value and (type(value["transitionCount"]) is not int or value["transitionCount"] < 0):
            raise ValueError("transition count")
        if "uncertainSend" in value and type(value["uncertainSend"]) is not bool:
            raise ValueError("uncertain send")
        event = value.get("transition")
        if event is not None:
            if (not isinstance(event, dict) or not isinstance(event.get("fingerprint"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", event["fingerprint"])
                    or not isinstance(event.get("attempts"), list)):
                raise ValueError("transition")
            if any(not isinstance(attempt, dict) or attempt.get("outcome") not in (
                    "sending", "confirmed", "failed", "uncertain") for attempt in event["attempts"]):
                raise ValueError("transition attempts")
        return value, None
    except (OSError, ValueError, RecursionError):
        return {"version": 2, "incident": None}, "state_corrupt"


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".rca-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_log(state_dir: Path, record: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "research-chain-alert.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _atom(cause: dict, date: str) -> str:
    code = cause.get("code", "")
    reason = cause.get("reason", "")
    reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,96}", reason) else ""
    return f"{date}|{code}|{reason}"


def _stage(atom: str) -> str:
    code = atom.split("|")[1]
    # Preserve legacy draft atoms as historical state; new core observations do
    # not produce or prove recovery of them. Do not turn scope removal into a
    # claim that an old on-demand command recovered.
    for prefix in ("generation", "seal", "relay", "telegram", "draft", "calendar"):
        if code.startswith(prefix):
            return prefix
    return "chain"


def process(incidents: list[dict], state_dir: Path, now: datetime, *, date: str,
            sender=None, dry_run: bool = False, success: bool = False,
            success_evidence: dict | None = None) -> dict:
    """Stable fault changes, evidence-only recovery, one locked send owner.

    Legacy state inherits its active causes. Disappearing/unreadable evidence
    cannot erase them. Every uncertain send pauses automatic sending; retry is
    allowed only for a definite failure, bounded per transition.
    """
    summary = {"checked": len(incidents), "notified": 0, "skipped": 0, "recovered": 0,
               "paused": 0, "problems": []}
    if dry_run:
        summary["notified"] = len(incidents)
        return summary
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "research-chain-alert.lock").open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            summary["problems"].append("delivery_busy")
            return summary
        path = _state_path(state_dir, date)
        state, problem = _load_state(path)
        if problem:
            summary.update(paused=1, problems=["state_corrupt_paused"])
            _append_log(state_dir, {"at": now.isoformat(), "event": "state_corrupt_paused", "file": path.name})
            return summary
        prior_incident = state.get("incident")
        old_causes = (prior_incident or {}).get("causes", [])
        prior = set(state.get("businessAtoms", [_atom(cause, date) for cause in old_causes]))
        details = dict(state.get("atomDetails") or {})
        details.update({_atom(cause, date): cause for cause in old_causes})
        item = incidents[0] if incidents else None
        incoming = {_atom(cause, date): cause for cause in (item or {}).get("causes", [])}
        evidence = success_evidence or {}
        full_recovery = bool(success and evidence.get("delivery_state") == "confirmed"
                             and ((evidence.get("seal_bound") is True
                                   and evidence.get("telegram_state") == "confirmed")
                                  or (evidence.get("calendar_bound") is True
                                      and evidence.get("telegram_state") == "not_required")))
        stages = evidence.get("stages") if isinstance(evidence.get("stages"), dict) else {}
        proven = {name for name, value in stages.items() if value is True}
        if full_recovery:
            proven.update(("generation", "relay", "chain", "calendar"))
            if evidence.get("calendar_bound") is not True:
                proven.update(("seal", "telegram"))
        # Genuine positive stage evidence, not the absence of an incident, is
        # the only basis for clearing a business atom.
        incoming = {atom: cause for atom, cause in incoming.items() if _stage(atom) not in proven}
        active = {atom for atom in prior if _stage(atom) not in proven} | set(incoming)
        added, recovered = active - prior, prior - active
        details.update(incoming)
        state["businessAtoms"] = sorted(active)
        state["atomDetails"] = {atom: details.get(atom, {"code": atom.split("|")[1], "detail": "既有故障证据待复核。"})
                                for atom in active}
        state["last_observed_at"] = now.isoformat()
        event = state.get("transition")
        legacy_attempts = (prior_incident or {}).get("attempts", [])
        all_attempts = list(legacy_attempts) + list((event or {}).get("attempts", []))
        if any(attempt.get("outcome") in ("sending", "uncertain") for attempt in all_attempts):
            state["uncertainSend"] = True
        for attempt in all_attempts:
            if attempt.get("outcome") == "sending":
                attempt.update(outcome="uncertain", detail="crash_between_send_and_result")
                if "crash_recovered_as_uncertain" not in summary["problems"]:
                    summary["problems"].append("crash_recovered_as_uncertain")
        migration_pending = bool(event is None and active and not any(
            attempt.get("outcome") in ("confirmed", "uncertain") for attempt in legacy_attempts))
        if added or recovered or migration_pending:
            count = state.get("transitionCount", 0) + 1
            state["transitionCount"] = count
            change = {"date": date, "before": sorted(prior), "after": sorted(active),
                      "added": sorted(added or (active if migration_pending else set())),
                      "recovered": sorted(recovered)}
            fingerprint = hashlib.sha256(json.dumps(change, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            event = {**change, "fingerprint": fingerprint, "sequence": count,
                     "attempts": list(legacy_attempts) if migration_pending and not added and not recovered else [],
                     "observed_at": now.isoformat()}
            state["transition"] = event
        if recovered:
            summary["recovered"] = 1
            _append_log(state_dir, {"at": now.isoformat(), "event": "recovered",
                                    "atoms": sorted(recovered), "evidence": evidence})
        if not active and prior_incident is not None:
            resolved = {**prior_incident, "resolved_at": now.isoformat(), "resolution": evidence}
            state["history"] = state.get("history", []) + [resolved]
            state["incident"] = None
            state["notified_once"] = True
        elif active:
            state["incident"] = {**(prior_incident or {}), "key": f"{date}:research_chain", "date": date,
                                 "first_seen": (prior_incident or {}).get("first_seen", now.isoformat()),
                                 "last_seen": now.isoformat(), "causes": list(state["atomDetails"].values()),
                                 "attempts": (event or {}).get("attempts", legacy_attempts),
                                 "delivery_wording": (item or {}).get("delivery_wording", "既有故障仍待完整证据确认。")}
        if full_recovery:
            state["last_success"] = {"at": now.isoformat(), "evidence": evidence}
        if state.get("uncertainSend"):
            summary["paused"] = 1
            summary["skipped"] = 1
            summary["problems"].append("uncertain_send_requires_review")
            _save_state(path, state)
            return summary
        if event is None:
            summary["skipped"] = int(bool(active))
            _save_state(path, state)
            return summary
        attempts = event["attempts"]
        if any(attempt.get("outcome") == "confirmed" for attempt in attempts) or len(attempts) >= MAX_ATTEMPTS:
            summary["skipped"] = 1
            _save_state(path, state)
            _append_log(state_dir, {"at": now.isoformat(), "event": "unchanged_suppressed",
                                    "fingerprint": event["fingerprint"]})
            return summary
        message_item = {**(item or {}), "date": date, "key": f"{date}:research_chain",
                        "causes": list(state["atomDetails"].values()),
                        "delivery_wording": (item or {}).get("delivery_wording", "已取得本次完整确认依据。"),
                        "added": event["added"], "recovered_atoms": event["recovered"]}
        event["attempts"].append({"at": now.isoformat(), "outcome": "sending"})
        _save_state(path, state)
        try:
            outcome, detail, extra = getattr(sender, "send", sender)(_message(message_item))
            if outcome not in ("confirmed", "failed", "uncertain"):
                outcome, detail, extra = "uncertain", "invalid_sender_outcome", {}
        except Exception:
            outcome, detail, extra = "uncertain", "sender_result_unknown", {}
        event["attempts"][-1].update(outcome=outcome, detail=detail, evidence=extra)
        if state.get("incident") is not None:
            state["incident"]["attempts"] = event["attempts"]
        if outcome == "confirmed":
            summary["notified"] = 1
            if state.get("incident") is not None:
                state["incident"]["notified_at"] = now.isoformat()
        else:
            summary["problems"].append(f"send_{outcome}")
            if outcome == "uncertain":
                state["uncertainSend"] = True
        _save_state(path, state)
        _append_log(state_dir, {"at": now.isoformat(), "event": "send", "outcome": outcome,
                                "detail": detail, "key": message_item["key"], "fingerprint": event["fingerprint"]})
        return summary


def _message(item: dict) -> str:
    recovered = item.get("recovered_atoms", [])
    label = "恢复" if recovered and not item.get("causes") else "告警"
    header = "【演练·非真实故障】研究链路告警通道自检" if item.get("drill") else f"【研究链路{label}】{item['date']}"
    lines = [header,
             f"投递状态：{item['delivery_wording']}"]
    lines += [f"- {cause['detail']}" for cause in item["causes"]]
    if recovered:
        lines.append("本次证据确认恢复：" + "、".join(sorted({atom.split("|")[1] for atom in recovered})))
    lines.append("（告警只做故障发现与描述；不重发日报、不追发、不改动研究与验签。）")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Research-chain failure alerting (read-only classification)")
    parser.add_argument("--phase", choices=("generation", "predeadline", "post", "drill"),
                        default="predeadline")
    parser.add_argument("--date")
    parser.add_argument("--now")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--relay-status", choices=("held", "rejected", "not_sent", "failed", "already_failed",
                                                   "ambiguous", "accepted", "already_accepted", "unknown"))
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now).astimezone(TZ) if args.now else datetime.now(TZ)
    date = args.date or now.date().isoformat()
    if args.phase == "drill":
        # A drill exercises the real sender and the real state machine, but in
        # its own state directory so it never consumes the day's alert quota.
        if args.state_dir == STATE_DIR:
            args.state_dir = STATE_DIR / f"drill-{date}"
        drill_incident = {"key": f"{date}:drill", "date": date, "drill": True,
                          "causes": [{"code": "drill", "detail": "通道自检：这是演练消息，不代表任何真实故障。"}],
                          "delivery_state": "n/a",
                          "delivery_wording": "演练（不涉及真实投递状态）",
                          "delivery_evidence": {}, "success_evidence": {}, "in_flight": False}
        summary = process([drill_incident], args.state_dir, now, date=f"drill-{date}",
                          sender=HermesSender(), dry_run=args.dry_run)
        payload = {"phase": "drill", "date": date, "dry_run": args.dry_run,
                   "state_dir": str(args.state_dir), "incidents": [{"key": drill_incident["key"],
                   "causes": ["drill"]}], **summary}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json
              else f"research_chain_alert phase=drill date={date} notified={summary['notified']} "
                   f"paused={summary['paused']}")
        if summary["paused"]:
            return 3
        return 0 if summary["notified"] == 1 else 1
    facts = collect(date=date, now=now)
    if args.relay_status and args.phase == "post":
        # The wrapper has the completed result before journald necessarily
        # exposes it. Carry only its bounded status; never accept body/secrets.
        runs = facts["relay_runs"]
        if not runs or runs[-1].get("status") != args.relay_status:
            runs.append({"started": None, "status": args.relay_status, "reason": None})
    incidents = classify(facts)
    if args.phase == "generation" and not incidents:
        incidents = [{"key": f"{date}:research_chain", "date": date,
                      "causes": [{"code": "generation_failed", "detail": "生成任务失败（OnFailure 触发）。"}],
                      "delivery_state": delivery_evidence(facts)[0],
                      "delivery_wording": delivery_evidence(facts)[1],
                      "delivery_evidence": delivery_evidence(facts)[2],
                      "success_evidence": {"seal_bound": facts["seal"] is not None,
                                           "delivery_state": delivery_evidence(facts)[0], "receipt": {}},
                      "in_flight": False}]
    success, success_evidence = _success_evidence(facts)
    summary = process(incidents, args.state_dir, now, date=date,
                      sender=HermesSender(), dry_run=args.dry_run,
                      success=success, success_evidence=success_evidence)
    payload = {"phase": args.phase, "date": date, "dry_run": args.dry_run,
               "incidents": [{"key": item["key"], "causes": [cause["code"] for cause in item["causes"]],
                              "delivery_wording": item["delivery_wording"]} for item in incidents],
               **summary}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json
          else f"research_chain_alert phase={args.phase} date={date} incidents={len(incidents)} "
               f"notified={summary['notified']} skipped={summary['skipped']} paused={summary['paused']} "
               f"recovered={summary['recovered']}")
    return 3 if summary["paused"] else 0


if __name__ == "__main__":
    sys.exit(main())
