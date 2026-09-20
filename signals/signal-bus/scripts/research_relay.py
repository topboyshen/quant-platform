#!/usr/bin/env python3
"""Relay only today's completed, sealed formal AStock research (stdlib only).

Import/prepare/dry-run are read-only; the CLI never prints report text, endpoint,
credentials or response bodies. No model, notification policy or XHS body is read.
"""
from __future__ import annotations

import argparse
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
HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
REF_RE = re.compile(r"\[来源\s*([^\]\n]*)\]")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
URL_RE = re.compile(r"https?://[^\s<>\[\]）)]+", re.I)


class Held(Exception):
    """Fixed safe reason codes only, never artifact contents."""


def hold(reason):
    raise Held(reason)


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
    except OSError:
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
    evidence, counter = [], []
    if analysis_blocks:
        chosen = next((b for b in analysis_blocks if re.match(r"^-\s+\[推断\]\s*摘要[：:]", b)), analysis_blocks[0])
        conclusion_lines = []
        destination = conclusion_lines
        for line in chosen.splitlines():
            stripped = re.sub(r"^\s*-\s*", "", line).strip()
            if re.match(r"支持证据\s*[：:]", stripped):
                destination = evidence
                destination.append(stripped)
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
        if not counter:
            hold("analysis_counter_missing")
        for value in counter:
            if references_in(value):
                continue
            if (not re.match(r"^\[推断\]\s*风险[：:]", conclusion)
                    or len(counter) != 1
                    or re.fullmatch(r"(?:反证[：:]\s*)?未提供结构化反证来源，不升级为事实结论。", value) is None):
                hold("analysis_counter_missing")
            limitations.append("原风险块明确未提供结构化反证来源；该缺失提示不是已核验反证，未补造来源。")
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
    return as_of, summary


def prepare_research(project_root=PROJECT_ROOT, now=None):
    """Read-only API returning payload and sealed proof, never any credentials."""
    now = observation_time(now)
    target = now.astimezone(SHANGHAI).date().isoformat()
    occurrence_file = f"work/orchestrator/states/morning-report-{target}.json"
    occurrence = read_json(project_root, occurrence_file)
    run = read_json(project_root, "work/last-run.json")
    o, r = occurrence["value"], run["value"]
    if o.get("schemaVersion") != "1.0" or r.get("schemaVersion") != "1.0" or o.get("task") != "morning-report" or o.get("targetDate") != target or r.get("targetDate") != target:
        hold("today_run_missing")
    os_, oe = parse_instant(o.get("startedAt")), parse_instant(o.get("completedAt"))
    rs, re_ = parse_instant(r.get("startedAt")), parse_instant(r.get("completedAt"))
    if not (os_ <= rs <= re_ <= oe <= now) or os_.astimezone(SHANGHAI).date().isoformat() != target:
        hold("run_time_order_invalid")
    if o.get("childExitCode") != 0 or isinstance(o.get("childExitCode"), bool) or o.get("childSignal", "missing") is not None or o.get("timedOut") is not False or o.get("interrupted") is not False:
        hold("source_occurrence_not_successful")
    if o.get("status") == "skipped" and r.get("status") == "skipped":
        return {"status": "skipped", "reason": "non_trading_day", "target_date": target}
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
    as_of, summary = extract_summary(report_text, target, r["dataStatus"], semantic)
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
    return {"status": "ready", "target_date": target, "as_of": as_of, "payload": payload,
            "proof": proof, "completed_at": re_}


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
            if acknowledgement.get("status") != "accepted" or acknowledgement.get("source") != "nng8" or acknowledgement.get("day") != payload["as_of"]:
                return {"status": "ambiguous", "reason": "acknowledgement_invalid"}
            # 三件事分开：HTTP 接受 / 接收端确认存储 / 内容一致。
            # 内容哈希必须是对“已保存内容”算的，也就是我们实际发出的这份字节。
            sent_sha256 = sha256(body)
            stored = acknowledgement.get("stored")
            if stored is True:
                storage = "receiver_reported_stored"
            elif stored is False:
                storage = "receiver_reported_not_stored"
            else:
                storage = "not_in_acknowledgement"
            ack_content = acknowledgement.get("content_sha256") or acknowledgement.get("contentSha256")
            record_id = acknowledgement.get("record_id") or acknowledgement.get("recordId")
            common = {"ack_scope": "http_accepted", "receiver_storage": storage,
                      "payload_sha256": sent_sha256}
            if isinstance(record_id, str) and record_id:
                common["receiver_record_id"] = record_id
            if not isinstance(ack_content, str) or not ack_content:
                content_match = None
            else:
                common["receiver_content_sha256"] = ack_content
                content_match = ack_content == sent_sha256
            common["content_match"] = content_match
            if stored is False:
                return {"status": "failed", "reason": "receiver_reported_not_stored", **common}
            if content_match is False:
                return {"status": "failed", "reason": "content_mismatch", **common}
            if stored is True and content_match is True:
                return {"status": "confirmed", "reason": "accepted", **common}
            # HTTP 接受了，但存储或内容一致性没有证据：只能记“未确认”，不重发。
            return {"status": "accepted", "reason": "http_accepted_storage_unconfirmed", **common}
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429):
            return {"status": "rejected", "reason": "http_rejected"}
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


def query_record(config, target_date, now, expected_payload_sha256):
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
            if record.get("status") != "accepted" or record.get("source") != "nng8":
                return {"status": "query_failed"}
            stored = record.get("stored")
            content = record.get("content_sha256") or record.get("contentSha256")
            if stored is True and isinstance(content, str) and content == expected_payload_sha256:
                return {"status": "confirmed", "receiver_content_sha256": content,
                        "receiver_record_id": record.get("record_id") or record.get("recordId"),
                        "stored_at": record.get("stored_at")}
            return {"status": "unconfirmed"}
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
        if prepared["status"] == "skipped":
            return {"status": "skipped", "reason": "non_trading_day", "sent": 0, "would_send": False}
        public = {"target_date": prepared["target_date"], "as_of": prepared["as_of"],
                  "report_sha256": prepared["proof"]["report_sha256"],
                  "data_status": prepared["payload"]["research_contract"]["data_status"]}
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
                if existing.get("report_sha256") != prepared["proof"]["report_sha256"]:
                    hold("same_day_report_changed")
                if existing["status"] == "confirmed":
                    return {"status": "already_confirmed", **public, "sent": 0, "confirmed": True}
                # 旧记录（HTTP 已接受但存储未确认）与新终态都只回显，绝不重新放回待发送队列，
                # 否则“状态升级”会变成重复投递。
                if existing["status"] == "accepted":
                    # 优先只读补证：查询确认存储与内容一致后升级本地状态，不发任何东西。
                    try:
                        probed = query_reader(config, target, now,
                                              existing.get("payload_sha256", ""))
                    except Exception:
                        probed = {"status": "query_failed"}
                    if probed.get("status") == "confirmed":
                        upgraded = dict(existing, status="confirmed", reason="confirmed_via_read_only_query",
                                        confirmed_via="read_only_query",
                                        receiver_content_sha256=probed.get("receiver_content_sha256"),
                                        content_match=True)
                        if probed.get("receiver_record_id"):
                            upgraded["receiver_record_id"] = probed["receiver_record_id"]
                        if probed.get("stored_at"):
                            upgraded["stored_at"] = probed["stored_at"]
                        _write_state(directory, target, upgraded)
                        echo = {"status": "confirmed_from_query", **public, "sent": 0,
                                "confirmed": True, "confirmed_via": "read_only_query",
                                "receiver_content_sha256": probed.get("receiver_content_sha256"),
                                "content_match": True}
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
                      "report_sha256": prepared["proof"]["report_sha256"],
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
                               "transport_result_unknown"}
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
            for key in ("payload_sha256", "receiver_content_sha256", "receiver_record_id", "content_match"):
                if key in result:
                    record[key] = result[key]
            _write_state(directory, target, record)
            outcome = {"status": status, "reason": reason, **public,
                       "sent": int(status == "confirmed"), "confirmed": status == "confirmed"}
            if "ack_scope" in record:
                outcome["ack_scope"] = record["ack_scope"]
            if "receiver_storage" in record:
                outcome["receiver_storage"] = record["receiver_storage"]
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only sealed research preview or scheduled relay")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_relay(dry_run=args.dry_run)
    # --json is explicit for operators; all output is always safe metadata JSON.
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] in ("ambiguous", "not_sent", "rejected", "failed") else 0


if __name__ == "__main__":
    sys.exit(main())
