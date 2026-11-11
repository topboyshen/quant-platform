"""Bounded CNINFO window proof with fake responses; no collector/network runs."""
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from cninfo_source import CninfoClient, CninfoError

CST = dt.timezone(dt.timedelta(hours=8))
START = dt.datetime(2026, 10, 8, tzinfo=CST)
CUTOFF = dt.datetime(2026, 10, 9, 8, tzinfo=CST)
OBSERVED = dt.datetime(2026, 10, 9, 7, 50, tzinfo=CST)
CODE = "002617"
ORG = "org_002617"


def row(identity, *, day="2026-10-08", code=CODE):
    instant = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(0), CST)
    return {"secCode": code, "orgId": ORG, "announcementId": str(identity),
            "announcementTitle": f"公告 {identity}", "announcementTime": int(instant.timestamp() * 1000),
            "adjunctUrl": f"finalpage/{day}/{identity}.PDF"}


def page(rows, total):
    return {"announcements": rows, "totalAnnouncement": total}


class WindowQueryTests(unittest.TestCase):
    def setUp(self):
        self.client = CninfoClient.__new__(CninfoClient)
        self.client.org_ids = {CODE: ORG}
        self.request = mock.Mock()
        self.client.request_json = self.request

    def query(self, pages, **overrides):
        self.request.side_effect = pages
        with mock.patch("cninfo_source._now_cst", return_value=OBSERVED):
            return self.client.announcement_window(
                CODE, window_start=START, cutoff=CUTOFF, page_size=2,
                max_pages=4, **overrides)

    def test_all_pages_have_explicit_window_and_preserve_actual_observation(self):
        result = self.query([page([row(1), row(2)], 3), page([row(3)], 3)])
        coverage = result["queryCoverage"]
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["returned"], 3)
        self.assertEqual(coverage["pagesFetched"], 2)
        self.assertEqual(coverage["queriedAt"], OBSERVED.isoformat())
        self.assertEqual(coverage["observedThroughAt"], OBSERVED.isoformat())
        self.assertNotEqual(coverage["observedThroughAt"], coverage["cutoffAt"])
        self.assertEqual([call.kwargs["data"]["pageNum"] for call in self.request.call_args_list], ["1", "2"])
        self.assertTrue(all(call.kwargs["data"]["seDate"] == "2026-10-08~2026-10-09"
                            for call in self.request.call_args_list))
        self.assertTrue(all(call.kwargs["not_after"] == CUTOFF for call in self.request.call_args_list))

    def test_successful_empty_query_is_complete_only_as_of_actual_query(self):
        result = self.query([page(None, 0)])
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["queriedAt"], OBSERVED.isoformat())

    def test_multiple_pages_do_not_promote_earlier_page_to_later_visibility(self):
        last_observation = OBSERVED + dt.timedelta(minutes=1)
        self.request.side_effect = [page([row(1), row(2)], 3), page([row(3)], 3)]
        # Each page checks the cutoff before discovery and request, then records
        # its real completion. Page two is not an atomic refresh of page one.
        with mock.patch("cninfo_source._now_cst", side_effect=[
                OBSERVED, OBSERVED, OBSERVED,
                last_observation, last_observation, last_observation]):
            result = self.client.announcement_window(
                CODE, window_start=START, cutoff=CUTOFF, page_size=2, max_pages=4)
        coverage = result["queryCoverage"]
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["queriedAt"], last_observation.isoformat())
        self.assertEqual(coverage["observedThroughAt"], OBSERVED.isoformat())
        self.assertEqual([item["observedAt"] for item in result["rows"]],
                         [OBSERVED.isoformat(), OBSERVED.isoformat(), last_observation.isoformat()])

    def test_failure_empty_never_proves_absence(self):
        result = self.query([CninfoError("announcements", "HTTPStatus", CODE, http_status=503)])
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["reason"], "query_failed")

    def test_failed_later_page_keeps_partial_rows_without_completeness(self):
        result = self.query([page([row(1), row(2)], 3), CninfoError("announcements", "Timeout", CODE)])
        self.assertEqual(len(result["rows"]), 2)
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["reason"], "query_failed")

    def test_page_cap_never_silently_calls_truncated_results_complete(self):
        with mock.patch("cninfo_source._now_cst", return_value=OBSERVED):
            self.request.side_effect = [page([row(1), row(2)], 5)]
            result = self.client.announcement_window(
                CODE, window_start=START, cutoff=CUTOFF, page_size=2, max_pages=1)
        self.assertEqual(result["queryCoverage"]["reason"], "page_limit_reached")
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(self.request.call_count, 1)

    def test_inconsistent_total_does_not_certify_query(self):
        result = self.query([page([row(1), row(2)], 4), page([row(3)], 3)])
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["reason"], "pagination_changed")
        self.assertEqual(len(result["rows"]), 2)

    def test_duplicate_between_pages_is_not_complete(self):
        repeated = row(2)
        repeated["announcementTitle"] = "同一公告在分页期间变更了标题"
        result = self.query([page([row(1), row(2)], 3), page([repeated], 3)])
        self.assertEqual(result["queryCoverage"]["reason"], "pagination_duplicate")
        self.assertFalse(result["queryCoverage"]["complete"])

    def test_ignored_date_filter_is_not_complete(self):
        result = self.query([page([row(1, day="2026-09-01")], 1)])
        self.assertEqual(result["queryCoverage"]["reason"], "window_filter_mismatch")
        self.assertEqual(result["rows"], [])

    def test_short_page_does_not_prove_empty_or_complete(self):
        result = self.query([page([row(1)], 3)])
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["reason"], "query_failed")

    def test_security_mismatch_remains_failed_closed(self):
        result = self.query([page([row(1, code="600875")], 1)])
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["queryCoverage"]["complete"])

    def test_no_request_is_started_after_cutoff(self):
        with mock.patch("cninfo_source._now_cst", return_value=CUTOFF + dt.timedelta(seconds=1)):
            result = self.client.announcement_window(CODE, window_start=START, cutoff=CUTOFF)
        self.request.assert_not_called()
        self.assertEqual(result["queryCoverage"]["reason"], "cutoff_reached")
        self.assertFalse(result["queryCoverage"]["complete"])

    def test_late_response_is_discarded_not_backdated(self):
        self.request.return_value = page([row(1)], 1)
        with mock.patch("cninfo_source._now_cst", side_effect=[OBSERVED, OBSERVED, CUTOFF + dt.timedelta(seconds=1)]):
            result = self.client.announcement_window(CODE, window_start=START, cutoff=CUTOFF)
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["reason"], "observed_after_cutoff")

    def test_next_page_does_not_start_when_cutoff_passes(self):
        self.request.return_value = page([row(1), row(2)], 3)
        with mock.patch("cninfo_source._now_cst", side_effect=[OBSERVED, OBSERVED, OBSERVED, CUTOFF + dt.timedelta(seconds=1)]):
            result = self.client.announcement_window(CODE, window_start=START, cutoff=CUTOFF, page_size=2)
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(len(result["rows"]), 2)
        self.assertFalse(result["queryCoverage"]["complete"])

    def test_transport_retry_does_not_start_another_request_after_cutoff(self):
        response = mock.Mock(status_code=503)
        self.client.session = mock.Mock()
        self.client.session.request.return_value = response
        with mock.patch("cninfo_source._now_cst", side_effect=[OBSERVED, CUTOFF + dt.timedelta(seconds=1)]), \
                mock.patch("cninfo_source.time.sleep"), self.assertRaises(CninfoError) as raised:
            CninfoClient.request_json(self.client, "POST", "https://www.cninfo.com.cn/query",
                                      "announcements", CODE, not_after=CUTOFF)
        self.assertEqual(raised.exception.diagnostic["errorType"], "CutoffReached")
        self.assertEqual(self.client.session.request.call_count, 1)
        response.close.assert_called_once()

    def test_transport_retry_stops_when_monotonic_budget_expires(self):
        response = mock.Mock(status_code=503)
        self.client.session = mock.Mock()
        self.client.session.request.return_value = response
        with mock.patch("cninfo_source._now_cst", return_value=OBSERVED), \
                mock.patch("cninfo_source.time.monotonic", side_effect=[100.0, 103.0]), \
                mock.patch("cninfo_source.time.sleep"), self.assertRaises(CninfoError) as raised:
            CninfoClient.request_json(self.client, "POST", "https://www.cninfo.com.cn/query",
                                      "announcements", CODE, not_after=CUTOFF, deadline=102.0)
        self.assertEqual(raised.exception.diagnostic["errorType"], "BudgetReached")
        self.assertEqual(self.client.session.request.call_count, 1)
        self.assertEqual(self.client.session.request.call_args.kwargs["timeout"], (1.0, 1.0))

    def test_transport_timeout_uses_smaller_wall_clock_and_monotonic_budget(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {}
        self.client.session = mock.Mock()
        self.client.session.request.return_value = response
        with mock.patch("cninfo_source._now_cst", return_value=CUTOFF - dt.timedelta(seconds=4)), \
                mock.patch("cninfo_source.time.monotonic", return_value=100.0):
            CninfoClient.request_json(self.client, "POST", "https://www.cninfo.com.cn/query",
                                      "announcements", CODE, not_after=CUTOFF, deadline=200.0)
        self.assertEqual(self.client.session.request.call_args.kwargs["timeout"], (2.0, 2.0))

    def test_bounded_historical_query_propagates_both_deadlines(self):
        self.request.return_value = page([row(1, day="2026-08-26")], 1)
        with mock.patch("cninfo_source._now_cst", return_value=OBSERVED):
            self.client.announcements(CODE, page_size=30, not_after=CUTOFF, deadline=200.0)
        self.assertEqual(self.request.call_args.kwargs["not_after"], CUTOFF)
        self.assertEqual(self.request.call_args.kwargs["deadline"], 200.0)

    def test_stock_map_completing_after_cutoff_does_not_start_query(self):
        self.client.org_ids = None
        self.request.return_value = {"stockList": [{"code": CODE, "orgId": ORG}]}
        with mock.patch("cninfo_source._now_cst", side_effect=[OBSERVED, CUTOFF + dt.timedelta(seconds=1)]):
            result = self.client.announcement_window(CODE, window_start=START, cutoff=CUTOFF)
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.request.call_args.args[0], "GET")
        self.assertFalse(result["queryCoverage"]["complete"])
        self.assertEqual(result["queryCoverage"]["reason"], "cutoff_reached")

    def test_legacy_unfiltered_discovery_retains_older_financial_rows(self):
        self.request.return_value = page([row(1, day="2026-08-26")], 1)
        with mock.patch("cninfo_source._now_cst", return_value=OBSERVED):
            rows = self.client.announcements(CODE, page_size=30)
        self.assertEqual(rows[0]["date"], "2026-08-26")
        self.assertEqual(self.request.call_args.kwargs["data"]["seDate"], "")
        self.assertEqual(self.request.call_args.kwargs["data"]["pageNum"], "1")

    def test_invalid_window_and_unbounded_request_are_rejected(self):
        for start, end, size, pages in (
            (START.replace(tzinfo=None), CUTOFF, 30, 4),
            (START, CUTOFF.replace(tzinfo=None), 30, 4),
            (CUTOFF + dt.timedelta(days=1), CUTOFF, 30, 4),
            (START, CUTOFF, 30, 5),
            (START, CUTOFF, 30, True),
        ):
            with self.subTest(start=start, size=size, pages=pages), self.assertRaises(CninfoError):
                self.client.announcement_window(CODE, window_start=start, cutoff=end,
                                                page_size=size, max_pages=pages)


if __name__ == "__main__":
    unittest.main()
