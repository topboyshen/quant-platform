#!/usr/bin/env python3
"""VIX risk-gate monitor delivered by Hermes stdout.

The monitor is deliberately quiet when the risk level is unchanged. It uses
Cboe's delayed VIX quote as the primary source and the existing trading-radar
Yahoo snapshot as an independent fallback. No order or portfolio mutation is
performed here.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo


CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
STATE_FILE = Path(
    os.environ.get(
        "VIX_WATCH_STATE_FILE",
        "/home/ubuntu/trading-radar/data/vix_risk_gate_state.json",
    )
)
MACRO_STATE_FILE = Path(
    os.environ.get(
        "VIX_WATCH_MACRO_STATE_FILE",
        "/home/ubuntu/trading-radar/data/radar_risk_state.json",
    )
)
NEW_YORK = ZoneInfo("America/New_York")
MAX_SOURCE_AGE_SECONDS = 90 * 60
LEVELS = ("green", "yellow", "orange", "red")
ENTER_THRESHOLDS = {"yellow": 17.0, "orange": 20.0, "red": 25.0}
EXIT_THRESHOLDS = {"yellow": 16.0, "orange": 19.0, "red": 24.0}
LEVEL_CN = {"green": "绿", "yellow": "黄", "orange": "橙", "red": "红"}
LEVEL_ICON = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}
GATE_ACTION = {
    "green": "常规研究",
    "yellow": "高波动标的结论降置信度",
    "orange": "研究结论需人工复核",
    "red": "暂停直接行动标签并补充第二类证据",
}


@dataclass(frozen=True)
class Quote:
    value: float
    source: str
    source_timestamp: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.source_timestamp)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_state() -> dict[str, Any]:
    state = _load_json(STATE_FILE)
    level = str(state.get("level") or "green").lower()
    if level == "ok":
        level = "green"
    if level not in LEVELS:
        level = "green"
    state["level"] = level
    state.setdefault("consecutive_failures", 0)
    state.setdefault("health_alerted", False)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_FILE)


def in_us_cash_window(now: datetime | None = None) -> bool:
    observed = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    return observed.weekday() < 5 and clock_time(9, 30) <= observed.time() <= clock_time(16, 30)


def _parse_cboe_timestamp(payload: dict[str, Any]) -> float:
    data = payload.get("data")
    last_trade = data.get("last_trade_time") if isinstance(data, dict) else None
    text = str(last_trade or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=NEW_YORK)
            return parsed.timestamp()
        except ValueError:
            pass
    response_timestamp = str(payload.get("timestamp") or "").strip()
    if response_timestamp:
        try:
            return datetime.strptime(response_timestamp, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            pass
    return time.time()


def fetch_cboe_quote(timeout: int = 8) -> tuple[Quote | None, str]:
    delays = (0, 2, 6, 18)
    last_error = "unknown"
    opener = build_opener(ProxyHandler({}))
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        request = Request(
            CBOE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "QuantDinger-Risk-Gate/1.0",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = payload.get("data") if isinstance(payload, dict) else None
            value = _number(data.get("current_price")) if isinstance(data, dict) else None
            if value is None:
                last_error = "cboe_missing_price"
                continue
            quote = Quote(
                value=value,
                source="cboe_delayed",
                source_timestamp=_parse_cboe_timestamp(payload),
            )
            if quote.age_seconds <= MAX_SOURCE_AGE_SECONDS:
                return quote, ""
            return None, "cboe_stale"
        except HTTPError as exc:
            last_error = f"cboe_http_{exc.code}"
            if exc.code == 429 and attempt < len(delays) - 1:
                retry_after = _number(exc.headers.get("Retry-After"))
                if retry_after:
                    time.sleep(min(30.0, retry_after))
        except (OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError) as exc:
            last_error = f"cboe_{type(exc).__name__.lower()}"
    return None, last_error


def load_yahoo_fallback() -> tuple[Quote | None, dict[str, Any], str]:
    macro = _load_json(MACRO_STATE_FILE)
    value = _number(macro.get("vix"))
    source_timestamps = macro.get("source_timestamps")
    sources = macro.get("sources")
    source_timestamp = None
    if isinstance(source_timestamps, dict):
        source_timestamp = _number(source_timestamps.get("vix"))
    if source_timestamp is None:
        source_timestamp = _number(macro.get("timestamp"))
    source = "yahoo_snapshot"
    if isinstance(sources, dict) and sources.get("vix"):
        source = f"trading-radar/{sources['vix']}"
    if value is None or source_timestamp is None:
        return None, macro, "yahoo_snapshot_missing"
    quote = Quote(value=value, source=source, source_timestamp=source_timestamp)
    if quote.age_seconds > MAX_SOURCE_AGE_SECONDS:
        return None, macro, "yahoo_snapshot_stale"
    return quote, macro, ""


def fetch_quote() -> tuple[Quote | None, dict[str, Any], str]:
    cboe, cboe_error = fetch_cboe_quote()
    macro = _load_json(MACRO_STATE_FILE)
    if cboe is not None:
        return cboe, macro, ""
    yahoo, macro, yahoo_error = load_yahoo_fallback()
    if yahoo is not None:
        return yahoo, macro, cboe_error
    return None, macro, f"{cboe_error}+{yahoo_error}"


def next_level(previous: str, vix: float) -> str:
    previous = "green" if previous == "ok" else previous
    index = LEVELS.index(previous) if previous in LEVELS else 0
    while index < len(LEVELS) - 1:
        target = LEVELS[index + 1]
        if vix < ENTER_THRESHOLDS[target]:
            break
        index += 1
    while index > 0:
        current = LEVELS[index]
        if vix >= EXIT_THRESHOLDS[current]:
            break
        index -= 1
    return LEVELS[index]


def _context_value(macro: dict[str, Any], key: str, suffix: str = "") -> str:
    value = _number(macro.get(key))
    return f"{value:.2f}{suffix}" if value is not None else "暂无"


def render_transition(
    previous_level: str,
    current_level: str,
    previous_vix: float | None,
    quote: Quote,
    macro: dict[str, Any],
) -> str:
    direction = "升级" if LEVELS.index(current_level) > LEVELS.index(previous_level) else "解除/降级"
    previous_text = f"{previous_vix:.2f}" if previous_vix is not None else "暂无"
    age_minutes = int(quote.age_seconds // 60)
    return "\n".join(
        [
            f"{LEVEL_ICON[current_level]} VIX 风险门控{direction}",
            (
                f"VIX：{previous_text} → {quote.value:.2f}｜"
                f"{LEVEL_CN[previous_level]} → {LEVEL_CN[current_level]}"
            ),
            f"数据：{quote.source}｜fresh {age_minutes}分钟",
            (
                f"上下文：美债10Y {_context_value(macro, 'us_10y', '%')}｜"
                f"原油 {_context_value(macro, 'crude_oil')}"
            ),
            f"研究门控：{GATE_ACTION[current_level]}",
            "说明：仅作 QuantDinger 宏观风险标签，不触发交易。",
        ]
    )


def render_health_alert(error: str) -> str:
    return "\n".join(
        [
            "⚠️ VIX 风险门控数据降级",
            "Cboe 与 trading-radar/Yahoo 连续两次均无可用新鲜报价。",
            f"错误类型：{error}",
            "系统已冻结上次风险档，不会依据过期数据改变研究结论。",
        ]
    )


def run() -> int:
    if os.environ.get("VIX_WATCH_FORCE_RUN") != "1" and not in_us_cash_window():
        return 0

    state = load_state()
    quote, macro, error = fetch_quote()
    now = time.time()
    if quote is None:
        failures = int(state.get("consecutive_failures") or 0) + 1
        state.update(
            {
                "consecutive_failures": failures,
                "last_fetch_error": error,
                "last_fetch_failed_at": now,
                "updated_at": now,
            }
        )
        should_alert = failures >= 2 and not bool(state.get("health_alerted"))
        if should_alert:
            state["health_alerted"] = True
            state["last_health_alert_at"] = now
        save_state(state)
        if should_alert:
            sys.stdout.write(render_health_alert(error) + "\n")
        return 0

    previous_level = str(state.get("level") or "green")
    if previous_level == "ok":
        previous_level = "green"
    previous_vix = _number(state.get("vix"))
    current_level = next_level(previous_level, quote.value)
    was_unhealthy = bool(state.get("health_alerted"))
    state.update(
        {
            "schema_version": 1,
            "vix": quote.value,
            "level": current_level,
            "source": quote.source,
            "source_timestamp": quote.source_timestamp,
            "freshness": "fresh",
            "consecutive_failures": 0,
            "health_alerted": False,
            "last_fetch_error": "",
            "last_success_at": now,
            "updated_at": now,
        }
    )
    messages: list[str] = []
    if was_unhealthy:
        messages.append(
            "✅ VIX 风险门控数据恢复\n"
            f"VIX {quote.value:.2f}｜{quote.source}｜风险档{LEVEL_CN[current_level]}"
        )
    if current_level != previous_level:
        state["last_transition_at"] = now
        messages.append(
            render_transition(
                previous_level,
                current_level,
                previous_vix,
                quote,
                macro,
            )
        )
    save_state(state)
    if messages:
        sys.stdout.write("\n\n".join(messages) + "\n")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:
        sys.stderr.write(f"vix-risk-gate unexpected error: {type(exc).__name__}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
