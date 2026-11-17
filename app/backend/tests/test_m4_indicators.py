"""Deterministic tests for the pure-Python indicator semantics (pandas parity targets)."""
import datetime as dt

import pytest

from qp.signals.indicators import (
    Bar,
    ema,
    is_golden_cross,
    macd,
    volume_breakout,
    weekly_bars,
    weekly_golden_cross,
)


def test_ema_matches_adjust_false():
    assert ema([1, 2, 3, 4], 3) == [1, 1.5, 2.25, 3.125]
    assert ema([], 3) == []
    with pytest.raises(ValueError):
        ema([1], 0)


def test_macd_flat_series_is_zero_and_no_cross():
    dif, dea = macd([10.0] * 40)
    assert all(v == 0.0 for v in dif)
    assert all(v == 0.0 for v in dea)
    assert is_golden_cross(dif, dea) is False


def test_golden_cross_predicate():
    assert is_golden_cross([-1.0, 0.1], [-0.5, 0.0]) is True
    assert is_golden_cross([0.0, -0.1], [0.0, 0.0]) is False
    assert is_golden_cross([0.1], [0.0]) is False


def test_weekly_aggregation_only_completed_weeks():
    bars = [
        Bar(dt.date(2026, 11, 2), 10.5, open=10, high=11, low=9, volume=100),
        Bar(dt.date(2026, 11, 3), 11.0, open=10.5, high=12, low=10, volume=200),
        Bar(dt.date(2026, 11, 6), 12.0, open=11, high=12.5, low=10.5, volume=300),
        Bar(dt.date(2026, 11, 9), 13.0, open=12, high=13, low=12, volume=400),
    ]
    weekly = weekly_bars(bars)
    assert len(weekly) == 1
    w = weekly[0]
    assert w.date == dt.date(2026, 11, 6)
    assert (w.open, w.high, w.low, w.close, w.volume) == (10, 12.5, 9, 12.0, 600)


def test_week_without_friday_bar_completes_only_next_week():
    mon_thu = [Bar(dt.date(2026, 11, d), 10.0, open=10, high=10, low=10, volume=100) for d in (2, 3, 4, 5)]
    assert weekly_bars(mon_thu) == []
    mon_thu.append(Bar(dt.date(2026, 11, 9), 10.0, open=10, high=10, low=10, volume=100))
    weekly = weekly_bars(mon_thu)
    assert len(weekly) == 1 and weekly[0].date == dt.date(2026, 11, 6)


def test_weekly_golden_cross_diagnostics():
    start = dt.date(2026, 1, 5)  # Monday
    bars = []
    for week in range(35):
        monday = start + dt.timedelta(days=7 * week)
        for offset in range(5):
            bars.append(Bar(monday + dt.timedelta(days=offset), 10.0, open=10, high=10, low=10, volume=100))
    for offset in range(3):  # Wed of week 36: newest daily bar, completed weeks stay 35
        bars.append(Bar(start + dt.timedelta(days=7 * 35 + offset), 10.0, open=10, high=10, low=10, volume=100))
    triggered, diag = weekly_golden_cross(bars)
    assert triggered is False
    assert diag["completed_bars"] == 35
    assert diag["data_date"] == (start + dt.timedelta(days=7 * 34 + 4)).isoformat()


def test_weekly_golden_cross_insufficient_history():
    start = dt.date(2026, 1, 5)
    bars = [Bar(start + dt.timedelta(days=7 * week + offset), 10.0, open=10, high=10, low=10, volume=100)
            for week in range(34) for offset in range(5)]
    triggered, diag = weekly_golden_cross(bars)
    assert triggered is False and diag["completed_bars"] == 34


def test_volume_breakout_triggers_on_cross_and_volume():
    bars = [Bar(dt.date(2026, 1, 1) + dt.timedelta(days=i), 10.0, open=10, high=10, low=10, volume=100)
            for i in range(64)]
    bars.append(Bar(bars[-1].date + dt.timedelta(days=1), 11.0, open=10, high=11, low=10, volume=200))
    triggered, diag = volume_breakout(bars)
    assert triggered is True
    assert diag["volume_ratio"] == pytest.approx(2.0)
    assert diag["previous_close"] == 10.0 and diag["previous_ma"] == 10.0

    bars[-1] = Bar(bars[-1].date, 11.0, open=10, high=11, low=10, volume=120)
    triggered, diag = volume_breakout(bars)
    assert triggered is False


def test_volume_breakout_requires_history():
    bars = [Bar(dt.date(2026, 1, 1) + dt.timedelta(days=i), 10.0, volume=100) for i in range(64)]
    triggered, _ = volume_breakout(bars)
    assert triggered is False


def test_volume_ratio_is_rounded_before_the_threshold():
    """Legacy rounds the raw ratio to 2 decimals and compares ``>=``; 1.496 triggers."""
    base = [Bar(dt.date(2026, 1, 1) + dt.timedelta(days=i), 10.0, volume=100) for i in range(64)]
    below = base + [Bar(base[-1].date + dt.timedelta(days=1), 11.0, volume=149.4)]  # 1.494 -> 1.49
    at = base + [Bar(base[-1].date + dt.timedelta(days=1), 11.0, volume=149.6)]       # 1.496 -> 1.50
    triggered, diag = volume_breakout(below)
    assert triggered is False and diag["volume_ratio"] == 1.49
    triggered, diag = volume_breakout(at)
    assert triggered is True and diag["volume_ratio"] == 1.5
