"""Offline regressions for wrong-session US-index data, not collector reruns."""

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from akshare_provider import AkShareMorningProvider, RunState
from us_equity_calendar import latest_completed_us_session, us_equity_session_close, us_index_freshness


class RowsApi:
    def __init__(self, dates):
        self.dates = dates

    def index_us_stock_sina(self, symbol):
        return [{"date": day, "open": 99, "high": 102, "low": 98,
                 "close": 100 + index, "volume": 10}
                for index, day in enumerate(self.dates)]


class OfflineProvider(AkShareMorningProvider):
    def _call(self, name, *args, **kwargs):
        # No HTTP layer, optional requests dependency or external collectors:
        # these tests exercise date selection, score classification and cache.
        return getattr(self.api, name)(*args, **kwargs)


class UsIndexFreshnessTests(unittest.TestCase):
    def test_october_wrong_day_is_preserved_but_not_live(self):
        cutoff = datetime.fromisoformat("2026-10-09T08:00:00+08:00")
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(["2026-10-06", "2026-10-07"]), cache_dir=cache)
            payload = provider._us_index(".IXIC", cutoff)
            self.assertEqual(payload.data["sessionDate"], "2026-10-07")
            self.assertEqual(payload.data["expectedSessionDate"], "2026-10-08")
            self.assertEqual(payload.data["dataAsOf"], "2026-10-08T04:00:00+08:00")
            self.assertEqual(payload.data["freshnessStatus"], "stale")
            self.assertEqual(payload.sources[0]["freshnessStatus"], "stale")
            self.assertTrue(payload.warnings)
            result = provider._module(RunState(), "us_index_.IXIC", "fixture", lambda: payload, None)
            self.assertEqual(result.status, "stale")
            self.assertEqual(result.score, 0)

    def test_latest_session_is_current_and_future_row_excluded(self):
        cutoff = datetime.fromisoformat("2026-10-09T08:00:00+08:00")
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(["2026-10-07", "2026-10-08", "2026-10-09"]), cache_dir=cache)
            payload = provider._us_index(".INX", cutoff)
            self.assertEqual(payload.data["sessionDate"], "2026-10-08")
            self.assertEqual(payload.data["freshnessStatus"], "current")
            self.assertEqual(payload.warnings, [])

    def test_friday_is_current_over_weekend_and_labor_day(self):
        for cutoff in ("2026-09-07T08:00:00+08:00", "2026-09-08T08:00:00+08:00"):
            self.assertEqual(latest_completed_us_session(datetime.fromisoformat(cutoff)), date(2026, 9, 4))

    def test_good_friday_is_closed(self):
        self.assertEqual(latest_completed_us_session(datetime.fromisoformat("2026-04-04T08:00:00+08:00")), date(2026, 4, 2))

    def test_juneteenth_is_closed(self):
        self.assertEqual(latest_completed_us_session(datetime.fromisoformat("2026-06-20T08:00:00+08:00")), date(2026, 6, 18))

    def test_early_close_exact_boundary_and_before_close(self):
        self.assertEqual(latest_completed_us_session(datetime.fromisoformat("2026-11-27T12:59:59-05:00")), date(2026, 11, 25))
        self.assertEqual(latest_completed_us_session(datetime.fromisoformat("2026-11-27T13:00:00-05:00")), date(2026, 11, 27))
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(["2026-11-25", "2026-11-27"]), cache_dir=cache)
            payload = provider._us_index(".IXIC", datetime.fromisoformat("2026-11-27T13:00:00-05:00"))
            self.assertEqual(payload.data["dataAsOf"], "2026-11-28T02:00:00+08:00")

    def test_dst_changes_close_instant_not_session_identity(self):
        self.assertEqual(us_equity_session_close(date(2026, 3, 6)).isoformat(), "2026-03-06T16:00:00-05:00")
        self.assertEqual(us_equity_session_close(date(2026, 3, 9)).isoformat(), "2026-03-09T16:00:00-04:00")
        self.assertEqual(us_equity_session_close(date(2026, 10, 30)).isoformat(), "2026-10-30T16:00:00-04:00")
        self.assertEqual(us_equity_session_close(date(2026, 11, 2)).isoformat(), "2026-11-02T16:00:00-05:00")

    def test_leap_day_and_new_year_observance(self):
        self.assertEqual(us_equity_session_close(date(2028, 2, 29)).isoformat(), "2028-02-29T16:00:00-05:00")
        self.assertIsNotNone(us_equity_session_close(date(2027, 12, 31)))
        self.assertEqual(latest_completed_us_session(datetime.fromisoformat("2028-01-01T08:00:00+08:00")), date(2027, 12, 31))

    def test_missing_calendar_coverage_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            latest_completed_us_session(datetime.fromisoformat("2029-01-03T08:00:00+08:00"))

    def test_old_current_flag_is_rechecked_for_new_cutoff(self):
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(["2026-10-07", "2026-10-08"]), cache_dir=cache)
            payload = provider._us_index(".DJI", datetime.fromisoformat("2026-10-09T08:00:00+08:00"))
            self.assertEqual(payload.data["freshnessStatus"], "current")
            self.assertEqual(us_index_freshness(payload.data, datetime.fromisoformat("2026-10-10T08:00:00+08:00")), "stale")

    def test_non_session_or_timestamp_mismatch_is_rejected(self):
        cutoff = datetime.fromisoformat("2026-10-09T08:00:00+08:00")
        record = {"marketCalendar": "us_equity_regular_v1", "marketSymbol": ".IXIC",
                  "sessionDate": "2026-10-08", "dataAsOf": "2026-10-09T05:00:00+08:00"}
        with self.assertRaisesRegex(ValueError, "timestamp invalid"):
            us_index_freshness(record, cutoff)
        record.update(sessionDate="2026-10-04", dataAsOf="2026-10-05T04:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "timestamp invalid"):
            us_index_freshness(record, cutoff)

    def test_decades_of_history_do_not_require_ancient_calendar_scope(self):
        with tempfile.TemporaryDirectory() as cache:
            provider = OfflineProvider(RowsApi(["2006-01-03", "2026-10-07", "2026-10-08"]), cache_dir=cache)
            payload = provider._us_index(".IXIC", datetime.fromisoformat("2026-10-09T08:00:00+08:00"))
            self.assertEqual(payload.data["freshnessStatus"], "current")
            self.assertEqual(payload.data["sessionDate"], "2026-10-08")


if __name__ == "__main__":
    unittest.main()
