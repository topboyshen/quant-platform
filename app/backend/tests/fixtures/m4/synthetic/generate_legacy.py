#!/usr/bin/env python3
"""Generate M4 synthetic expectations by running the *legacy* code directly.

Run on the legacy host with its own runtime (read-only, no writes):

    ssh Ubuntu-nnG8 '/home/ubuntu/trading-radar/runtime/current-swing/bin/python -' \
        < tests/fixtures/m4/synthetic/generate_legacy.py \
        > /tmp/m4-synthetic.json

The printed JSON is the fixture file ``legacy_expected.json``.  Expectations are
never hand-written: the risk-gate matrix comes from
``radar_swing_eod._apply_eod_risk_gate`` and the volume-breakout case from
``radar.swing.trend_strategy.TrendConfirmStrategy`` in the legacy runtime.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

logging.disable(logging.CRITICAL)
sys.path.insert(0, "/home/ubuntu/trading-radar")

import radar_swing_eod  # noqa: E402
from radar.swing.trend_strategy import TrendConfirmStrategy  # noqa: E402


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def risk_gate_matrix() -> dict:
    targets = [
        {"code": "510300", "name": "沪深300ETF", "sector": "宽基"},
        {"code": "159915", "name": "创业板ETF", "sector": "成长"},
        {"code": "512480", "name": "半导体ETF", "sector": "行业"},
        {"code": "513050", "name": "中概互联网ETF", "sector": "港股科技"},
        {"code": "518880", "name": "黄金ETF", "sector": "避险"},
    ]
    signals = [SimpleNamespace(code=item["code"], name=item["name"]) for item in targets]
    levels = [
        {"level": "ok", "risk_state": {"level": "ok"}},
        {"level": "OK", "risk_state": {"level": "OK"}},
        {"level": "growth_blocked", "risk_state": {"level": "growth_blocked"}},
        {"level": "critical", "risk_state": {"level": "critical"}},
        {"level": "warning", "risk_state": {"level": "warning"}},
        {"level": "unavailable", "risk_state": {"level": "unavailable"}},
        {"level": "unknown", "risk_state": {"level": "something_new"}},
        {"level": "missing", "risk_state": None},
        {"level": "missing_level", "risk_state": {}},
    ]
    cases = []
    for entry in levels:
        allowed, blocked = radar_swing_eod._apply_eod_risk_gate(
            list(signals), targets, entry["risk_state"], logging.getLogger("fixture"))
        cases.append({
            **entry,
            "expected": {
                "allowed": [signal.code for signal in allowed],
                "blocked": [signal.code for signal in blocked],
            },
        })
    return {"targets": targets, "cases": cases}


class _BarsFetcher:
    def __init__(self, frame):
        self.frame = frame

    def get_historical_data(self, code, **kwargs):
        return self.frame.copy()


def volume_breakout_case() -> dict:
    import pandas as pd

    start = dt.date(2026, 5, 1)
    bars = []
    for offset in range(80):
        day = start + dt.timedelta(days=offset)
        close = 100.0
        volume = 1000.0
        if offset == 78:
            close, volume = 99.0, 1000.0
        elif offset == 79:
            close, volume = 101.0, 3000.0
        bars.append({"date": day.isoformat(), "open": close, "high": close,
                     "low": close, "close": close, "volume": volume})
    frame = pd.DataFrame(bars)
    frame["date"] = pd.to_datetime(frame["date"])
    frame.attrs["source"] = "synthetic-fixture"
    strategy = TrendConfirmStrategy(_BarsFetcher(frame), logging.getLogger("fixture"),
                                    daily_breakout_enabled=True)
    signals = strategy.evaluate("510300", "沪深300ETF")
    return {
        "code": "510300",
        "name": "沪深300ETF",
        "daily_breakout_enabled": True,
        "bars": bars,
        "expected_signals": [
            {
                "code": signal.code,
                "name": signal.name,
                "signal_type": signal.signal_type,
                "period": signal.period,
                "title": signal.title,
                "detail": signal.detail,
                "confidence": signal.confidence,
            }
            for signal in signals
        ],
        "diagnostics": strategy.last_diagnostics,
    }


def main() -> None:
    payload = {
        "schema_version": "1.0",
        "generator": "tests/fixtures/m4/synthetic/generate_legacy.py",
        "legacy_source": {
            "radar_swing_eod.py": _sha256(radar_swing_eod.__file__),
            "radar/swing/trend_strategy.py": _sha256(
                sys.modules["radar.swing.trend_strategy"].__file__),
        },
        "risk_gate": risk_gate_matrix(),
        "volume_breakout": volume_breakout_case(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
