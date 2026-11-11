"""Cross-day settlement must be honest; frozen/holiday schedules must not invent runs."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import phase2 as p2
import test_phase2

TZ = ZoneInfo("Asia/Shanghai")


def Base():
    # Reuse the existing fixtures without re-collecting their tests.
    return test_phase2.Phase2Tests("test_calendar_and_before_close_time_travel_gate")


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.h = Base()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        r = p2.run_producer(self.h.db, self.h.proto, datetime(2026, 8, 13, 15, 21, tzinfo=TZ), self.h.fetch())
        self.assertEqual(r["status"], "SEALED")  # target session 2026-08-14

    def later_fetch(self):
        """Source rows continue past the target session (settling a day late)."""
        base = self.h.fetch(day="2026-08-14")
        def f(url):
            raw = json.loads(base(url))
            rows = raw["result"]["data"]
            rows.append({"day": "2026-08-17", "close": rows[-1]["close"], "volume": rows[-1]["volume"]})
            return json.dumps(raw)
        return f

    def test_cross_day_settlement_uses_target_row(self):
        s = p2.run_settler(self.h.db, self.h.proto, datetime(2026, 8, 17, 15, 41, tzinfo=TZ), self.later_fetch())
        self.assertEqual(s["pending_review_created"], 30)
        self.assertEqual(s["blocked"], 0)
        self.assertEqual(s["status"], "OK")

    def test_all_blocked_is_not_ok(self):
        # Source only reaches the cutoff day: target not final for any forecast.
        s = p2.run_settler(self.h.db, self.h.proto, datetime(2026, 8, 14, 15, 41, tzinfo=TZ),
                           self.h.fetch(day="2026-08-13"))
        self.assertEqual(s["pending_review_created"], 0)
        self.assertEqual(s["blocked"], 30)
        self.assertEqual(s["status"], "BLOCKED")
        self.assertEqual(s["blocked_reasons"], {"TARGET_SESSION_NOT_FINAL": 30})

    def test_partial_is_not_ok(self):
        s = p2.run_settler(self.h.db, self.h.proto, datetime(2026, 8, 14, 15, 41, tzinfo=TZ),
                           self.partial_fetch())
        self.assertEqual(s["status"], "PARTIAL")
        self.assertGreater(s["pending_review_created"], 0)
        self.assertGreater(s["blocked"], 0)

    def partial_fetch(self):
        good, bad = self.h.fetch(day="2026-08-14"), self.h.fetch(day="2026-08-13")
        def f(url):
            return (good if "sh6" in url else bad)(url)
        return f


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.h = Base()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.mode = Path(self.h.t.name) / "runtime-health.mode"

    def board(self, now, mode):
        self.mode.write_text(mode + "\n")
        return p2.dashboard(self.h.db, self.h.proto, now=now, mode_path=self.mode)

    def test_frozen_shows_no_next_run(self):
        x = self.board(datetime(2026, 8, 13, 10, 0, tzinfo=TZ), "FROZEN")
        self.assertIsNone(x["next_producer_run"])
        self.assertIsNone(x["next_settler_run"])
        self.assertEqual(x["schedule_state"], "FROZEN_NO_AUTOMATIC_RUN")

    def test_unreadable_mode_does_not_claim_a_schedule(self):
        x = p2.dashboard(self.h.db, self.h.proto, now=datetime(2026, 8, 13, 10, 0, tzinfo=TZ),
                         mode_path=Path(self.h.t.name) / "missing.mode")
        self.assertIsNone(x["next_producer_run"])
        self.assertEqual(x["schedule_state"], "MODE_UNKNOWN")

    def test_holiday_morning_points_to_next_trading_session(self):
        x = self.board(datetime(2026, 10, 4, 10, 0, tzinfo=TZ), "ACTIVE")
        self.assertEqual(x["next_producer_run"], "2026-10-08T15:20:00+08:00")
        self.assertEqual(x["next_settler_run"], "2026-10-08T15:40:00+08:00")
        self.assertEqual(x["schedule_state"], "SCHEDULED")

    def test_trading_day_morning_is_today(self):
        x = self.board(datetime(2026, 8, 13, 10, 0, tzinfo=TZ), "ACTIVE")
        self.assertEqual(x["next_producer_run"], "2026-08-13T15:20:00+08:00")


if __name__ == "__main__":
    unittest.main()
