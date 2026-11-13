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
    for key in ("market_data", "macro", "signals", "research_eligibility"):
        value = reading.get(key)
        if value:
            lines.append(str(value))
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
    return _clip(f"{row.subject or 'qp 通知'}\n\n{row.body}")
