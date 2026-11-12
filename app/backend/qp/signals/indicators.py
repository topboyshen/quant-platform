"""Pure-Python indicators for the M4 swing module. Semantics mirror the legacy pandas code:

- ``ema`` == ``Series.ewm(span=span, adjust=False).mean()``
- weekly bars == ``resample("W-FRI", label="right", closed="right")`` with
  ``close=last, open=first, high=max, low=min, volume=sum``, completed weeks only
- weekly MACD golden cross == previous DIF <= DEA and current DIF > DEA
- volume breakout == close crosses above MA(long) with volume > ratio * prior-window average
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Sequence

MA_LONG = 60
VOLUME_WINDOW = 20
VOLUME_RATIO = 1.5
MIN_WEEKLY_BARS = 35


@dataclass(frozen=True)
class Bar:
    date: dt.date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


def ema(values: Sequence[float], span: int) -> list[float]:
    """Exponential moving average with pandas' ``adjust=False`` seeding (first value)."""
    if span <= 0:
        raise ValueError("span must be positive")
    out: list[float] = []
    alpha = 2.0 / (span + 1.0)
    for i, value in enumerate(values):
        x = float(value)
        out.append(x if i == 0 else alpha * x + (1.0 - alpha) * out[-1])
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float]]:
    """Return (DIF, DEA); the histogram is intentionally not used by the trigger."""
    if not closes:
        return [], []
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    dif = [a - b for a, b in zip(fast_ema, slow_ema)]
    dea = ema(dif, signal)
    return dif, dea


def is_golden_cross(dif: Sequence[float], dea: Sequence[float]) -> bool:
    if len(dif) < 2 or len(dea) < 2:
        return False
    return dif[-2] <= dea[-2] and dif[-1] > dea[-1]


def _week_end(date: dt.date) -> dt.date:
    """First Friday on or after ``date``; matches a right-closed W-FRI bin."""
    return date + dt.timedelta(days=(4 - date.weekday()) % 7)


def weekly_bars(bars: Sequence[Bar]) -> list[Bar]:
    """Aggregate daily bars into completed W-FRI bars (label=right, closed=right).

    A week is completed only when the newest daily bar is on or after its Friday label.
    """
    ordered = sorted((b for b in bars if b.close is not None), key=lambda b: b.date)
    if not ordered:
        return []
    latest = ordered[-1].date
    groups: dict[dt.date, list[Bar]] = {}
    for bar in ordered:
        groups.setdefault(_week_end(bar.date), []).append(bar)
    out: list[Bar] = []
    for label in sorted(groups):
        if label > latest:
            continue
        chunk = groups[label]
        closes = [b.close for b in chunk]
        opens = [b.open for b in chunk if b.open is not None]
        highs = [b.high for b in chunk if b.high is not None]
        lows = [b.low for b in chunk if b.low is not None]
        volumes = [b.volume for b in chunk if b.volume is not None]
        out.append(Bar(
            date=label,
            close=closes[-1],
            open=opens[0] if opens else None,
            high=max(highs) if highs else None,
            low=min(lows) if lows else None,
            volume=sum(volumes) if volumes else None,
        ))
    return out


def weekly_golden_cross(bars: Sequence[Bar]) -> tuple[bool, dict[str, float | None]]:
    """Evaluate the weekly MACD golden cross; returns (triggered, diagnostics)."""
    weekly = weekly_bars(bars)
    diag: dict[str, float | None] = {
        "completed_bars": float(len(weekly)),
        "dif": None, "dea": None, "previous_dif": None, "previous_dea": None,
        "weekly_close": None, "data_date": None,
    }
    if len(weekly) < MIN_WEEKLY_BARS:
        return False, diag
    closes = [b.close for b in weekly]
    dif, dea = macd(closes)
    if not all(math.isfinite(v) for v in (dif[-1], dea[-1], dif[-2], dea[-2])):
        return False, diag
    diag.update({
        "dif": dif[-1], "dea": dea[-1], "previous_dif": dif[-2], "previous_dea": dea[-2],
        "weekly_close": closes[-1], "data_date": weekly[-1].date.isoformat(),
    })
    return is_golden_cross(dif, dea), diag


def volume_breakout(bars: Sequence[Bar], ma_long: int = MA_LONG, volume_window: int = VOLUME_WINDOW,
                    ratio: float = VOLUME_RATIO) -> tuple[bool, dict[str, float | None]]:
    """Daily close crossing above MA(long) on volume > ratio * prior average."""
    diag: dict[str, float | None] = {
        "ma": None, "previous_ma": None, "close": None, "previous_close": None,
        "volume": None, "avg_volume": None, "volume_ratio": None,
    }
    if len(bars) < ma_long + 5:
        return False, diag
    closes = [b.close for b in bars]
    ma = sum(closes[-ma_long:]) / ma_long
    previous_ma = sum(closes[-ma_long - 1:-1]) / ma_long
    if not (math.isfinite(ma) and math.isfinite(previous_ma)):
        return False, diag
    volume = bars[-1].volume
    prior = [b.volume for b in bars[-(volume_window + 1):-1]]
    if volume is None or any(v is None for v in prior):
        return False, diag
    avg_volume = sum(prior) / len(prior)  # type: ignore[arg-type]
    if avg_volume <= 0 or not math.isfinite(avg_volume):
        return False, diag
    # Legacy rounds the ratio to two decimals before comparing with ``>=``; mirror it
    # so a shadow comparison cannot diverge on the 1.495-1.4999 boundary.
    volume_ratio = round(volume / avg_volume, 2)
    diag.update({
        "ma": ma, "previous_ma": previous_ma, "close": closes[-1], "previous_close": closes[-2],
        "volume": volume, "avg_volume": avg_volume, "volume_ratio": volume_ratio,
    })
    triggered = closes[-2] <= previous_ma and closes[-1] > ma and volume_ratio >= ratio
    return triggered, diag
