"""Regression: observation instants keep their microseconds end to end.

Production 2026-10-12: the CNINFO pre-collection recorded the window query at
07:50:02.926549, the provider re-serialised the module ``queriedAt`` through a
whole-second formatter (07:50:02), so the quality gate saw
``coverage.queriedAt != payload.queriedAt`` and ``observedThroughAt > queriedAt``
and declared three complete queries "incomplete". No network, no delivery.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from akshare_provider import AkShareMorningProvider  # noqa: E402
import akshare_provider  # noqa: E402
from test_akshare_provider import request_fixture, CN  # noqa: E402
from test_provider_quality_integration import CompleteInputs  # noqa: E402

CUTOFF = datetime(2026, 8, 2, 8, tzinfo=CN)


class InstantIso(unittest.TestCase):
    def setUp(self):
        self._instant_iso = akshare_provider._instant_iso
    def test_keeps_microseconds_and_offset(self):
        value = datetime(2026, 10, 12, 7, 50, 2, 926549, tzinfo=CN)
        self.assertEqual(self._instant_iso(value), "2026-10-12T07:50:02.926549+08:00")
        self.assertEqual(datetime.fromisoformat(self._instant_iso(value)), value)

    def test_whole_second_has_no_fraction_and_other_zone_is_converted(self):
        from datetime import timezone
        value = datetime(2026, 10, 11, 23, 50, 2, 5, tzinfo=timezone.utc)
        self.assertEqual(self._instant_iso(value), "2026-10-12T07:50:02.000005+08:00")
        self.assertEqual(self._instant_iso(datetime(2026, 10, 12, 7, 50, 2, tzinfo=CN)),
                         "2026-10-12T07:50:02+08:00")

    def test_naive_is_taken_as_china_time(self):
        self.assertEqual(self._instant_iso(datetime(2026, 10, 12, 7, 50, 2, 1)),
                         "2026-10-12T07:50:02.000001+08:00")


class MicrosecondCoverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def window(self, queried, through=None):
        through = through or queried
        def fetch(code, *args, **kwargs):
            return {"rows": [], "queryCoverage": {
                "kind": "window", "windowStartAt": "2026-07-31T00:00:00+08:00",
                "cutoffAt": CUTOFF.isoformat(), "queriedAt": queried.isoformat(),
                "observedThroughAt": through.isoformat(), "pageSize": 30, "maxPages": 4,
                "pagesFetched": 1, "total": 0, "returned": 0,
                "complete": True, "reason": "all_query_pages_collected"}}
        return fetch

    def response(self, queried, through=None):
        provider = AkShareMorningProvider(
            CompleteInputs(), cache_dir=self.tmp.name,
            clock=lambda: CUTOFF - timedelta(minutes=1), isolate_media_loaders=False,
            collect_documents=False, cninfo_fetcher=lambda code: [],
            cninfo_window_fetcher=self.window(queried, through),
            staging_dir=Path(self.tmp.name) / "staging")
        return provider.get_morning_context(request_fixture())

    def coverage(self, response):
        return response["meta"]["dataQuality"]["officialWindowCoverage"]

    def assert_all(self, response, complete):
        self.assertTrue(self.coverage(response))
        for item in self.coverage(response):
            self.assertEqual(item["completeAsObserved"], complete, item)

    def test_microsecond_query_time_is_preserved_and_complete(self):
        queried = CUTOFF - timedelta(minutes=9, seconds=57, microseconds=73451)
        self.assertNotEqual(queried.microsecond, 0)
        response = self.response(queried)
        self.assert_all(response, True)
        for item in self.coverage(response):
            self.assertEqual(item["queriedAt"], queried.isoformat())
            self.assertEqual(item["observedThroughAt"], queried.isoformat())
        self.assertEqual(response["meta"]["status"], "live")

    def test_query_time_equal_to_observed_through_is_complete(self):
        at = CUTOFF - timedelta(minutes=5, microseconds=1)
        self.assert_all(self.response(at, through=at), True)

    def test_observed_through_earlier_than_query_time_is_complete(self):
        queried = CUTOFF - timedelta(minutes=5, microseconds=10)
        through = queried - timedelta(microseconds=1)
        self.assert_all(self.response(queried, through=through), True)

    def test_observed_through_later_than_query_time_is_incomplete(self):
        queried = CUTOFF - timedelta(minutes=5, microseconds=10)
        through = queried + timedelta(microseconds=1)
        self.assert_all(self.response(queried, through=through), False)

    def test_query_time_exactly_at_cutoff_is_boundary_complete_and_covers_cutoff(self):
        response = self.response(CUTOFF)
        self.assert_all(response, True)
        self.assertTrue(all(i["finalCutoffCovered"] for i in self.coverage(response)))

    def test_query_time_one_microsecond_after_cutoff_is_incomplete(self):
        self.assert_all(self.response(CUTOFF + timedelta(microseconds=1)), False)

    def test_query_time_one_microsecond_before_cutoff_does_not_claim_cutoff_covered(self):
        response = self.response(CUTOFF - timedelta(microseconds=1))
        self.assert_all(response, True)
        self.assertFalse(any(i["finalCutoffCovered"] for i in self.coverage(response)))

    def test_node_gate_accepts_the_same_microsecond_evidence(self):
        queried = CUTOFF - timedelta(minutes=9, seconds=57, microseconds=73451)
        response = self.response(queried)
        script = (
            'import {disclosureQueryState} from "./src/disclosure-evidence.mjs";'
            'let raw="";for await(const c of process.stdin)raw+=c;'
            'const r=JSON.parse(raw);const out=[];'
            'for(const s of r.data.watchlist){out.push(disclosureQueryState(s,r.sources,'
            '"2026-08-02T08:00:00+08:00",{previousDate:r.data.tradingCalendar.previousTradingDate}).coverage)}'
            'console.log(JSON.stringify(out));')
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                input=json.dumps(response), text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(c == "declared_complete" for c in json.loads(result.stdout)), result.stdout)


if __name__ == "__main__":
    unittest.main()
