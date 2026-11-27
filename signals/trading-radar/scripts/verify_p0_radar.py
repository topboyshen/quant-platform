#!/usr/bin/env python3
"""Frozen 2026-08-15 report replay for the P0 Telegram renderer."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
SHANGHAI = ZoneInfo("Asia/Shanghai")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(ROOT))
radar_path = ROOT / "quantdinger_research_radar_p0.py"
if not radar_path.exists():
    radar_path = ROOT / "quantdinger_research_radar.py"
radar = load("radar_p0_candidate", radar_path)
gate = load("research_eligibility_fixture", ROOT / "research_eligibility.py")


now = datetime(2026, 8, 15, 18, 56, tzinfo=SHANGHAI)
sessions = []
cursor = date(2022, 1, 3)
while cursor <= date(2026, 12, 31):
    if cursor.weekday() < 5:
        sessions.append(cursor)
    cursor += timedelta(days=1)

payload = {
    "schema_version": 1,
    "source": "akshare.tool_trade_date_hist_sina",
    "generated_at": "2026-08-14T01:20:00+08:00",
    "count": len(sessions),
    "min_date": sessions[0].isoformat(),
    "max_date": sessions[-1].isoformat(),
    "trading_dates": [item.isoformat() for item in sessions],
}

symbols = ["600875", "002617", "300088", "300033", "603011", "002979", "300502"]
names = ["东方电气", "露笑科技", "长信科技", "同花顺", "合锻智能", "雷赛智能", "新易盛"]
watchlist = [
    {"id": index, "user_id": 1, "market": "CNStock", "symbol": symbol, "name": name}
    for index, (symbol, name) in enumerate(zip(symbols, names), 1)
]
analyses = [
    {
        "symbol": symbol,
        "decision": "BUY" if index == 0 else "SELL",
        "confidence": 72 if index == 0 else 61,
        "price_at_analysis": 20 + index,
        "summary": "建议轻仓低吸，反弹即卖出并设置止损；原始方向文案不得输出。",
        "indicators_snapshot": {"trend": "up", "rsi": 51.1, "macd_signal": "golden_cross", "atr_pct": 4.03},
        "task_status": "completed",
        "created_at": "2026-08-14T14:00:00+08:00",
    }
    for index, symbol in enumerate(symbols)
]
strategy_hash = "b" * 64
backtests = []
for index, symbol in enumerate(symbols[:5], 1):
    backtests.append(
        {
            "id": index,
            "symbol": symbol,
            "indicator_id": None,
            "strategy_id": 1,
            "strategy_name": "A股长多趋势动量",
            "start_date": "2023-08-01",
            "end_date": "2026-07-30",
            "status": "success",
            "code_hash": "a" * 64,
            "created_at": "2026-08-06T13:49:11+08:00",
            "result": {
                "totalReturn": 46.0,
                "maxDrawdown": -21.53,
                "sharpeRatio": 0.79,
                "profitFactor": 3.95,
                "totalTrades": 9,
                "benchmark": {"totalReturn": 101.90, "maxDrawdown": -44.29, "sharpeRatio": 1.0},
            },
        }
    )

data = radar.ResearchData(
    watchlist=watchlist,
    analyses=analyses,
    indicators=[{"id": 1, "name": "个人趋势指标"}],
    strategies=[{"id": 1, "strategy_name": "A股长多趋势动量", "status": "stopped", "code_hash": strategy_hash}],
    backtests=backtests,
)
quotes = {
    symbol: {
        "name": name,
        "price": 20 + index,
        "pct": 0.5,
        "observed_at": "2026-08-14T15:00:00+08:00",
        "source": "腾讯",
    }
    for index, (symbol, name) in enumerate(zip(symbols, names), 1)
}

with tempfile.TemporaryDirectory() as temp:
    calendar_path = Path(temp) / "calendar.json"
    calendar_path.write_text(json.dumps(payload), encoding="utf-8")
    calendar = gate.load_trading_calendar(calendar_path, now)
    assert calendar.valid, calendar.error_code
    decisions = radar._build_decision_map(data, quotes, calendar, now)
    report = radar.render_report(
        data,
        quotes=quotes,
        generated_at=now,
        live_quotes_expected=True,
        trading_calendar=calendar,
        decisions=decisions,
    )

assert gate.expected_last_closed_trade_date(calendar, now) == date(2026, 8, 14)
assert len(decisions) == 7
assert all(item.qualification == "BLOCKED" for item in decisions.values())
assert "最近收盘日：2026-08-14" in report
assert "状态：BLOCKED 7｜RESEARCH_ONLY 0｜VALIDATED_CANDIDATE 0" in report
assert report.count("资格：⛔ BLOCKED") == 7
assert report.count("最近收盘价：") == 7
assert "收盘行情7/7" in report
assert "实时行情过期7" not in report
assert "行动结论：" not in report
assert not re.search(r"轻仓|低吸|做空|买入|卖出|加仓|减仓|止损", report), report
assert len(report) <= radar.TELEGRAM_SAFE_CHARS

print("FROZEN_REPLAY=PASS asof=2026-08-14 blocked=7 candidates=0")
print("TELEGRAM_CONTRACT=PASS closed_quotes=7 action_phrases=0")
print(f"REPORT_LENGTH=PASS chars={len(report)} limit={radar.TELEGRAM_SAFE_CHARS}")

