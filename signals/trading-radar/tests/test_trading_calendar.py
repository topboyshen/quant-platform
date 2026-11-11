from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from radar.core.scan_scheduler import HolidayCalendar
from scripts import update_trading_calendar


def _reset_calendar_cache() -> None:
    HolidayCalendar._cache_state = (None, frozenset(), None, None)


def _weekday_dates(start: datetime, count: int) -> list[str]:
    values = []
    cursor = start
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return values


def _weekday_dates_ending(end: datetime, count: int) -> list[str]:
    values = []
    cursor = end
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)
    return sorted(values)


def test_holiday_calendar_prefers_cached_trading_dates(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    dates = _weekday_dates(datetime(2026, 1, 1), 120)
    dates.remove("2026-01-02")
    payload = {
        "min_date": dates[0],
        "max_date": dates[-1],
        "trading_dates": dates,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(path))
    _reset_calendar_cache()

    assert HolidayCalendar.is_holiday(datetime(2026, 1, 1)) is False
    assert HolidayCalendar.is_holiday(datetime(2026, 1, 2)) is True


def test_holiday_calendar_reloads_atomically_replaced_cache(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    base = _weekday_dates(datetime(2026, 1, 1), 120)
    path.write_text(
        json.dumps({"min_date": base[0], "max_date": base[-1], "trading_dates": base}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(path))
    _reset_calendar_cache()
    assert HolidayCalendar.is_holiday(datetime(2026, 2, 3)) is False

    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {
                "min_date": "2026-01-01",
                "max_date": base[-1],
                "trading_dates": [day for day in base if day != "2026-02-03"],
            }
        ),
        encoding="utf-8",
    )
    replacement.replace(path)
    assert HolidayCalendar.is_holiday(datetime(2026, 2, 3)) is True


def test_holiday_calendar_detects_equal_size_atomic_replacement(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    base = _weekday_dates(datetime(2026, 1, 1), 120)
    payload = {"min_date": base[0], "max_date": base[-1], "trading_dates": base}
    original = json.dumps(payload)
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(path))
    _reset_calendar_cache()
    assert HolidayCalendar.is_holiday(datetime(2026, 2, 3)) is False

    changed = list(base)
    changed[changed.index("2026-02-03")] = "2026-02-04"
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {"min_date": changed[0], "max_date": changed[-1], "trading_dates": changed}
        ),
        encoding="utf-8",
    )
    assert replacement.stat().st_size == path.stat().st_size
    replacement.replace(path)

    assert HolidayCalendar.is_holiday(datetime(2026, 2, 3)) is True


def test_holiday_calendar_preserves_last_valid_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    dates = _weekday_dates(datetime(2026, 8, 3), 120)
    path.write_text(
        json.dumps(
            {"min_date": dates[0], "max_date": dates[-1], "trading_dates": dates}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(path))
    _reset_calendar_cache()
    # Synthetic cache marks the official holiday as a trading day so fallback is observable.
    assert HolidayCalendar.is_holiday(datetime(2026, 10, 1)) is False

    path.write_text("[]\n", encoding="utf-8")
    assert HolidayCalendar.is_holiday(datetime(2026, 10, 1)) is False


def test_holiday_calendar_keeps_static_fallback_outside_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(tmp_path / "missing.json"))
    _reset_calendar_cache()

    assert HolidayCalendar.is_holiday(datetime(2026, 10, 1)) is True
    assert HolidayCalendar.is_holiday(datetime(2027, 1, 4)) is False


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (datetime(2026, 2, 15), True),
        (datetime(2026, 2, 23), True),
        (datetime(2026, 9, 25), True),
        (datetime(2026, 6, 22), False),
        (datetime(2026, 9, 28), False),
        (datetime(2026, 9, 29), False),
        (datetime(2026, 9, 30), False),
        (datetime(2026, 10, 8), False),
    ],
)
def test_static_fallback_matches_2026_exchange_calendar(
    day, expected, monkeypatch, tmp_path
):
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(tmp_path / "missing.json"))
    _reset_calendar_cache()

    assert HolidayCalendar.is_holiday(day) is expected


def test_holiday_calendar_rejects_non_object_cache(monkeypatch, tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(["2026-01-01"] * 120), encoding="utf-8")
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(path))
    _reset_calendar_cache()

    assert HolidayCalendar.is_holiday(datetime(2026, 10, 1)) is True


def test_updater_rejects_inconsistent_existing_range(tmp_path):
    dates = _weekday_dates_ending(datetime(2026, 12, 31), 1001)
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "count": len(dates),
                "min_date": dates[0],
                "max_date": "2099-12-31",
                "trading_dates": dates,
            }
        ),
        encoding="utf-8",
    )

    assert update_trading_calendar._load_existing(path) == {}


def test_updater_skips_network_for_fresh_well_covered_cache(monkeypatch, tmp_path):
    now = datetime(
        2026,
        7,
        15,
        12,
        0,
        tzinfo=update_trading_calendar.ZoneInfo("Asia/Shanghai"),
    )
    dates = _weekday_dates_ending(datetime(2026, 12, 31), 1201)
    output = tmp_path / "calendar.json"
    output.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(timespec="seconds"),
                "count": len(dates),
                "min_date": dates[0],
                "max_date": dates[-1],
                "trading_dates": dates,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(update_trading_calendar, "_now", lambda: now)
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            tool_trade_date_hist_sina=lambda: pytest.fail("fresh cache used network")
        ),
    )

    assert (
        update_trading_calendar.refresh(
            output, force=False, min_ahead_days=30, max_age_days=14
        )
        == 0
    )


def test_updater_rejects_sparse_far_future_outlier():
    now = datetime(
        2026,
        7,
        15,
        12,
        0,
        tzinfo=update_trading_calendar.ZoneInfo("Asia/Shanghai"),
    )
    dates = _weekday_dates_ending(datetime(2026, 7, 15), 1000)
    dates.append("2099-12-31")

    with pytest.raises(ValueError, match="远期边界异常"):
        update_trading_calendar._validate_calendar_dates(dates, now)


def test_updater_preserves_file_when_source_coverage_is_insufficient(
    monkeypatch, tmp_path
):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=update_trading_calendar.ZoneInfo("Asia/Shanghai"))
    date_strings = _weekday_dates_ending(now.replace(tzinfo=None), 1201)
    dates = [datetime.fromisoformat(value).date() for value in date_strings]

    class FakeFrame:
        columns = ["trade_date"]

        def __getitem__(self, key):
            assert key == "trade_date"
            return SimpleNamespace(tolist=lambda: dates)

    monkeypatch.setattr(update_trading_calendar, "_now", lambda: now)
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(tool_trade_date_hist_sina=lambda: FakeFrame()),
    )
    output = tmp_path / "calendar.json"
    output.write_text("old-cache\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="覆盖不足"):
        update_trading_calendar.refresh(output, force=True, min_ahead_days=30)

    assert output.read_text(encoding="utf-8") == "old-cache\n"
