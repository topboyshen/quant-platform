"""Exercise the real cache fallback, never collectors or network sends."""

from __future__ import annotations

import copy
import math
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from akshare_provider import AkShareMorningProvider, ModulePayload, RunState  # noqa: E402
from us_equity_calendar import validate_us_index_return  # noqa: E402


CUTOFF = datetime.fromisoformat("2026-10-09T08:00:00+08:00")


def current_row():
    return {
        "symbol": ".IXIC", "marketSymbol": ".IXIC",
        "marketCalendar": "us_equity_regular_v1",
        "sessionDate": "2026-10-08", "expectedSessionDate": "2026-10-08",
        "dataAsOf": "2026-10-09T04:00:00+08:00", "freshnessStatus": "current",
        "open": 101, "high": 104, "low": 100, "close": 102,
        "previousCloseDate": "2026-10-07", "previousClose": 100,
        "change": 2, "changePercent": 2,
    }


class UsIndexCacheIntegrityTests(unittest.TestCase):
    def fallback(self, row, cutoff=CUTOFF):
        with tempfile.TemporaryDirectory() as cache:
            provider = AkShareMorningProvider(api=object(), cache_dir=cache, collect_documents=False)
            key = f"us-session-v1|{cutoff.isoformat()}"
            provider._write_cache("us_index_.IXIC", key, ModulePayload(copy.deepcopy(row), []))
            cache_path = provider._cache_path("us_index_.IXIC")
            before = (cache_path.read_bytes(), cache_path.stat().st_mtime_ns)
            state = RunState()

            def unavailable():
                raise TimeoutError("synthetic source timeout")

            result = provider._module(state, "us_index_.IXIC", key, unavailable, None)
            after = (cache_path.read_bytes(), cache_path.stat().st_mtime_ns)
            self.assertEqual(after, before, "validation must not rewrite cached evidence")
            return result, state

    def assert_rejected(self, row, cutoff=CUTOFF):
        result, state = self.fallback(row, cutoff)
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.score, 0)
        self.assertEqual(result.data["freshnessStatus"], "unverified")
        self.assertEqual(state.live_modules, 0)
        self.assertTrue(any("完整价格/紧邻前收证明" in warning for warning in state.warnings))

    def test_complete_valid_same_session_cache_can_supply_fallback_not_live(self):
        result, state = self.fallback(current_row())
        self.assertEqual(result.status, "cache")
        self.assertEqual(result.score, 50)
        self.assertEqual(result.data["previousClose"], 100)
        self.assertEqual(state.live_modules, 0)

    def test_another_supported_index_cannot_satisfy_nasdaq_cache_request(self):
        for changes in (
            {"symbol": ".DJI", "marketSymbol": ".DJI"},
            {"symbol": ".INX"},
            {"marketSymbol": ".INX"},
            {"symbol": None},
            {"marketSymbol": None},
        ):
            with self.subTest(changes=changes):
                row = current_row()
                row.update(changes)
                self.assert_rejected(row)

    def test_green_flag_cannot_hide_missing_previous_close_or_return_fields(self):
        for field in ("previousClose", "change", "changePercent"):
            for missing in (None, "missing"):
                with self.subTest(field=field, missing=missing):
                    row = current_row()
                    if missing == "missing":
                        row.pop(field)
                    else:
                        row[field] = missing
                    self.assert_rejected(row)

    def test_unverified_cache_with_correct_close_time_cannot_get_fifty_points(self):
        row = current_row()
        row.update(freshnessStatus="unverified", previousClose=None, change=None, changePercent=None)
        self.assert_rejected(row)

    def test_wrong_or_missing_previous_session_date_is_rejected(self):
        for value in (None, "2026-10-06", "2026-10-08", "20261007"):
            with self.subTest(value=value):
                row = current_row()
                row["previousCloseDate"] = value
                self.assert_rejected(row)

    def test_wrong_daily_return_arithmetic_is_rejected(self):
        for field, value in (("change", 3), ("changePercent", 3), ("previousClose", 99)):
            with self.subTest(field=field):
                row = current_row()
                row[field] = value
                self.assert_rejected(row)

    def test_nonfinite_nonpositive_or_boolean_ohlc_and_previous_close_are_rejected(self):
        for field in ("open", "high", "low", "close", "previousClose"):
            for value in (None, 0, -1, math.nan, math.inf, True, "102", 10 ** 1000):
                with self.subTest(field=field, value_type=type(value).__name__):
                    row = current_row()
                    row[field] = value
                    self.assert_rejected(row)

    def test_inconsistent_ohlc_bounds_are_rejected(self):
        for changes in ({"high": 101}, {"low": 102}, {"open": 105}):
            with self.subTest(changes=changes):
                row = current_row()
                row.update(changes)
                self.assert_rejected(row)

    def test_boolean_or_nonfinite_return_fields_are_rejected(self):
        for field in ("change", "changePercent"):
            for value in (True, math.nan, math.inf, "2"):
                with self.subTest(field=field, value_type=type(value).__name__):
                    row = current_row()
                    row[field] = value
                    self.assert_rejected(row)

    def test_wrong_session_fake_calendar_or_timestamp_remains_fail_closed(self):
        for changes in (
            {"marketCalendar": "made_up_calendar"},
            {"sessionDate": "2026-10-07", "dataAsOf": "2026-10-08T04:00:00+08:00"},
            {"dataAsOf": "2026-10-09T05:00:00+08:00"},
        ):
            with self.subTest(changes=changes):
                row = current_row()
                row.update(changes)
                self.assert_rejected(row)

    def test_weekend_holiday_gap_and_dst_previous_session_are_valid(self):
        for session, previous, as_of, cutoff in (
            ("2026-09-08", "2026-09-04", "2026-09-09T04:00:00+08:00", "2026-09-09T08:00:00+08:00"),
            ("2026-11-27", "2026-11-25", "2026-11-28T02:00:00+08:00", "2026-11-28T08:00:00+08:00"),
            ("2026-03-09", "2026-03-06", "2026-03-10T04:00:00+08:00", "2026-03-10T08:00:00+08:00"),
            ("2026-11-02", "2026-10-30", "2026-11-03T05:00:00+08:00", "2026-11-03T08:00:00+08:00"),
        ):
            with self.subTest(session=session):
                row = current_row()
                row.update(sessionDate=session, previousCloseDate=previous, dataAsOf=as_of)
                result, _ = self.fallback(row, datetime.fromisoformat(cutoff))
                self.assertEqual(result.status, "cache")
                self.assertEqual(result.score, 50)

    def test_negative_and_zero_daily_returns_are_valid(self):
        for close, previous, change, percent in ((100, 102, -2, -2 / 102 * 100), (100, 100, 0, 0)):
            with self.subTest(change=change):
                row = current_row()
                row.update(open=99, high=104, low=98, close=close, previousClose=previous,
                           change=change, changePercent=percent)
                result, _ = self.fallback(row)
                self.assertEqual(result.status, "cache")
                self.assertEqual(result.score, 50)
                validate_us_index_return(row)


if __name__ == "__main__":
    unittest.main()
