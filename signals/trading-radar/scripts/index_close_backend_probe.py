#!/usr/bin/env python3
"""Fetch exact-date A-share index closes inside the pinned QuantDinger backend.

The host updater streams this file to ``docker exec ... python -``.  Keeping
the probe separate makes the Tencent -> Sina fallback deterministic and lets
the fallback/validation logic be tested offline without importing QuantDinger.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REQUIRED_SYMBOLS = ("sh000001", "sh000300", "sz399001", "sz399006")
OUTPUT_PREFIX = "INDEX_CLOSE_JSON="


def _positive_close(value: object) -> float:
    close = float(value)
    if not math.isfinite(close) or close <= 0:
        raise ValueError("close must be finite and positive")
    return round(close, 4)


def _tencent_exact(rows: Iterable[object], expected_as_of: str) -> float | None:
    for raw in rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            continue
        if str(raw[0])[:10] != expected_as_of:
            continue
        return _positive_close(raw[2])
    return None


def _sina_exact(rows: Iterable[object], expected_as_of: str) -> float | None:
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            trade_date = datetime.fromtimestamp(
                float(raw["time"]), tz=SHANGHAI_TZ
            ).date().isoformat()
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
        if trade_date == expected_as_of:
            return _positive_close(raw.get("close"))
    return None


def fetch_exact_close(
    symbol: str,
    expected_as_of: str,
    *,
    fetch_tencent: Callable[[str], Iterable[object]],
    fetch_sina: Callable[[str], Iterable[object]],
) -> dict[str, object]:
    errors: list[str] = []
    try:
        close = _tencent_exact(fetch_tencent(symbol), expected_as_of)
    except Exception as exc:  # upstream boundary
        close = None
        errors.append(f"tencent:{type(exc).__name__}")
    if close is not None:
        return {
            "symbol": symbol,
            "close": close,
            "trade_date": expected_as_of,
            "source": "tencent",
        }

    try:
        close = _sina_exact(fetch_sina(symbol), expected_as_of)
    except Exception as exc:  # upstream boundary
        close = None
        errors.append(f"sina:{type(exc).__name__}")
    if close is not None:
        return {
            "symbol": symbol,
            "close": close,
            "trade_date": expected_as_of,
            "source": "sina",
        }

    detail = ",".join(errors) if errors else "exact-date rows absent"
    raise RuntimeError(f"{symbol}: {expected_as_of} unavailable ({detail})")


def collect_closes(
    expected_as_of: str,
    *,
    fetch_tencent: Callable[[str], Iterable[object]],
    fetch_sina: Callable[[str], Iterable[object]],
) -> dict[str, object]:
    closes = [
        fetch_exact_close(
            symbol,
            expected_as_of,
            fetch_tencent=fetch_tencent,
            fetch_sina=fetch_sina,
        )
        for symbol in REQUIRED_SYMBOLS
    ]
    sources = {str(item["source"]) for item in closes}
    aggregate_source = "+".join(
        source for source in ("tencent", "sina") if source in sources
    )
    return {
        "expected_as_of": expected_as_of,
        "source": aggregate_source,
        "closes": closes,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("usage: index_close_backend_probe.py YYYY-MM-DD")
    expected_as_of = datetime.strptime(args[0], "%Y-%m-%d").date().isoformat()

    from app.data_sources.cn_stock import CNStockDataSource
    from app.data_sources.tencent import fetch_kline

    data_source = CNStockDataSource()

    def fetch_tencent(symbol: str):
        return fetch_kline(symbol, period="day", count=10, adj="qfq", timeout=10)

    def fetch_sina(symbol: str):
        return data_source._fetch_sina_daily(  # noqa: SLF001 - pinned production API
            symbol, limit=10, before_time=None
        )

    payload = collect_closes(
        expected_as_of,
        fetch_tencent=fetch_tencent,
        fetch_sina=fetch_sina,
    )
    print(OUTPUT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
