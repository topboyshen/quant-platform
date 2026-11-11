"""Coverage gaps must survive every rounding layer; no collectors or sends."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from akshare_provider import (
    A_SHARE_INDEXES, FINANCIAL_REQUIRED_FIELD_GROUPS,
    AkShareMorningProvider, ModuleResult,
)


CUTOFF = datetime.fromisoformat("2026-10-09T08:00:00+08:00")
OBSERVED = "2026-10-09T07:55:00+08:00"
MARKET_AT = "2026-10-08T15:00:00+08:00"


def market_item(code, close_field="last"):
    return {
        "code": code, close_field: 11, "previousClose": 10, "change": 1,
        "changePercent": 10, "open": 10, "high": 12, "low": 9,
        "volume": 10, "turnover": 110, "dataAsOf": MARKET_AT,
        "sourceIds": ["fixture-market"],
    }


class NoCollectors:
    def __getattr__(self, name):
        raise AssertionError(f"collector must not execute: {name}")


class InjectedSourceProvider(AkShareMorningProvider):
    """Run the real context assembler with an explicit offline source layer."""

    def __init__(self, cache_dir, codes, *, missing_eps=False, missing_quote_amount=False):
        super().__init__(api=NoCollectors(), cache_dir=cache_dir,
                         clock=lambda: datetime.fromisoformat(OBSERVED),
                         collect_documents=False, isolate_media_loaders=False)
        self.codes = codes
        self.missing_eps = missing_eps
        self.missing_quote_amount = missing_quote_amount

    def _module(self, state, name, key, loader, default):
        # Never call loader, including the API, network, staging and cache paths.
        if name == "trading_calendar":
            data = {"status": "trading_day", "targetDate": "2026-10-09",
                    "previousTradingDate": "2026-10-08",
                    "previousSessionPreviousTradingDate": "2026-09-30"}
        elif name == "a_share_history":
            quotes = {code: market_item(code) for code in self.codes}
            if self.missing_quote_amount:
                quotes[self.codes[-1]]["turnover"] = None
            data = {"quotes": quotes, "dataAsOf": MARKET_AT,
                    "sourceIds": ["fixture-market"]}
        elif name == "a_share_indices_history":
            data = [market_item(code, "close") for code in A_SHARE_INDEXES]
        elif name == "a_share_market_turnover":
            data = {"totalTurnover": 100, "byExchange": {"SH": 50, "SZ": 50},
                    "dataAsOf": MARKET_AT, "sourceIds": ["fixture-market"]}
        elif name == "a_share_market_breadth":
            data = {"breadth": {"advancers": 3000, "decliners": 2000, "unchanged": 100},
                    "dataAsOf": MARKET_AT, "sourceIds": ["fixture-market"]}
        elif name == "sectors":
            data = []
        elif name == "capital_flows":
            data = {"entries": [], "dataAsOf": MARKET_AT, "sourceIds": ["fixture-market"]}
        elif name.startswith("us_index_"):
            data = {"symbol": name.removeprefix("us_index_"), "sessionDate": "2026-10-08",
                    "expectedSessionDate": "2026-10-08", "freshnessStatus": "current"}
        elif name.startswith("financial_"):
            data = {group[0]: 1 for group in FINANCIAL_REQUIRED_FIELD_GROUPS}
            data.update(reportDate="2026-06-30", noticeAt="2026-08-01T00:00:00+08:00",
                        dataAsOf="2026-08-01T00:00:00+08:00", sourceIds=["fixture-financial"])
            if self.missing_eps and name == f"financial_{self.codes[-1]}":
                data["eps"] = None
        elif name.startswith("cninfo_"):
            data = {"status": "available", "queriedAt": OBSERVED, "sourceIds": ["fixture-query"],
                    "items": [], "queryCoverage": {
                        "kind": "window", "complete": True, "reason": "all_query_pages_collected",
                        "windowStartAt": "2026-10-08T00:00:00+08:00", "cutoffAt": CUTOFF.isoformat(),
                        "queriedAt": OBSERVED, "observedThroughAt": OBSERVED,
                        "pageSize": 30, "maxPages": 4, "pagesFetched": 1, "total": 0, "returned": 0,
                    }}
        elif name == "global_news" or name.startswith("stock_news_"):
            data = []
        else:
            raise AssertionError(f"unexpected source: {name}")
        return ModuleResult(data, [], "live")

    def _media_module(self, state, name, key, loader, default, cutoff):
        return self._module(state, name, key, loader, default)


class CoverageRoundingTests(unittest.TestCase):
    def test_empty_and_complete_inputs_preserve_old_bounds(self):
        mean = AkShareMorningProvider._coverage_average
        self.assertEqual(mean([]), 0)
        self.assertEqual(mean([0]), 0)
        self.assertEqual(mean([0, 100]), 50)
        self.assertEqual(mean([100]), 100)
        self.assertEqual(mean([100] * 1000), 100)

    def test_one_missing_component_cannot_be_diluted_to_100(self):
        mean = AkShareMorningProvider._coverage_average
        for scores in ([100] * 16 + [92], [100] * 40 + [80],
                       [100] * 1000 + [0], [100, 99.999]):
            with self.subTest(length=len(scores), last=scores[-1]):
                self.assertEqual(mean(scores), 99)
        self.assertEqual(mean([100, 99]), 99)

    def test_bad_score_is_missing_not_dropped_or_upgraded(self):
        for value in (None, True, "100", float("nan"), float("inf"), -1, 101, 10 ** 400):
            with self.subTest(value=value):
                self.assertEqual(AkShareMorningProvider._coverage_average([100, value]), 50)

    def test_module_source_average_preserves_one_unavailable_source(self):
        sources = [ModuleResult([], [], "live") for _ in range(201)]
        sources[-1] = ModuleResult(None, [], "unavailable")
        self.assertEqual(AkShareMorningProvider._average(sources), 99)

    def test_missing_eps_survives_financial_watchlist_and_overall_means(self):
        full = {group[0]: 1 for group in FINANCIAL_REQUIRED_FIELD_GROUPS}
        partial = {**full, "eps": None}
        scores = [AkShareMorningProvider._financial_content_score(SimpleNamespace(data=full, score=100))] * 16
        scores.append(AkShareMorningProvider._financial_content_score(SimpleNamespace(data=partial, score=100)))
        financial = AkShareMorningProvider._coverage_average(scores)
        watchlist = AkShareMorningProvider._coverage_average([100, financial])
        overall = AkShareMorningProvider._coverage_average([100] * 7 + [watchlist])
        self.assertEqual((scores[-1], financial, watchlist, overall), (92, 99, 99, 99))

    def context(self, count, **gaps):
        codes = [f"{300001 + index:06d}" for index in range(count)]
        request = {"schemaVersion": "1.0", "task": "a_share_morning_context",
                   "targetDate": "2026-10-09", "timezone": "Asia/Shanghai",
                   "requestedAt": CUTOFF.isoformat(), "universe": [
                       {"code": code, "exchange": "SZ", "name": f"测试{code}"}
                       for code in codes
                   ]}
        with tempfile.TemporaryDirectory() as cache_dir:
            provider = InjectedSourceProvider(cache_dir, codes, **gaps)
            return provider.get_morning_context(request)

    def test_real_context_with_17_financial_items_stays_partial_if_one_eps_missing(self):
        response = self.context(17, missing_eps=True)
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertFalse(response["meta"]["isLive"])
        self.assertEqual(response["meta"]["dataCompleteness"]["financial"], 99)
        self.assertEqual(response["meta"]["dataCompleteness"]["overall"], 99)
        self.assertEqual(response["data"]["sectionCompleteness"]["watchlist"], 99)
        self.assertFalse(response["meta"]["dataQuality"]["declaredInputsComplete"])
        self.assertIsNone(response["data"]["watchlist"][-1]["financials"]["eps"])

    def test_real_context_large_quote_pool_cannot_hide_one_missing_amount(self):
        response = self.context(41, missing_quote_amount=True)
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertEqual(response["meta"]["dataCompleteness"]["market"], 99)
        self.assertEqual(response["data"]["sectionCompleteness"]["watchlist"], 99)
        self.assertEqual(response["meta"]["dataCompleteness"]["overall"], 99)
        self.assertFalse(response["meta"]["dataQuality"]["declaredInputsComplete"])

    def test_real_context_all_complete_still_reports_declared_complete(self):
        response = self.context(17)
        self.assertEqual(response["meta"]["status"], "live")
        self.assertTrue(response["meta"]["dataQuality"]["declaredInputsComplete"])
        self.assertTrue(all(value == 100 for value in response["meta"]["dataCompleteness"].values()))
        self.assertTrue(all(value == 100 for value in response["data"]["sectionCompleteness"].values()))


if __name__ == "__main__":
    unittest.main()
