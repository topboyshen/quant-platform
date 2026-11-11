#!/usr/bin/env python3
"""Render the personal QuantDinger A-share research radar for Telegram.

The job is intentionally read-only and deterministic:

* the watchlist comes only from ``qd_watchlist``;
* existing AI, indicator, strategy and backtest records are reused;
* no model call, backtest execution, trade action or database write occurs;
* missing/stale evidence is visible instead of being replaced with defaults.

The production scheduler runs this script through Hermes ``--no-agent`` so its
stdout is delivered verbatim and only once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from research_eligibility import (
    GATE_RULE_VERSION,
    GateDecision,
    TradingCalendarSnapshot,
    evaluate_eligibility,
    expected_last_closed_trade_date,
    load_trading_calendar,
    reason_labels,
    trading_session_age,
    write_audit_record,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_DB_CONTAINER = "radar-quantdinger-postgres"
DEFAULT_RISK_STATE = Path("/home/ubuntu/trading-radar/data/radar_risk_state.json")
DEFAULT_SWING_DB = Path("/home/ubuntu/trading-radar/data/swing_macro.db")
DEFAULT_TRADING_CALENDAR = Path("/home/ubuntu/trading-radar/data/trading_calendar.json")
DEFAULT_AUDIT_DIR = Path("/home/ubuntu/trading-radar/runtime/research-radar-audit")
TELEGRAM_SAFE_CHARS = 3_800

VIX_GATE_LABELS = {
    "green": ("🟢", "低", "仅作宏观背景"),
    "yellow": ("🟡", "偏高", "仅作宏观背景"),
    "orange": ("🟠", "高", "仅作宏观背景"),
    "red": ("🔴", "极高", "仅作宏观背景"),
    "unknown": ("⚪", "未知", "仅作宏观背景"),
}


class ResearchRadarError(RuntimeError):
    """Raised when the report cannot safely read its authoritative source."""


@dataclass(frozen=True)
class ResearchData:
    watchlist: list[dict[str, Any]]
    analyses: list[dict[str, Any]]
    indicators: list[dict[str, Any]]
    strategies: list[dict[str, Any]]
    backtests: list[dict[str, Any]]


def _json_rows(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _query_json_rows(
    sql: str,
    *,
    db_container: str = DEFAULT_DB_CONTAINER,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    command = [
        "docker",
        "exec",
        db_container,
        "sh",
        "-lc",
        (
            'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" '
            '-d "$POSTGRES_DB" -At -c "$1"'
        ),
        "sh",
        sql,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResearchRadarError(f"QuantDinger 数据库不可用：{type(exc).__name__}") from exc

    if completed.returncode != 0:
        error = (completed.stderr or "psql query failed").strip().splitlines()[-1]
        raise ResearchRadarError(f"QuantDinger 数据读取失败：{error[:160]}")

    try:
        return _json_rows(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchRadarError("QuantDinger 返回了无法解析的数据") from exc


def load_research_data(
    user_id: int,
    *,
    db_container: str = DEFAULT_DB_CONTAINER,
) -> ResearchData:
    if user_id <= 0:
        raise ResearchRadarError("QuantDinger 用户映射无效")

    watchlist = _query_json_rows(
        f"""
        SELECT row_to_json(q)::text
        FROM (
            SELECT id, user_id, market, symbol, COALESCE(name, '') AS name
            FROM qd_watchlist
            WHERE user_id = {user_id} AND market = 'CNStock'
            ORDER BY id
        ) AS q;
        """,
        db_container=db_container,
    )

    if not watchlist:
        return ResearchData([], [], [], [], [])

    analyses = _query_json_rows(
        f"""
        SELECT row_to_json(q)::text
        FROM (
            SELECT DISTINCT ON (m.symbol)
                m.symbol,
                m.decision,
                m.confidence,
                m.price_at_analysis,
                m.summary,
                m.indicators_snapshot,
                m.task_status,
                m.created_at
            FROM qd_analysis_memory AS m
            JOIN qd_watchlist AS w
              ON w.user_id = m.user_id
             AND w.market = m.market
             AND w.symbol = m.symbol
            WHERE m.user_id = {user_id}
              AND m.market = 'CNStock'
              AND m.task_status = 'completed'
            ORDER BY m.symbol, m.created_at DESC, m.id DESC
        ) AS q;
        """,
        db_container=db_container,
    )

    indicators = _query_json_rows(
        f"""
        SELECT row_to_json(q)::text
        FROM (
            SELECT id, name, asset_type, review_status, updated_at
            FROM qd_indicator_codes
            WHERE user_id = {user_id}
            ORDER BY updated_at DESC NULLS LAST, id DESC
        ) AS q;
        """,
        db_container=db_container,
    )

    strategies = _query_json_rows(
        f"""
        SELECT row_to_json(q)::text
        FROM (
            SELECT id, strategy_name, strategy_type, market_category,
                   execution_mode, status, symbol, timeframe, updated_at,
                   ENCODE(
                       SHA256(CONVERT_TO(BTRIM(strategy_code), 'UTF8')),
                       'hex'
                   ) AS code_hash
            FROM qd_strategies_trading
            WHERE user_id = {user_id}
              AND market_category = 'CNStock'
            ORDER BY updated_at DESC NULLS LAST, id DESC
        ) AS q;
        """,
        db_container=db_container,
    )

    primary_strategy = strategies[0] if strategies else {}
    strategy_id = int(_number(primary_strategy.get("id")) or 0)
    strategy_hash = str(primary_strategy.get("code_hash") or "").lower()
    if strategy_id and re.fullmatch(r"[0-9a-f]{64}", strategy_hash):
        provenance_order = (
            f"CASE WHEN b.strategy_id = {strategy_id} "
            f"AND LOWER(b.code_hash) = '{strategy_hash}' THEN 0 ELSE 1 END"
        )
    else:
        provenance_order = "1"

    backtests = _query_json_rows(
        f"""
        SELECT row_to_json(q)::text
        FROM (
            SELECT DISTINCT ON (b.symbol)
                b.id,
                b.symbol,
                b.indicator_id,
                b.strategy_id,
                b.strategy_name,
                b.run_type,
                b.start_date,
                b.end_date,
                b.status,
                b.code_hash,
                b.created_at,
                CASE
                    WHEN NULLIF(BTRIM(b.result_json), '') IS NULL THEN NULL
                    ELSE b.result_json::jsonb
                END AS result
            FROM qd_backtest_runs AS b
            JOIN qd_watchlist AS w
              ON w.user_id = b.user_id
             AND w.market = b.market
             AND w.symbol = b.symbol
            WHERE b.user_id = {user_id}
              AND b.market = 'CNStock'
              AND b.status = 'success'
            ORDER BY b.symbol, {provenance_order}, b.created_at DESC, b.id DESC
        ) AS q;
        """,
        db_container=db_container,
    )

    return ResearchData(watchlist, analyses, indicators, strategies, backtests)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _format_number(value: Any, digits: int = 2) -> str:
    number = _number(value)
    return "暂无" if number is None else f"{number:.{digits}f}"


def _format_pct(value: Any, *, signed: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "暂无"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:.2f}%"


def _short_date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else "暂无"


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_analysis_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(SHANGHAI)


def _business_days_since(
    value: Any,
    now: datetime,
    trading_calendar: TradingCalendarSnapshot | None = None,
) -> int | None:
    observed_at = _parse_analysis_datetime(value)
    observed = observed_at.date() if observed_at is not None else _parse_date(value)
    today = now.astimezone(SHANGHAI).date()
    if observed is None:
        return None
    if observed >= today:
        return 0
    if trading_calendar is not None and trading_calendar.valid:
        expected = expected_last_closed_trade_date(trading_calendar, now)
        if expected is None:
            return None
        if observed >= expected:
            return 0
        return trading_session_age(trading_calendar, observed, expected)

    # Compatibility fallback for callers that do not yet supply a calendar.
    count = 0
    cursor = observed + timedelta(days=1)
    while cursor <= today:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def _analysis_age_label(
    value: Any,
    now: datetime,
    trading_calendar: TradingCalendarSnapshot | None = None,
) -> str:
    age = _business_days_since(value, now, trading_calendar)
    if age is None:
        return "日期未知"
    if age == 0:
        return "当日"
    return f"滞后{age}个交易日"


def _analysis_is_fresh(
    value: Any,
    now: datetime,
    trading_calendar: TradingCalendarSnapshot | None = None,
) -> bool:
    age = _business_days_since(value, now, trading_calendar)
    return age is not None and age <= 1


def _analysis_time_label(
    value: Any,
    now: datetime,
    trading_calendar: TradingCalendarSnapshot | None = None,
) -> str:
    observed = _parse_analysis_datetime(value)
    if observed is None:
        return "时间未知"
    return (
        f"{observed.strftime('%m-%d %H:%M')}"
        f"（{_analysis_age_label(value, now, trading_calendar)}）"
    )


def _single_line(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _first_evidence_sentence(summary: Any, limit: int = 78) -> str:
    text = re.sub(r"\s+", " ", str(summary or "")).strip()
    if not text:
        return "暂无摘要"
    first = re.split(r"[。；\n]", text, maxsplit=1)[0]
    return _single_line(first, limit)


def _action_conclusion(summary: Any, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", str(summary or "")).strip()
    if not text:
        return "未单列"
    sentences = [part.strip(" ，,：:") for part in re.split(r"[。；\n]", text) if part.strip()]
    action_terms = (
        "建议",
        "观望",
        "等待",
        "回避",
        "止损",
        "轻仓",
        "谨慎",
        "不宜",
        "不建议",
        "可考虑",
        "关注",
    )
    for sentence in reversed(sentences):
        if "多周期客观共识" in sentence:
            continue
        if any(term in sentence for term in action_terms):
            return _single_line(sentence, limit)
    return "未单列"


def _tencent_symbol(symbol: str) -> str | None:
    symbol = re.sub(r"\D", "", str(symbol or ""))
    if not re.fullmatch(r"\d{6}", symbol):
        return None
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    if symbol.startswith(("4", "8", "9")):
        return "bj" + symbol
    return None


def _parse_tencent_quote_datetime(value: Any) -> datetime | None:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) < 14:
        return None
    try:
        observed = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return observed.replace(tzinfo=SHANGHAI)


def _parse_quote_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = _parse_tencent_quote_datetime(text)
    if parsed is not None:
        return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def parse_tencent_quotes(payload: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_line in payload.split(";"):
        line = raw_line.strip()
        if "=" not in line:
            continue
        variable, raw_fields = line.split("=", 1)
        code = re.sub(r"^v_", "", variable.strip(), flags=re.IGNORECASE)
        code = code[-6:]
        fields = raw_fields.strip().strip('"').split("~")
        if not re.fullmatch(r"\d{6}", code) or len(fields) < 33:
            continue
        price = _number(fields[3])
        change = _number(fields[31])
        pct = _number(fields[32])
        observed_at = _parse_tencent_quote_datetime(fields[30])
        if price is None or price <= 0 or observed_at is None:
            continue
        result[code] = {
            "name": fields[1].strip(),
            "price": price,
            "change": change,
            "pct": pct,
            "observed_at": observed_at.isoformat(),
            "source": "腾讯",
        }
    return result


def fetch_live_quotes(symbols: Iterable[str], timeout: int = 8) -> dict[str, dict[str, Any]]:
    codes = [code for code in (_tencent_symbol(item) for item in symbols) if code]
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    request = urllib.request.Request(url, headers={"User-Agent": "QuantDinger-Research-Radar/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read().decode("gbk", errors="replace")
    except (OSError, TimeoutError):
        return {}
    return parse_tencent_quotes(payload)


def load_macro_risk(path: Path = DEFAULT_RISK_STATE) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_market_breadth(path: Path = DEFAULT_SWING_DB) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT date, total_amount, margin_balance, margin_growth_pct, "
            "limit_up, limit_down, limit_ratio, source "
            "FROM macro_slice ORDER BY date DESC LIMIT 1"
        ).fetchone()
        connection.close()
    except (OSError, sqlite3.Error):
        return {}
    return dict(row) if row is not None else {}


def _vix_risk_gate(
    risk: Mapping[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    vix = _number(risk.get("vix"))
    if vix is None:
        level = "unknown"
    elif vix >= 25:
        level = "red"
    elif vix >= 20:
        level = "orange"
    elif vix >= 17:
        level = "yellow"
    else:
        level = "green"

    source_timestamps = risk.get("source_timestamps")
    source_timestamp = None
    if isinstance(source_timestamps, Mapping):
        source_timestamp = _number(source_timestamps.get("vix"))
    if source_timestamp is None:
        source_timestamp = _number(risk.get("timestamp"))

    age_minutes = None
    if source_timestamp is not None:
        now_timestamp = generated_at.astimezone(timezone.utc).timestamp()
        age_minutes = max(0, int((now_timestamp - source_timestamp) // 60))
    if age_minutes is None:
        freshness = "unknown"
    elif age_minutes <= 90:
        freshness = "fresh"
    else:
        new_york_now = generated_at.astimezone(NEW_YORK)
        cash_market_open = (
            new_york_now.weekday() < 5
            and (new_york_now.hour, new_york_now.minute) >= (9, 30)
            and (new_york_now.hour, new_york_now.minute) <= (16, 30)
        )
        freshness = "closed" if not cash_market_open and age_minutes <= 72 * 60 else "stale"

    sources = risk.get("sources")
    source = "trading-radar"
    if isinstance(sources, Mapping) and sources.get("vix"):
        source = str(sources.get("vix"))

    icon, label, action = VIX_GATE_LABELS[level]
    return {
        "level": level,
        "icon": icon,
        "label": label,
        "action": action,
        "freshness": freshness,
        "age_minutes": age_minutes,
        "source": source,
    }


def _vix_gate_freshness_label(gate: Mapping[str, Any]) -> str:
    freshness = str(gate.get("freshness") or "unknown")
    age_minutes = gate.get("age_minutes")
    if isinstance(age_minutes, int):
        return f"{freshness} {age_minutes}分钟"
    return freshness


def _macro_lines(
    risk: Mapping[str, Any],
    breadth: Mapping[str, Any],
    generated_at: datetime,
) -> list[str]:
    level = str(risk.get("level") or "unknown").lower()
    label_map = {
        "normal": ("🟢", "环境正常"),
        "green": ("🟢", "环境正常"),
        "growth_blocked": ("🟠", "成长受限"),
        "risk_off": ("🔴", "风险收缩"),
        "red": ("🔴", "风险收缩"),
    }
    icon, label = label_map.get(level, ("⚪", "状态未知"))
    line = (
        f"{icon} {label}"
        f"｜VIX {_format_number(risk.get('vix'), 1)}"
        f"｜美债10Y {_format_number(risk.get('us_10y'), 2)}%"
        f"｜原油 {_format_number(risk.get('crude_oil'), 2)}"
    )
    gate = _vix_risk_gate(risk, generated_at)
    lines = [
        line,
        (
            f"VIX风险背景：{gate['icon']}{gate['label']}"
            f"｜{_vix_gate_freshness_label(gate)}"
            "｜三年验证未证明A股回撤区分度，不参与个股门控"
        ),
    ]

    if breadth:
        breadth_date = _short_date(breadth.get("date"))
        breadth_prefix = (
            "市场宽度"
            if _parse_date(breadth.get("date")) == generated_at.astimezone(SHANGHAI).date()
            else "最近正式收盘宽度"
        )
        lines.append(
            f"{breadth_prefix}({breadth_date})："
            f"涨停{breadth.get('limit_up', '暂无')} / 跌停{breadth.get('limit_down', '暂无')}"
            f"｜成交额{_format_number(breadth.get('total_amount'), 0)}亿"
        )
    else:
        lines.append("市场宽度：暂无正式快照")

    timestamp = _number(risk.get("timestamp"))
    if timestamp is not None:
        observed = datetime.fromtimestamp(timestamp, tz=SHANGHAI).strftime("%m-%d %H:%M")
        lines.append(f"宏观数据时间：{observed}｜来源：trading-radar")
    else:
        lines.append("宏观数据时间：暂无｜来源：trading-radar")
    return lines


def _decision_label(value: Any) -> str:
    return {
        "BUY": "偏多",
        "SELL": "偏空",
        "HOLD": "中性",
    }.get(str(value or "").upper(), "未知")


def _trend_label(value: Any) -> str:
    return {
        "strong_uptrend": "强上行",
        "uptrend": "上行",
        "sideways": "震荡",
        "neutral": "中性",
        "downtrend": "下行",
        "strong_downtrend": "强下行",
    }.get(str(value or "").lower(), str(value or "未知"))


def _macd_label(value: Any) -> str:
    return {
        "golden_cross": "金叉",
        "death_cross": "死叉",
        "bullish": "偏多",
        "bearish": "偏空",
    }.get(str(value or "").lower(), str(value or "未知"))


def _strategy_status_label(value: Any) -> str:
    return {
        "running": "运行中",
        "stopped": "已停止",
        "paused": "已暂停",
        "draft": "草稿",
    }.get(str(value or "").lower(), str(value or "未知"))


def _indicator_line(snapshot: Mapping[str, Any]) -> str:
    moving = snapshot.get("moving_averages")
    moving = moving if isinstance(moving, Mapping) else {}
    rsi = snapshot.get("rsi")
    rsi = rsi if isinstance(rsi, Mapping) else {}
    macd = snapshot.get("macd")
    macd = macd if isinstance(macd, Mapping) else {}
    volatility = snapshot.get("volatility")
    volatility = volatility if isinstance(volatility, Mapping) else {}
    trend = snapshot.get("trend") or moving.get("trend")
    return (
        f"AI技术快照：{_trend_label(trend)}"
        f"｜RSI {_format_number(rsi.get('value'), 1)}"
        f"｜MACD {_macd_label(macd.get('trend') or macd.get('signal'))}"
        f"｜ATR {_format_number(volatility.get('pct'), 2)}%"
    )


def _same_id(left: Any, right: Any) -> bool:
    left_number = _number(left)
    right_number = _number(right)
    return left_number is not None and right_number is not None and int(left_number) == int(right_number)


def _backtest_provenance(
    backtest: Mapping[str, Any],
    strategy: Mapping[str, Any] | None,
) -> str:
    if not strategy:
        return "no_current_strategy"
    if backtest.get("strategy_id") is None:
        return "unlinked"
    if not _same_id(backtest.get("strategy_id"), strategy.get("id")):
        return "other_strategy"
    current_hash = str(strategy.get("code_hash") or "").lower()
    backtest_hash = str(backtest.get("code_hash") or "").lower()
    if not current_hash or not backtest_hash:
        return "unknown_version"
    return "current" if current_hash == backtest_hash else "historical"


def _provenance_label(provenance: str) -> str:
    return {
        "current": "当前策略代码",
        "historical": "历史代码版本",
        "unlinked": "未关联策略",
        "other_strategy": "其他策略",
        "unknown_version": "版本无法核验",
        "no_current_strategy": "无当前策略",
    }.get(provenance, "来源未知")


def _backtest_lines(backtest: Mapping[str, Any]) -> tuple[list[str], bool]:
    result = backtest.get("result")
    result = result if isinstance(result, Mapping) else {}
    benchmark = result.get("benchmark")
    benchmark = benchmark if isinstance(benchmark, Mapping) else {}
    strategy_return = _number(result.get("totalReturn"))
    benchmark_return = _number(benchmark.get("totalReturn"))
    if benchmark_return is None:
        benchmark_text = "基准待重跑"
        benchmark_missing = True
    else:
        benchmark_text = f"基准{_format_pct(benchmark_return, signed=True)}"
        if strategy_return is not None:
            benchmark_text += f"｜超额{_format_pct(strategy_return - benchmark_return, signed=True)}"
        benchmark_missing = False

    strategy_drawdown = _format_pct(result.get("maxDrawdown"))
    benchmark_drawdown = _format_pct(benchmark.get("maxDrawdown"))
    strategy_sharpe = _format_number(result.get("sharpeRatio"), 2)
    benchmark_sharpe = _format_number(benchmark.get("sharpeRatio"), 2)
    lines = [
        (
            f"回测：策略{_format_pct(strategy_return, signed=True)}"
            f"｜{benchmark_text}"
        ),
        (
            f"风险：回撤{strategy_drawdown} / 基准{benchmark_drawdown}"
            f"｜Sharpe {strategy_sharpe} / {benchmark_sharpe}"
            f"｜PF {_format_number(result.get('profitFactor'), 2)}"
            f"｜{int(_number(result.get('totalTrades')) or 0)}笔"
        ),
    ]
    return lines, benchmark_missing


def _status_line(
    analysis: Mapping[str, Any] | None,
    backtest: Mapping[str, Any] | None,
    provenance: str,
) -> str:
    if analysis is None and backtest is None:
        return "验证：研究资料不足"
    if backtest is None:
        return "验证：已有 AI 技术研究，尚无回测"
    if provenance != "current":
        return {
            "historical": "验证：历史代码版本，不能代表当前策略",
            "unlinked": "验证：回测未关联策略，不能代表当前策略",
            "other_strategy": "验证：其他策略结果，不能代表当前策略",
            "unknown_version": "验证：代码版本无法核验",
            "no_current_strategy": "验证：当前没有可核验策略",
        }.get(provenance, "验证：回测来源无法核验")

    result = backtest.get("result") if isinstance(backtest, Mapping) else {}
    result = result if isinstance(result, Mapping) else {}
    benchmark = result.get("benchmark")
    if not isinstance(benchmark, Mapping):
        return "验证：当前策略已有回测，buy&hold 基准待补"
    strategy_return = _number(result.get("totalReturn"))
    benchmark_return = _number(benchmark.get("totalReturn"))
    if strategy_return is None or benchmark_return is None:
        return "验证：当前策略回测数据不完整"
    strategy_drawdown = _number(result.get("maxDrawdown"))
    benchmark_drawdown = _number(benchmark.get("maxDrawdown"))
    drawdown_improved = (
        strategy_drawdown is not None
        and benchmark_drawdown is not None
        and abs(strategy_drawdown) < abs(benchmark_drawdown)
    )
    if strategy_return > benchmark_return and (_number(result.get("profitFactor")) or 0) > 1:
        return "验证：当前代码历史回测优于基准，仍需样本外验证"
    if drawdown_improved:
        return "验证：收益未跑赢基准；回撤改善"
    return "验证：收益未跑赢基准；回撤未改善"


def _quote_line(quote: Mapping[str, Any], observed_at: datetime) -> str:
    source = _single_line(quote.get("source") or "腾讯", 10)
    return (
        f"最近收盘价：{_format_number(quote.get('price'), 2)}"
        f"｜涨跌{_format_pct(quote.get('pct'), signed=True)}"
        f"｜{observed_at.strftime('%m-%d %H:%M')}｜{source}"
    )


def _analysis_price_line(
    analysis: Mapping[str, Any],
    quote: Mapping[str, Any] | None,
    quote_state: str,
    generated_at: datetime,
    trading_calendar: TradingCalendarSnapshot,
) -> str:
    line = (
        f"AI分析价：{_format_number(analysis.get('price_at_analysis'), 2)}"
        f"｜分析{_analysis_time_label(analysis.get('created_at'), generated_at, trading_calendar)}"
    )
    analysis_price = _number(analysis.get("price_at_analysis"))
    quote_price = _number(quote.get("price")) if quote else None
    if quote_state == "fresh_close" and analysis_price and quote_price is not None:
        line += f"｜收盘偏离{_format_pct((quote_price / analysis_price - 1) * 100, signed=True)}"
    return line


def _decision_reason_line(decision: GateDecision, limit: int = 4) -> str:
    labels = reason_labels(decision.reason_codes)
    if not labels:
        return "硬门禁：无"
    visible = labels[:limit]
    suffix = f"等{len(labels)}项" if len(labels) > limit else ""
    return "阻断：" + "、".join(visible) + suffix


def _build_decision_map(
    data: ResearchData,
    quotes: Mapping[str, Mapping[str, Any]],
    trading_calendar: TradingCalendarSnapshot,
    generated_at: datetime,
) -> dict[str, GateDecision]:
    analyses = {str(item.get("symbol")): item for item in data.analyses}
    backtests = {str(item.get("symbol")): item for item in data.backtests}
    strategy = data.strategies[0] if data.strategies else {}
    indicator = data.indicators[0] if data.indicators else {}
    result: dict[str, GateDecision] = {}
    for stock in data.watchlist:
        symbol = str(stock.get("symbol") or "")
        quote = quotes.get(symbol) or {}
        analysis = analyses.get(symbol)
        backtest = backtests.get(symbol) or {}
        backtest_result = backtest.get("result")
        backtest_result = backtest_result if isinstance(backtest_result, Mapping) else {}
        benchmark = backtest_result.get("benchmark")
        benchmark_present = (
            isinstance(benchmark, Mapping)
            and _number(benchmark.get("totalReturn")) is not None
        )
        result[symbol] = evaluate_eligibility(
            symbol,
            calendar=trading_calendar,
            generated_at=generated_at,
            quote_observed_at=_parse_quote_datetime(quote.get("observed_at")),
            quote_price=quote.get("price"),
            quote_source=str(quote.get("source") or "") or None,
            # P0 refuses to infer the missing snapshot/adjustment/fetch contract.
            market_data_lineage_complete=False,
            strategy_hash=str(strategy.get("code_hash") or "") or None,
            backtest_hash=str(backtest.get("code_hash") or "") or None,
            # Current hash covers source only, not dependencies/config/indicator.
            strategy_fingerprint_complete=False,
            backtest_id=backtest.get("id"),
            indicator_id=backtest.get("indicator_id"),
            expected_indicator_id=indicator.get("id"),
            benchmark_present=benchmark_present,
            # No immutable data snapshot/replay receipt exists in current rows.
            backtest_reproducible=False,
            lookahead_proven=False,
            p2_validated=False,
            manual_reviewed=False,
            ai_stale=(
                analysis is not None
                and not _analysis_is_fresh(
                    analysis.get("created_at"), generated_at, trading_calendar
                )
            ),
        )
    return result


def _render_stock(
    stock: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    backtest: Mapping[str, Any] | None,
    quote: Mapping[str, Any] | None,
    strategy: Mapping[str, Any] | None,
    decision: GateDecision,
    trading_calendar: TradingCalendarSnapshot,
    *,
    generated_at: datetime,
    compact: bool,
    live_quotes_expected: bool,
) -> list[str]:
    symbol = str(stock.get("symbol") or "")
    fallback_name = quote.get("name") if quote else ""
    name = _single_line(stock.get("name") or fallback_name or "未命名", 20)
    lines = [f"🔹 {name} {symbol}"]
    if decision.qualification == "BLOCKED":
        lines.append("资格：⛔ BLOCKED｜仅进入证据修复队列")
        lines.append(_decision_reason_line(decision, 3 if compact else 4))
    elif decision.qualification == "RESEARCH_ONLY":
        lines.append("资格：🧪 RESEARCH_ONLY｜仅人工研究")
    else:
        lines.append("资格：🔎 VALIDATED_CANDIDATE｜仍须人工决策")

    quote_state = str(decision.evidence.get("quote_state") or "missing")
    quote_observed_at = _parse_quote_datetime(quote.get("observed_at")) if quote else None
    expected_asof = decision.evidence.get("expected_market_asof") or "未知"
    if quote_state == "fresh_close" and quote and quote_observed_at:
        lines.append(_quote_line(quote, quote_observed_at))
    elif live_quotes_expected:
        observed_text = quote_observed_at.strftime("%m-%d %H:%M") if quote_observed_at else "缺失"
        lines.append(
            f"最近收盘价：不可用于资格（预期{expected_asof}｜数据{observed_text}）"
        )

    if analysis:
        lines.append(
            _analysis_price_line(
                analysis,
                quote,
                quote_state,
                generated_at,
                trading_calendar,
            )
        )
        confidence = int(_number(analysis.get("confidence")) or 0)
        lines.append(
            f"AI研究标签：{_decision_label(analysis.get('decision'))}({confidence})"
            "｜原始方向性文案已从P0输出移除"
        )
        snapshot = analysis.get("indicators_snapshot")
        if isinstance(snapshot, Mapping):
            lines.append(_indicator_line(snapshot))
    else:
        lines.append("AI技术研究：暂无已完成分析")

    if backtest:
        backtest_lines, _ = _backtest_lines(backtest)
        lines.extend(backtest_lines)
        provenance = _backtest_provenance(backtest, strategy)
        lines.append(
            f"版本：{_provenance_label(provenance)}"
            f"｜样本{backtest.get('start_date') or '暂无'}~{backtest.get('end_date') or '暂无'}"
            f"｜生成{_short_date(backtest.get('created_at'))}"
        )
    else:
        provenance = "missing"
        lines.append("回测：暂无已保存结果")

    lines.append(_status_line(analysis, backtest, provenance))
    if decision.qualification == "BLOCKED":
        lines.append("下一步：补齐门禁证据后再进入人工复核")
    return lines


def render_report(
    data: ResearchData,
    *,
    macro_risk: Mapping[str, Any] | None = None,
    market_breadth: Mapping[str, Any] | None = None,
    quotes: Mapping[str, Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
    max_chars: int = TELEGRAM_SAFE_CHARS,
    compact: bool = False,
    live_quotes_expected: bool = False,
    trading_calendar: TradingCalendarSnapshot | None = None,
    decisions: Mapping[str, GateDecision] | None = None,
) -> str:
    now = generated_at or datetime.now(tz=SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    risk = macro_risk or {}
    risk_gate = _vix_risk_gate(risk, now)
    breadth = market_breadth or {}
    quotes = quotes or {}
    calendar = trading_calendar or load_trading_calendar(DEFAULT_TRADING_CALENDAR, now)
    decision_by_symbol = dict(decisions or _build_decision_map(data, quotes, calendar, now))
    expected_asof = expected_last_closed_trade_date(calendar, now)

    lines = [
        "📊 QuantDinger 个人 A 股研究雷达",
        f"⏰ 生成时间：{now.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')}",
        "",
        "🌍 宏观环境",
        *_macro_lines(risk, breadth, now),
        "",
    ]

    if not data.watchlist:
        lines.extend(
            [
                "🛡 P0资格门禁",
                f"规则：{GATE_RULE_VERSION}｜最近收盘日：{expected_asof or '不可核验'}",
                "状态：观察名单为空",
                "",
                "👁 个人 A 股观察名单",
                "当前名单为空；系统未使用任何默认股票。",
                "",
                "⚠️ 本报告仅展示已有研究记录，不构成投资建议。",
            ]
        )
        return "\n".join(lines)

    analysis_by_symbol = {str(item.get("symbol")): item for item in data.analyses}
    backtest_by_symbol = {str(item.get("symbol")): item for item in data.backtests}
    primary_indicator = data.indicators[0] if data.indicators else None
    primary_strategy = data.strategies[0] if data.strategies else None
    qualification_counts = {
        name: sum(1 for item in decision_by_symbol.values() if item.qualification == name)
        for name in ("BLOCKED", "RESEARCH_ONLY", "VALIDATED_CANDIDATE")
    }
    quote_states = {
        symbol: str(item.evidence.get("quote_state") or "missing")
        for symbol, item in decision_by_symbol.items()
    }
    benchmark_count = sum(
        1
        for item in data.backtests
        if isinstance(item.get("result"), Mapping)
        and isinstance(item["result"].get("benchmark"), Mapping)
    )
    fresh_ai_count = sum(
        1
        for item in data.analyses
        if _analysis_is_fresh(item.get("created_at"), now, calendar)
    )
    current_backtest_count = sum(
        1
        for item in data.backtests
        if _backtest_provenance(item, primary_strategy) == "current"
    )
    indicator_backtest_count = sum(
        1
        for item in data.backtests
        if primary_indicator and _same_id(item.get("indicator_id"), primary_indicator.get("id"))
    )
    fresh_quote_count = sum(1 for state in quote_states.values() if state == "fresh_close")
    quote_coverage = (
        f"｜收盘行情{fresh_quote_count}/{len(data.watchlist)}" if live_quotes_expected else ""
    )

    lines.extend(
        [
            "🛡 P0资格门禁",
            (
                f"规则：{GATE_RULE_VERSION}"
                f"｜最近收盘日：{expected_asof.isoformat() if expected_asof else '不可核验'}"
            ),
            (
                f"状态：BLOCKED {qualification_counts['BLOCKED']}"
                f"｜RESEARCH_ONLY {qualification_counts['RESEARCH_ONLY']}"
                f"｜VALIDATED_CANDIDATE {qualification_counts['VALIDATED_CANDIDATE']}"
            ),
            "",
            "🧪 研究配置",
            "个人指标：" + (
                f"{_single_line(primary_indicator.get('name'), 38)}"
                f"（回测关联{indicator_backtest_count}/{len(backtest_by_symbol)}）"
                if primary_indicator
                else "暂无个人指标"
            ),
            "策略：" + (
                f"{_single_line(primary_strategy.get('strategy_name'), 34)}"
                f"（{_strategy_status_label(primary_strategy.get('status'))}；"
                f"当前代码回测{current_backtest_count}/{len(backtest_by_symbol)}）"
                if primary_strategy
                else "暂无个人策略"
            ),
            (
                f"覆盖：观察{len(data.watchlist)}｜AI {len(analysis_by_symbol)}"
                f"（新鲜{fresh_ai_count}）"
                f"｜回测{len(backtest_by_symbol)}｜含基准{benchmark_count}"
                f"{quote_coverage}"
            ),
            "",
            "👁 个人 A 股观察名单",
        ]
    )

    for index, stock in enumerate(data.watchlist):
        symbol = str(stock.get("symbol") or "")
        if index:
            lines.append("────────────")
        lines.extend(
            _render_stock(
                stock,
                analysis_by_symbol.get(symbol),
                backtest_by_symbol.get(symbol),
                quotes.get(symbol),
                primary_strategy,
                decision_by_symbol[symbol],
                calendar,
                generated_at=now,
                compact=compact,
                live_quotes_expected=live_quotes_expected,
            )
        )

    missing_ai = len(data.watchlist) - len(analysis_by_symbol)
    missing_backtest = len(data.watchlist) - len(backtest_by_symbol)
    missing_benchmark = len(backtest_by_symbol) - benchmark_count
    stale_ai = len(analysis_by_symbol) - fresh_ai_count
    noncurrent_backtest = len(backtest_by_symbol) - current_backtest_count
    unlinked_indicator = len(backtest_by_symbol) - indicator_backtest_count
    missing_quotes = sum(1 for state in quote_states.values() if state == "missing") if live_quotes_expected else 0
    stale_quotes = sum(
        1
        for state in quote_states.values()
        if state not in ("fresh_close", "missing")
    ) if live_quotes_expected else 0
    gaps = []
    if risk_gate.get("freshness") == "stale":
        gaps.append("VIX背景过期")
    elif risk_gate.get("freshness") == "unknown":
        gaps.append("VIX背景缺失")
    if missing_ai:
        gaps.append(f"AI缺失{missing_ai}")
    if missing_backtest:
        gaps.append(f"回测缺失{missing_backtest}")
    if missing_benchmark:
        gaps.append(f"基准待补{missing_benchmark}")
    if stale_ai:
        gaps.append(f"AI过期{stale_ai}")
    if noncurrent_backtest:
        gaps.append(f"非当前代码回测{noncurrent_backtest}")
    if unlinked_indicator and primary_indicator:
        gaps.append(f"个人指标未关联{unlinked_indicator}")
    if missing_quotes:
        gaps.append(f"收盘行情缺失{missing_quotes}")
    if stale_quotes:
        gaps.append(f"收盘行情过期{stale_quotes}")
    if qualification_counts["BLOCKED"]:
        gaps.append(f"资格阻断{qualification_counts['BLOCKED']}")
    lines.extend(
        [
            "",
            "📎 数据质量：" + ("关键证据齐全且新鲜" if not gaps else "｜".join(gaps)),
            "⚠️ 本报告只输出研究资格与补证队列；P2验证前不产生交易行动标签。",
        ]
    )

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    if not compact:
        return render_report(
            data,
            macro_risk=risk,
            market_breadth=breadth,
            quotes=quotes,
            generated_at=now,
            max_chars=max_chars,
            compact=True,
            live_quotes_expected=live_quotes_expected,
            trading_calendar=calendar,
            decisions=decision_by_symbol,
        )
    suffix = "\n…报告已按 Telegram 长度限制截断"
    return rendered[: max(1, max_chars - len(suffix))].rstrip() + suffix

def _failure_report(message: str, generated_at: datetime | None = None) -> str:
    now = generated_at or datetime.now(tz=SHANGHAI)
    return "\n".join(
        [
            "📊 QuantDinger 个人 A 股研究雷达",
            f"⏰ 生成时间：{now.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')}",
            "",
            "❌ 报告生成失败",
            _single_line(message, 240),
            "系统未使用默认股票，也未执行 AI、回测或交易操作。",
        ]
    )


def _parse_generated_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=SHANGHAI)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchRadarError("generated-at 格式无效") from exc
    if parsed.tzinfo is None:
        raise ResearchRadarError("generated-at 必须包含时区")
    return parsed.astimezone(SHANGHAI)


def _audit_payload(
    data: ResearchData,
    calendar: TradingCalendarSnapshot,
    decisions: Mapping[str, GateDecision],
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "contract": "quantdinger.personal-research-eligibility.audit.v1",
        "gate_rule_version": GATE_RULE_VERSION,
        "generated_at": generated_at.astimezone(SHANGHAI).isoformat(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "output_channels": ["telegram_stdout"],
        "directional_action_labels_enabled": False,
        "watchlist_count": len(data.watchlist),
        "calendar": {
            "valid": calendar.valid,
            "source": calendar.source,
            "generated_at": calendar.generated_at.isoformat() if calendar.generated_at else None,
            "min_date": calendar.min_date.isoformat() if calendar.min_date else None,
            "max_date": calendar.max_date.isoformat() if calendar.max_date else None,
            "sha256": calendar.sha256,
            "error_code": calendar.error_code,
            "last_closed_trade_date": (
                expected_last_closed_trade_date(calendar, generated_at).isoformat()
                if expected_last_closed_trade_date(calendar, generated_at)
                else None
            ),
        },
        "decisions": [decisions[key].to_dict() for key in sorted(decisions)],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True, help="QuantDinger user id")
    parser.add_argument("--db-container", default=DEFAULT_DB_CONTAINER)
    parser.add_argument("--risk-state", type=Path, default=DEFAULT_RISK_STATE)
    parser.add_argument("--swing-db", type=Path, default=DEFAULT_SWING_DB)
    parser.add_argument("--trading-calendar", type=Path, default=DEFAULT_TRADING_CALENDAR)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument(
        "--generated-at",
        help="timezone-aware deterministic replay timestamp; production leaves this unset",
    )
    parser.add_argument(
        "--live-quotes",
        action="store_true",
        help="opt in to sending watchlist symbols to the Tencent quote endpoint",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    generated_at = datetime.now(tz=SHANGHAI)
    try:
        generated_at = _parse_generated_at(args.generated_at)
        calendar = load_trading_calendar(args.trading_calendar, generated_at)
        data = load_research_data(args.user_id, db_container=args.db_container)
        quotes = (
            fetch_live_quotes(item.get("symbol", "") for item in data.watchlist)
            if args.live_quotes
            else {}
        )
        decisions = _build_decision_map(data, quotes, calendar, generated_at)
        report = render_report(
            data,
            macro_risk=load_macro_risk(args.risk_state),
            market_breadth=load_market_breadth(args.swing_db),
            quotes=quotes,
            generated_at=generated_at,
            live_quotes_expected=args.live_quotes,
            trading_calendar=calendar,
            decisions=decisions,
        )
        write_audit_record(
            args.audit_dir,
            _audit_payload(data, calendar, decisions, generated_at),
            generated_at=generated_at,
        )
    except ResearchRadarError as exc:
        report = _failure_report(str(exc), generated_at)
    except Exception as exc:  # fail closed without leaking a traceback to Telegram
        report = _failure_report(f"未预期错误：{type(exc).__name__}", generated_at)
    sys.stdout.write(report.rstrip() + "\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
