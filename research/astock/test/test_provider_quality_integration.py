"""Real provider + fake sources; no external HTTP, generation or delivery."""
import copy
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from akshare_provider import AkShareMorningProvider
from test_akshare_provider import FakeAkShare, request_fixture, CN
from jsonschema import Draft202012Validator, FormatChecker


class CompleteInputs(FakeAkShare):
    def stock_zh_index_daily(self, symbol):
        return [{**row, "amount": 100_000_000} for row in super().stock_zh_index_daily(symbol)]


class QualityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cutoff = datetime(2026, 8, 2, 8, tzinfo=CN)

    def window(self, code, **kwargs):
        observed = (self.cutoff - timedelta(minutes=1)).isoformat()
        return {"rows": [], "queryCoverage": {
            "kind": "window", "windowStartAt": "2026-07-31T00:00:00+08:00",
            "cutoffAt": self.cutoff.isoformat(), "queriedAt": observed,
            "observedThroughAt": observed, "pageSize": 30, "maxPages": 4,
            "pagesFetched": 1, "total": 0, "returned": 0,
            "complete": True, "reason": "all_query_pages_collected"}}

    def provider(self, api=None, fetcher=None, history=None):
        return AkShareMorningProvider(api or CompleteInputs(), cache_dir=self.tmp.name,
            clock=lambda: self.cutoff - timedelta(minutes=1), isolate_media_loaders=False,
            collect_documents=False, cninfo_fetcher=history or (lambda code: []),
            cninfo_window_fetcher=fetcher or self.window,
            staging_dir=Path(self.tmp.name) / "staging")

    def test_complete_declared_scope_can_be_live_without_inventing_northbound(self):
        response = self.provider().get_morning_context(request_fixture())
        self.assertEqual(response["meta"]["status"], "live")
        self.assertTrue(response["meta"]["dataQuality"]["declaredInputsComplete"])
        self.assertTrue(all(v == 100 for v in response["data"]["sectionCompleteness"].values()))
        self.assertEqual(response["data"]["capitalFlows"][0]["status"], "unavailable")
        self.assertEqual(response["data"]["capitalFlows"][0]["availabilityReason"], "not_disclosed")
        self.assertEqual(response["meta"]["dataQuality"]["sourceCredibility"]["mediaNews"]["kind"], "media_not_official")

    def test_provider_json_passes_existing_schema_and_new_renderer_contract(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "mcp" / "contract.schema.json").read_text())
        for api in (CompleteInputs(), FakeAkShare()):
            with self.subTest(api=type(api).__name__):
                response = self.provider(api).get_morning_context(request_fixture())
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(response)
                result = subprocess.run(["node", "--input-type=module", "-e",
                    'import {validateMorningContext} from "./src/data.mjs"; '
                    'let raw=""; for await(const chunk of process.stdin) raw+=chunk; '
                    'validateMorningContext(JSON.parse(raw));'],
                    cwd=root, input=json.dumps(response), text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_index_amount_keeps_partial(self):
        response = self.provider(FakeAkShare()).get_morning_context(request_fixture())
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertEqual(len(response["meta"]["dataQuality"]["requiredFields"]["missing"]), 3)

    def test_failed_window_is_partial_even_when_historic_official_rows_available(self):
        def failed(*args, **kwargs):
            result = self.window(*args, **kwargs)
            result["queryCoverage"].update(complete=False, reason="query_failed")
            return result
        response = self.provider(fetcher=failed).get_morning_context(request_fixture())
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertTrue(all(v["status"] == "incomplete" for v in response["meta"]["dataQuality"]["officialWindowCoverage"]))

    def test_stale_nasdaq_retains_date_but_not_live_credit(self):
        class Stale(CompleteInputs):
            def index_us_stock_sina(self, symbol):
                rows = super().index_us_stock_sina(symbol)
                if symbol == ".IXIC":
                    return [{**rows[0], "date": "2026-07-29"}, rows[0]]
                return rows
        response = self.provider(Stale()).get_morning_context(request_fixture())
        self.assertEqual(response["meta"]["status"], "partial")
        nasdaq = next(x for x in response["data"]["overnightGlobal"] if x["symbol"] == ".IXIC")
        self.assertEqual(nasdaq["sessionDate"], "2026-07-30")
        self.assertEqual(nasdaq["freshnessStatus"], "stale")
        self.assertEqual(response["meta"]["dataQuality"]["sourceAvailability"]["us_index_.IXIC"], "stale")

    def test_invalid_window_row_cannot_leave_complete_proof(self):
        def bad(*args, **kwargs):
            result = self.window(*args, **kwargs)
            result["rows"] = [{"date": "2026-07-32", "observedAt": result["queryCoverage"]["queriedAt"]}]
            result["queryCoverage"].update(returned=1, total=1)
            return result
        response = self.provider(fetcher=bad).get_morning_context(request_fixture())
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertTrue(all(v["reason"] == "window_row_evidence_invalid" for v in response["meta"]["dataQuality"]["officialWindowCoverage"]))

    def test_early_page_observation_cannot_be_promoted_by_final_page_time(self):
        def inflated(*args, **kwargs):
            result = self.window(*args, **kwargs)
            result["rows"] = [{"date": "2026-07-31", "title": "公告",
                "observedAt": "2026-08-02T07:58:00+08:00",
                "url": "https://static.cninfo.com.cn/finalpage/2026-07-31/1.PDF"}]
            result["queryCoverage"].update(returned=1, total=1)
            return result
        response = self.provider(fetcher=inflated).get_morning_context(request_fixture())
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertFalse(response["meta"]["dataQuality"]["declaredInputsComplete"])

    def test_same_official_url_different_title_is_not_two_disclosures(self):
        url = "https://static.cninfo.com.cn/finalpage/2026-07-31/1.PDF"
        historical = {"date": "2026-07-31", "title": "旧标题", "url": url,
                      "observedAt": "2026-08-02T07:58:00+08:00"}
        def queried(*args, **kwargs):
            result = self.window(*args, **kwargs)
            result["rows"] = [{**historical, "title": "当前官方标题", "observedAt": result["queryCoverage"]["queriedAt"]}]
            result["queryCoverage"].update(returned=1, total=1)
            return result
        result = self.provider(fetcher=queried, history=lambda code: [historical])._official_announcements(
            {"code": "002617", "name": "露笑科技"}, self.cutoff, datetime(2026, 7, 31).date())
        self.assertEqual(len(result.data["items"]), 1)
        self.assertEqual(result.data["items"][0]["title"], "当前官方标题")


if __name__ == "__main__":
    unittest.main()
