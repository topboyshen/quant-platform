"""Python -> Node parity at microsecond precision for the official-window gate.

The Python provider compares instants as microsecond datetimes. The Node gate
(disclosure-evidence / research-data-quality) must reach the *same* verdict on
the same bytes, otherwise Python records ``completeAsObserved=false`` while
Node recomputes ``true`` and the binding check throws, rejecting the whole
morning context. No network, generation or delivery.
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
from akshare_provider import AkShareMorningProvider, ModulePayload  # noqa: E402
from test_akshare_provider import request_fixture, CN  # noqa: E402
from test_provider_quality_integration import CompleteInputs  # noqa: E402

CUTOFF = datetime(2026, 8, 2, 8, tzinfo=CN)
US = timedelta(microseconds=1)

NODE = (
    'import {validateMorningContext} from "./src/data.mjs";'
    'import {disclosureQueryState} from "./src/disclosure-evidence.mjs";'
    'let raw="";for await(const c of process.stdin)raw+=c;'
    'const r=JSON.parse(raw);const out={states:[],error:null};'
    'for(const s of r.data.watchlist){const x=disclosureQueryState(s,r.sources,r.meta.dataQuality.requestedCutoffAt,'
    '{previousDate:r.data.tradingCalendar.previousTradingDate});out.states.push(x.coverage??x.state)}'
    'try{validateMorningContext(r,{requestedAt:r.meta.dataQuality.requestedCutoffAt})}catch(e){out.error=e.message}'
    'console.log(JSON.stringify(out));'
)


class CrossLanguage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, queried, through=None, module_queried_shift=None):
        through = through or queried

        def window(code, *args, **kwargs):
            return {"rows": [], "queryCoverage": {
                "kind": "window", "windowStartAt": "2026-07-31T00:00:00+08:00",
                "cutoffAt": CUTOFF.isoformat(), "queriedAt": queried.isoformat(),
                "observedThroughAt": through.isoformat(), "pageSize": 30, "maxPages": 4,
                "pagesFetched": 1, "total": 0, "returned": 0,
                "complete": True, "reason": "all_query_pages_collected"}}

        provider = AkShareMorningProvider(
            CompleteInputs(), cache_dir=self.tmp.name,
            clock=lambda: CUTOFF - timedelta(minutes=1), isolate_media_loaders=False,
            collect_documents=False, cninfo_fetcher=lambda code: [],
            cninfo_window_fetcher=window, staging_dir=Path(self.tmp.name) / "staging")
        if module_queried_shift is not None:
            original = provider._official_announcements

            def shifted(*args, **kwargs):
                result = original(*args, **kwargs)
                data = dict(result.data)
                moved = (datetime.fromisoformat(data["queriedAt"]) + module_queried_shift).isoformat()
                data["queriedAt"] = moved
                sources = [dict(s, dataAsOf=moved) if s.get("sourceType") == "official_disclosure_query" else s
                           for s in result.sources]
                return ModulePayload(data, sources, result.warnings)
            provider._official_announcements = shifted
        return provider.get_morning_context(request_fixture())

    def node(self, response):
        result = subprocess.run(["node", "--input-type=module", "-e", NODE], cwd=ROOT,
                                input=json.dumps(response), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_parity(self, response):
        python = [i["completeAsObserved"] for i in response["meta"]["dataQuality"]["officialWindowCoverage"]]
        node = self.node(response)
        self.assertIsNone(node["error"], f"Node rejected a Python-consistent context: {node['error']}")
        self.assertEqual([s == "declared_complete" for s in node["states"]], python,
                         f"python={python} node={node['states']}")
        return python

    # (a) query one microsecond after the cutoff
    def test_a_query_one_microsecond_after_cutoff(self):
        self.assertEqual(self.assert_parity(self.build(CUTOFF + US)), [False] * 3)

    # (b) observedThroughAt one microsecond later than queriedAt
    def test_b_observed_through_one_microsecond_after_queried(self):
        queried = CUTOFF - timedelta(minutes=5, microseconds=10)
        self.assertEqual(self.assert_parity(self.build(queried, through=queried + US)), [False] * 3)

    # (c) module-level queriedAt and coverage.queriedAt differ by one microsecond
    def test_c_module_and_coverage_query_time_differ_by_one_microsecond(self):
        queried = CUTOFF - timedelta(minutes=5, microseconds=10)
        for shift in (US, -US):
            with self.subTest(shift=shift):
                self.assertEqual(self.assert_parity(self.build(queried, module_queried_shift=shift)), [False] * 3)

    # boundaries that must stay complete in BOTH languages (no loosening / no tightening)
    def test_equal_and_one_microsecond_before_cutoff_stay_complete_in_both(self):
        for queried in (CUTOFF, CUTOFF - US, CUTOFF - timedelta(minutes=5, microseconds=10)):
            with self.subTest(queried=queried.isoformat()):
                self.assertEqual(self.assert_parity(self.build(queried)), [True] * 3)

    def test_through_equal_or_one_microsecond_earlier_stays_complete_in_both(self):
        queried = CUTOFF - timedelta(minutes=5, microseconds=10)
        for through in (queried, queried - US):
            with self.subTest(through=through.isoformat()):
                self.assertEqual(self.assert_parity(self.build(queried, through=through)), [True] * 3)


if __name__ == "__main__":
    unittest.main()
