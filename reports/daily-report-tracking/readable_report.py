"""Pure projection of already verified Profit report facts, never research.

The source body is retained in the frozen summary with its SHA and execution
identity. Every displayed card/statistic is an exact line of that body. This
module neither prices a candidate nor recomputes any return or research result.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

PRESENTATION_SCHEMA = "quant.daily-readable.v1"
SOURCE_SCHEMA = "quant.profit-readable-source.v1"
MAX_SOURCE_BYTES = 64 * 1024
MAX_CARDS = 40
MAX_TEXT_UTF16 = 3200  # reserve the existing immutable-evidence footer
# Real retained security names may contain internal ASCII spaces (e.g. 万  科Ａ).
# Preserve them exactly; edges and all other whitespace remain forbidden.
_CARD = re.compile(r"^(?P<name>[^\s｜|<>](?:(?:[^\s｜|<>]| ){0,28}[^\s｜|<>])?) (?P<code>\d{6})｜信号 (?P<day>\d{4}-\d{2}-\d{2})$", re.M)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PERFORMANCE_PREFIXES = (
    "证据：运行", "闭合胜率 ", "以上为逐笔信号统计", "本期比较基准：",
    "历史回放（非前瞻）：", "历史回撤：",
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


def _require(ok, reason):
    if not ok:
        raise ValueError(reason)


def retained_source(text, profit):
    """Called only after the collector's receipt/attempt/archive checks pass."""
    _require(isinstance(text, str) and len(text.encode("utf-8")) <= MAX_SOURCE_BYTES,
             "readable_source_budget")
    return {"schema": SOURCE_SCHEMA, "sourceText": text, "sourceReportSha256": sha(text),
            **{key: profit[key] for key in ("executionId", "resultSha256", "generatedAt", "marketDate")}}


def _one(lines, prefix, *, required=True):
    found = [line for line in lines if line.startswith(prefix)]
    _require(len(found) <= 1 and (not required or len(found) == 1), "readable_field_missing_or_ambiguous")
    if not found:
        return None
    line = found[0]
    _require(len(line) <= 350 and not any(ord(c) < 32 or ord(c) == 127 or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b" for c in line),
             "readable_field_invalid")
    return line


def _cards(section, *, candidate, day):
    headings = list(_CARD.finditer(section))
    _require(len(re.findall(r"^.*｜信号 .*?$", section, re.M)) == len(headings),
             "readable_card_heading_invalid")
    _require(len(headings) <= MAX_CARDS, "readable_card_budget")
    rows, seen = [], set()
    for i, heading in enumerate(headings):
        key = (heading["code"], heading["day"])
        _require(key not in seen, "readable_duplicate_card")
        seen.add(key)
        date.fromisoformat(heading["day"])
        if candidate:
            _require(heading["day"] == day, "readable_candidate_date_mismatch")
        else:
            _require(heading["day"] <= day, "readable_review_future_signal")
        lines = section[heading.end():headings[i+1].start() if i+1 < len(headings) else len(section)].splitlines()
        fields = {name: _one(lines, prefix, required=required) for name, prefix, required in (
            ("state", "状态：", True), ("entry", "入场参考：", candidate),
            ("condition", "条件：", candidate), ("exit", "退出：", candidate),
            ("expiry", "未满足则过期", candidate), ("cost", "单边模拟成本 ", candidate),
            ("version", "来源版本：", True), ("asOf", "核验截至 ", not candidate),
            ("simulatedEntry", "模拟入场 ", False), ("simulatedExit", "模拟退出 ", False),
            ("unrealized", "未实现净收益 ", False),
        )}
        if not candidate:
            matched = re.fullmatch(r"核验截至 (\d{4}-\d{2}-\d{2})；不是实时买点。", fields["asOf"])
            _require(matched is not None and matched[1] <= day, "readable_review_asof_invalid")
            date.fromisoformat(matched[1])
            if fields["simulatedExit"]:
                exited = re.match(r"模拟退出 (\d{4}-\d{2}-\d{2})｜", fields["simulatedExit"])
                _require(exited is not None and exited[1] <= matched[1], "readable_exit_date_invalid")
                date.fromisoformat(exited[1])
        rows.append({"name": heading["name"], "code": heading["code"], "signalDate": heading["day"],
                     "heading": heading[0], **fields})
    return rows


def _verified_source(profit):
    """Common trust boundary: no group is parsed until the entire source passes."""
    source = profit.get("readableReport")
    _require(isinstance(source, dict) and source.get("schema") == SOURCE_SCHEMA,
             "readable_report_unavailable")
    for key in ("executionId", "resultSha256", "generatedAt", "marketDate"):
        _require(source.get(key) == profit.get(key), "readable_identity_mismatch")
    text = source.get("sourceText")
    _require(isinstance(text, str) and len(text.encode("utf-8")) <= MAX_SOURCE_BYTES,
             "readable_source_budget")
    _require(not any((ord(c) < 32 and c != "\n") or ord(c) == 127
                     or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b" for c in text),
             "readable_source_control_character")
    _require(_HASH.fullmatch(str(source.get("sourceReportSha256"))) is not None
             and sha(text) == source["sourceReportSha256"] == profit.get("sourceReportSha256"), "readable_source_hash_mismatch")
    _require(re.findall(r"^执行编号：([^\r\n]+)$", text, re.M) == [profit["executionId"]]
             and re.findall(r"^生成：([^\r\n]+)$", text, re.M) == [profit["generatedAt"]],
             "readable_report_identity_mismatch")
    _require(re.findall(r"^行情与跟踪截至：(\d{4}-\d{2}-\d{2})｜收盘口径，非实时$", text, re.M)
             == [profit["marketDate"]], "readable_report_market_date_mismatch")
    return text


def _candidate_facts(profit, text):
    parts = text.split("👁 本期候选计划")
    _require(len(parts) <= 2, "readable_candidate_sections_ambiguous")
    if len(parts) == 1:
        _require("本期候选计划" not in text, "readable_candidate_section_invalid")
    section = parts[1].split("📋", 1)[0] if len(parts) == 2 else ""
    cards = _cards(section, candidate=True, day=profit["marketDate"])
    _require(type(profit.get("candidateCount")) is int and len(cards) == profit["candidateCount"],
             "readable_candidate_count_mismatch")
    identities = profit.get("candidateIdentities")
    _require(isinstance(identities, list) and len(identities) == len(cards),
             "readable_candidate_result_identity_missing")
    for card, identity in zip(cards, identities):
        _require(isinstance(identity, dict)
                 and identity.get("code") == card["code"] and identity.get("name") == card["name"]
                 and identity.get("signalDate") == card["signalDate"]
                 and re.fullmatch(r"(?:sh|sz|bj)" + card["code"], str(identity.get("symbol"))) is not None,
                 "readable_candidate_result_identity_mismatch")
    return cards


def _review_facts(profit, text):
    parts = text.split("📋 既有信号回顾（不是新买入建议）")
    _require(len(parts) <= 2, "readable_review_sections_ambiguous")
    if len(parts) == 1:
        # A malformed section marker cannot silently become "no historical data".
        _require("既有信号回顾" not in text, "readable_review_section_invalid")
        return [], "not_present"
    rows = _cards(parts[1], candidate=False, day=profit["marketDate"])
    _require(bool(rows), "readable_review_section_empty")
    return rows, "verified"


def _performance_facts(text):
    prefix = text.split("👁 本期候选计划", 1)[0].split("📋", 1)[0]
    lines = prefix.splitlines()
    performance = [_one(lines, key, required=False) for key in _PERFORMANCE_PREFIXES]
    performance = [line for line in performance if line is not None]
    if performance:
        # Positive, negative and risk statistics remain one indivisible group.
        _require(len(performance) == len(_PERFORMANCE_PREFIXES),
                 "readable_performance_incomplete")
    return performance


def _group_issue(exc):
    code = str(exc)
    return code if re.fullmatch(r"readable_[a-z_]+", code) else "readable_group_invalid"


def project(profit):
    """Verify the source once, then reject independent display groups separately."""
    text = _verified_source(profit)
    groups = {}
    cards, reviews, performance = [], [], []
    try:
        cards = _candidate_facts(profit, text)
        groups["candidates"] = {"status": "verified", "errorCode": None, "count": len(cards)}
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        groups["candidates"] = {"status": "rejected", "errorCode": _group_issue(exc), "count": None}
    try:
        reviews, state = _review_facts(profit, text)
        groups["reviews"] = {"status": state, "errorCode": None, "count": len(reviews)}
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        groups["reviews"] = {"status": "rejected", "errorCode": _group_issue(exc), "count": None}
    try:
        performance = _performance_facts(text)
        groups["performance"] = {
            "status": "verified" if performance else "not_present",
            "errorCode": None if performance else "readable_performance_not_present",
            "count": len(performance),
        }
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        groups["performance"] = {"status": "rejected", "errorCode": _group_issue(exc), "count": None}
    complete = (groups["candidates"]["status"] == "verified"
                and groups["reviews"]["status"] in ("verified", "not_present")
                and groups["performance"]["status"] == "verified")
    return {"candidates": cards, "reviews": reviews, "performance": performance,
            "sourceVerified": True, "groups": groups,
            "readableStatus": "complete" if complete else "partial",
            "sourceReportSha256": profit["readableReport"]["sourceReportSha256"],
            "scope": "same_execution_original_report_line_projection_not_recomputation"}


def render_facts(profit):
    facts = project(profit)
    cards = facts["candidates"]
    paragraphs = []
    if cards:
        common = {key: cards[0][key] for key in ("condition", "expiry", "cost", "version")
                  if all(card[key] == cards[0][key] for card in cards)}
        block = ["候选详情｜" + str(len(cards)) + "只，沿用原研究条件，仅模拟"]
        for card in cards:
            block.extend([card["heading"], card["state"], card["entry"], card["exit"]])
            block.extend(card[key] for key in ("condition", "expiry", "cost", "version") if key not in common)
        block.extend(common.values())
        paragraphs.append("\n".join(block))
    if facts["groups"]["candidates"]["status"] == "rejected":
        paragraphs.append("候选详情缺证｜本期候选的身份或关键条件未能完整核验，本组未展示；不补写或猜测、不作入场建议，原报告保留。")
    if facts["reviews"]:
        block = ["模拟记录回顾｜按各记录日期展示，不表示都在今天退出"]
        for row in facts["reviews"]:
            block.extend([row["heading"], row["state"], row["asOf"]])
            block.extend(row[key] for key in ("simulatedEntry", "simulatedExit", "unrealized") if row[key])
        paragraphs.append("\n".join(block))
    if facts["groups"]["reviews"]["status"] == "rejected":
        paragraphs.append("历史回顾缺证｜本次历史卡片未能完整核验，本组未展示；不据此推断无亏损或无历史记录，原报告保留。")
    if facts["performance"]:
        paragraphs.append("表现｜原报告已有统计，未重算\n" + "\n".join(facts["performance"]))
    elif facts["groups"]["performance"]["status"] == "rejected":
        paragraphs.append("表现缺证｜原报告已有统计未能完整核验，正负收益与风险统计均未展示；不择优展示或重算，原报告保留。")
    else:
        paragraphs.append("表现｜本次原报告未提供完整可展示的收益统计，未推算。")
    return paragraphs, facts


def bounded_join(paragraphs):
    text = "\n\n".join(paragraphs)
    _require(utf16(text) <= MAX_TEXT_UTF16, "readable_report_delivery_limit")
    return text
