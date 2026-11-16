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
    return text[:MAX_TEXT - 1] + "…"


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
        lines.append(str(macro_text) if str(macro_text).startswith("宏观")
                     else "宏观：" + str(macro_text))
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


def _etf_shadow(payload: dict, subject: str | None) -> str:
    day = payload.get("trade_date") or ""
    lines = [f"【qp】{subject or f'ETF 研究影子 {day}'}", "",
             "只读研究影子：不是交易信号，不下单、不写正式记录链。"]
    scores = payload.get("scores") or {}
    for horizon in ("5", "20"):
        block = scores.get(horizon)
        if not block:
            lines.append(f"{horizon}d：缺失")
            continue
        lines.append(f"{horizon}d {block.get('model_version')} · cutoff "
                     f"{block.get('data_cutoff')} · {block.get('eligible')}/40"
                     + (" · STALE" if block.get("stale") else ""))
        top = block.get("top8") or []
        if top:
            lines.append("  " + " · ".join(
                f"{item.get('code')} {item.get('name')} {_pct(item.get('score'))}"
                for item in top))
    rotation = payload.get("rotation") or {}
    if rotation:
        lines.append("C13：" + f"NAV {float(rotation.get('nav') or 0):,.0f}"
                     + (f"（{_pct(rotation.get('nav_return'))}）"
                        if rotation.get("nav_return") is not None else "")
                     + f" · 510300 {_pct(rotation.get('benchmark_return'))}"
                     + f" · EW {_pct(rotation.get('ew_return'))}")
        positions = rotation.get("positions") or []
        if positions:
            lines.append("  持仓：" + " / ".join(
                str(item.get("name") or item.get("code")) for item in positions))
    data_quality = payload.get("data_quality") or {}
    if data_quality.get("note"):
        lines.append("数据：" + str(data_quality["note"]))
    if payload.get("conclusion"):
        lines.extend(["", "结论：" + str(payload["conclusion"])])
    return "\n".join(lines)


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
    return _clip(f"{row.subject or 'qp 通知'}\n\n{row.body}")
