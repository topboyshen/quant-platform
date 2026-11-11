"""No-network contracts for adjacent-session returns and valid US OHLC."""

from __future__ import annotations

import copy
import math
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from akshare_provider import AkShareMorningProvider, RunState  # noqa: E402


CUTOFF = datetime.fromisoformat("2026-10-09T08:00:00+08:00")


def quote(day, close):
    return {"date": day, "open": close - 1, "high": close + 2,
            "low": close - 2, "close": close, "volume": 10}


class RowsApi:
    def __init__(self, rows):
        self.rows = rows

    def index_us_stock_sina(self, symbol):
        return copy.deepcopy(self.rows)


class OfflineProvider(AkShareMorningProvider):
    def _call(self, name, *args, **kwargs):
        return getattr(self.api, name)(*args, **kwargs)


class UsIndexReturnIntegrityTests(unittest.TestCase):
    def payload(self, rows, cutoff=CUTOFF):
        with tempfile.TemporaryDirectory() as cache:
            return OfflineProvider(RowsApi(rows), cache_dir=cache)._us_index(".IXIC", cutoff)

    def test_missing_adjacent_session_cannot_turn_two_day_return_into_daily_return(self):
        rows = [quote("2026-10-06", 100), quote("2026-10-08", 103)]
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(rows), cache_dir=cache)
            payload = provider._us_index(".IXIC", CUTOFF)
            self.assertEqual(payload.data["close"], 103)
            self.assertEqual(payload.data["sessionDate"], "2026-10-08")
            self.assertEqual(payload.data["expectedSessionDate"], "2026-10-08")
            self.assertEqual(payload.data["previousCloseDate"], "2026-10-07")
            self.assertIsNone(payload.data["previousClose"])
            self.assertIsNone(payload.data["change"])
            self.assertIsNone(payload.data["changePercent"])
            self.assertEqual(payload.data["freshnessStatus"], "unverified")
            self.assertEqual(payload.sources[0]["freshnessStatus"], "unverified")
            self.assertTrue(any("紧邻前序交易日" in message for message in payload.warnings))
            result = provider._module(RunState(), "us_index_.IXIC", "fixture", lambda: payload, None)
            self.assertEqual(result.score, 0)
            self.assertEqual(list(Path(cache).iterdir()), [])

    def test_valid_return_uses_adjacent_row_not_any_older_row(self):
        payload = self.payload([
            quote("2026-10-06", 90), quote("2026-10-07", 100), quote("2026-10-08", 102),
        ])
        self.assertEqual(payload.data["previousClose"], 100)
        self.assertEqual(payload.data["previousCloseDate"], "2026-10-07")
        self.assertEqual(payload.data["change"], 2)
        self.assertEqual(payload.data["changePercent"], 2)
        self.assertEqual(payload.data["freshnessStatus"], "current")
        self.assertEqual(payload.data["units"]["previousClose"], "index_point")

    def test_duplicate_latest_session_fails_closed_even_if_values_equal(self):
        rows = [quote("2026-10-07", 100), quote("2026-10-08", 102), quote("2026-10-08", 102)]
        with self.assertRaisesRegex(ValueError, "日线重复"):
            self.payload(rows)
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(rows), cache_dir=cache)
            result = provider._module(RunState(), "us_index_.IXIC", "fixture", lambda: provider._us_index(".IXIC", CUTOFF), None)
            self.assertEqual(result.status, "unavailable")
            self.assertIsNone(result.data)
            self.assertEqual(result.score, 0)

    def test_duplicate_adjacent_previous_session_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "紧邻前序交易日.*日线重复"):
            self.payload([quote("2026-10-07", 100), quote("2026-10-07", 101), quote("2026-10-08", 102)])

    def test_invalid_previous_close_is_missing_not_a_denominator(self):
        for value in (None, 0, -1, math.nan, math.inf):
            with self.subTest(value=value):
                previous = quote("2026-10-07", 100)
                previous["close"] = value
                payload = self.payload([previous, quote("2026-10-08", 102)])
                self.assertIsNone(payload.data["previousClose"])
                self.assertIsNone(payload.data["changePercent"])
                self.assertEqual(payload.data["close"], 102)
                self.assertEqual(payload.data["freshnessStatus"], "unverified")

    def test_invalid_current_ohlc_does_not_count_as_current_available(self):
        for field in ("open", "high", "low", "close"):
            for value in (None, 0, -1, math.nan, math.inf):
                with self.subTest(field=field, value=value):
                    current = quote("2026-10-08", 102)
                    current[field] = value
                    with self.assertRaisesRegex(ValueError, "OHLC 缺失"):
                        self.payload([quote("2026-10-07", 100), current])

    def test_invalid_current_ohlc_bounds_are_rejected(self):
        for values in ({"high": 101}, {"low": 102}, {"high": 99, "low": 105}):
            with self.subTest(values=values):
                current = quote("2026-10-08", 102)
                current.update(values)
                with self.assertRaisesRegex(ValueError, "OHLC 算术关系"):
                    self.payload([quote("2026-10-07", 100), current])

    def test_weekend_and_holiday_previous_session_is_not_previous_calendar_day(self):
        cases = [
            ("2026-09-04", "2026-09-08", "2026-09-09T08:00:00+08:00"),
            ("2026-11-25", "2026-11-27", "2026-11-28T08:00:00+08:00"),
        ]
        for previous, current, cutoff in cases:
            with self.subTest(previous=previous, current=current):
                payload = self.payload([quote(previous, 100), quote(current, 102)], datetime.fromisoformat(cutoff))
                self.assertEqual(payload.data["previousCloseDate"], previous)
                self.assertEqual(payload.data["changePercent"], 2)
                self.assertEqual(payload.data["freshnessStatus"], "current")

    def test_dst_does_not_break_adjacent_session_identity(self):
        for previous, current, cutoff in (
            ("2026-03-06", "2026-03-09", "2026-03-10T08:00:00+08:00"),
            ("2026-10-30", "2026-11-02", "2026-11-03T08:00:00+08:00"),
        ):
            with self.subTest(current=current):
                payload = self.payload([quote(previous, 100), quote(current, 102)], datetime.fromisoformat(cutoff))
                self.assertEqual(payload.data["previousCloseDate"], previous)
                self.assertEqual(payload.data["changePercent"], 2)
                self.assertEqual(payload.data["freshnessStatus"], "current")

    def test_future_and_closed_day_rows_cannot_supply_previous_session(self):
        payload = self.payload([
            quote("2026-10-04", 90), quote("2026-10-06", 95), quote("2026-10-08", 102),
            quote("2026-10-09", 200), quote("2026-10-09", 200),
        ])
        self.assertEqual(payload.data["sessionDate"], "2026-10-08")
        self.assertEqual(payload.data["close"], 102)
        self.assertIsNone(payload.data["change"])
        self.assertEqual(payload.data["freshnessStatus"], "unverified")


if __name__ == "__main__":
    unittest.main()
