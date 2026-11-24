"""Human-readable Telegram text for outbox rows.

Shadow rows keep their machine-readable JSON body for C7/C10 comparison.  Live delivery
renders the stable fields a person needs; unknown payloads fall back to subject + body.
"""

from __future__ import annotations

import json

from qp.models.notify import Outbox

MAX_TEXT = 3900  # Telegram hard limit is 4096; leave room for the header.


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_TEXT:
        return text
    return text[: MAX_TEXT - 1] + "…"


def _number(value, decimals: int = 2, signed: bool = False) -> str | None:
    """Compact thousands-separated number; ``None`` and junk are omitted."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        number = 0.0  # drop the sign of -0.0
    text = f"{('+' if signed and number > 0 else '')}{number:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _macro_detail(payload: dict) -> str | None:
    """成交额 / 两融 / 涨跌停 detail for the daily card; partial data is fine."""
    macro = payload.get("macro")
    if not isinstance(macro, dict):
        return None
    parts: list[str] = []
    total = _number(macro.get("total_amount"))
    if total:
        parts.append(f"成交额 {total} 亿")
    balance = _number(macro.get("margin_balance"))
    if balance:
        growth = _number(macro.get("margin_growth_pct"), signed=True)
        parts.append(f"两融余额 {balance} 亿" + (f"（{growth}%）" if growth else ""))
    up, down = macro.get("limit_up"), macro.get("limit_down")
    if up is not None or down is not None:
        parts.append(
            "涨停 " + (str(up) if up is not None else "—") + " / 跌停 " + (str(down) if down is not None else "—")
        )
    return "；".join(parts) or None


def _daily(payload: dict, subject: str | None) -> str:
    day = payload.get("trade_date") or ""
    lines = [f"【qp】{subject or f'EOD 日报 {day}'}", ""]
    terminal_ok = bool(payload.get("terminal_ok"))
    reason = str(payload.get("terminal_reason") or "")
    lines.append("终态：" + ("正常" if terminal_ok else "异常") + (f"（{reason}）" if reason else ""))
    reading = payload.get("reading") or {}
    market = payload.get("market") or {}
    if market.get("text"):
        lines.append("市场：" + str(market["text"]))
    if reading.get("market_data"):
        lines.append(str(reading["market_data"]))
    macro_text = payload.get("macro_text") or reading.get("macro")
    if macro_text:
        lines.append(str(macro_text) if str(macro_text).startswith("宏观") else "宏观：" + str(macro_text))
        detail = _macro_detail(payload)
        if detail:
            lines.append(f"宏观明细：{detail}")
    risk_text = payload.get("risk_text") or reading.get("risk")
    if risk_text:
        lines.append(str(risk_text))
    funnel = payload.get("funnel") or reading.get("funnel")
    if funnel:
        lines.append(str(funnel))
    else:
        for key in ("signals",):
            value = reading.get(key)
            if value:
                lines.append(str(value))
    if reading.get("research_eligibility"):
        lines.append(str(reading["research_eligibility"]))
    signals = payload.get("today_trend_signals")
    if signals is not None:
        lines.append(f"当日趋势信号：{signals} 条")
    shadow = payload.get("etf_shadow")
    if isinstance(shadow, dict):
        lines.extend([""] + _shadow_lines(shadow))
    conclusion = payload.get("conclusion")
    if conclusion:
        lines.extend(["", f"结论：{conclusion}"])
    return "\n".join(lines)


def _failure(payload: dict, subject: str | None) -> str:
    day = payload.get("trade_date") or ""
    lines = [f"【qp】{subject or f'Swing EOD 审计异常 {day}'}", ""]
    reason = payload.get("terminal_reason") or payload.get("reason") or "unknown"
    lines.append(f"原因：{reason}")
    message = payload.get("message")
    if message:
        lines.append(str(message))
    follow_up = payload.get("follow_up")
    if follow_up:
        lines.append(f"跟进：{follow_up}")
    return "\n".join(lines)


def _pct(value: object, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _names(block: dict | None) -> list[str]:
    return [str(item.get("name") or item.get("code")) for item in (block or {}).get("top8") or []]


def _shadow_lines(payload: dict) -> list[str]:
    scores = payload.get("scores") or {}
    s5, s20 = scores.get("5"), scores.get("20")
    lines = ["ETF 研究观察（不是买卖建议）"]
    n5, n20 = _names(s5), _names(s20)
    if n5 and n20 and set(n5) == set(n20):
        lines.append(f"5日/20日模型都选中这{len(n5)}只：" + "、".join(n5))
    else:
        if n5:
            lines.append("5日模型前8：" + "、".join(n5))
        elif not s5:
            lines.append("5日模型：今天没有快照")
        if n20:
            lines.append("20日模型前8：" + "、".join(n20))
        elif not s20:
            lines.append("20日模型：今天没有快照")
    rotation = payload.get("rotation") or {}
    if rotation:
        lines.append(
            "纸面轮动：今天 "
            + _pct(rotation.get("nav_return"))
            + "；沪深300 "
            + _pct(rotation.get("benchmark_return"))
            + "；40只等权 "
            + _pct(rotation.get("ew_return"))
        )
        positions = rotation.get("positions") or []
        if positions:
            lines.append("持有：" + "、".join(str(item.get("name") or item.get("code")) for item in positions))
    quality = payload.get("data_quality") or {}
    if quality.get("missing") or quality.get("stale"):
        lines.append("数据：" + "；".join((quality.get("missing") or []) + (quality.get("stale") or [])))
    else:
        lines.append("数据：40只ETF齐全，截至今天收盘")
    lines.append("说明：5日模型历史检验仍不稳定，只观察，不用于实盘。")
    return lines


def _etf_shadow(payload: dict, subject: str | None) -> str:
    day = payload.get("trade_date") or ""
    lines = [f"【qp】{subject or f'ETF 研究观察 {day}'}", ""]
    lines.extend(_shadow_lines(payload))
    if payload.get("conclusion"):
        lines.extend(["", "结论：" + str(payload["conclusion"])])
    return "\n".join(lines)


def _attribution(payload: dict, subject: str | None) -> str:
    text = str(payload.get("text") or "").strip()
    if not text:
        return _clip(f"【qp】{subject or '信号链绩效归因'}\n\n本周暂无可展示的归因摘要。")
    return _clip(f"【qp】{subject or '信号链绩效归因'}\n\n{text}")


def render_outbox(row: Outbox) -> str:
    try:
        payload = json.loads(row.body)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        report_type = payload.get("report_type")
        if report_type == "eod_daily_report":
            return _clip(_daily(payload, row.subject))
        if report_type == "eod_watchdog_failure":
            return _clip(_failure(payload, row.subject))
        if report_type == "etf_shadow_report":
            return _clip(_etf_shadow(payload, row.subject))
        if report_type == "attribution_weekly":
            return _attribution(payload, row.subject)
        if report_type == "daily_combined_report":
            return _clip(_daily(payload, row.subject))
    return _clip(f"{row.subject or 'qp 通知'}\n\n{row.body}")
