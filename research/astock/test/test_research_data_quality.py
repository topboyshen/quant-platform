"""Coverage policy regressions; no collectors or external sends."""
import copy
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from research_data_quality import build_quality, official_window_assessment

CUTOFF = datetime.fromisoformat("2026-10-09T08:00:00+08:00")
PREVIOUS = date(2026, 10, 8)


def window():
    return {"status": "available", "queriedAt": "2026-10-09T07:55:00+08:00",
            "queryCoverage": {"kind": "window", "complete": True,
                "windowStartAt": "2026-10-08T00:00:00+08:00", "cutoffAt": CUTOFF.isoformat(),
                "queriedAt": "2026-10-09T07:55:00+08:00",
                "observedThroughAt": "2026-10-09T07:50:00+08:00",
                "pageSize": 30, "maxPages": 4, "pagesFetched": 1,
                "total": 0, "returned": 0, "reason": "all_query_pages_collected"}}


class ResearchQualityTests(unittest.TestCase):
    def test_complete_pagination_is_not_a_final_cutoff_or_atomic_claim(self):
        result = official_window_assessment(window(), CUTOFF, PREVIOUS)
        self.assertTrue(result["completeAsObserved"])
        self.assertFalse(result["finalCutoffCovered"])
        self.assertIn("not_atomic", result["scope"])
        self.assertEqual(result["observedThroughAt"], "2026-10-09T07:50:00+08:00")

    def test_failed_truncated_narrow_late_and_missing_proofs_stay_incomplete(self):
        for change in ({"complete": False}, {"reason": "page_limit"},
                       {"windowStartAt": "2026-10-09T00:00:00+08:00"},
                       {"queriedAt": "2026-10-09T08:01:00+08:00"},
                       {"observedThroughAt": None}, {"total": 1},
                       {"pagesFetched": True}, {"maxPages": 8, "pageSize": 30}):
            with self.subTest(change=change):
                payload = window()
                payload["queryCoverage"].update(change)
                self.assertFalse(official_window_assessment(payload, CUTOFF, PREVIOUS)["completeAsObserved"])
        self.assertFalse(official_window_assessment(window(), CUTOFF, None)["completeAsObserved"])
        self.assertFalse(official_window_assessment({}, CUTOFF, PREVIOUS)["completeAsObserved"])

    def test_credibility_and_policy_unavailable_do_not_fake_missing_source(self):
        current = SimpleNamespace(status="live", data={"symbol": ".IXIC", "sessionDate": "2026-10-08",
                                      "expectedSessionDate": "2026-10-08", "freshnessStatus": "current"})
        official = SimpleNamespace(status="live", data=window())
        args = dict(cutoff=CUTOFF, previous_day=PREVIOUS,
                    modules={"us_index_.IXIC": current, "cninfo_002617": official},
                    indices=[{"code": c, "turnover": 10} for c in ("sh000001", "sz399001", "sz399006")],
                    official_results=[official], stock_codes=["002617"],
                    market_score=100, financial_score=100, section_scores={"market": 100, "news": 100})
        result = build_quality(**args)
        self.assertTrue(result["declaredInputsComplete"])
        self.assertEqual(result["requiredFields"]["missing"], [])
        self.assertFalse(result["policyUnavailable"][0]["required"])
        self.assertFalse(result["sourceCredibility"]["mediaNews"]["affectsCoverageScore"])
        self.assertIn("not_full_research", result["scope"])
        args["section_scores"]["news"] = 50
        args["indices"][0]["turnover"] = None
        current.data["freshnessStatus"] = "stale"
        result = build_quality(**args)
        self.assertFalse(result["declaredInputsComplete"])
        self.assertEqual(result["freshness"]["status"], "unverified_or_stale")
        self.assertEqual(result["requiredFields"]["missing"], ["indices.sh000001.turnover"])


if __name__ == "__main__":
    unittest.main()
