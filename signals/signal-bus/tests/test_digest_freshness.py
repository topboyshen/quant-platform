"""Digest pointer freshness: dated files stay, latest cannot impersonate today."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "digest_freshness.py"
spec = importlib.util.spec_from_file_location("digest_freshness", MODULE)
freshness_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freshness_mod)

NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class DigestFreshnessTests(unittest.TestCase):
    def test_same_local_date_is_current(self):
        mark = freshness_mod.freshness(
            {"target_date": "2026-09-16", "as_of": "2026-09-15",
             "produced_at": "2026-09-16T08:01:16+08:00"},
            NOW,
        )
        self.assertFalse(mark["stale"])
        self.assertIsNone(mark["reason"])
        self.assertEqual(mark["today"], "2026-09-16")

    def test_old_latest_file_is_stale_not_current(self):
        digest = {
            "target_date": "2026-09-08",
            "as_of": "2026-09-07",
            "produced_at": "2026-09-09T07:40:14.721427+08:00",
            "plain_text": "旧摘要",
        }
        marked = freshness_mod.attach_freshness(digest, NOW)
        self.assertTrue(marked["freshness"]["stale"])
        self.assertEqual(marked["freshness"]["reason"], "target_date_is_not_today")
        self.assertEqual(marked["plain_text"], "旧摘要")
        self.assertEqual(marked["target_date"], "2026-09-08")


if __name__ == "__main__":
    unittest.main()
