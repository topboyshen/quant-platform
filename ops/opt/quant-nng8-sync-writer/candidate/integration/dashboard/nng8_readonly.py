"""Bounded fixed-file adapter. It neither imports nnG8 nor runs any producer."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 1024 * 1024
SNAPSHOT_SECONDS = 15 * 60
FUTURE_SECONDS = 5 * 60
DOCUMENTS = frozenset({"today", "progress", "delivery", "projects", "ledger", "phase2"})
CONTRACT = "workbench.nng8.v1"
DISCLAIMER = "外部只读证据；可读取不等于业务成功；内容哈希不是来源认证"
CALIBRATION_LIMIT = "不同协议样本不相加；与 Phase2/研究账本可能重叠，不计入 ETF/Profit 样本"


class BundleUnavailable(ValueError):
    """Public errors deliberately contain no paths or upstream content."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleUnavailable("invalid bundle")
        result[key] = value
    return result


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _timestamp(value):
    result = _instant(value)
    return result.isoformat() if result else None


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _text(value, limit=500):
    if not isinstance(value, str):
        return None
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)[:limit]


def _texts(value, limit=16, width=500):
    if not isinstance(value, list):
        return []
    result = []
    for row in value[:limit]:
        text = _text(row if isinstance(row, str) else row.get("text") if isinstance(row, dict) else None, width)
        if text:
            result.append(text)
    return result


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**9 else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _status(value, fallback="unknown"):
    return {
        "ok": "ok", "pass": "pass", "passed": "pass", "success": "pass",
        "partial": "partial", "warning": "partial", "degraded": "partial",
        "failed": "failed", "fail": "failed", "critical": "failed", "error": "failed",
        "stale": "stale", "unknown": "unknown", "unavailable": "unavailable",
        "insufficient": "insufficient", "insufficientProspective": "insufficient",
        "blocked": "blocked", "skipped": "skipped", "running": "running",
    }.get(value if isinstance(value, str) else None, fallback)


def _issue(issues, source, code, message):
    if len(issues) < 64:
        issues.append({"source": source, "code": code, "message": message})


def _read_regular_bytes(path, limit):
    """Compare two bounded passes over one pinned descriptor without reopening.

    Content comparison does not depend on filesystem timestamp precision. It is
    a bounded consistency check, not a snapshot guarantee against arbitrary ABA
    writes: publishers must still replace complete, immutable file generations.
    """
    if not path:
        raise BundleUnavailable("bundle unavailable")
    fd = directory = None
    try:
        target = Path(path)
        if not target.is_absolute() or ".." in target.parts or not target.name:
            raise BundleUnavailable("bundle unavailable")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        directory = os.open("/", flags | os.O_DIRECTORY)
        for component in target.parent.parts[1:]:
            following = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = following
        before = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise BundleUnavailable("bundle unavailable")
        fd = os.open(target.name, flags, dir_fd=directory)
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BundleUnavailable("bundle unavailable")
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        final = os.fstat(fd)
        if len(raw) > limit or (after.st_size, after.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise BundleUnavailable("bundle unavailable")
        # Rewind this same inode: a concurrent atomic rename may unlink it, but
        # must not make us switch to a newer path generation for verification.
        os.lseek(fd, 0, os.SEEK_SET)
        expected = memoryview(raw)
        offset, remaining = 0, limit + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            end = offset + len(chunk)
            if end > limit or chunk != expected[offset:end]:
                raise BundleUnavailable("bundle unavailable")
            offset = end
            remaining -= len(chunk)
        final = os.fstat(fd)
        if offset != len(raw) or (after.st_size, after.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise BundleUnavailable("bundle unavailable")
        return raw
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def read_bundle_snapshot(path):
    """Return one verified byte sequence, its parsed JSON and its exact raw SHA."""
    try:
        raw = _read_regular_bytes(path, MAX_BYTES)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(BundleUnavailable("invalid bundle")))
        if not isinstance(value, dict):
            raise BundleUnavailable("invalid bundle")
        return raw, value, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise BundleUnavailable("bundle unavailable") from exc


def read_bundle(path):
    """Compatibility entrypoint for consumers needing only the parsed bundle."""
    return read_bundle_snapshot(path)[1]


def _entry(entry):
    if not isinstance(entry, dict) or set(entry) != {"payload", "sha256"}:
        raise BundleUnavailable("invalid bundle")
    payload, checksum = entry["payload"], entry["sha256"]
    if not isinstance(payload, dict) or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise BundleUnavailable("invalid bundle")
    try:
        if digest(payload) != checksum:
            raise BundleUnavailable("invalid bundle")
    except (ValueError, TypeError, RecursionError) as exc:
        raise BundleUnavailable("invalid bundle") from exc
    return payload


def _freshness(payload, source, now, issues, *, observation_only=False):
    """Export/capture freshness is never substituted for business freshness."""
    stale, known = False, observation_only
    exported = payload.get("exportedAt")
    if not observation_only and exported is None:
        _issue(issues, source, "export_time_missing", "导出时间缺失；本路不能判为当前可用")
        return "unavailable"
    if exported is not None:
        stamp = _instant(exported)
        if stamp is None or (stamp - now).total_seconds() > FUTURE_SECONDS:
            raise BundleUnavailable("invalid source timestamp")
        stale |= (now - stamp).total_seconds() > SNAPSHOT_SECONDS
    if payload.get("stale") is True:
        stale, known = True, True
    source_time = _instant(payload.get("sourceTime"))
    if not observation_only and source_time is None:
        _issue(issues, source, "source_time_invalid", "业务时间缺失或无有效时区；本路不能判为当前可用")
        return "unavailable"
    if source_time and (source_time - now).total_seconds() > FUTURE_SECONDS:
        raise BundleUnavailable("invalid source timestamp")
    valid_stale = observation_only or type(payload.get("stale")) is bool
    if not valid_stale:
        _issue(issues, source, "stale_flag_unknown", "上游stale缺失或不是布尔值；不判为新鲜")
    policy = _dict(payload.get("freshnessPolicy"))
    deadline = _instant(policy.get("deadlineAt"))
    if source == "projects" and source_time:
        known = True
        stale |= (now - source_time).total_seconds() > SNAPSHOT_SECONDS
    elif source == "progress" and source_time:
        known = True
        stale |= (now - source_time).total_seconds() > 30 * 3600
    elif deadline is not None:
        known = True
        stale |= now > deadline
    elif source in {"today", "delivery"} and source_time:
        stale |= (now - source_time).total_seconds() > 30 * 3600
        _issue(issues, source, "freshness_unknown", "缺少合法报告日历截止；按30小时保守检查，不判为新鲜")
    if stale:
        _issue(issues, source, "source_stale", "上游导出或业务证据已陈旧；重新捕获不改变该状态")
        return "stale"
    if not known or not valid_stale:
        _issue(issues, source, "freshness_unknown", "缺少可用业务时效契约；不判为新鲜")
        return "unknown"
    return "fresh"


def _report(payload, fresh, issues):
    row = _dict(payload.get("report"))
    report_date, checksum = _day(row.get("targetDate")), row.get("sha256")
    if not report_date or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        _issue(issues, "today", "report_unavailable", "报告缺少日期或完整原报告指纹")
        return []
    raw_blocks = row.get("blocks") if isinstance(row.get("blocks"), list) else None
    body_block_count = len(raw_blocks) if raw_blocks is not None else None
    blocks = []
    body_truncated = raw_blocks is None or len(raw_blocks) > 128
    if raw_blocks is None:
        code = "body_not_provided" if "blocks" not in row else "block_unsupported"
        message = "来源未提供正文段落；段数与完整性未知" if "blocks" not in row else "正文段落不是列表；段数未知，不能当作完整0段正文"
        _issue(issues, "today", code, message)
    for block in (raw_blocks or [])[:128]:
        if isinstance(block, dict):
            text = _text(block.get("text"), 2000)
            if text is not None:
                blocks.append({"heading": _text(block.get("heading"), 160) or "", "text": text})
                body_truncated |= len(block["text"]) > 2000 or (isinstance(block.get("heading"), str) and len(block["heading"]) > 160)
            else:
                body_truncated = True
                _issue(issues, "today", "block_unsupported", "某报告段落结构未支持；仅展示可识别纯文本，不执行链接或嵌入内容")
        else:
            body_truncated = True
            _issue(issues, "today", "block_unsupported", "某报告段落结构未支持；仅展示可识别纯文本，不执行链接或嵌入内容")
    if body_truncated:
        _issue(issues, "today", "content_truncated", "报告正文缺失、结构不支持或受128段/单段2000字边界限制，未完整保留；不能当作完整正文")
    state = _status(row.get("dataStatus"))
    if fresh in {"stale", "unavailable"}:
        state = fresh
    elif fresh == "unknown" and state in {"ok", "pass"}:
        state = "unknown"
    if body_truncated and state in {"ok", "pass"}:
        state = "partial"
    if state not in {"ok", "pass"}:
        _issue(issues, "today", "source_partial", "报告状态不是全部通过")
    return [{"id": f"nng8:astock:{report_date}:{checksum}", "title": "A股基础数据快报",
             "report_date": report_date, "generated_at": _timestamp(row.get("generatedAt")),
             "data_cutoff": _day(row.get("dataCutoff")), "source_time": _timestamp(payload.get("sourceTime")),
             "evidence_cutoff": _text(row.get("cutoffLocal"), 80), "status": state,
             "publication_gate": _text(row.get("publicationGate"), 64) or "unknown",
             "review_status": _text(row.get("reviewStatus"), 64) or "unknown",
             "conclusion": _texts(row.get("conclusion")), "risks": _texts(row.get("risks")),
             "blocks": blocks, "body_block_count": body_block_count, "body_truncated": body_truncated,
             "content_sha256": checksum, "source": "nng8/astock"}]


def _quality(payload, fresh, issues):
    market = _dict(payload.get("coreMarket"))
    quality = _dict(market.get("quality"))
    decision = _dict(payload.get("activeRunDecision"))
    verify = _dict(decision.get("verification"))
    active = payload.get("activeContract")
    contract = active.get("id") if isinstance(active, dict) else active
    state = _status(quality.get("priceStatus") or market.get("status"))
    limitations = ["full_snapshot=false 不等于完整全市场快照通过"] if market.get("fullSnapshot") is False else []
    if not market or not quality:
        state = "unknown"
        limitations.append("当前市场质量字段缺失；不以历史问题日期替代当前行情日期")
    elif state in {"ok", "pass"} and (market.get("fullSnapshot") is not True or fresh != "fresh"):
        state = "partial" if market.get("fullSnapshot") is False else "unknown"
    if fresh in {"stale", "unavailable"}:
        state = fresh
    counts = [_count(quality.get(key)) for key in ("eligible", "valid", "invalid")]
    # Nested quality belongs to this coreMarket only. Missing metadata may inherit
    # explicitly named parent metadata; conflicting explicit values never inherit.
    parent_scope = market.get("scope") if "scope" in market else _dict(active).get("scope")
    scope = quality.get("scope") if "scope" in quality else parent_scope
    target = _day(quality.get("targetDate")) if "targetDate" in quality else _day(market.get("latestValidDate"))
    parent_contract = market.get("contract") if "contract" in market else contract
    quality_contract = quality.get("contract") if "contract" in quality else parent_contract
    verified_scope = (scope in {"csi_members", "full_market", "sealed_core_qlib_summary"} if isinstance(scope, str) else False)
    verified_scope = (verified_scope and (parent_scope is None or scope == parent_scope)
                      and target is not None and target == _day(market.get("latestValidDate"))
                      and isinstance(contract, str) and bool(contract)
                      and quality_contract == contract and parent_contract == contract)
    if scope == "sealed_core_qlib_summary":
        limitations.append("封存的QLib核心摘要，不是完整全市场价格档案")
    if not verified_scope or any(value is None for value in counts) or counts[1] + counts[2] != counts[0]:
        counts = [None, None, None]
        limitations.append("标的数缺少同一scope/targetDate/contract的完整可核验三元组；不补零或混算")
        _issue(issues, "progress", "quality_counts_unknown", "eligible/valid/invalid 的完整性或口径不能核验")
        if state in {"ok", "pass"}:
            state = "unknown"
    dates = []
    for issue in _dict(payload.get("health")).get("issues", [])[:32] if isinstance(_dict(payload.get("health")).get("issues"), list) else []:
        if not isinstance(issue, dict):
            continue
        for day in issue.get("dates", [])[:64] if isinstance(issue.get("dates"), list) else []:
            parsed = _day(day)
            if parsed and parsed not in dates and len(dates) < 64:
                dates.append(parsed)
    if _status(_dict(payload.get("health")).get("status")) == "failed":
        _issue(issues, "progress", "health_critical", "上游业务健康存在严重问题；当前运行核验通过不覆盖该问题")
    if dates:
        _issue(issues, "progress", "historical_issues", "存在历史问题日期；与当前运行核验分开展示")
    if state not in {"ok", "pass"}:
        _issue(issues, "progress", "source_partial", "当前质量只代表本路证据；不以历史健康问题冒充当前全量失败")
    return {"status": state, "source_time": _timestamp(payload.get("sourceTime")),
            "active_contract": _text(contract, 160), "market_date": _day(market.get("latestValidDate")),
            "current_run_status": _status(verify.get("status")),
            "full_snapshot": market.get("fullSnapshot") if type(market.get("fullSnapshot")) is bool else None,
            "eligible": counts[0], "valid": counts[1], "invalid": counts[2],
            "counts_scope": scope if verified_scope else None, "counts_contract": quality_contract if verified_scope else None,
            "counts_target_date": target if verified_scope else None,
            "historical_issue_dates": dates, "limitations": limitations}


def _delivery(payload, fresh, now, issues):
    labels = {"seal": "报告封存", "relay": "投递中继", "telegram": "Telegram 投递"}
    layers = _dict(payload.get("layers"))
    rows = []
    for name, label in labels.items():
        layer = _dict(layers.get(name))
        state = _status(layer.get("status"), "unavailable" if not layer else "unknown")
        source_time = _timestamp(layer.get("sourceTime"))
        instant = _instant(layer.get("sourceTime"))
        if layer and instant is None:
            _issue(issues, "delivery", "layer_time_unknown", "投递层缺少独立有效业务时间；不从外层导出时间补入")
        elif instant and (instant - now).total_seconds() > FUTURE_SECONDS:
            source_time, instant, state = None, None, "unknown"
            _issue(issues, "delivery", "layer_time_future", "投递层业务时间超出未来容差；不能判通过")
        elif instant and (now - instant).total_seconds() > 30 * 3600:
            state = "stale"
            _issue(issues, "delivery", "layer_time_stale", "投递层业务证据超过30小时；外层重新导出不洗鲜")
        if fresh in {"stale", "unavailable"} and layer:
            state = fresh
        elif (fresh == "unknown" or source_time is None) and state in {"ok", "pass"}:
            state = "unknown"
        rows.append({"id": name, "label": label, "status": state,
                     "source_time": source_time,
                     "limitations": _texts(layer.get("limitations")) + (["本层未提供独立source_time，不从捕获/其他层填入"] if source_time is None else [])})
    return rows


def _services(payload, fresh):
    rows = []
    for row in payload.get("services", [])[:64] if isinstance(payload.get("services"), list) else []:
        if not isinstance(row, dict) or not _text(row.get("unit"), 160):
            continue
        health = row.get("health")
        state = _status(health.get("status") if isinstance(health, dict) else health)
        if fresh in {"stale", "unavailable"}:
            state = fresh
        elif fresh == "unknown" and state in {"ok", "pass"}:
            state = "unknown"
        rows.append({"id": _text(row["unit"], 160), "label": _text(row["unit"], 160), "status": state,
                     "last_run": _timestamp(row.get("lastStart")), "next_run": _timestamp(row.get("nextElapse"))})
    return rows


def _calibration(name, payload, mode, fresh, issues):
    counts = _dict(payload.get("stats")) if name == "phase2" else payload
    nominal = counts.get("nominal_forecast_count") if name == "phase2" else counts.get("matured_forecast_count")
    observed = counts.get("observed_forecast_count")
    pending = counts.get("pending_review_n") if name == "phase2" else counts.get("pending_review_count")
    formal = counts.get("formal_n") if name == "phase2" else counts.get("eligible_forecast_count")
    state = "insufficient" if name == "ledger" and payload.get("status") == "INSUFFICIENT_PROSPECTIVE_SAMPLES" else _status(payload.get("status"))
    values = [_count(value) for value in (nominal, observed, pending, formal)]
    if name == "ledger" and payload and (any(value is None for value in values)
                                        or not (values[3] <= values[1] <= values[0] and values[2] <= values[1])):
        values, state = [None, None, None, None], "unknown"
        _issue(issues, name, "calibration_counts_unknown", "Ledger正式/已观察/已到期/待复核数量缺失或包含范围矛盾")
    elif any(raw is not None and parsed is None for raw, parsed in zip((nominal, observed, pending, formal), values)):
        state = "unknown"
        _issue(issues, name, "calibration_counts_unknown", "校准数量不是非负整数；不补零")
    elif name == "phase2" and payload and all(value is None for value in values):
        state = "unknown"
        _issue(issues, name, "calibration_counts_unknown", "Phase2缺少可识别统计；不以空对象判全部通过")
    if mode == "UNKNOWN" and state in {"ok", "pass"}:
        state = "unknown"
    if fresh in {"stale", "unavailable"}:
        state = fresh
    if not payload:
        state = "unavailable"
    return {"id": name, "label": "研究账本" if name == "ledger" else "Phase2 冻结校准",
            "runtime_mode": mode, "status": state, "nominal": values[0], "observed": values[1],
            "pending_review": values[2], "formal": values[3], "limitations": [CALIBRATION_LIMIT]}


def normalize_bundle(bundle, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise BundleUnavailable("zoned observation required")
    now = now.astimezone(timezone.utc)
    if not isinstance(bundle, dict) or bundle.get("schema") != "nng8.readonly-bundle.v1":
        raise BundleUnavailable("invalid bundle")
    captured = _instant(bundle.get("captured_at"))
    if captured is None or (captured - now).total_seconds() > FUTURE_SECONDS:
        raise BundleUnavailable("invalid bundle")
    documents = bundle.get("documents")
    if not isinstance(documents, dict) or not set(documents).issubset(DOCUMENTS):
        raise BundleUnavailable("invalid bundle")
    docs = {name: _entry(entry) for name, entry in documents.items()}
    modes = bundle.get("runtime_modes", {})
    if not isinstance(modes, dict) or not set(modes).issubset({"ledger", "phase2"}):
        raise BundleUnavailable("invalid bundle")
    modes = {name: _entry(entry) for name, entry in modes.items()}
    issues, freshness = [], {}
    stale = (now - captured).total_seconds() > SNAPSHOT_SECONDS
    if stale:
        _issue(issues, "bundle", "snapshot_stale", "捕获已超过15分钟；仅代表捕获时读数")
    for name in sorted(DOCUMENTS):
        if name not in docs:
            _issue(issues, name, "source_unavailable", "该路证据缺失")
        else:
            freshness[name] = _freshness(docs[name], name, now, issues, observation_only=name in {"ledger", "phase2"})
    runtime = {}
    for name in ("ledger", "phase2"):
        mode = modes.get(name, {})
        observed = _instant(mode.get("observed_at"))
        if observed is not None and (observed - now).total_seconds() > FUTURE_SECONDS:
            raise BundleUnavailable("invalid runtime observation")
        if mode and (set(mode) != {"mode", "observed_at"} or not isinstance(mode.get("mode"), str)
                     or mode.get("mode") not in {"RUNNING", "FROZEN", "UNKNOWN"} or not observed):
            raise BundleUnavailable("invalid runtime observation")
        runtime[name] = mode.get("mode") if observed else None
        if not runtime[name]:
            actual = docs.get(name, {}).get("runtime_mode")
            runtime[name] = actual if isinstance(actual, str) and actual in {"RUNNING", "FROZEN", "UNKNOWN"} else None
        if not runtime[name]:
            runtime[name] = "UNKNOWN"
            _issue(issues, name, "runtime_unknown", "缺少实际运行模式观测；不从目录存在性推断")
        elif observed and (now - observed).total_seconds() > SNAPSHOT_SECONDS:
            _issue(issues, name, "runtime_stale", "运行模式观测已陈旧")
    reports = _report(docs.get("today", {}), freshness.get("today", "unknown"), issues) if "today" in docs else []
    quality = _quality(docs.get("progress", {}), freshness.get("progress", "unknown"), issues)
    if "progress" not in docs:
        quality["status"] = "unavailable"
    delivery = _delivery(docs.get("delivery", {}), freshness.get("delivery", "unknown"), now, issues)
    services = _services(docs.get("projects", {}), freshness.get("projects", "unknown"))
    if "projects" in docs and not services:
        _issue(issues, "projects", "services_unavailable", "没有可识别任务状态；不以空列表判全部通过")
    calibration = [_calibration(name, docs.get(name, {}), runtime[name], freshness.get(name, "unknown"), issues) for name in ("ledger", "phase2")]
    available = bool(any(row["status"] != "unavailable" for row in reports)
                     or (quality["status"] != "unavailable" and _dict(docs.get("progress", {}).get("coreMarket")))
                     or (freshness.get("delivery") != "unavailable" and _dict(docs.get("delivery", {}).get("layers")))
                     or any(row["status"] != "unavailable" for row in services)
                     or any(row["status"] != "unavailable" and (_count(row["nominal"]) is not None or _count(row["observed"]) is not None) for row in calibration))
    states = [row["status"] for row in reports + delivery + services + calibration] + [quality["status"]]
    status = "unavailable" if not available else "stale" if stale else "partial" if issues or any(state not in {"ok", "pass"} for state in states) else "ok"
    result = {"contract_version": CONTRACT, "source": "nng8", "captured_at": captured.isoformat(),
              "observed_at": now.isoformat(), "snapshot_stale": stale, "status": status,
              "reports": reports, "data_quality": quality, "delivery": delivery, "services": services,
              "calibration": calibration, "issues": issues, "limitations": [DISCLAIMER, "共享捕获副本仅供显式账号白名单查看；不是逐用户原始账本"]}
    return result, 200 if available else 503


def load_configured_bundle(now=None):
    if __package__:
        from .nng8_sync_health import load_sync_health
    else:
        # Keep standalone diagnostic imports compatible without changing sys.path.
        import importlib.util
        spec = importlib.util.spec_from_file_location("nng8_sync_health", Path(__file__).with_name("nng8_sync_health.py"))
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        load_sync_health = health.load_sync_health

    now = now or datetime.now(timezone.utc)
    _raw, bundle, checksum = read_bundle_snapshot(os.getenv("NNG8_BUNDLE_FILE", ""))
    result, status = normalize_bundle(bundle, now)
    result["sync"] = load_sync_health(os.getenv("NNG8_SYNC_HEALTH_FILE", ""), checksum,
                                      bundle["captured_at"], now, _read_regular_bytes)
    return result, status
