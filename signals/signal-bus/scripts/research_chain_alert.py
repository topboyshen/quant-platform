#!/usr/bin/env python3
"""Research-chain failure alerting v2: classify, describe, deduplicate.

Scope: failure detection, accurate wording, one alert per incident. It never
generates research, never relays, never approves, never resends a digest and
never widens a delivery window. Everything it reads is read-only.

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
        value = json.loads(path.read_bytes()[:limit])
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def file_sha256(path: Path, limit: int = 2 * 1024 * 1024) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return hashlib.sha256(path.read_bytes()[:limit]).hexdigest()
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
    draft = _read_json(root / "work/xhs/last-run.json")
    draft = draft if draft and draft.get("targetDate") == date else None
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
        # Reuse the relay's full current-day proof, not status=skipped alone.
        try:
            spec = importlib.util.spec_from_file_location("research_relay_alert_readonly",
                                                         Path(__file__).with_name("research_relay.py"))
            relay = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(relay)
            prepared = relay.prepare_research(root, now)
            if prepared.get("kind") == "skip" and prepared.get("target_date") == date:
                calendar_skip = prepared
        except Exception:
            calendar_skip = None
    return {"date": date, "now": now, "occurrence": occurrence, "last_run": last_run,
            "report_exists": report.is_file() and not report.is_symlink(),
            "report_sha256": report_sha, "seal": seal, "seal_mtime": seal_mtime,
            "draft": draft, "delivery": delivery, "relay_runs": parse_relay_runs(relay_log),
            "calendar_skip": calendar_skip}


def delivery_evidence(facts: dict) -> tuple[str, str, dict]:
    """Delivery state bound to date, research/skip identity and stored content."""
    record, date = facts["delivery"], facts["date"]
    if not isinstance(record, dict):
        return "unconfirmed", "尚未确认（无投递回执）", {}
    skip = facts.get("calendar_skip")
    identity_label = "休市证明" if skip else "报告"
    identity_key = "skip_sha256" if skip else "report_sha256"
    identity = skip.get("proof", {}).get(identity_key) if skip else facts.get("report_sha256")
    if not identity:
        return "unverified", "未核验（当前报告哈希不可用，不能确认投递对应本日研究）", {}
    status = str(record.get("status", ""))
    reason = str(record.get("reason", ""))
    bound = (record.get("target_date") == date
             and record.get("payload_kind", "research") == ("skip" if skip else "research")
             and record.get(identity_key) == identity)
    evidence = {"status": status, "reason": reason,
                "payload_kind": "skip" if skip else "research", identity_key: record.get(identity_key),
                "receiver_storage": record.get("receiver_storage"),
                "receiver_content_sha256": record.get("receiver_content_sha256"),
                "record_id": record.get("receiver_record_id")}
    payload_hash, receiver_hash = record.get("payload_sha256"), record.get("receiver_content_sha256")
    storage_verified = (record.get("receiver_storage") == "receiver_reported_stored"
                        or (record.get("confirmed_via") == "read_only_query"
                            and record.get("receiver_storage") != "receiver_reported_not_stored"))
    content_verified = (isinstance(payload_hash, str) and HASH_RE.fullmatch(payload_hash)
                        and isinstance(receiver_hash, str) and HASH_RE.fullmatch(receiver_hash)
                        and payload_hash == receiver_hash and record.get("content_match") is True)
    if status == "confirmed" and bound and storage_verified and content_verified:
        return "confirmed", f"已确认（reason={reason or 'accepted'}，日期与{identity_label}哈希匹配，接收端已保存且正文一致）", evidence
    if status == "confirmed" and bound:
        return "unverified", "未核验（旧确认记录缺少接收端存储或正文一致性证据），不自动重发", evidence
    if status == "confirmed" and not bound:
        return "uncertain", f"结果不确定（回执日期或{identity_label}哈希与当前合同不一致）", evidence
    if status in ("rejected", "not_sent", "failed") and bound:
        return "failed", f"明确失败（reason={reason or 'unknown'}）", evidence
    if status in ("sending", "ambiguous", "accepted"):
        return "uncertain", f"结果不确定（status={status}，reason={reason or 'unknown'}），不盲目重发", evidence
    return "uncertain", f"结果不确定（status={status or 'unknown'}）", evidence


def classify(facts: dict) -> list[dict]:
    """One incident per date, aggregating every detected cause."""
    now, date = facts["now"], facts["date"]
    deadline = now.replace(hour=PRE_DEADLINE[0], minute=PRE_DEADLINE[1], second=0, microsecond=0)
    window_end = now.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    state, wording, evidence = delivery_evidence(facts)
    causes: list[dict] = []

    def add(code: str, detail: str) -> None:
        if not any(item["code"] == code for item in causes):
            causes.append({"code": code, "detail": detail})

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

    draft = facts["draft"]
    if not calendar_skip and isinstance(draft, dict) and str(draft.get("status", "")) not in ("", "draft_ready"):
        add("draft_failed", f"XHS 草稿状态={draft.get('status')}，原因码={draft.get('reasonCodes') or ['未提供']}；"
                            "仅描述草稿环节，不能据此断言研究未投递。")

    latest = facts["relay_runs"][-1] if facts["relay_runs"] else None
    in_flight = bool(latest and latest.get("status") is None and latest.get("started")
                     and timedelta(0) <= (now - latest["started"]) <= timedelta(seconds=IN_FLIGHT_SECONDS))
    real_runs = [] if in_flight else [run for run in facts["relay_runs"] if run.get("status")]
    scheduled_gap = now < now.replace(hour=RELAY_FIRST_RUN[0], minute=RELAY_FIRST_RUN[1],
                                      second=0, microsecond=0)
    # If the current receipt proves success, historic research errors are not
    # current causes. A draft failure remains independently classified above.
    relay_ok, _ = _success_evidence(facts)
    for run in ([] if relay_ok else real_runs):
        reason = str(run.get("reason") or "")
        if run["status"] in ("held", "rejected", "not_sent", "failed", "already_failed") and reason not in BENIGN_REASONS:
            add("relay_blocked", f"relay 结果 status={run['status']} reason={reason or 'unparseable_output'}；"
                                 f"投递状态：{wording}。")
        elif run["status"] in ("ambiguous", "accepted", "already_accepted", "unknown"):
            add("relay_unconfirmed", f"relay 结果未确认：status={run['status']} reason={reason or 'unknown'}；{wording}。")
    if not relay_ok and isinstance(facts["delivery"], dict):
        reason = str(facts["delivery"].get("reason", ""))
        status = str(facts["delivery"].get("status", ""))
        if status in ("held", "rejected", "not_sent", "failed") and reason not in BENIGN_REASONS \
                and not any(item["code"] == "relay_blocked" for item in causes):
            add("relay_blocked", f"relay 状态文件 status={status} reason={reason or 'unknown'}；投递状态：{wording}。")
        elif status in ("ambiguous", "accepted"):
            add("relay_unconfirmed", f"relay 状态文件 status={status} reason={reason or 'unknown'}；投递状态：{wording}。")

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
    """Recovery requires a hash-bound seal and a matching confirmed receipt."""
    state, _, evidence = delivery_evidence(facts)
    seal_ok = valid_seal(facts["seal"], facts["report_sha256"], facts["date"])
    calendar_ok = bool(facts.get("calendar_skip"))
    ok = bool(state == "confirmed" and (seal_ok or calendar_ok))
    return ok, {"seal_bound": seal_ok, "calendar_bound": calendar_ok, "delivery_state": state, "receipt": evidence}


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


def process(incidents: list[dict], state_dir: Path, now: datetime, *, date: str,
            sender=None, dry_run: bool = False, success: bool = False,
            success_evidence: dict | None = None) -> dict:
    """Lock -> read -> persist "sending" -> send -> persist outcome.

    Never notifies blind after a crash, never retries an uncertain outcome, and
    pauses on corrupt state instead of treating it as empty.
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
            _append_log(state_dir, {"at": now.isoformat(), "event": "state_corrupt_paused", "file": path.name})
            summary["problems"].append("state_corrupt_paused")
            summary["paused"] = 1
            return summary

        incident = state.get("incident")
        attempts = [item for item in (incident or {}).get("attempts", []) if isinstance(item, dict)]
        # A crash between the send and its result must not cause a second send.
        interrupted = [item for item in attempts if item.get("outcome") == "sending"]
        if interrupted and incident is not None:
            attempts = [item for item in attempts if item.get("outcome") != "sending"]
            for item in interrupted:
                attempts.append({**item, "outcome": "uncertain", "detail": "crash_between_send_and_result"})
            incident["attempts"] = attempts
            summary["problems"].append("crash_recovered_as_uncertain")

        if not incidents and success:
            state["last_success"] = {"at": now.isoformat(), "evidence": success_evidence or {}}
            if incident is not None:
                incident["resolved_at"] = now.isoformat()
                incident["resolution"] = success_evidence or {}
                state["history"] = state.get("history", []) + [incident]
                state["incident"] = None
                state["notified_once"] = True          # dedupe evidence survives the day
                summary["recovered"] += 1
                _append_log(state_dir, {"at": now.isoformat(), "event": "recovered",
                                        "key": incident.get("key"),
                                        "was_notified": bool(incident.get("notified_at")),
                                        "causes": [cause["code"] for cause in incident.get("causes", [])
                                                   if isinstance(cause, dict)],
                                        "evidence": success_evidence or {}})
            _save_state(path, state)
            return summary

        if not incidents:
            _save_state(path, state)
            return summary

        item = incidents[0]
        research_success = bool(success and not any(cause["code"] == "draft_failed"
                                                    for cause in item["causes"]))
        if research_success:              # only research causes are cleared by success
            if incident is not None:
                incident["resolved_at"] = now.isoformat()
                incident["resolution"] = success_evidence or {}
                state["history"] = state.get("history", []) + [incident]
                state["incident"] = None
                state["notified_once"] = True
                summary["recovered"] += 1
                _append_log(state_dir, {"at": now.isoformat(), "event": "recovered",
                                        "key": incident.get("key"),
                                        "was_notified": bool(incident.get("notified_at")),
                                        "causes": [cause["code"] for cause in incident.get("causes", [])],
                                        "evidence": success_evidence or {}})
            _save_state(path, state)
            return summary

        if incident is None:
            incident = {"key": item["key"], "date": date, "first_seen": now.isoformat(),
                        "causes": item["causes"], "attempts": attempts}
            state["incident"] = incident
        if state.get("notified_once"):
            previous = {cause["code"] for cause in incident.get("causes", []) if isinstance(cause, dict)}
            incident["causes"] = item["causes"]            # new causes must still be recorded
            incident["last_seen"] = now.isoformat()
            incident["attempts"] = attempts
            incident["delivery_wording"] = item["delivery_wording"]
            incident.setdefault("suppressed_updates", []).append(
                {"at": now.isoformat(), "causes": [cause["code"] for cause in item["causes"]]})
            state["incident"] = incident
            summary["skipped"] += 1
            _append_log(state_dir, {"at": now.isoformat(), "event": "same_day_suppressed",
                                    "new_causes": sorted({cause["code"] for cause in item["causes"]} - previous),
                                    "causes": [cause["code"] for cause in item["causes"]]})
            _save_state(path, state)
            return summary
        else:
            incident["causes"] = item["causes"]          # merge new causes into one incident
            incident["last_seen"] = now.isoformat()
        incident["delivery_wording"] = item["delivery_wording"]
        incident["attempts"] = attempts

        notified = any(attempt.get("outcome") == "confirmed" for attempt in attempts)
        # An uncertain outcome (no platform ack, or a crash between send and
        # result) must never be retried automatically: the message may already
        # be delivered. Only a definite failure may be retried, up to the cap.
        uncertain = any(attempt.get("outcome") == "uncertain" for attempt in attempts)
        if notified or uncertain or len(attempts) >= MAX_ATTEMPTS:
            summary["skipped"] += 1
            _save_state(path, state)
            return summary

        incident["attempts"] = attempts + [{"at": now.isoformat(), "outcome": "sending"}]
        _save_state(path, state)                          # persisted before the send
        call = getattr(sender, "send", sender)
        outcome, detail, extra = call(_message(item))
        incident["attempts"] = attempts + [{"at": datetime.now(TZ).isoformat(), "outcome": outcome,
                                            "detail": detail, "evidence": extra}]
        if outcome == "confirmed":
            incident["notified_at"] = now.isoformat()
            state["notified_once"] = True
            summary["notified"] += 1
        else:
            summary["problems"].append(f"send_{outcome}")
        _save_state(path, state)
        _append_log(state_dir, {"at": now.isoformat(), "event": "send", "outcome": outcome,
                                "detail": detail, "key": item["key"]})
        return summary


def _message(item: dict) -> str:
    header = "【演练·非真实故障】研究链路告警通道自检" if item.get("drill") else f"【研究链路告警】{item['date']}"
    lines = [header,
             f"投递状态：{item['delivery_wording']}"]
    lines += [f"- {cause['detail']}" for cause in item["causes"]]
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
