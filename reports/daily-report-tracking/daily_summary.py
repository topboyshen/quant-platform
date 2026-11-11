"""Pure, fail-closed rendering of the unified research daily summary.

No clocks, files, databases, network, delivery, or mutations are used here.
The collector supplies verified evidence; this boundary additionally validates
the account, dates, identity agreement, counts, and business-state semantics.
All original evidence is retained in ``details.sources``. Legacy text retains
its five-section contract; the versioned readable projection preserves whole
source facts and refuses oversize output rather than truncating safety facts.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any

from etf_identity import coverage_identity, coverage_marker, validate_coverage_identity
from readable_report import MAX_TEXT_UTF16 as READABLE_LIMIT, PRESENTATION_SCHEMA, bounded_join, render_facts


VERSION = "daily-research-summary.v1"
SHANGHAI = timezone(timedelta(hours=8))
MAX_TEXT_UTF16 = 3499
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_IDENTIFIERS = re.compile(
    r"(?i)(?<![a-z0-9])[a-f0-9]{64}(?![a-z0-9])|"
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b"
)
_SOURCES = ("profit", "health", "etf", "capture", "portfolio")
_NATURAL_OBSERVATION_ONLY_REASON = "仅留存自然执行；正式协议待证据齐备后启用，不回填旧样本"


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(SHANGHAI) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _account(value: Any) -> str | None:
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _stamp(value: Any) -> str:
    parsed = _time(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S（北京时间）") if parsed else "读取时间未知"


def _number(value: Any) -> str:
    if type(value) in (int, float):
        try:
            return format(value, ".8g") if math.isfinite(value) else "未知"
        except (OverflowError, ValueError):
            pass
    return "未知"


def _prose(value: Any, hidden: tuple[str, ...], limit: int = 160) -> str:
    """Bound prose and prevent source payloads and immutable IDs leaking to text."""
    if not isinstance(value, str) or not value.strip():
        return ""
    if any(char in value for char in "{}[]"):
        return "具体原因见证据详情"
    clean = _IDENTIFIERS.sub("（证据标识见详情）", value)
    for identifier in hidden:
        if identifier:
            clean = clean.replace(identifier, "（证据标识见详情）")
    clean = " ".join(clean.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="replace")) // 2


def _gap_explanation(profit: dict) -> tuple[str, bool]:
    """Interpret only the named diagnostic; never infer suspension from a gap."""
    diagnostic = _mapping(profit.get("diagnostics"))
    if diagnostic.get("kind") != "oos_holding_interval_gap":
        return "", False
    symbol = diagnostic.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
        symbol = "相关股票"
    endpoint = _day(diagnostic.get("interval_end"))
    if diagnostic.get("missing_end") is True and endpoint:
        if diagnostic.get("is_last_period") is False:
            text = f"历史回测中，{symbol} 在下一调仓日 {endpoint.isoformat()} 所需的开盘价缺失。"
        else:
            text = f"历史回测中，{symbol} 在 {endpoint.isoformat()} 调仓所需的行情/价格缺失。"
    else:
        text = f"历史回测中，{symbol} 的持有区间行情不完整。"
    explanation = _mapping(profit.get("explanation"))
    start = _day(explanation.get("suspensionStart"))
    end = _day(explanation.get("suspensionEnd"))
    resumed = _day(explanation.get("resumedOn"))
    references = explanation.get("sourceReferences")
    matched = (
        explanation.get("verified") is True
        and explanation.get("cause") == "trading_suspension"
        and diagnostic.get("missing_end") is True
        and symbol != "相关股票"
        and all(explanation.get(key) == diagnostic.get(key)
                for key in ("kind", "symbol", "interval_end"))
        and endpoint is not None and start is not None and end is not None
        and resumed is not None and start <= endpoint <= end < resumed
        and isinstance(references, list) and bool(references)
    )
    if matched:
        return text + f"已核实该日停牌，无可补真实价格；停牌处理规则待单独评审。该证券于 {resumed.isoformat()} 复牌。", True
    return text + "缺失原因待核实，具体诊断见详情。", False


def _bound_text(paragraphs: list[str]) -> str:
    text = "\n\n".join(paragraphs)
    if _utf16_length(text) <= MAX_TEXT_UTF16:
        return text
    # Keep all five sections even if a malformed source supplied enormous values.
    limited = []
    for paragraph in paragraphs:
        chars, length = [], 0
        for char in paragraph:
            size = _utf16_length(char)
            if length + size > 685:
                chars.append("…")
                break
            chars.append(char)
            length += size
        limited.append("".join(chars))
    return "\n\n".join(limited)



def _readable_layout(paragraphs: list[str], facts: dict | None) -> list[str]:
    """Rearrange already validated complete groups; never add market facts.

    The originals and exact source lines remain in readableFacts. Only headings,
    line placement, and the unsigned display of a win-rate are presentation.
    Rejected groups keep their original fail-closed notices unchanged.
    """
    if not facts:
        return paragraphs
    rendered = []
    for paragraph in paragraphs:
        if paragraph.startswith("候选详情｜"):
            cards = facts["candidates"]
            common = {key: cards[0][key] for key in ("condition", "expiry", "cost", "version")
                      if all(card[key] == cards[0][key] for card in cards)}
            lines = [f"候选详情｜{len(cards)}条当日信号；不代表首次出现的证券，仅模拟"]
            for card in cards:
                lines.extend([card["heading"] + "；" + card["state"], card["entry"], card["exit"]])
                lines.extend(card[key] for key in ("condition", "expiry", "cost", "version") if key not in common)
            lines.extend(common.values())
            rendered.append("\n".join(lines))
        elif paragraph.startswith("模拟记录回顾｜"):
            rendered.append(paragraph.replace(
                "模拟记录回顾｜按各记录日期展示，不表示都在今天退出",
                f"模拟记录回顾｜原报告选取{len(facts['reviews'])}条回顾，非当日全部闭合明细；不表示都在今天退出", 1))
        elif paragraph.startswith("表现｜原报告已有统计"):
            performance = facts["performance"]
            # Keep all six source statistics together, including loss/risk and
            # replay. Changing this leading plus never changes the source value.
            lines = [line.replace("闭合胜率 +", "闭合胜率 ", 1)
                     if line.startswith("闭合胜率 +") else line for line in performance]
            rendered.append("表现｜原报告已有统计，未重算\n前瞻闭合信号统计：\n" +
                            "\n".join(lines[:4]) +
                            "\n历史回放单列；样本、窗口与统计方式未证明与前瞻一致，不能直接比较：\n" +
                            "\n".join(lines[4:]))
        else:
            rendered.append(paragraph)
    return rendered


def _readable_overview(profit: dict, profit_ok: bool, etf: dict, etf_ok: bool,
                       facts: dict | None, reference_only: bool, capture: dict,
                       capture_ok: bool, same_etf_batch: bool) -> str:
    items = []
    if reference_only:
        items.append("Profit同次原通知仅作证据引用")
    elif profit_ok:
        if profit["status"] == "DATA_BLOCKED":
            items.append("Profit数据阻断，候选计数0")
        elif profit["status"] == "CASH":
            items.append("Profit检查完成，候选计数0")
        else:
            items.append(f"Profit当日候选信号{profit['candidateCount']}条")
    else:
        items.append("Profit终态证据不足")
    final = _mapping(etf.get("finalRun"))
    if etf_ok and not same_etf_batch:
        outcome, state = final.get("decision_outcome"), final.get("status")
        if state == "success" and outcome == "no_signal":
            items.append(f"ETF最终评估{final['evaluated_count']}个目标，无信号")
        elif state in ("failed", "partial", "skipped"):
            items.append("ETF最终业务评估未完整完成，详见批次记录")
        else:
            items.append("ETF信号及放行状态见批次记录")
    elif not etf_ok:
        items.append("ETF批次证据不足")
    result = "今日要点｜" + "；".join(items) + "。"
    if facts and facts["groups"]["performance"]["status"] == "verified":
        # A source with any missing loss/risk/replay field never reaches here.
        performance = facts["performance"]
        result += "\n前瞻统计（原报告口径）：" + performance[0] + "；" + performance[1].replace("闭合胜率 +", "闭合胜率 ", 1) + "。"
    if capture_ok:
        result += f"正式入组{capture['formalAdmitted']}；观察与闭合信号记录不等于正式样本。"
    return result


def _tracking_require(ok):
    if not ok:
        raise ValueError("paper_tracking_invalid")


def _positive_id(value):
    return _count(value) and value > 0


def _positive_price(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _stock_label(row):
    code, symbol, name = row.get("code"), row.get("symbol"), row.get("name")
    _tracking_require(isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code)
                      and isinstance(symbol, str) and re.fullmatch(r"(?:sh|sz|bj)" + code, symbol))
    _tracking_require(isinstance(name, str) and name == name.strip() and
                      re.fullmatch(r"[\w\u3400-\u9fff ·*Ａ-Ｚａ-ｚ-]{1,32}", name))
    return f"{name} {code}"


def _tracked_observation(row, cutoff, event_day):
    at = _time(row.get("observedAt"))
    _tracking_require(_positive_id(row.get("pickId")) and _positive_id(row.get("observationId"))
                      and _day(row.get("tradeDate")) == event_day
                      and at is not None and at.date() == event_day and at <= cutoff
                      and isinstance(row.get("pickSha256"), str) and _SHA256.fullmatch(row["pickSha256"])
                      and isinstance(row.get("sourceSha256"), str) and _SHA256.fullmatch(row["sourceSha256"]))


def _candidate_line(row, prior, cutoff):
    label = _stock_label(row)
    _tracking_require(row.get("signalDate") == prior["day"] and _positive_id(row.get("runId"))
                      and row["runId"] == prior["runId"])
    status = row.get("status")
    if status == "unconfirmed":
        _tracking_require(all(row.get(k) is None for k in ("observationId", "observedAt", "sourceSha256",
                              "event", "open", "close", "stateBefore", "stateAfter")))
        reason = {
            "pick_missing":"缺少该候选的模拟记录",
            "observation_missing":"缺少当日观察记录",
            "pick_or_observation_ambiguous":"候选与观察记录无法唯一对应",
            "observation_unverified":"当日观察身份待核",
            "entry_event_unconfirmed":"当日状态尚不能确认入场或过期",
        }.get(row.get("reason"), "当日观察证据不足")
        return f"{label}｜{reason}，模拟入场结果待核。"
    _tracked_observation(row, cutoff, cutoff.date())
    transition = row.get("event"), row.get("stateBefore"), row.get("stateAfter")
    if status == "entered":
        _tracking_require(transition in (("NEXT_OPEN_GATE_PASSED","PENDING_ENTRY","OPEN"),
                                         ("CLOSE_BELOW_FROZEN_MA60","PENDING_ENTRY","EXIT_PENDING"))
                          and _positive_price(row.get("open")) and _positive_price(row.get("close")))
        return (label + f"｜通过开盘条件，模拟入场 {_number(row['open'])}，收盘 {_number(row['close'])}"
                + ("；当日转为待退出。" if transition[2] == "EXIT_PENDING" else "。"))
    _tracking_require(status == "expired" and transition == ("NEXT_OPEN_GATE_FAILED","PENDING_ENTRY","EXPIRED"))
    return label + "｜下一开盘条件未满足，信号过期。"


def _pending_line(row, cutoff):
    label = _stock_label(row)
    trigger, signal = _day(row.get("triggerDate")), _day(row.get("signalDate"))
    _tracking_require(trigger and signal and signal <= trigger <= cutoff.date() and _positive_id(row.get("runId")))
    _tracked_observation(row, cutoff, trigger)
    _tracking_require(row.get("stateAfter") == "EXIT_PENDING" and row.get("stateBefore") in ("OPEN", "PENDING_ENTRY"))
    if row.get("event") == "MAX_HOLDING_SESSIONS":
        _tracking_require(_positive_id(row.get("holdingSessions")))
        reason = f"已持有 {row['holdingSessions']} 个交易日，达到持有期上限"
    else:
        _tracking_require(row.get("event") == "CLOSE_BELOW_FROZEN_MA60")
        reason = "收盘低于信号日冻结的 MA60"
    return f"{label}｜{row['signalDate']} 信号，{row['triggerDate']} 进入待退出。{reason}，尚无模拟退出价。"


def _unique_rows(rows, keys):
    # Repeated securities across distinct signal dates are valid pending positions.
    return all(len({row.get(k) for row in rows if row.get(k) is not None}) ==
               sum(row.get(k) is not None for row in rows) for k in keys)


def _tracking_parts(tracking, source, portfolio, portfolio_ok):
    """Render each independently verified group from this cutoff's immutable projection."""
    unavailable = ("上期候选｜逐股结果缺证。", [])
    no_exits = ("待退出｜逐股状态及原因缺证。", [])
    checks = {"envelopeVerified": False, "priorVerified": False, "pendingVerified": False}
    try:
        _tracking_require(isinstance(tracking, dict))
        canonical = json.dumps({k:v for k,v in tracking.items() if k != "canonicalSha256"},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        day = _day(source.get("businessDate"))
        cutoff, observed, generated = (_time(x) for x in (tracking.get("cutoffAt"), tracking.get("observedAt"),
                                                            source.get("generatedAt")))
        _tracking_require(portfolio_ok and tracking.get("schema") == "quant.paper-tracking.v1"
                          and tracking.get("verified") is True and type(tracking.get("accountId")) is int
                          and tracking["accountId"] == 1 and _account(source.get("accountId")) == "1"
                          and day and _day(tracking.get("businessDate")) == day
                          and cutoff and observed and generated and cutoff.date() == observed.date() == day
                          and cutoff == _time(source.get("sourceCutoffAt")) and cutoff <= observed <= generated
                          and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == tracking.get("canonicalSha256"))
    except (ValueError, TypeError, OverflowError, UnicodeError):
        return [unavailable, no_exits], checks
    checks["envelopeVerified"] = True
    try:
        prior, candidates = _mapping(tracking.get("prior")), tracking.get("candidates")
        _tracking_require(prior.get("verified") is True and _positive_id(prior.get("runId"))
                          and isinstance(prior.get("executionId"), str) and 0 < len(prior["executionId"]) <= 128
                          and isinstance(prior.get("resultSha256"), str) and _SHA256.fullmatch(prior["resultSha256"])
                          and _day(prior.get("day")) and _day(prior["day"]) < day
                          and isinstance(candidates, list) and _count(prior.get("candidateCount"))
                          and len(candidates) == prior["candidateCount"] <= 40
                          and isinstance(prior.get("reportSha256"),str) and _SHA256.fullmatch(prior["reportSha256"])
                          and isinstance(prior.get("bindingSha256"),str) and _SHA256.fullmatch(prior["bindingSha256"])
                          and all(isinstance(x, dict) for x in candidates)
                          and prior.get("candidateIdentities") == [
                              {k: r.get(k) for k in ("code", "symbol", "name", "signalDate")} for r in candidates]
                          and _unique_rows(candidates, ("code", "pickId", "observationId")))
        status = prior.get("status")
        _tracking_require(status in ("PAPER_CANDIDATES", "CASH", "DATA_BLOCKED")
                          and (not candidates or status == "PAPER_CANDIDATES"))
        lines = [_candidate_line(row, prior, cutoff) for row in candidates]
        heading = (f"上期候选｜{prior['day']} 的 {len(lines)} 条信号，按今日模拟记录跟踪。"
                   if lines else f"上期候选｜{prior['day']} " +
                   ("选股受数据阻断。" if status == "DATA_BLOCKED" else "没有候选信号。"))
        candidates_part = heading, lines
        checks["priorVerified"] = True
    except (TypeError, ValueError, OverflowError):
        candidates_part = unavailable
    try:
        rows = tracking.get("pendingExits")
        _tracking_require(tracking.get("pendingVerified") is True and isinstance(rows, list)
                          and len(rows) == portfolio["exitPendingRecords"] <= 64 and all(isinstance(x, dict) for x in rows)
                          and _unique_rows(rows, ("pickId", "observationId")))
        pending_part = (f"待退出｜截至本次快照 {len(rows)} 条模拟记录。",
                        [_pending_line(row, cutoff) for row in rows])
        checks["pendingVerified"] = True
    except (TypeError, ValueError, OverflowError):
        pending_part = no_exits
    return [candidates_part, pending_part], checks


def _place_tracking(paragraphs, parts):
    """Retain every row in details. Fit whole rows, with an explicit count for omitted detail."""
    shown = [len(rows) for _, rows in parts]
    while True:
        blocks = []
        for (heading, rows), n in zip(parts, shown):
            extra = [f"另 {len(rows)-n} 条见本日报逐股明细。"] if n < len(rows) else []
            blocks.append("\n".join([heading, *rows[:n], *extra]))
        proposed = [paragraphs[0], *blocks, *paragraphs[1:]]
        if _utf16_length("\n\n".join(proposed)) <= READABLE_LIMIT or not any(shown):
            return proposed  # bounded_join refuses if even the complete summaries cannot fit.
        group = max(range(len(shown)), key=shown.__getitem__)
        shown[group] -= 1


def build_summary(data: dict) -> dict:
    """Return text and auditable details for the supplied account and business day.

    Required source fields follow the collector contract.  Optional source
    ``accountId`` or ``scopeId`` is checked when supplied.  Optional Profit
    ``dataReady``/``data_ready`` must agree with its business status.  Optional
    ETF ``decisionDate``/``decision_date`` on either run must match the day.
    Naive timestamps and future evidence are rejected.  A historical portfolio
    snapshot is explicitly dated and never described as today's/current state.
    Monetary values without evidence remain unknown; no NAV is synthesized.
    """
    source = deepcopy(data) if isinstance(data, dict) else {}
    if source.get("presentationSchema") not in (None, PRESENTATION_SCHEMA):
        raise ValueError("presentation_schema_unsupported")
    readable = source.get("presentationSchema") == PRESENTATION_SCHEMA
    rows = {name: _mapping(source.get(name)) for name in _SOURCES}
    business_date = _day(source.get("businessDate"))
    generated_at = _time(source.get("generatedAt"))
    account = _account(source.get("accountId"))
    global_issues = []
    if business_date is None:
        global_issues.append("业务日期缺失或格式无效")
    if generated_at is None:
        global_issues.append("日报生成时间缺失或缺少时区")
    if account is None:
        global_issues.append("账户身份缺失")
    if business_date and generated_at and business_date > generated_at.date():
        global_issues.append("业务日期晚于日报生成时间")
    checks = {name: {"verified": False, "issues": []} for name in _SOURCES}
    references: list[dict] = []
    pending: list[str] = []
    hidden = tuple(
        value for row in rows.values()
        for key, value in row.items()
        if key in ("executionId", "releaseSha256", "resultSha256")
        and isinstance(value, str)
    )

    def issue(name: str, message: str) -> None:
        if message not in checks[name]["issues"]:
            checks[name]["issues"].append(message)

    def base(name: str) -> bool:
        row = rows[name]
        if global_issues:
            issue(name, "日报日期、时间或账户身份无效")
        if row.get("verified") is not True:
            issue(name, "来源未核验或缺失")
        for key in ("accountId", "scopeId"):
            if key in row and _account(row[key]) != account:
                issue(name, "账户身份冲突")
        return not checks[name]["issues"]

    def day_matches(name: str, value: Any, label: str) -> None:
        if _day(value) is None or _day(value) != business_date:
            issue(name, label + "缺失或不属于本业务日")

    def timestamp(name: str, value: Any, label: str, same_day: bool = True) -> None:
        parsed = _time(value)
        if parsed is None:
            issue(name, label + "缺失或缺少时区")
        elif generated_at and parsed > generated_at:
            issue(name, label + "晚于日报截止时间")
        elif same_day and parsed.date() != business_date:
            issue(name, label + "不属于本业务日")

    def counts(name: str, row: dict, keys: tuple[str, ...]) -> None:
        for key in keys:
            if not _count(row.get(key)):
                issue(name, key + "缺失或不是非负整数")

    def valid(name: str) -> bool:
        result = not checks[name]["issues"]
        checks[name]["verified"] = result
        return result

    profit, health, etf, capture, portfolio = (rows[key] for key in _SOURCES)
    base("profit")
    day_matches("profit", profit.get("marketDate"), "行情日期")
    timestamp("profit", profit.get("generatedAt"), "执行生成时间")
    counts("profit", profit, ("candidateCount",))
    if not isinstance(profit.get("executionId"), str) or not profit["executionId"].strip():
        issue("profit", "执行身份缺失")
    if not _count(profit.get("runId")):
        issue("profit", "运行编号缺失或无效")
    for key in ("releaseSha256", "resultSha256"):
        if not isinstance(profit.get(key), str) or not _SHA256.fullmatch(profit[key]):
            issue("profit", "不可变证据摘要缺失或无效")
    status = profit.get("status")
    if status not in ("PAPER_CANDIDATES", "CASH", "DATA_BLOCKED"):
        issue("profit", "未取得可识别的业务终态")
    if status in ("CASH", "DATA_BLOCKED") and profit.get("candidateCount") != 0:
        issue("profit", "业务状态与候选数冲突")
    for key in ("dataReady", "data_ready"):
        if key in profit and profit[key] is not (status in ("PAPER_CANDIDATES", "CASH")):
            issue("profit", "数据就绪状态与业务状态冲突")

    base("health")
    day_matches("health", health.get("marketDate"), "健康复核行情日期")
    if not isinstance(health.get("executionId"), str) or not health["executionId"].strip():
        issue("health", "健康复核执行身份缺失")
    if not isinstance(health.get("resultSha256"), str) or not _SHA256.fullmatch(health["resultSha256"]):
        issue("health", "健康复核结果摘要缺失或无效")
    if health.get("status") not in ("PAPER_CANDIDATES", "CASH", "DATA_BLOCKED"):
        issue("health", "健康状态不是已核验的业务状态")
    identity_conflict = False
    if profit.get("verified") is True and health.get("verified") is True:
        identity_fields = ("executionId", "resultSha256", "marketDate", "status")
        if any(profit.get(key) is not None and health.get(key) is not None
               and profit[key] != health[key] for key in identity_fields):
            identity_conflict = True
            issue("profit", "研究结果与健康复核身份或业务状态冲突")
            issue("health", "研究结果与健康复核身份或业务状态冲突")
    profit_ok, health_ok = valid("profit"), valid("health")
    if not profit_ok:
        profit_text = "Profit｜证据不足；截至日报截止时间未取得有效业务终态。"
        pending.append("核验 Profit 当日结果与身份")
    else:
        count = profit["candidateCount"]
        if status == "DATA_BLOCKED":
            profit_text = "Profit｜数据阻断，候选 0；不计为无信号或成功产出。"
            reason, suspension_verified = _gap_explanation(profit)
            if not reason:
                reason = _prose(profit.get("reason"), hidden)
            if reason:
                profit_text += "原因：" + reason.rstrip("。") + "。"
            pending.append("单独评审历史停牌处理规则" if suspension_verified else "补齐 Profit 阻断所需证据")
        elif status == "CASH":
            profit_text = "Profit｜当日检查完成，空仓，候选 0。"
        else:
            profit_text = f"Profit｜当日检查完成，研究候选 {count} 个；仅为研究候选。"
        profit_text += "执行生成于 " + _stamp(profit["generatedAt"]) + "。"
        references.append({
            "sources": ["profit"] + (["health"] if health_ok else []),
            "executionId": profit["executionId"], "runId": profit["runId"],
            "marketDate": profit["marketDate"], "generatedAt": profit["generatedAt"],
            "releaseSha256": profit["releaseSha256"], "resultSha256": profit["resultSha256"],
        })
        if health_ok:
            profit_text += "健康复核引用同一执行，合计 1 次研究执行。"
    if not health_ok:
        profit_text += "健康复核证据不足。"
        pending.append("核验健康复核与研究结果一致性")
    supplemental = _mapping(profit.get("sourceDiagnostics"))
    supplemental_ok = (
        profit_ok and supplemental.get("verified") is True
        and supplemental.get("executionId") == profit.get("executionId")
        and isinstance(supplemental.get("pending"), list)
    )
    checks["profit"]["sourceDiagnosticsVerified"] = supplemental_ok
    if supplemental_ok:
        for item in supplemental["pending"][:3]:
            note = _prose(item, hidden, limit=100)
            if note:
                pending.append(note)

    base("etf")
    day_matches("etf", etf.get("decisionDate"), "ETF 决策日期")
    first, final = _mapping(etf.get("firstRun")), _mapping(etf.get("finalRun"))
    etf_coverage = None
    if "coverageIdentity" in etf:
        try:
            supplied = validate_coverage_identity(etf["coverageIdentity"],
                                                  source.get("businessDate"), account)
            expected = coverage_identity(first, final, source.get("businessDate"), account)
            if supplied != expected:
                raise ValueError("etf_coverage_source_conflict")
            etf_coverage = supplied
        except (ValueError, TypeError, KeyError, OverflowError):
            issue("etf", "ETF 批次覆盖身份与已核验来源不一致")
    etf_base_issues = list(checks["etf"]["issues"])
    run_checks: dict[str, dict] = {}
    allowed_outcomes = {
        "success": ("emitted", "no_signal", "blocked"),
        "partial": ("emitted", "blocked", "data_unavailable"),
        "failed": ("data_unavailable",), "skipped": ("skipped",),
    }

    def etf_run(row: dict, name: str) -> str:
        local_issues = list(etf_base_issues)
        state, outcome = row.get("status"), row.get("decision_outcome")
        completed = _time(row.get("completed_at"))
        if (not row or not isinstance(state, str) or state not in allowed_outcomes
                or outcome not in allowed_outcomes.get(state, ())):
            local_issues.append("缺少有效业务终态或状态组合冲突")
        if completed is None or completed.date() != business_date or (generated_at and completed > generated_at):
            local_issues.append("完成时间缺失、错日或晚于截止时间")
        for date_key in ("decisionDate", "decision_date"):
            if date_key in row and _day(row[date_key]) != business_date:
                local_issues.append("运行决策日期冲突")
        required = ("failed_count",) if name == "firstRun" and state == "failed" else ()
        if name == "finalRun":
            required += ("evaluated_count", "signal_count")
        if any(not _count(row.get(key)) for key in required):
            local_issues.append("统计计数缺失或无效")
        signals = row.get("signal_count")
        if _count(signals):
            if outcome in ("no_signal", "data_unavailable", "skipped") and signals != 0:
                local_issues.append("无信号结果与信号数量冲突")
            if outcome in ("emitted", "blocked") and signals == 0:
                local_issues.append("信号结果与信号数量冲突")
        run_checks[name] = {"verified": not local_issues, "issues": local_issues}
        label = "首轮" if name == "firstRun" else "最终轮"
        if local_issues:
            return label + "证据不足，截至截止时间未取得有效终态"
        at = completed.strftime("%H:%M:%S")
        if state == "failed":
            failed = row.get("failed_count")
            suffix = f"，{failed} 个目标取数失败" if _count(failed) else ""
            return f"{label} {at} 失败（数据不可用{suffix}），不算成功检查"
        if state == "skipped":
            return f"{label} {at} 跳过，未完成业务评估"
        prefix = f"{label} {at} " + ("部分完成" if state == "partial" else "检查完成")
        if name == "finalRun":
            prefix += f"，已评估 {row['evaluated_count']} 个目标"
        if outcome == "no_signal":
            return prefix + "，无信号，新增候选 0"
        if outcome == "data_unavailable":
            return prefix + "，数据不足，不能解释为无信号"
        signal_label = f" {signals} 个" if _count(signals) else ""
        if outcome == "blocked":
            return prefix + f"，{signal_label.strip()}信号被风控拦截，未放行"
        return prefix + f"，产生{signal_label}信号；实际放行及入组数量另核"

    first_text, final_text = etf_run(first, "firstRun"), etf_run(final, "finalRun")
    first_time, final_time = _time(first.get("completed_at")), _time(final.get("completed_at"))
    if first_time and final_time and first_time > final_time:
        run_checks["finalRun"]["verified"] = False
        run_checks["finalRun"]["issues"].append("首末运行时间顺序冲突")
        final_text = "最终轮证据不足，首末运行时间顺序冲突"
    if (etf_coverage is not None
            and etf_coverage["firstRun"]["runId"] == etf_coverage["finalRun"]["runId"]
            and etf_coverage["firstRun"] != etf_coverage["finalRun"]):
        issue("etf", "同一 ETF 批次对应不同结果身份")
        for run in run_checks.values():
            run["verified"] = False
            run["issues"].append("同一 ETF 批次对应不同结果身份")
        first_text = "首轮证据不足，同一批次结果身份冲突"
        final_text = "最终轮证据不足，同一批次结果身份冲突"
    checks["etf"]["runs"] = run_checks
    for run in run_checks.values():
        for message in run["issues"]:
            issue("etf", message)
    etf_ok = valid("etf")
    if not etf_ok:
        etf_coverage = None
    checks["etf"]["coverageIdentityVerified"] = etf_coverage is not None
    same_etf_batch = (etf_ok and etf_coverage is not None
                      and etf_coverage["firstRun"] == etf_coverage["finalRun"])
    checks["etf"]["sameFirstFinalBatch"] = same_etf_batch
    if readable and same_etf_batch:
        etf_text = "ETF｜" + final_text.replace("最终轮 ", "同一自然批次 ", 1) + "；首轮与最终记录为同一批次，不重复计检查。"
    else:
        etf_text = "ETF｜" + first_text + "；" + final_text + "。"
    if etf_ok and _mapping(etf.get("evidence")).get("savedReportStale") is True:
        etf_text += "保存报表早于终态；已按同批次数据库与不可变运行收据对账。"
    if etf_coverage is not None and not readable:
        # Provider body SHA now binds the whole future ETF coverage projection.
        # This bounded line is part of the existing ETF section, never an old
        # frozen-summary retrofit and never a second business execution.
        etf_text += "\n" + coverage_marker(etf_coverage)
    if not etf_ok:
        pending.append("核验 ETF 当日首轮与最终轮记录")
    elif final.get("status") in ("failed", "partial", "skipped"):
        pending.append("跟进 ETF 未完成的业务评估")
    if not etf_base_issues:
        reference = {"sources": ["etf"], "decisionDate": etf["decisionDate"],
                     "firstRunCompletedAt": first.get("completed_at"),
                     "finalRunCompletedAt": final.get("completed_at")}
        if etf_coverage is not None:
            reference.update(firstRunId=etf_coverage["firstRun"]["runId"],
                             finalRunId=etf_coverage["finalRun"]["runId"],
                             firstRunResultSha256=etf_coverage["firstRun"]["resultSha256"],
                             finalRunResultSha256=etf_coverage["finalRun"]["resultSha256"])
        references.append(reference)

    base("capture")
    timestamp("capture", capture.get("observedAt"), "样本读取时间")
    capture_keys = ("developmentOnly", "forwardObserved", "formalAdmitted", "todayNewForward", "todayNewFormal")
    counts("capture", capture, capture_keys)
    if all(_count(capture.get(key)) for key in capture_keys):
        if (capture["formalAdmitted"] > capture["forwardObserved"]
                or capture["todayNewForward"] > capture["forwardObserved"]
                or capture["todayNewFormal"] > capture["formalAdmitted"]):
            issue("capture", "样本分层计数冲突")
    capture_ok = valid("capture")
    if capture_ok:
        sample_text = (
            f"样本｜Profit：开发样本 {capture['developmentOnly']}，"
            f"前瞻观察 {capture['forwardObserved']}（今日新增 {capture['todayNewForward']}），"
            f"正式入组 {capture['formalAdmitted']}（今日新增 {capture['todayNewFormal']}）。"
        )
        reason = _prose(capture.get("protocolReason"), hidden)
        if reason:
            sample_text += "正式协议：" + reason + "。"
        if capture["formalAdmitted"] == 0:
            pending.append("跟进正式样本入组证据")
        references.append({"sources": ["capture"], "observedAt": capture["observedAt"]})
    else:
        sample_text = "样本｜Profit 样本证据不足，今日新增和正式入组数量未知。"
        pending.append("核验 Profit 样本分层与今日增量")

    samples = _mapping(etf.get("samples"))
    sample_issues = list(etf_base_issues)
    sample_keys = ("candidateRecords", "eligibleMature", "excluded", "todayNewEligible")
    if any(not _count(samples.get(key)) for key in sample_keys):
        sample_issues.append("ETF 样本数量缺失或无效")
    else:
        if (samples["eligibleMature"] + samples["excluded"] > samples["candidateRecords"]
                or samples["todayNewEligible"] > samples["candidateRecords"]):
            sample_issues.append("ETF 样本计数冲突")
        mature_date = _day(samples.get("matureAsOf"))
        if samples["eligibleMature"] > 0 and (mature_date is None or (business_date and mature_date > business_date)):
            sample_issues.append("成熟样本评估日期缺失或超前")
    checks["etf"]["samples"] = {"verified": not sample_issues, "issues": sample_issues}
    if sample_issues:
        sample_text += "ETF 样本证据不足。"
        pending.append("核验 ETF 样本口径与评估日期")
    else:
        sample_text += (
            f"ETF：候选记录 {samples['candidateRecords']}，可纳入成熟样本 n={samples['eligibleMature']}，"
            f"排除 {samples['excluded']}，今日新增可纳入候选 {samples['todayNewEligible']}。"
        )
        if samples["eligibleMature"]:
            sample_text += f"成熟评估截至 {samples['matureAsOf']}；多个持有期不重复计样本。"
        else:
            sample_text += "尚无可纳入成熟样本。"
        sample_text += "ETF 样本独立于 Profit 正式入组。"

    base("portfolio")
    timestamp("portfolio", portfolio.get("observedAt"), "持仓读取时间", same_day=False)
    portfolio_keys = ("openRecords", "exitPendingRecords", "activeSecurities", "overlapRecords")
    counts("portfolio", portfolio, portfolio_keys)
    if all(_count(portfolio.get(key)) for key in portfolio_keys):
        active = portfolio["openRecords"] + portfolio["exitPendingRecords"]
        if (portfolio["activeSecurities"] > active
                or portfolio["overlapRecords"] != active - portfolio["activeSecurities"]):
            issue("portfolio", "持仓记录、证券与重叠数量冲突")
    portfolio_ok = valid("portfolio")
    portfolio_time = _time(portfolio.get("observedAt"))
    if portfolio_ok:
        historical = portfolio_time.date() != business_date
        description = "历史持仓快照（非今日持仓）" if historical else "当前持仓快照"
        portfolio_text = (
            f"持仓｜{description}，读取于 {_stamp(portfolio['observedAt'])}；"
            f"未退出记录 {active}（持有中 {portfolio['openRecords']}、待退出 {portfolio['exitPendingRecords']}），"
            f"涉及 {portfolio['activeSecurities']} 只证券，重叠记录 {portfolio['overlapRecords']}。"
            f"实际资金占用：{_number(portfolio.get('actualCashUsed'))}；"
            f"组合收益：{_number(portfolio.get('portfolioReturn'))}；现金/NAV 账户依据未提供时不估算。"
        )
        checks["portfolio"]["historical"] = historical
        references.append({"sources": ["portfolio"], "observedAt": portfolio["observedAt"], "historical": historical})
        if historical:
            pending.append("补充今日持仓读取证据")
    else:
        portfolio_text = "持仓｜证据不足；读取时间：" + _stamp(portfolio.get("observedAt")) + "；持仓数量、实际资金占用和组合收益未知。"
        pending.append("核验持仓读取时间与数量")

    source_errors = source.get("sourceErrors", [])
    if not isinstance(source_errors, list):
        source_errors = [source_errors]
    if source_errors:
        pending.append("处理来源读取或核验异常（详见证据）")
    if identity_conflict:
        pending.insert(0, "先消除研究结果与健康复核身份冲突")
    if global_issues:
        pending.insert(0, "补齐有效业务日期、日报时间和账户身份")
    pending = list(dict.fromkeys(pending))
    pending_text = "待处理｜" + ("；".join(pending) if pending else "未发现新增待处理项") + "。"
    pending_text += "日报截至 " + _stamp(source.get("generatedAt")) + "。"
    coverage = _mapping(source.get("notificationCoverage"))
    if (profit_ok and coverage.get("mode") == "reference_only"
            and coverage.get("executionId") == profit.get("executionId")
            and coverage.get("resultSha256") == profit.get("resultSha256")
            and coverage.get("previousStatus") in ("sent", "unknown", "blocked")):
        # Keep all research facts and machine identities in details. A prior
        # uncertain original owns this research copy too: a new path/version
        # must not turn UNKNOWN into permission to send its full text again.
        prior = ("已有平台接受回执，非已读确认" if coverage["previousStatus"] == "sent"
                 else "先前通知结果待核，不自动重发")
        profit_text = "Profit｜同次研究原通知" + prior + "；本日报仅提供证据引用，不重复展开研究正文。"
    readable_facts = None
    if readable:
        extra = []
        reference_only = (profit_ok and coverage.get("mode") == "reference_only"
                          and coverage.get("executionId") == profit.get("executionId")
                          and coverage.get("resultSha256") == profit.get("resultSha256")
                          and coverage.get("previousStatus") in ("sent", "unknown", "blocked"))
        if profit_ok and not reference_only:
            try:
                extra, readable_facts = render_facts(profit)
                checks["profit"]["readableSourceVerified"] = True
                checks["profit"]["readableGroups"] = readable_facts["groups"]
                checks["profit"]["readableStatus"] = readable_facts["readableStatus"]
                checks["profit"]["readableReportVerified"] = readable_facts["readableStatus"] == "complete"
                group_issues = [{"group": group, "status": value["status"], "code": value["errorCode"]}
                                for group, value in readable_facts["groups"].items() if value["errorCode"]]
                checks["profit"]["readableReportIssues"] = group_issues
                if group_issues:
                    checks["profit"]["readableReportIssue"] = group_issues[0]["code"]
                if status == "PAPER_CANDIDATES":
                    rationale = _prose(profit.get("reason"), hidden)
                    profit_text += ("共同筛选依据（原执行）：" + rationale + "。" if rationale
                                    else "原结果未提供可展示的入选依据，不代拟。")
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                checks["profit"]["readableReportVerified"] = False
                checks["profit"]["readableSourceVerified"] = False
                checks["profit"]["readableStatus"] = "source_rejected"
                checks["profit"]["readableGroups"] = {
                    group: {"status": "blocked", "errorCode": "readable_source_unverified", "count": None}
                    for group in ("candidates", "reviews", "performance")}
                checks["profit"]["readableReportIssue"] = (str(exc) if isinstance(exc, ValueError)
                                                         else "readable_projection_invalid")
                checks["profit"]["readableReportIssues"] = [{"group": "source", "status": "rejected",
                                                            "code": checks["profit"]["readableReportIssue"]}]
                if status == "DATA_BLOCKED" and checks["profit"]["readableReportIssue"] == "readable_report_market_date_mismatch":
                    profit_text += "原报告行情日期标记未通过校验，本日报不引用其中的表现。"
                else:
                    profit_text += "候选详情及表现未能完整核验；不补写或猜测，原报告保留。"
        if capture_ok:
            sample_text = (f"样本进度｜Profit：前瞻观察 {capture['forwardObserved']}（今日+{capture['todayNewForward']}），"
                           f"正式入组 {capture['formalAdmitted']}（今日+{capture['todayNewFormal']}），"
                           f"开发观察 {capture['developmentOnly']}。观察次数不是交易次数。")
            sample_text += "前瞻观察按执行观察计数；闭合信号按记录计数；正式入组按协议准入计数。"
            if capture['formalAdmitted'] == 0:
                if capture.get("protocolReason") == _NATURAL_OBSERVATION_ONLY_REASON:
                    sample_text += "当前仅留存自然执行观察；正式入组协议尚未启用。"
                else:
                    sample_text += "正式入组0；观察不会自动转为正式样本。"
        else:
            sample_text = "样本进度｜Profit样本证据不足，不能据此填0。"
        if sample_issues:
            sample_text += "ETF样本证据不足。"
        else:
            sample_text += (f"ETF：候选决策 {samples['candidateRecords']}，合格成熟 {samples['eligibleMature']}，"
                            f"排除 {samples['excluded']}；今日新增可纳入候选 {samples['todayNewEligible']}。")
            if samples['eligibleMature']:
                sample_text += f"{samples['matureAsOf']}是历史合格样本的评估日期，不是今日ETF运行日期。"
            sample_text += "一个决策多个持有期只计一次；与Profit正式样本分开。"
        if portfolio_ok:
            description = "历史模拟快照（非今日）" if historical else "模拟跟踪快照"
            portfolio_text = (f"模拟跟踪｜{description}，读取于 {_stamp(portfolio['observedAt'])}；"
                              f"持有中 {portfolio['openRecords']} 条、待退出 {portfolio['exitPendingRecords']} 条，"
                              f"共 {active} 条记录、{portfolio['activeSecurities']} 只证券；"
                              f"超出每证券一条的重叠记录 {portfolio['overlapRecords']} 条。非实盘持仓，不推算资金占用或组合收益。")
        else:
            portfolio_text = "模拟跟踪｜记录数量或读取时间证据不足；不当作实盘持仓。"
        if capture_ok and capture['formalAdmitted'] == 0:
            protocol_reason = _prose(capture.get("protocolReason"), hidden)
            generic = "跟进正式样本入组证据"
            replacement = ("正式入组门禁：" + protocol_reason if protocol_reason else
                           "核验正式入组的具体门禁缺项（本次来源未提供逐项清单）")
            pending = [replacement if item == generic else item for item in pending]
            pending_text = "待处理｜" + ("；".join(pending) if pending else "未发现新增待处理项") + "。"
            pending_text += "日报截至 " + _stamp(source.get("generatedAt")) + "。"
        extra = _readable_layout(extra, readable_facts)
        overview = _readable_overview(profit, profit_ok, etf, etf_ok, readable_facts,
                                      reference_only, capture, capture_ok, same_etf_batch)
        title = f"{source.get('businessDate')} 收盘研究日报｜仅研究/模拟，不下单"
        paragraphs = [title, overview, profit_text, *extra, etf_text, portfolio_text, sample_text, pending_text]
        if "tracking" in portfolio and coverage.get("mode") != "reference_only":
            tracking_parts, tracking_checks = _tracking_parts(portfolio["tracking"], source, portfolio, portfolio_ok)
            checks["portfolio"]["tracking"] = tracking_checks
            paragraphs = _place_tracking(paragraphs, tracking_parts)
        text = bounded_join(paragraphs)
    else:
        text = _bound_text([profit_text, etf_text, sample_text, portfolio_text, pending_text])
    if etf_coverage is not None and not readable and coverage_marker(etf_coverage) not in text.splitlines():
        raise ValueError("etf_coverage_marker_truncated")
    for reference in references:
        reference["accountId"] = source.get("accountId")
    return {
        **({"presentationSchema": PRESENTATION_SCHEMA} if readable else {}),
        "version": VERSION, "businessDate": source.get("businessDate"),
        "generatedAt": source.get("generatedAt"), "text": text,
        "sourceReferences": references,
        "details": {
            "accountId": source.get("accountId"), "sources": rows, "checks": checks,
            "globalIssues": global_issues, "sourceErrors": source_errors,
            "pending": pending, "researchExecutionCount": 1 if profit_ok else 0,
            "profitHealthSameExecution": profit_ok and health_ok,
            "identityConflict": identity_conflict, "textUtf16Length": _utf16_length(text),
            "notificationCoverage": coverage,
            "etfCoverage": etf_coverage,
            **({"readableFacts": readable_facts} if readable else {}),
        },
    }
