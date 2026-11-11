"""Compatibility and fail-closed validation of optional window staging proof."""
import copy
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from cninfo_staging import load_staging, write_staging

CST = dt.timezone(dt.timedelta(hours=8))
TARGET = "2026-10-09"
CUTOFF = dt.datetime(2026, 10, 9, 8, tzinfo=CST)
BATCH = dt.datetime(2026, 10, 9, 7, 56, tzinfo=CST)


def announcement(day="2026-10-08", observed="2026-10-09T07:55:00+08:00"):
    return {"title": "公告", "date": day, "dateKind": "source_disclosure_date",
            "observedAt": observed, "nextDayDisclosure": False,
            "url": f"https://static.cninfo.com.cn/finalpage/{day}/1.PDF"}


def window():
    return {"rows": [announcement()], "queryCoverage": {
        "kind": "window", "windowStartAt": "2026-10-08T00:00:00+08:00",
        "cutoffAt": CUTOFF.isoformat(), "queriedAt": "2026-10-09T07:55:00+08:00",
        "observedThroughAt": "2026-10-09T07:55:00+08:00", "pageSize": 30,
        "maxPages": 4, "pagesFetched": 1, "total": 1, "returned": 1,
        "complete": True, "reason": "all_query_pages_collected",
    }}


class WindowStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def roundtrip(self, query):
        entry = {"queriedAt": "2026-10-09T07:50:00+08:00",
                 "rows": [announcement("2026-08-26", "2026-10-09T07:49:00+08:00")]}
        if query is not None:
            entry["windowQuery"] = query
        write_staging(self.directory, TARGET, BATCH, {"002617": entry}, {}, "a" * 64)
        return load_staging(self.directory, TARGET, CUTOFF, expected_codes=["002617"])

    def test_old_schema2_has_no_invented_window_completeness(self):
        loaded = self.roundtrip(None)
        self.assertIsNotNone(loaded)
        self.assertNotIn("windowQuery", loaded["codes"]["002617"])

    def test_new_window_retains_older_pdf_discovery_and_distinct_times(self):
        loaded = self.roundtrip(window())
        entry = loaded["codes"]["002617"]
        self.assertEqual(entry["rows"][0]["date"], "2026-08-26")
        self.assertEqual(entry["queriedAt"].hour, 7)
        self.assertEqual(entry["queriedAt"].minute, 50)
        self.assertEqual(entry["windowQuery"]["queryCoverage"]["queriedAt"], "2026-10-09T07:55:00+08:00")
        self.assertTrue(entry["windowQuery"]["queryCoverage"]["complete"])

    def test_failure_empty_is_preserved_as_incomplete(self):
        query = window()
        query["rows"] = []
        query["queryCoverage"].update(queriedAt=None, observedThroughAt=None,
                                      returned=0, total=None, pagesFetched=0,
                                      complete=False, reason="query_failed")
        loaded = self.roundtrip(query)
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded["codes"]["002617"]["windowQuery"]["queryCoverage"]["complete"])

    def test_multi_page_completion_does_not_extend_early_page_coverage(self):
        query = window()
        later = announcement(observed="2026-10-09T07:56:00+08:00")
        later["url"] = "https://static.cninfo.com.cn/finalpage/2026-10-08/2.PDF"
        query["rows"].append(later)
        query["queryCoverage"].update(queriedAt="2026-10-09T07:56:00+08:00",
                                      observedThroughAt="2026-10-09T07:55:00+08:00",
                                      pageSize=1, pagesFetched=2, returned=2, total=2)
        loaded = self.roundtrip(query)
        self.assertIsNotNone(loaded)
        coverage = loaded["codes"]["002617"]["windowQuery"]["queryCoverage"]
        self.assertEqual(coverage["queriedAt"], "2026-10-09T07:56:00+08:00")
        self.assertEqual(coverage["observedThroughAt"], "2026-10-09T07:55:00+08:00")
        # A later completion cannot be assigned to the earlier row's visibility.
        query["queryCoverage"]["observedThroughAt"] = "2026-10-09T07:56:00+08:00"
        self.assertIsNone(self.roundtrip(query))

    def test_valid_hash_does_not_make_nonexistent_calendar_day_complete(self):
        query = window()
        query["queryCoverage"]["windowStartAt"] = "2026-09-25T00:00:00+08:00"
        query["rows"][0]["date"] = "2026-09-31"
        # roundtrip writes a fresh valid payload hash; the invalid date must
        # still be rejected by the new window proof validator.
        self.assertIsNone(self.roundtrip(query))

    def test_malformed_or_unproved_complete_queries_are_not_used(self):
        baseline = window()
        mutations = [
            lambda q: q["queryCoverage"].update(total=2),
            lambda q: q["queryCoverage"].update(returned=2),
            lambda q: q["queryCoverage"].update(reason="page_limit_reached"),
            lambda q: q["queryCoverage"].update(reason={}),
            lambda q: q["queryCoverage"].update(pagesFetched=0),
            lambda q: q["queryCoverage"].update(maxPages=5),
            lambda q: q["queryCoverage"].update(observedThroughAt="2026-10-09T07:56:00+08:00"),
            lambda q: q["queryCoverage"].update(queriedAt="2026-10-09T07:57:00+08:00",
                                               observedThroughAt="2026-10-09T07:57:00+08:00"),
            lambda q: q["queryCoverage"].update(cutoffAt="2026-10-10T08:00:00+08:00"),
            lambda q: q["rows"].__setitem__(0, announcement("2026-09-01")),
            lambda q: q["rows"].__setitem__(0, announcement(observed="2026-10-09T08:01:00+08:00")),
        ]
        for mutate in mutations:
            query = copy.deepcopy(baseline)
            mutate(query)
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                self.assertIsNone(self.roundtrip(query))


if __name__ == "__main__":
    unittest.main()
