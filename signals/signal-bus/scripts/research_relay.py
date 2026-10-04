#!/usr/bin/env python3
"""Relay only today's completed, sealed formal AStock research (stdlib only).

Import/prepare/dry-run are read-only; the CLI never prints report text, endpoint,
credentials or response bodies. No model, notification policy or XHS body is read.
"""
from __future__ import annotations

import argparse
import importlib.util
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import ssl
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path("/home/ubuntu/AStock_AI_System")
BUS_ROOT = Path("/home/ubuntu/signal-bus")
MAX_REPORT_BYTES = 1024 * 1024
MAX_SUMMARY_CHARS = 20000
MAX_SUMMARY_ITEM_CHARS = 1200
MAX_SUMMARY_LIST_ITEMS = 24
MAX_PAYLOAD_BYTES = 64000
MAX_DIGEST_BYTES = 1024 * 1024
DIGEST_SCHEMA = "astock.research-digest.v1"
DIGEST_KIND = "research_digest"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
REF_RE = re.compile(r"\[来源\s*([^\]\n]*)\]")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
URL_RE = re.compile(r"https?://[^\s<>\[\]）)]+", re.I)
EVIDENCE_ROLES = {"上涨占优", "下跌占优", "成交额", "单日行情", "报告期利润同比"}
INFERENCE_LIMIT = "未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。"
LEGACY_RISK_LIMIT = "未提供结构化反证来源，不升级为事实结论。"
CALENDAR_SOURCE_ID = "akshare:sina:trade-calendar"


class Held(Exception):
    """Fixed safe reason codes only, never artifact contents."""


def hold(reason):
    raise Held(reason)


REJECT_HTTP_STATUSES = (400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429)
PERMANENT_REJECT_HTTP_STATUSES = (400, 401, 403, 404, 405, 409, 410, 413, 415, 422)


def interpret_receiver_acknowledgement(record, *, source, report_date, data_date,
                                      expected_payload_sha256):
    """Shared strict receipt interpreter for POST, GET, alert and dashboard.

    Missing evidence stays unconfirmed. Explicit conflict is failed.
    Confirmation requires source, report date, data date, a non-empty record id,
    stored=true, and a legal content hash of the same sent object.
    """
    if not isinstance(record, dict):
        return {"verdict": "invalid", "reason": "acknowledgement_invalid"}
    try:
        if checked_date(data_date) > checked_date(report_date):
            return {"verdict": "invalid", "reason": "acknowledgement_invalid"}
    except Held:
        return {"verdict": "invalid", "reason": "acknowledgement_invalid"}
    if record.get("source") != source or record.get("status") != "accepted":
        return {"verdict": "invalid", "reason": "acknowledgement_invalid"}
    target = record.get("target_date") or record.get("report_date")
    day = record.get("day") if "day" in record else record.get("data_date")
    record_id = record.get("record_id") or record.get("recordId")
    stored = record.get("stored")
    ack_content = record.get("content_sha256") or record.get("contentSha256")
    evidence = {}
    if isinstance(record_id, str) and record_id.strip():
        evidence["receiver_record_id"] = record_id.strip()
    if stored is True:
        evidence["receiver_storage"] = "receiver_reported_stored"
    elif stored is False:
        evidence["receiver_storage"] = "receiver_reported_not_stored"
    else:
        evidence["receiver_storage"] = "not_in_acknowledgement"
    if isinstance(ack_content, str) and ack_content:
        evidence["receiver_content_sha256"] = ack_content
    if not isinstance(target, str) or not target:
        return {"verdict": "unconfirmed", "reason": "acknowledgement_target_date_missing", **evidence}
    if target != report_date:
        return {"verdict": "failed", "reason": "acknowledgement_target_date_mismatch", **evidence}
    if not isinstance(day, str) or not day:
        return {"verdict": "unconfirmed", "reason": "acknowledgement_day_missing", **evidence}
    if day != data_date:
        return {"verdict": "failed", "reason": "acknowledgement_day_mismatch", **evidence}
    if stored is False:
        return {"verdict": "failed", "reason": "receiver_reported_not_stored",
                "content_match": False if ack_content else None, **evidence}
    if stored is not True:
        return {"verdict": "unconfirmed", "reason": "http_accepted_storage_unconfirmed",
                "content_match": None, **evidence}
    if "receiver_record_id" not in evidence:
        return {"verdict": "unconfirmed", "reason": "acknowledgement_record_id_missing", **evidence}
    if not isinstance(ack_content, str) or not ack_content:
        return {"verdict": "unconfirmed", "reason": "http_accepted_storage_unconfirmed",
                "content_match": None, **evidence}
    if not HASH_RE.fullmatch(ack_content):
        return {"verdict": "failed", "reason": "acknowledgement_content_hash_invalid",
                "content_match": False, **evidence}
    if not isinstance(expected_payload_sha256, str) or not HASH_RE.fullmatch(expected_payload_sha256):
        return {"verdict": "unconfirmed", "reason": "http_accepted_storage_unconfirmed",
                "content_match": None, **evidence}
    if ack_content != expected_payload_sha256:
        return {"verdict": "failed", "reason": "content_mismatch",
                "content_match": False, **evidence}
    result = {"verdict": "confirmed", "reason": "accepted", "content_match": True, **evidence}
    if isinstance(record.get("stored_at"), str) and record["stored_at"]:
        result["stored_at"] = record["stored_at"]
    return result


# One pure proof contract, deliberately separate from run_relay's resend suppression.
try:
    _DELIVERY_SPEC = importlib.util.spec_from_file_location(
        "signal_delivery_evidence", Path(__file__).resolve().with_name("delivery_evidence.py"))
    if _DELIVERY_SPEC is None or _DELIVERY_SPEC.loader is None:
        raise ValueError("delivery_contract_unavailable")
    _DELIVERY = importlib.util.module_from_spec(_DELIVERY_SPEC)
    _DELIVERY_SPEC.loader.exec_module(_DELIVERY)
    if type(_DELIVERY.CONTRACT_VERSION) is not int or _DELIVERY.CONTRACT_VERSION != 1 \
            or not all(callable(getattr(_DELIVERY, name, None)) for name in
                       ("local_delivery_is_confirmed", "telegram_verdict")):
        raise ValueError("delivery_contract_unavailable")
except Exception:
    # A partial installation cannot turn incomplete proof into success or crash
    # the alert reader. This says nothing about permission to send/retry.
    _DELIVERY = None


def local_delivery_is_confirmed(record, **expected):
    if _DELIVERY is None:
        return False, "unverified", {"contract_status": "insufficient",
                                      "contract_reason": "delivery_contract_unavailable"}
    return _DELIVERY.local_delivery_is_confirmed(record, **expected)


def delivery_decision(record, **expected):
    """Shared resend-hold vs evidence decision; fails toward hold + unconfirmed."""
    if _DELIVERY is None or not callable(getattr(_DELIVERY, "relay_delivery_decision", None)):
        status = record.get("status") if isinstance(record, dict) else None
        return {"doNotResend": isinstance(record, dict) and status != "not_sent",
                "manualReviewRequired": status in ("sending", "ambiguous"), "recordedStatus": status,
                "evidenceConfirmed": False,
                "proof": {"status": "insufficient", "reason": "delivery_contract_unavailable", "state": "unverified"}}
    return _DELIVERY.relay_delivery_decision(record, **expected)


def _expected_proof(prepared):
    kind = prepared.get("kind", "research")
    if kind == "skip":
        return {"report_date": prepared["target_date"], "payload_kind": "skip",
                "skip_sha256": prepared["proof"]["skip_sha256"], "data_date": prepared["as_of"]}
    return {"report_date": prepared["target_date"], "report_sha256": prepared["proof"]["report_sha256"],
            "data_date": prepared["as_of"]}


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def parse_instant(value):
    if not isinstance(value, str) or not ISO_RE.fullmatch(value):
        hold("timestamp_invalid")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        hold("timestamp_invalid")
    return result.astimezone(dt.timezone.utc)


def observation_time(now):
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        hold("observation_time_invalid")
    return now.astimezone(dt.timezone.utc)


def checked_date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return dt.date.fromisoformat(value)
    except ValueError:
        hold("date_invalid")


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_root(root):
    root = Path(os.path.abspath(root))
    for candidate in reversed((root, *root.parents)):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            hold("artifact_path_invalid")
    return root


def read_artifact(root, relative, limit=65536):
    """Fixed relative paths, no symlink traversal; detect replacement/change."""
    root = _safe_root(root)
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ("..", ".") for p in parts):
        hold("artifact_path_invalid")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        descriptor = os.open(root, directory_flags)
        stack.callback(os.close, descriptor)
        for component in parts[:-1]:
            descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        stack.callback(os.close, file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            hold("artifact_file_invalid")
        chunks, total = [], 0
        while True:
            chunk = os.read(file_fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                hold("artifact_file_invalid")
        after = os.fstat(file_fd)
        current = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        body = b"".join(chunks)
        if not body or _fingerprint(before) != _fingerprint(after) or _fingerprint(after) != _fingerprint(current):
            hold("artifact_changed")
        return {"body": body, "sha256": sha256(body), "fingerprint": _fingerprint(after)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            hold("artifact_json_invalid")
        result[key] = value
    return result


def decode_json(body):
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: hold("artifact_json_invalid"))
    except (UnicodeError, ValueError, RecursionError):
        hold("artifact_json_invalid")
    if not isinstance(value, dict):
        hold("artifact_json_invalid")
    return value


def read_optional_json(root, relative, limit=65536):
    """Return None only when the artifact is absent; invalid artifacts still hold."""
    try:
        return read_json(root, relative, limit)
    except FileNotFoundError:
        return None


def read_json(root, relative, limit=65536):
    result = read_artifact(root, relative, limit)
    return {**result, "value": decode_json(result["body"])}


def _sections(report):
    sections = {}
    active = None
    for line in report.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            number = re.match(r"(\d+)\.", title)
            active = number.group(1) if number else title
            if active in sections:
                hold("report_section_ambiguous")
            sections[active] = {"title": title, "lines": []}
        elif active is not None:
            sections[active]["lines"].append(line)
    return sections


def _blocks(lines):
    """Preserve all continuation lines; never slice away a counterargument."""
    blocks, current = [], []
    for line in lines:
        if re.match(r"^-\s+", line) and current:
            blocks.append("\n".join(current).strip())
            current = []
        if line.strip():
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return blocks


def references_in(value):
    """Accept explicit numeric reference lists; never ignore an unknown member."""
    matches = list(REF_RE.finditer(value))
    if len(matches) != value.count("[来源"):
        hold("source_reference_invalid")
    references = []
    for match in matches:
        expression = match.group(1).strip()
        if not re.fullmatch(r"[1-9]\d*(?:(?:\s*[、,，]\s*|\s+)[1-9]\d*)*", expression):
            hold("source_reference_invalid")
        references.extend(re.findall(r"\d+", expression))
    return list(dict.fromkeys(references))


def _safe_source_urls(value):
    changed = False

    def replace(match):
        nonlocal changed
        raw = match.group(0).rstrip("。，；;")
        suffix = match.group(0)[len(raw):]
        try:
            url = urllib.parse.urlsplit(raw)
            host = url.hostname or ""
            if url.scheme not in ("https", "http") or not host or url.username or url.password:
                raise ValueError
            if host.lower() == "localhost" or host.lower().endswith((".local", ".internal")):
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError
            # Never include query/fragment credentials in a report relay.
            safe = urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, "", ""))
            changed |= safe != raw
            return safe + suffix
        except (ValueError, UnicodeError):
            changed = True
            return "[链接已隐藏]" + suffix

    return URL_RE.sub(replace, value), changed


def extract_summary(report, target_date, data_status, semantic_review):
    sections = _sections(report)
    if not all(key in sections for key in ("2", "6", "7", "来源")):
        hold("report_sections_missing")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", sections["2"]["title"])
    if len(dates) != 1 or checked_date(dates[0]) >= checked_date(target_date):
        hold("market_date_invalid")
    as_of = dates[0]
    analysis_blocks = [block for block in _blocks(sections["6"]["lines"])
                       if re.match(r"^-\s+\[推断\]", block)]
    limitations = []
    analysis_contract = None
    evidence, counter = [], []
    if analysis_blocks:
        chosen = next((b for b in analysis_blocks if re.match(r"^-\s+\[推断\]\s*摘要[：:]", b)), analysis_blocks[0])
        conclusion_lines = []
        destination = conclusion_lines
        evidence_roles = []
        period_note = None
        inference_limit = None
        for line in chosen.splitlines():
            stripped = re.sub(r"^\s*-\s*", "", line).strip()
            support = re.match(r"^支持证据(?:（([^）]+)）)?\s*[：:]", stripped)
            if support:
                role = support.group(1)
                if role is not None:
                    if role not in EVIDENCE_ROLES:
                        hold("analysis_evidence_role_invalid")
                    refs = references_in(stripped)
                    if len(refs) != 1:
                        hold("analysis_evidence_role_invalid")
                    evidence_roles.append({"source_id": refs[0], "role": role})
                destination = evidence
                destination.append(stripped)
            elif stripped.startswith("支持证据"):
                hold("analysis_evidence_role_invalid")
            elif stripped.startswith("期间："):
                period = re.fullmatch(r"期间：行情 (\d{4}-\d{2}-\d{2})；利润同比报告期 (\d{4}-\d{2}-\d{2})。", stripped)
                if not period or period_note is not None:
                    hold("analysis_period_invalid")
                if any(checked_date(value) >= checked_date(target_date) for value in period.groups()):
                    hold("analysis_period_invalid")
                period_note = stripped[len("期间："):-1]
            elif stripped.startswith("推断限制："):
                if stripped != "推断限制：" + INFERENCE_LIMIT or inference_limit is not None:
                    hold("analysis_limit_invalid")
                inference_limit = INFERENCE_LIMIT
            elif re.match(r"反证\s*[：:]", stripped):
                destination = counter
                destination.append(stripped)
            elif destination is conclusion_lines:
                destination.append(stripped)
            elif destination:
                # A continuation belongs to the same evidence/counter-evidence
                # item. Splitting it would sever its source attribution.
                destination[-1] += "\n" + stripped
        conclusion = "\n".join(conclusion_lines)
        if len(analysis_blocks) > 1:
            limitations.append(f"摘要保留一个完整分析块及其支持证据、反证；其余 {len(analysis_blocks) - 1} 个分析块未摘录。")
        if not evidence:
            hold("analysis_support_missing")
        if any(not references_in(value) for value in evidence):
            hold("analysis_support_missing")
        if not counter and inference_limit is None:
            hold("analysis_counter_missing")
        if counter and inference_limit is not None:
            hold("analysis_limit_conflict")
        original_counter = counter
        counter = []
        for value in original_counter:
            if references_in(value):
                counter.append(value)
                continue
            if (not re.match(r"^\[推断\]\s*风险[：:]", conclusion)
                    or len(original_counter) != 1
                    or re.fullmatch(r"(?:反证[：:]\s*)?未提供结构化反证来源，不升级为事实结论。", value) is None):
                hold("analysis_counter_missing")
            inference_limit = LEGACY_RISK_LIMIT
            limitations.append("原风险块明确未提供结构化反证来源；该缺失提示不是已核验反证，未补造来源。")
        if inference_limit is not None:
            if inference_limit == INFERENCE_LIMIT and not evidence_roles:
                hold("analysis_evidence_role_invalid")
            limitations.append("推断限制：" + inference_limit)
        if period_note is not None:
            limitations.append("期间：" + period_note + "。")
        analysis_contract = {"version": 2, "claim_type": "inference",
                             "inference_status": "unverified",
                             "counter_evidence_status": "provided" if counter else "explicitly_missing",
                             "evidence_roles": evidence_roles, "period_note": period_note,
                             "inference_limit": inference_limit}
    else:
        base = sections.get("一句话结论", {}).get("lines", [])
        conclusion_lines = [line.strip() for line in base if line.strip()]
        if conclusion_lines:
            # Normalize only the leading bullet of an already labelled headline.
            # Never invent a label or rewrite a continuation's content.
            conclusion_lines[0] = re.sub(
                r"^[-+*][ \t]+(?=\[(?:事实|推断)\])", "", conclusion_lines[0], count=1)
        conclusion = "\n".join(conclusion_lines)
        if not conclusion:
            conclusion = "[事实] 本次为程序核验基础版，没有可转发的结构化研究结论。"
        evidence = _blocks(sections["2"]["lines"])
        limitations.append("基础版：第 6 节未提供结构化分析块；仅保留原文结论和行情事实，不生成因果判断。")
    risks = _blocks(sections["7"]["lines"])
    if not risks:
        hold("risk_section_empty")
    if not re.match(r"^\[(?:事实|推断)\]", conclusion):
        hold("conclusion_label_missing")
    if not references_in(conclusion):
        # The same sealed block provides these references. Copy them explicitly
        # instead of inventing attribution or presenting an unsupported headline.
        supporting_references = references_in("\n".join(evidence))
        if not supporting_references:
            hold("conclusion_reference_missing")
        conclusion += "\n支持证据引用：" + "、".join(f"[来源 {item}]" for item in supporting_references)
    if data_status == "partial":
        limitations.append("数据状态 partial：部分来源或字段不可用，具体缺口见风险；不可解释为完整研究。")
    if semantic_review != "human_passed":
        limitations.append("研究语义未通过人工审核；结构、来源和时效门禁通过不代表投资结论已经验证。")
    limitations.append("仅供研究整理，不是交易指令；封存草稿仍需人工批准，不发布 XHS 内容。")

    # Keep the full entry corresponding to each retained reference.
    sources_by_id, active = {}, None
    for line in sections["来源"]["lines"]:
        if line.strip() == "---":
            break
        entry = re.match(r"^\s*(\d+)\.\s+(.+)", line)
        if entry:
            active = entry.group(1)
            if active in sources_by_id:
                hold("source_reference_ambiguous")
            sources_by_id[active] = line.strip()
        elif active and line.strip():
            sources_by_id[active] += "\n" + line.strip()
    retained = "\n".join([conclusion, *evidence, *counter, *risks])
    references = references_in(retained)
    if any(reference not in sources_by_id for reference in references):
        hold("source_reference_missing")
    if not references:
        hold("conclusion_reference_missing")
    sources = [sources_by_id[reference] for reference in references]
    summary = {"conclusion": conclusion, "evidence": evidence, "counter_evidence": counter,
               "risks": risks, "sources": sources, "limitations": limitations}
    changed = False
    for key, value in list(summary.items()):
        if isinstance(value, str):
            summary[key], altered = _safe_source_urls(value)
            changed |= altered
        else:
            cleaned = []
            for text in value:
                safe, altered = _safe_source_urls(text)
                cleaned.append(safe)
                changed |= altered
            summary[key] = cleaned
    if changed:
        summary["limitations"].append("来源链接已移除查询参数、片段或不安全地址；未转发链接中的凭据。")
    if (len(summary["conclusion"]) > MAX_SUMMARY_ITEM_CHARS
            or any(len(values) > MAX_SUMMARY_LIST_ITEMS or any(len(value) > MAX_SUMMARY_ITEM_CHARS for value in values)
                   for key, values in summary.items() if key != "conclusion")
            or len(json.dumps(summary, ensure_ascii=False)) > MAX_SUMMARY_CHARS):
        hold("research_summary_too_large")
    if analysis_contract is not None:
        summary["analysis_contract"] = analysis_contract
    if len(json.dumps(summary, ensure_ascii=False)) > MAX_SUMMARY_CHARS:
        hold("research_summary_too_large")
    return as_of, summary


def extract_summary_from_digest(digest, target_date, data_status, semantic_review):
    """Build the relay summary from the sealed structured digest alone.

    Dates, evidence references and limitations come from the digest that the
    sealing step bound to this same report and compilation input. The report body
    is never parsed here, and missing real counter-evidence stays a limitation
    instead of being invented.
    """
    data_date = digest.get("dataDate")
    if (not isinstance(data_date, str) or not DATE_RE.fullmatch(data_date)
            or checked_date(data_date) > checked_date(target_date)):
        hold("digest_data_date_invalid")
    items = digest.get("items")
    if not isinstance(items, list):
        hold("digest_items_invalid")
    index = {}
    for entry in digest.get("sourceIndex") or []:
        if not isinstance(entry, dict):
            hold("digest_source_index_invalid")
        number, source_id = entry.get("number"), entry.get("sourceId")
        if (isinstance(number, bool) or not isinstance(number, int)
                or not isinstance(source_id, str) or not source_id or number in index):
            hold("digest_source_index_invalid")
        index[number] = entry
    declared = [value for value in (digest.get("limitations") or []) if isinstance(value, str) and value.strip()]
    # R08: the producer emits a limitation only when an item really lacks counter
    # evidence (or when there are no items at all). A digest whose every item
    # carries counter-evidence is legitimate with an empty list.
    needs_declared = (not items) or any(
        not (item.get("counterEvidence") or []) for item in items if isinstance(item, dict))
    if not declared and needs_declared:
        hold("digest_limitations_missing")

    def reference_number(value):
        match = re.match(r"^\[来源 (\d+)\]", str(value))
        if not match:
            hold("digest_reference_invalid")
        return int(match.group(1))

    def support_line(entry):
        if not isinstance(entry, dict):
            hold("digest_reference_invalid")
        reference, role = entry.get("reference"), entry.get("role")
        if not isinstance(reference, str):
            hold("digest_reference_invalid")
        reference_number(reference)
        label = f"支持证据（{role}）：" if isinstance(role, str) and role.strip() else "支持证据："
        return label + reference

    limitations = []
    analysis_contract = None
    chosen = next((item for item in items if isinstance(item, dict) and item.get("kind") == "summary"), None)
    if chosen is None:
        chosen = next((item for item in items if isinstance(item, dict)), None)
    if chosen is None:
        lines = [str(line).strip() for line in (digest.get("conclusionLines") or []) if str(line).strip()]
        if not lines:
            hold("digest_conclusion_missing")
        conclusion = "\n".join(lines)
        facts = [str(line).strip() for line in (digest.get("facts") or []) if str(line).strip()]
        evidence = [line for line in [*lines, *facts] if re.search(r"\[来源 \d+\]", line)]
        counter = []
        limitations.append("基础版：封存摘要没有结构化推断条目；仅保留事实结论与限制，不生成因果判断。")
    else:
        inference = chosen.get("inference")
        if not isinstance(inference, str) or not inference.strip():
            hold("digest_inference_invalid")
        # R09: keep the subject (stock code / label) and the explicit period with the
        # conclusion, so the final payload cannot lose who and when it is about.
        subject = None
        if isinstance(chosen.get("code"), str) and chosen["code"].strip():
            subject = chosen["code"].strip()
        elif isinstance(chosen.get("label"), str) and chosen["label"].strip():
            subject = chosen["label"].strip()
        conclusion = f"[推断] {subject}：{inference.strip()}" if subject else f"[推断] {inference.strip()}"
        period = chosen.get("periodNote")
        if isinstance(period, str) and period.strip():
            period = period.strip()
            period_match = re.fullmatch(r"行情 (\d{4}-\d{2}-\d{2})；利润同比报告期 (\d{4}-\d{2}-\d{2})", period)
            if not period_match or any(checked_date(value) >= checked_date(target_date) for value in period_match.groups()):
                hold("analysis_period_invalid")
            conclusion += f"（期间：{period}）"
            limitations.append("期间：" + period + "。")
        else:
            period = None
        evidence = [support_line(entry) for entry in chosen.get("evidence") or []]
        if not evidence:
            hold("analysis_support_missing")
        evidence_roles = []
        for entry in chosen.get("evidence") or []:
            role = entry.get("role")
            if role is not None:
                if role not in EVIDENCE_ROLES:
                    hold("analysis_evidence_role_invalid")
                evidence_roles.append({"source_id": str(reference_number(entry["reference"])), "role": role})
        counter = []
        for entry in chosen.get("counterEvidence") or []:
            reference = entry.get("reference") if isinstance(entry, dict) else None
            if not isinstance(reference, str):
                hold("digest_reference_invalid")
            reference_number(reference)
            counter.append(f"反证：{reference}")
        limitation = chosen.get("limitation")
        if not counter:
            if limitation not in (INFERENCE_LIMIT, LEGACY_RISK_LIMIT):
                hold("analysis_limit_invalid")
            if limitation == INFERENCE_LIMIT and not evidence_roles:
                hold("analysis_evidence_role_invalid")
            limitations.append("推断限制：" + limitation)
        elif limitation is not None:
            hold("analysis_limit_conflict")
        analysis_contract = {"version": 2, "claim_type": "inference", "inference_status": "unverified",
                             "counter_evidence_status": "provided" if counter else "explicitly_missing",
                             "evidence_roles": evidence_roles, "period_note": period,
                             "inference_limit": limitation}
    limitations.extend(value for value in declared if value not in limitations)
    if not re.search(r"\[来源 \d+\]", conclusion):
        numbers = [int(number) for line in evidence for number in re.findall(r"\[来源 (\d+)\]", line)]
        if not numbers:
            hold("conclusion_reference_missing")
        conclusion += "\n支持证据引用：" + "、".join(f"[来源 {number}]" for number in numbers)
    risks = [f"[限制] {value}" for value in limitations]
    if data_status == "partial":
        limitations.append("数据状态 partial：部分来源或字段不可用，具体缺口见风险；不可解释为完整研究。")
    if semantic_review != "human_passed":
        limitations.append("研究语义未通过人工审核；结构、来源和时效门禁通过不代表投资结论已经验证。")
    limitations.append("仅供研究整理，不是交易指令；封存草稿仍需人工批准，不发布 XHS 内容。")
    retained = "\n".join([conclusion, *evidence, *counter, *risks])
    referenced = sorted({int(value) for value in re.findall(r"\[来源 (\d+)\]", retained)})
    if not referenced:
        hold("conclusion_reference_missing")
    if any(number not in index for number in referenced):
        hold("source_reference_missing")
    sources = [f"{number}. {index[number].get('title') or index[number]['sourceId']}"
               + (f"（{index[number]['url']}）" if index[number].get("url") else "")
               for number in referenced]
    summary = {"conclusion": conclusion, "evidence": evidence, "counter_evidence": counter,
               "risks": risks, "sources": sources, "limitations": limitations}
    # R07: the send boundary sanitizes URLs for both paths (digest and legacy body).
    # Query/fragment credentials, userinfo and non-public hosts never leave here.
    changed = False
    for key, value in list(summary.items()):
        if key == "conclusion":
            summary[key], altered = _safe_source_urls(value)
            changed |= altered
        else:
            cleaned = []
            for text in value:
                safe, altered = _safe_source_urls(text)
                cleaned.append(safe)
                changed |= altered
            summary[key] = cleaned
    if changed:
        summary["limitations"].append("来源链接已移除查询参数、片段或不安全地址；未转发链接中的凭据。")
    if (len(summary["conclusion"]) > MAX_SUMMARY_ITEM_CHARS
            or any(len(value) > MAX_SUMMARY_ITEM_CHARS for key, values in summary.items() if key != "conclusion" for value in values)
            or len(json.dumps(summary, ensure_ascii=False)) > MAX_SUMMARY_CHARS):
        hold("research_summary_too_large")
    if analysis_contract is not None:
        summary["analysis_contract"] = analysis_contract
        if len(json.dumps(summary, ensure_ascii=False)) > MAX_SUMMARY_CHARS:
            hold("research_summary_too_large")
    return data_date, summary


def occurrence_cutoff(occurrence, target):
    """Validate the versioned cutoff claim without repairing mixed records."""
    version = occurrence.get("schemaVersion")
    if version == "1.0" and "frozenCutoffInstant" not in occurrence:
        # v1's skip proof used startedAt; its research path keeps its existing
        # sealed-run checks. Never reinterpret a v1 record containing a v2 field.
        return occurrence.get("startedAt")
    if version != "2.0":
        hold("occurrence_version_invalid")
    value = occurrence.get("frozenCutoffInstant")
    try:
        cutoff = parse_instant(value)
        started = parse_instant(occurrence.get("startedAt"))
    except Held:
        hold("occurrence_cutoff_invalid")
    if cutoff.astimezone(SHANGHAI).date().isoformat() != target or cutoff > started:
        hold("occurrence_cutoff_invalid")
    # Schema v2 defines the fixed Shanghai 08:00 schedule in canonical UTC.
    # Do not reinterpret an archived occurrence through today's configuration.
    if value != f"{target}T00:00:00.000Z":
        hold("occurrence_cutoff_invalid")
    return value


def prepare_research(project_root=PROJECT_ROOT, now=None):
    """Read-only API returning payload and sealed proof, never any credentials."""
    now = observation_time(now)
    target = now.astimezone(SHANGHAI).date().isoformat()
    occurrence_file = f"work/orchestrator/states/morning-report-{target}.json"
    occurrence = read_json(project_root, occurrence_file)
    run = read_json(project_root, "work/last-run.json")
    o, r = occurrence["value"], run["value"]
    if o.get("schemaVersion") not in ("1.0", "2.0") or r.get("schemaVersion") != "1.0" or o.get("task") != "morning-report" or o.get("targetDate") != target or r.get("targetDate") != target:
        hold("today_run_missing")
    os_, oe = parse_instant(o.get("startedAt")), parse_instant(o.get("completedAt"))
    rs, re_ = parse_instant(r.get("startedAt")), parse_instant(r.get("completedAt"))
    if not (os_ <= rs <= re_ <= oe <= now) or os_.astimezone(SHANGHAI).date().isoformat() != target:
        hold("run_time_order_invalid")
    claimed_cutoff = occurrence_cutoff(o, target)
    if o.get("schemaVersion") == "2.0" and r.get("frozenCutoffInstant") != claimed_cutoff:
        hold("occurrence_cutoff_mismatch")
    if o.get("childExitCode") != 0 or isinstance(o.get("childExitCode"), bool) or o.get("childSignal", "missing") is not None or o.get("timedOut") is not False or o.get("interrupted") is not False:
        hold("source_occurrence_not_successful")
    if o.get("status") == "skipped" and r.get("status") == "skipped":
        return prepare_calendar_skip(project_root, now, occurrence, run)
    if o.get("status") != "success" or r.get("status") != "success" or r.get("phase") != "publish":
        hold("source_occurrence_not_successful")
    if r.get("publicationGate") != "passed" or r.get("evidenceReview") != "passed" or r.get("publicationBlockers") != [] or r.get("dataStatus") not in ("live", "partial"):
        hold("content_gate_not_passed")
    semantic = r.get("researchSemanticReview")
    if semantic not in ("not_human_reviewed", "human_passed") or not HASH_RE.fullmatch(str(r.get("contextSha256", ""))):
        hold("run_proof_invalid")
    cutoff = parse_instant(r.get("frozenCutoffInstant"))
    if cutoff.astimezone(SHANGHAI).date().isoformat() != target or cutoff > rs or r.get("cutoffLocal") != cutoff.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M"):
        hold("frozen_cutoff_invalid")
    report_file = f"reports/daily/{target}.md"
    if r.get("reportFile") != report_file:
        hold("formal_report_path_invalid")
    report = read_artifact(project_root, report_file, MAX_REPORT_BYTES)
    # The research seal is owned by the research pipeline; the legacy XHS draft
    # manifest stays accepted so an un-migrated host keeps working. A display-only
    # draft failure must not be able to block research delivery.
    seal = None
    seal_file = ""
    seal_kind = ""
    for kind, relative in (("research", f"work/research-seal/{target}/{report['sha256']}.json"),
                           ("legacy", f"work/xhs/drafts/{target}/{report['sha256']}.json")):
        candidate = read_optional_json(project_root, relative, 512 * 1024)
        if candidate is not None:
            seal, seal_file, seal_kind = candidate, relative, kind
            break
    if seal is None:
        hold("research_seal_missing")
    s = seal["value"]
    source = s.get("source")
    if not isinstance(source, dict) or s.get("targetDate") != target:
        hold("sealed_report_proof_invalid")
    if seal_kind == "research":
        if s.get("schemaVersion") != "astock.research-seal.v1" or s.get("kind") != "research_seal" or s.get("state") != "SEALED":
            hold("sealed_report_proof_invalid")
    elif (s.get("schemaVersion") != "astock.xhs-draft.v1" or s.get("kind") != "xhs_draft"
          or s.get("state") != "DRAFT_ONLY" or s.get("publishEligible") is not False
          or s.get("humanApprovalRequired") is not True):
        hold("sealed_report_proof_invalid")
    required = {"lastRunFile": "work/last-run.json", "reportFile": report_file,
                "lastRunSha256": run["sha256"], "reportSha256": report["sha256"],
                "contextSha256": r["contextSha256"], "dataStatus": r["dataStatus"],
                "researchSemanticReview": semantic, "frozenCutoffInstant": r["frozenCutoffInstant"],
                "cutoffLocal": r["cutoffLocal"], "publicationGate": "passed", "evidenceReview": "passed"}
    if any(source.get(key) != value for key, value in required.items()):
        hold("sealed_report_proof_invalid")
    try:
        report_text = report["body"].decode("utf-8")
    except UnicodeError:
        hold("report_encoding_invalid")
    if seal_kind == "research":
        # Batch 2: the structured digest is the only machine interface for a
        # research seal. Missing, corrupt or mismatched digests hold with zero
        # sender invocations; the body parser is never used as a fallback.
        expected_digest = f"work/research-seal/{target}/{report['sha256']}.digest.json"
        digest_relative = source.get("digestFile")
        digest_sha = source.get("digestSha256")
        if digest_relative != expected_digest:
            hold("structured_digest_path_invalid")
        if not isinstance(digest_sha, str) or not HASH_RE.fullmatch(digest_sha):
            hold("structured_digest_hash_invalid")
        digest = read_optional_json(project_root, digest_relative, MAX_DIGEST_BYTES)
        if digest is None:
            hold("structured_digest_missing")
        if digest["sha256"] != digest_sha:
            hold("structured_digest_hash_mismatch")
        archive_input = read_artifact(project_root, f"work/report-archives/{target}/input.sha256", 128)
        archive_input_sha = archive_input["body"].decode("ascii", "ignore").strip()
        if not HASH_RE.fullmatch(archive_input_sha):
            hold("structured_digest_archive_binding_invalid")
        d = digest["value"]
        if (d.get("schemaVersion") != DIGEST_SCHEMA or d.get("kind") != DIGEST_KIND
                or d.get("reportDate") != target):
            hold("structured_digest_binding_invalid")
        if d.get("reportSha256") != report["sha256"] or d.get("inputSha256") != archive_input_sha:
            hold("structured_digest_binding_invalid")
        if d.get("generatedAt") != r.get("completedAt"):
            hold("structured_digest_generated_at_mismatch")
        archive_result = read_optional_json(
            project_root, f"work/report-archives/{target}/result/result.json", 512 * 1024)
        if archive_result is None:
            hold("structured_digest_archive_binding_invalid")
        selection = archive_result["value"].get("selectionSha256") if isinstance(archive_result["value"], dict) else None
        if not isinstance(selection, str) or not HASH_RE.fullmatch(selection):
            hold("structured_digest_archive_binding_invalid")
        if d.get("selectionSha256") != selection:
            hold("structured_digest_binding_invalid")
        as_of, summary = extract_summary_from_digest(d, target, r["dataStatus"], semantic)
    else:
        as_of, summary = extract_summary(report_text, target, r["dataStatus"], semantic)
    # The current receiver contract accepts research data strictly before its
    # report day. A same-day research payload is incompatible; the separately
    # signed calendar-skip contract accepts its own same-day date.
    if checked_date(as_of) >= checked_date(target):
        hold("receiver_data_date_incompatible")
    contract = {"version": 1, "status": "ready", "report_date": target, "as_of": as_of,
                "completed_at": r["completedAt"], "report_sha256": report["sha256"],
                "last_run_sha256": run["sha256"], "context_sha256": r["contextSha256"],
                "data_status": r["dataStatus"], "semantic_review": semantic,
                "publication_gate": "passed", "evidence_review": "passed"}
    preview = "\n".join([f"研究摘要｜报告日 {target}｜行情截至 {as_of}", summary["conclusion"],
                          *summary["evidence"], *summary["counter_evidence"],
                          *summary["risks"], *summary["sources"], *summary["limitations"]])
    payload = {"source": "nng8", "as_of": as_of, "target_date": target,
               "produced_at": r["completedAt"], "signal_type": "research_summary",
               "research_contract": contract, "research_summary": summary,
               "text": preview, "markdown": preview}
    if len(encode_payload(payload)) > MAX_PAYLOAD_BYTES:
        hold("research_payload_too_large")
    proof = {"occurrence_sha256": occurrence["sha256"], "last_run_sha256": run["sha256"],
             "report_sha256": report["sha256"], "seal_sha256": seal["sha256"]}
    # Catch replacement of any of the four artifacts while the bundle was read.
    for relative, prior, limit in [(occurrence_file, occurrence, 65536), ("work/last-run.json", run, 65536),
                                   (report_file, report, MAX_REPORT_BYTES), (seal_file, seal, 512 * 1024)]:
        fresh = read_artifact(project_root, relative, limit)
        if fresh["fingerprint"] != prior["fingerprint"] or fresh["sha256"] != prior["sha256"]:
            hold("artifact_changed")
    return {"status": "ready", "kind": "research", "target_date": target, "as_of": as_of, "payload": payload,
            "proof": proof, "completed_at": re_}


def prepare_calendar_skip(project_root, now, occurrence, run):
    """Prove the current day's calendar skip from the claimed, hash-bound run.

    This sends a separate signed no-research contract, never a fake report or
    a prior-day research payload. Missing/invalid calendar material must hold.
    """
    target = now.astimezone(SHANGHAI).date().isoformat()
    o, r = occurrence["value"], run["value"]
    if (o.get("targetDate") != target or r.get("targetDate") != target
            or o.get("status") != "skipped" or r.get("status") != "skipped"
            or r.get("phase") != "collect" or r.get("tradingStatus") != "non_trading_day"
            or r.get("publicationGate") != "not_applicable"
            or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", str(o.get("invocationId", "")))
            or r.get("frozenCutoffInstant") != occurrence_cutoff(o, target)):
        hold("calendar_skip_run_invalid")
    context_file = r.get("contextFile")
    if (not isinstance(context_file, str) or not re.fullmatch(r"work/[A-Za-z0-9._/-]+\.json", context_file)
            or ".." in context_file):
        hold("calendar_skip_context_invalid")
    context = read_json(project_root, context_file, 4 * 1024 * 1024)
    if context["sha256"] != r.get("contextSha256"):
        hold("calendar_skip_context_invalid")
    data = context["value"].get("data")
    calendar = data.get("tradingCalendar") if isinstance(data, dict) else None
    if (not isinstance(calendar, dict) or calendar.get("targetDate") != target
            or calendar.get("status") != "non_trading_day"
            or not isinstance(calendar.get("sourceIds"), list)
            or CALENDAR_SOURCE_ID not in calendar["sourceIds"]):
        hold("calendar_skip_proof_invalid")
    previous, following = calendar.get("previousTradingDate"), calendar.get("nextTradingDate")
    if not checked_date(previous) < checked_date(target) < checked_date(following):
        hold("calendar_skip_coverage_invalid")
    observed = parse_instant(calendar.get("dataAsOf"))
    if (observed.astimezone(SHANGHAI).date().isoformat() != target
            or observed > parse_instant(r.get("completedAt"))):
        hold("calendar_skip_time_invalid")
    normalized_calendar = {"source_id": CALENDAR_SOURCE_ID, "target_date": target,
                           "status": "non_trading_day", "previous_trading_date": previous,
                           "next_trading_date": following, "data_as_of": calendar["dataAsOf"]}
    proof = {"occurrence_sha256": occurrence["sha256"], "last_run_sha256": run["sha256"],
             "context_sha256": context["sha256"], "calendar_sha256": sha256(encode_payload(normalized_calendar))}
    skip = {"version": 1, "status": "not_scheduled", "reason": "non_trading_day",
            "market": "XSHG", "target_date": target, "validated_at": o["completedAt"],
            "calendar": normalized_calendar, "proof": proof}
    payload = {"source": "nng8", "signal_type": "research_skip", "target_date": target,
               "as_of": target, "produced_at": o["completedAt"], "research_skip": skip}
    for relative, prior, limit in [
            (f"work/orchestrator/states/morning-report-{target}.json", occurrence, 65536),
            ("work/last-run.json", run, 65536), (context_file, context, 4 * 1024 * 1024)]:
        fresh = read_artifact(project_root, relative, limit)
        if fresh["fingerprint"] != prior["fingerprint"] or fresh["sha256"] != prior["sha256"]:
            hold("artifact_changed")
    return {"status": "ready", "kind": "skip", "target_date": target, "as_of": target,
            "payload": payload, "proof": {**proof, "skip_sha256": sha256(encode_payload(skip))},
            "completed_at": parse_instant(o["completedAt"])}


def build_payload(project_root=PROJECT_ROOT, now=None):
    prepared = prepare_research(project_root, now)
    if prepared["status"] != "ready":
        hold("research_not_ready")
    return prepared["payload"]


def encode_payload(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def send_window(now, prepared):
    local = now.astimezone(SHANGHAI)
    return (local.weekday() < 5 and local.hour == 8 and local.minute < 14
            and dt.timedelta(0) <= now - prepared["completed_at"] <= dt.timedelta(hours=2))


def read_post_config(bus_root):
    raw = read_artifact(bus_root, "config/pending.env", 16384)["body"]
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        hold("post_config_invalid")
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if key.strip() not in ("DEDIROCK_DIGEST_URL", "DEDIROCK_PENDING_SECRET", "X_SOURCE", "TLS_VERIFY"):
            continue
        key = key.strip()
        if not sep or key in values:
            hold("post_config_invalid")
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError:
            hold("post_config_invalid")
        if len(tokens) != 1:
            hold("post_config_invalid")
        values[key] = tokens[0]
    endpoint = values.get("DEDIROCK_DIGEST_URL", "")
    secret = values.get("DEDIROCK_PENDING_SECRET", "")
    try:
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.port not in (None, 443):
            hold("post_config_invalid")
    except ValueError:
        hold("post_config_invalid")
    if values.get("X_SOURCE", "nng8") != "nng8":
        hold("post_security_config_invalid")
    if not secret or len(secret) > 4096:
        hold("post_config_invalid")
    mode = values.get("TLS_VERIFY", "1").lower()
    if mode in ("1", "true", "yes"):
        return {"url": endpoint, "secret": secret, "tls_mode": "strict"}
    if mode != "0":
        hold("post_security_config_invalid")
    # A legacy TLS_VERIFY=0 is NOT permission to disable verification. Only the
    # explicitly installed, SSH-authenticated CA + exact certificate pin allow
    # an independent strict-chain/pinned-identity mode for this legacy endpoint.
    try:
        pin = read_json(bus_root, "config/relay-tls-pin.json", 4096)["value"]
        ca_body = read_artifact(bus_root, "config/dedirock-relay-ca.crt", 65536)["body"]
    except (OSError, Held):
        hold("post_tls_pin_missing_or_invalid")
    if (set(pin) != {"version", "host", "certificate_sha256"}
            or type(pin.get("version")) is not int or pin["version"] != 1
            or pin.get("host") != url.hostname
            or not isinstance(pin.get("certificate_sha256"), str)
            or not HASH_RE.fullmatch(pin["certificate_sha256"])):
        hold("post_tls_pin_missing_or_invalid")
    try:
        ca_pem = ca_body.decode("ascii")
        if ca_pem.count("-----BEGIN CERTIFICATE-----") != 1 or ca_pem.count("-----END CERTIFICATE-----") != 1:
            hold("post_tls_pin_missing_or_invalid")
        ca_der = ssl.PEM_cert_to_DER_cert(ca_pem)
        if not hmac.compare_digest(sha256(ca_der), pin["certificate_sha256"]):
            hold("post_tls_pin_missing_or_invalid")
        # Fail malformed CA material before attempting any connection.
        context = ssl.create_default_context(cadata=ca_pem)
        if context.verify_mode != ssl.CERT_REQUIRED:
            hold("post_tls_pin_missing_or_invalid")
    except (UnicodeError, ValueError, ssl.SSLError):
        hold("post_tls_pin_missing_or_invalid")
    return {"url": endpoint, "secret": secret, "tls_mode": "pinned",
            "pin_host": pin["host"], "certificate_sha256": pin["certificate_sha256"], "ca_pem": ca_pem}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Verify chain + exact peer certificate before http.client can send HTTP."""

    def __init__(self, host, *, context, pin_host, certificate_sha256, **kwargs):
        self._pin_host = pin_host
        self._certificate_sha256 = certificate_sha256
        super().__init__(host, context=context, **kwargs)

    def set_tunnel(self, *args, **kwargs):
        # A CONNECT request would be HTTP written before the peer is verified.
        raise ssl.SSLCertVerificationError("pinned_tls_proxy_forbidden")

    def connect(self):
        if self.host != self._pin_host or self.port != 443 or self._tunnel_host:
            raise ssl.SSLCertVerificationError("pinned_tls_endpoint_mismatch")
        # The inherited connect performs the CERT_REQUIRED TLS handshake. With
        # check_hostname=False only this explicit pin establishes peer identity.
        super().connect()
        try:
            certificate = self.sock.getpeercert(binary_form=True)
            if not certificate or not hmac.compare_digest(sha256(certificate), self._certificate_sha256):
                raise ssl.SSLCertVerificationError("pinned_tls_certificate_mismatch")
        except Exception:
            self.close()
            raise


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, ca_pem, pin_host, certificate_sha256):
        context = ssl.create_default_context(cadata=ca_pem)
        # The pre-existing self-signed certificate has no SAN. Its exact DER pin
        # replaces hostname checking, never chain/expiry/certificate validation.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        super().__init__(context=context)
        self._pin_host = pin_host
        self._certificate_sha256 = certificate_sha256

    def https_open(self, request):
        def connection(host, **kwargs):
            return _PinnedHTTPSConnection(host, context=self._context, pin_host=self._pin_host,
                                          certificate_sha256=self._certificate_sha256, **kwargs)
        return self.do_open(connection, request)


def post_payload(payload, config, now):
    """Return fixed classified outcomes; an uncertain POST is never retried."""
    body = encode_payload(payload)
    timestamp = str(int(now.timestamp()))
    digest = hmac.new(config["secret"].encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(config["url"], data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Timestamp": timestamp,
        "X-Signature": f"sha256={digest}", "X-Source": "nng8"})
    if config.get("tls_mode", "strict") == "pinned":
        handler = _PinnedHTTPSHandler(ca_pem=config["ca_pem"], pin_host=config["pin_host"],
                                      certificate_sha256=config["certificate_sha256"])
    else:
        handler = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    # Do not let process HTTP(S)_PROXY variables redirect authenticated traffic.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), handler)
    try:
        with opener.open(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                return {"status": "ambiguous", "reason": "unexpected_http_status"}
            response_body = response.read(16385)
            if len(response_body) > 16384:
                return {"status": "ambiguous", "reason": "acknowledgement_invalid"}
            try:
                acknowledgement = decode_json(response_body)
            except Held:
                return {"status": "ambiguous", "reason": "acknowledgement_invalid"}
            sent_sha256 = sha256(body)
            interpreted = interpret_receiver_acknowledgement(
                acknowledgement, source="nng8", report_date=payload["target_date"],
                data_date=payload["as_of"], expected_payload_sha256=sent_sha256)
            common = {"ack_scope": "http_accepted", "payload_sha256": sent_sha256}
            for key in ("receiver_storage", "receiver_content_sha256", "receiver_record_id",
                        "content_match"):
                if key in interpreted:
                    common[key] = interpreted[key]
            if interpreted["verdict"] == "invalid":
                return {"status": "ambiguous", "reason": "acknowledgement_invalid"}
            if interpreted["verdict"] == "confirmed":
                return {"status": "confirmed", "reason": "accepted", **common}
            if interpreted["verdict"] == "failed":
                return {"status": "failed", "reason": interpreted["reason"], **common}
            # HTTP 接受了，但存储、日期、接收 ID 或内容一致性没有完整证据：只能记“未确认”，不重发。
            return {"status": "accepted", "reason": interpreted["reason"], **common}
    except urllib.error.HTTPError as error:
        if error.code in REJECT_HTTP_STATUSES:
            return {"status": "rejected", "reason": "http_rejected", "http_status": error.code}
        return {"status": "ambiguous", "reason": "http_result_unknown"}
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)):
            return {"status": "not_sent", "reason": "connection_not_established"}
        return {"status": "ambiguous", "reason": "transport_result_unknown"}
    except Exception:
        return {"status": "ambiguous", "reason": "transport_result_unknown"}


def record_url(config, target_date):
    """只读记录查询地址：由投递地址推导，不新增配置项。"""
    parts = urllib.parse.urlsplit(config["url"])
    if parts.scheme != "https" or not parts.netloc:
        hold("record_endpoint_invalid")
    return f"{parts.scheme}://{parts.netloc}/records/nng8/{target_date}"


def query_record(config, target_date, now, expected_payload_sha256, data_date=None):
    """只读查询接收记录：绝不 POST、绝不写入、绝不改变记录。

    accepted（HTTP 已接受、存储未确认）只能靠这条路径补证；重发是不允许的。
    """
    url = record_url(config, target_date)
    path = urllib.parse.urlsplit(url).path
    timestamp = str(int(now.timestamp()))
    digest = hmac.new(config["secret"].encode(),
                      f"{timestamp}.GET.{path}".encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, method="GET", headers={
        "X-Timestamp": timestamp, "X-Signature": f"sha256={digest}", "X-Source": "nng8"})
    if config.get("tls_mode", "strict") == "pinned":
        handler = _PinnedHTTPSHandler(ca_pem=config["ca_pem"], pin_host=config["pin_host"],
                                      certificate_sha256=config["certificate_sha256"])
    else:
        handler = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), handler)
    try:
        with opener.open(request, timeout=20) as response:
            if response.status == 404:
                return {"status": "not_found"}
            if not 200 <= response.status < 300:
                return {"status": "query_failed"}
            body = response.read(16385)
            if len(body) > 16384:
                return {"status": "query_failed"}
            try:
                record = decode_json(body)
            except Held:
                return {"status": "query_failed"}
            interpreted = interpret_receiver_acknowledgement(
                record, source="nng8", report_date=target_date,
                data_date=data_date, expected_payload_sha256=expected_payload_sha256)
            if interpreted["verdict"] == "confirmed":
                result = {"status": "confirmed",
                          "receiver_content_sha256": interpreted.get("receiver_content_sha256"),
                          "receiver_record_id": interpreted.get("receiver_record_id"),
                          "receiver_storage": interpreted.get("receiver_storage"),
                          "content_match": True}
                if interpreted.get("stored_at"):
                    result["stored_at"] = interpreted["stored_at"]
                return result
            if interpreted["verdict"] == "failed":
                return {"status": "query_failed", "reason": interpreted["reason"]}
            return {"status": "unconfirmed", "reason": interpreted.get("reason")}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"status": "not_found"}
        return {"status": "query_failed"}
    except Exception:
        return {"status": "query_failed"}


def _state_directory(bus_root):
    root = _safe_root(bus_root)
    current = root
    for component in ("out", "relay-delivery"):
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            hold("delivery_state_invalid")
    return current


def _write_state(directory, target, value):
    destination = directory / f"{target}.json"
    if destination.is_symlink():
        hold("delivery_state_invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target}.", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encode_payload(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_relay(project_root=PROJECT_ROOT, bus_root=BUS_ROOT, now=None, dry_run=False,
              sender=post_payload, clock=None, query=None):
    """Orchestrate safe preview or bounded idempotent delivery.

    ``now`` and injected sender/clock are Python test seams, not CLI overrides.
    """
    supplied_now = now
    query_reader = query or query_record
    try:
        now = observation_time(now)
        prepared = prepare_research(project_root, now)
        kind = prepared.get("kind", "research")
        identity_key = "skip_sha256" if kind == "skip" else "report_sha256"
        public = {"target_date": prepared["target_date"], "as_of": prepared["as_of"],
                  identity_key: prepared["proof"][identity_key], "payload_kind": kind,
                  "data_status": "not_scheduled" if kind == "skip" else prepared["payload"]["research_contract"]["data_status"]}
        within_window = send_window(now, prepared)
        if dry_run:
            # Deliberately before any post config, state/lock or sender access.
            return {"status": "ready", "reason": "read_only_preview", **public,
                    "within_send_window": within_window, "would_send": False, "sent": 0}
        if not within_window:
            return {"status": "held", "reason": "outside_send_window", **public, "would_send": False, "sent": 0}
        config = read_post_config(bus_root)
        directory = _state_directory(bus_root)
        target = prepared["target_date"]
        lock_fd = os.open(directory / f"{target}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                hold("delivery_state_invalid")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "held", "reason": "delivery_busy", **public, "sent": 0}
            try:
                existing = read_json(directory, f"{target}.json", 8192)["value"]
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing.get("version") != 1 or existing.get("target_date") != target or existing.get("status") not in ("sending", "ambiguous", "confirmed", "accepted", "failed", "not_sent", "rejected"):
                    hold("delivery_state_invalid")
                if existing.get("payload_kind", "research") != kind:
                    hold("same_day_contract_kind_changed")
                if existing.get(identity_key) != prepared["proof"][identity_key]:
                    hold("same_day_report_changed")
                decision = delivery_decision(existing, **_expected_proof(prepared))
                if existing["status"] == "confirmed":
                    if decision["evidenceConfirmed"]:
                        return {"status": "already_confirmed", **public, "sent": 0, "confirmed": True}
                    # Recorded as confirmed but the proof is incomplete or conflicting:
                    # never resend, never report as confirmed.
                    return {"status": "already_recorded_unverified", **public, "sent": 0, "confirmed": False,
                            "reason": decision["proof"]["reason"], "evidence_state": decision["proof"]["state"]}
                if existing["status"] == "rejected":
                    echo = {"status": "already_rejected", **public, "sent": 0,
                            "confirmed": False, "reason": existing.get("reason")}
                    if "http_status" in existing:
                        echo["http_status"] = existing["http_status"]
                    return echo
                # 旧记录（HTTP 已接受但存储未确认）与新终态都只回显，绝不重新放回待发送队列，
                # 否则“状态升级”会变成重复投递。
                if existing["status"] == "accepted":
                    # 优先只读补证：查询确认存储与内容一致后升级本地状态，不发任何东西。
                    try:
                        probed = query_reader(config, target, now,
                                              existing.get("payload_sha256", ""),
                                              data_date=existing.get("as_of") or prepared["as_of"])
                    except TypeError:
                        probed = query_reader(config, target, now,
                                              existing.get("payload_sha256", ""))
                    except Exception:
                        probed = {"status": "query_failed"}
                    if probed.get("status") == "confirmed":
                        # 一次性保存经过校验的完整新证据；保留原 HTTP 接受事实，不只改 status。
                        upgraded = dict(existing)
                        upgraded.update(status="confirmed", reason="confirmed_via_read_only_query",
                                        confirmed_via="read_only_query",
                                        receiver_storage=probed.get("receiver_storage") or "receiver_reported_stored",
                                        receiver_content_sha256=probed.get("receiver_content_sha256"),
                                        content_match=True)
                        if probed.get("receiver_record_id"):
                            upgraded["receiver_record_id"] = probed["receiver_record_id"]
                        if probed.get("stored_at"):
                            upgraded["stored_at"] = probed["stored_at"]
                        if "ack_scope" not in upgraded:
                            upgraded["ack_scope"] = "http_accepted"
                        upgrade_decision = delivery_decision(upgraded, **_expected_proof(prepared))
                        if not upgrade_decision["evidenceConfirmed"]:
                            # The query answer does not bind to this record: keep it accepted.
                            return {"status": "already_accepted", **public, "sent": 0, "confirmed": False,
                                    "reason": existing.get("reason"), "query": "unverified",
                                    "query_reason": upgrade_decision["proof"]["reason"]}
                        _write_state(directory, target, upgraded)
                        echo = {"status": "confirmed_from_query", **public, "sent": 0,
                                "confirmed": True, "confirmed_via": "read_only_query",
                                "receiver_storage": upgraded["receiver_storage"],
                                "receiver_content_sha256": probed.get("receiver_content_sha256"),
                                "content_match": True, "ack_scope": upgraded["ack_scope"]}
                        if probed.get("receiver_record_id"):
                            echo["receiver_record_id"] = probed["receiver_record_id"]
                        return echo
                    echo = {"status": "already_accepted", **public, "sent": 0,
                            "confirmed": False, "reason": existing.get("reason"),
                            "query": probed.get("status")}
                    for key in ("ack_scope", "receiver_storage", "receiver_content_sha256",
                                "payload_sha256", "content_match", "receiver_record_id"):
                        if key in existing:
                            echo[key] = existing[key]
                    return echo
                if existing["status"] == "failed":
                    echo = {"status": "already_failed", **public, "sent": 0,
                            "confirmed": False, "reason": existing.get("reason")}
                    for key in ("ack_scope", "receiver_storage", "receiver_content_sha256",
                                "payload_sha256", "content_match", "receiver_record_id"):
                        if key in existing:
                            echo[key] = existing[key]
                    return echo
                if existing["status"] in ("sending", "ambiguous"):
                    hold("delivery_requires_manual_review")
            # Recheck both the sealed proof and clock at the actual send boundary.
            send_now = observation_time(clock() if clock else supplied_now)
            fresh = prepare_research(project_root, send_now)
            if fresh.get("proof") != prepared["proof"]:
                hold("artifact_changed")
            if not send_window(send_now, fresh):
                hold("outside_send_window")
            record = {"version": 1, "target_date": target, "as_of": prepared["as_of"],
                      "payload_kind": kind, identity_key: prepared["proof"][identity_key],
                      "last_run_sha256": prepared["proof"]["last_run_sha256"],
                      "status": "sending", "attempted_at": send_now.isoformat()}
            _write_state(directory, target, record)
            try:
                result = sender(prepared["payload"], config, send_now)
            except Exception:
                result = {"status": "ambiguous", "reason": "transport_result_unknown"}
            status = result.get("status")
            allowed_reasons = {"accepted", "http_accepted_storage_unconfirmed", "receiver_reported_not_stored",
                               "content_mismatch", "http_rejected", "connection_not_established",
                               "unexpected_http_status", "acknowledgement_invalid", "http_result_unknown",
                               "transport_result_unknown",
                               "acknowledgement_target_date_missing", "acknowledgement_target_date_mismatch",
                               "acknowledgement_day_missing", "acknowledgement_day_mismatch",
                               "acknowledgement_record_id_missing", "acknowledgement_content_hash_invalid"}
            if status not in ("confirmed", "accepted", "failed", "ambiguous", "not_sent", "rejected") \
                    or result.get("reason") not in allowed_reasons:
                status, reason = "ambiguous", "transport_result_unknown"
            else:
                reason = result["reason"]
            record.update(status=status, reason=reason)
            if result.get("ack_scope") == "http_accepted":
                record["ack_scope"] = "http_accepted"
            storage = result.get("receiver_storage")
            if storage in ("not_in_acknowledgement", "receiver_reported_stored",
                           "receiver_reported_not_stored"):
                record["receiver_storage"] = storage
            if isinstance(result.get("http_status"), int):
                record["http_status"] = result["http_status"]
            for key in ("payload_sha256", "receiver_content_sha256", "receiver_record_id", "content_match"):
                if key in result:
                    record[key] = result[key]
            _write_state(directory, target, record)
            send_decision = delivery_decision(record, **_expected_proof(prepared))
            if status == "confirmed" and not send_decision["evidenceConfirmed"]:
                # Delivered once (do not resend) but not proven; report it as such.
                status = "confirmed_unverified"
            outcome = {"status": status, "reason": reason, **public,
                       "sent": int(status in ("confirmed", "confirmed_unverified")),
                       "confirmed": status == "confirmed"}
            if status == "confirmed_unverified":
                outcome["evidence_reason"] = send_decision["proof"]["reason"]
            if "ack_scope" in record:
                outcome["ack_scope"] = record["ack_scope"]
            if "receiver_storage" in record:
                outcome["receiver_storage"] = record["receiver_storage"]
            if "http_status" in record:
                outcome["http_status"] = record["http_status"]
            for key in ("payload_sha256", "receiver_content_sha256", "receiver_record_id", "content_match"):
                if key in record:
                    outcome[key] = record[key]
            return outcome
        finally:
            os.close(lock_fd)
    except Held as error:
        return {"status": "held", "reason": str(error), "sent": 0, "would_send": False}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {"status": "held", "reason": "required_artifact_or_config_invalid", "sent": 0, "would_send": False}


# Explicit safe metadata vocabulary: never persist arbitrary exception/remote text.
ATTEMPT_REASONS = frozenset("""
non_trading_day outside_send_window artifact_changed artifact_file_invalid
artifact_json_invalid artifact_path_invalid date_invalid delivery_requires_manual_review
delivery_state_invalid formal_report_path_invalid frozen_cutoff_invalid market_date_invalid
research_not_ready research_seal_missing run_proof_invalid run_time_order_invalid
same_day_report_changed sealed_report_proof_invalid source_occurrence_not_successful
timestamp_invalid today_run_missing structured_digest_archive_binding_invalid
occurrence_version_invalid occurrence_cutoff_invalid occurrence_cutoff_mismatch
structured_digest_binding_invalid structured_digest_generated_at_mismatch
structured_digest_hash_invalid structured_digest_hash_mismatch structured_digest_missing
structured_digest_path_invalid digest_conclusion_missing digest_data_date_invalid
digest_inference_invalid digest_items_invalid digest_limitations_missing
digest_reference_invalid digest_source_index_invalid post_config_invalid
post_security_config_invalid post_tls_pin_missing_or_invalid record_endpoint_invalid
required_artifact_or_config_invalid accepted http_accepted_storage_unconfirmed
receiver_reported_not_stored content_mismatch http_rejected connection_not_established
unexpected_http_status acknowledgement_invalid http_result_unknown transport_result_unknown
acknowledgement_target_date_missing acknowledgement_target_date_mismatch
acknowledgement_day_missing acknowledgement_day_mismatch acknowledgement_record_id_missing
acknowledgement_content_hash_invalid delivery_busy already_confirmed already_accepted
already_failed already_rejected already_not_sent read_only_query receiver_data_date_incompatible
""".split())
ATTEMPT_STATUSES = frozenset(("held", "skipped", "confirmed", "accepted", "failed", "ambiguous",
                              "already_recorded_unverified", "confirmed_unverified",
                              "not_sent", "rejected", "already_confirmed", "already_accepted",
                              "already_failed", "already_rejected", "already_not_sent"))


def record_attempt(result, *, target_date, observed_at, bus_root=BUS_ROOT):
    """Diagnostic observation only; never read by delivery/idempotency decisions."""
    checked_date(target_date)
    status = result.get("status") if result.get("status") in ATTEMPT_STATUSES else "unknown"
    reason = result.get("reason")
    if reason not in ATTEMPT_REASONS:
        reason = "other_held" if status == "held" else "other_result"
    sent = result.get("sent")
    value = {"schemaVersion": "signal-bus.relay-attempt.v1", "target_date": target_date,
             "observedAt": observation_time(observed_at).isoformat(), "status": status,
             "reason": reason, "sent": sent if type(sent) is int and sent >= 0 else 0}
    current = _safe_root(bus_root)
    for component in ("out", "relay-attempts"):
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            hold("delivery_state_invalid")
    _write_state(current, target_date, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only sealed research preview or scheduled relay")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    started_at = observation_time(None)
    # Do not pass a fixed test clock: the actual send boundary must re-read now.
    result = run_relay(dry_run=args.dry_run)
    if not args.dry_run:
        try:
            record_attempt(result, target_date=started_at.astimezone(SHANGHAI).date().isoformat(),
                           observed_at=observation_time(None))
        except Exception:
            # Diagnostic failure cannot change delivery state, outcome or retries.
            print("relay_attempt_observation_unavailable", file=sys.stderr)
    # --json is explicit for operators; all output is always safe metadata JSON.
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] in ("ambiguous", "not_sent", "rejected", "failed") else 0


if __name__ == "__main__":
    sys.exit(main())
