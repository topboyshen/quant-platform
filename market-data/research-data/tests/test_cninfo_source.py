import copy
import datetime as dt
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import requests

import cninfo_source as source
import daily_snapshot


def response(document=None, status=200, json_error=None):
    result = mock.Mock(status_code=status)
    result.__enter__ = mock.Mock(return_value=result)
    result.__exit__ = mock.Mock(return_value=False)
    result.json.return_value = document
    if json_error:
        result.json.side_effect = json_error
    return result


def mapping():
    return {"stockList": [{"code": "002617", "orgId": "9900021308"},
                          {"code": "600875", "orgId": "gssh0600875"}]}


def announcements(code="002617"):
    return {"announcements": [{"secCode": code, "announcementTitle": "sample",
                               "announcementTime": 1788480000000}],
            "totalAnnouncement": 1}


OBSERVED_AT = dt.datetime(2026, 9, 7, 22, 30, 2, tzinfo=source.CST)


def next_day_announcements(code="002617", day="2026-09-08"):
    result = announcements(code)
    row = result["announcements"][0]
    row.update({"orgId": "9900021308", "announcementId": "1220000001",
                "announcementTime": int(dt.datetime.fromisoformat(day).replace(
                    tzinfo=source.CST).timestamp() * 1000),
                "adjunctUrl": f"finalpage/{day}/1220000001.PDF"})
    return result


class CninfoTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock(headers={}, trust_env=True)
        self.patch_session = mock.patch.object(source.requests, "Session", return_value=self.session)
        self.patch_sleep = mock.patch.object(source.time, "sleep")
        self.patch_now = mock.patch.object(source, "_now_cst", return_value=OBSERVED_AT)
        self.patch_session.start()
        self.sleep = self.patch_sleep.start()
        self.now = self.patch_now.start()
        self.addCleanup(self.patch_session.stop)
        self.addCleanup(self.patch_sleep.stop)
        self.addCleanup(self.patch_now.stop)

    def fetch(self, effects, code="002617"):
        self.session.request.side_effect = effects
        with source.CninfoClient() as client:
            return client.announcements(code)

    def test_success_keeps_query_scope_and_closes_session(self):
        rows = self.fetch([response(mapping()), response(announcements())])
        self.assertEqual(rows, [{"title": "sample", "date": "2026-09-04",
                                "dateKind": "source_disclosure_date",
                                "observedAt": OBSERVED_AT.isoformat(),
                                "nextDayDisclosure": False}])
        self.assertFalse(self.session.trust_env)
        self.session.close.assert_called_once()
        get_call, post_call = self.session.request.call_args_list
        self.assertEqual(get_call.args, ("GET", source.STOCK_MAP_URL))
        self.assertTrue(get_call.args[1].startswith("https://"))
        self.assertEqual(post_call.args, ("POST", source.ANNOUNCEMENTS_URL))
        self.assertEqual(post_call.kwargs["timeout"], (5, 15))
        self.assertFalse(post_call.kwargs["allow_redirects"])
        self.assertEqual(post_call.kwargs["data"]["column"], "")
        self.assertEqual(post_call.kwargs["data"]["seDate"], "")
        self.assertEqual(post_call.kwargs["data"]["pageSize"], "10")

    def test_timeout_retries_only_the_failed_query_and_recovers(self):
        self.fetch([response(mapping()), requests.ReadTimeout("private token=secret"),
                    response(announcements())])
        self.assertEqual(self.session.request.call_count, 3)
        self.sleep.assert_called_once_with(1)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list],
                         ["GET", "POST", "POST"])

    def test_mapping_timeout_retries_before_query(self):
        self.fetch([requests.ConnectTimeout("private"), response(mapping()),
                    response(announcements())])
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list],
                         ["GET", "GET", "POST"])

    def test_exhausted_timeout_is_bounded_and_does_not_leak_message(self):
        with self.assertRaises(source.CninfoError) as caught:
            self.fetch([response(mapping())] + [requests.ReadTimeout("token=secret")] * 3)
        self.assertEqual(caught.exception.diagnostic, {
            "source": "cninfo", "stage": "announcements", "code": "002617",
            "errorType": "ReadTimeout", "attempt": 3,
        })
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.session.request.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.session.close.assert_called_once()

    def test_retryable_http_statuses_are_bounded(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.fetch([response(mapping()), response(status=status), response(announcements())])
                self.assertEqual(self.session.request.call_count, 3)

    def test_http_auth_and_redirect_fail_without_retry_or_empty_success(self):
        for status in (301, 302, 401, 403, 404):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                with self.assertRaises(source.CninfoError) as caught:
                    self.fetch([response(mapping()), response(status=status)])
                self.assertEqual(caught.exception.diagnostic["httpStatus"], status)
                self.assertEqual(self.session.request.call_count, 2)

    def test_invalid_json_is_rejected_without_logging_body(self):
        with self.assertRaises(source.CninfoError) as caught:
            self.fetch([response(mapping()), response(json_error=ValueError("secret body"))])
        self.assertEqual(caught.exception.diagnostic["errorType"], "InvalidJSON")
        self.assertNotIn("secret", str(caught.exception))

    def test_missing_fields_and_partial_pages_are_not_empty_success(self):
        for document in ({}, {"announcements": None}, {"announcements": [], "totalAnnouncement": 1},
                         {"announcements": None, "totalAnnouncement": 10},
                         {"announcements": {}, "totalAnnouncement": 0},
                         {"announcements": [], "totalAnnouncement": True}):
            with self.subTest(document=document), self.assertRaises(source.CninfoError):
                self.fetch([response(mapping()), response(document)])

    def test_verified_no_announcements_is_allowed(self):
        for rows in (None, []):
            with self.subTest(rows=rows):
                self.assertEqual(self.fetch([response(mapping()), response(
                    {"announcements": rows, "totalAnnouncement": 0})]), [])

    def test_wrong_security_is_rejected(self):
        with self.assertRaises(source.CninfoError) as caught:
            self.fetch([response(mapping()), response(announcements("600875"))])
        self.assertEqual(caught.exception.diagnostic["errorType"], "SecurityMismatch")

    def test_invalid_titles_and_timestamps_are_rejected(self):
        for field, value in (("announcementTitle", ""), ("announcementTime", None),
                             ("announcementTime", True), ("announcementTime", "secret"),
                             ("announcementTime", 0), ("announcementTime", 99999999999999999)):
            data = announcements()
            data["announcements"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(source.CninfoError):
                self.fetch([response(mapping()), response(data)])

    def test_next_day_disclosure_label_is_preserved_not_backdated(self):
        data = next_day_announcements()
        rows = self.fetch([response(mapping()), response(data)])
        self.assertEqual(rows, [{"title": "sample", "date": "2026-09-08",
                                "dateKind": "source_disclosure_date",
                                "observedAt": "2026-09-07T22:30:02+08:00",
                                "nextDayDisclosure": True,
                                "disclosureEvidence": {
                                    "orgId": "9900021308", "announcementId": "1220000001",
                                    "adjunctUrl": "finalpage/2026-09-08/1220000001.PDF"}}])
        # This response necessarily failed the old date <= observation-day condition.
        self.assertGreater(dt.date.fromisoformat(rows[0]["date"]), OBSERVED_AT.date())

    def test_next_day_complete_300033_page_keeps_all_ten_rows(self):
        stocks = mapping()
        stocks["stockList"].append({"code": "300033", "orgId": "9900008300"})
        data = next_day_announcements("300033")
        data["announcements"][0]["orgId"] = "9900008300"
        data["announcements"] *= 10
        data["totalAnnouncement"] = 1397
        rows = self.fetch([response(stocks), response(data)], code="300033")
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["nextDayDisclosure"] for row in rows))
        self.now.assert_called_once_with()
        post = self.session.request.call_args_list[-1]
        self.assertEqual(post.kwargs["data"]["stock"], "300033,9900008300")
        self.assertEqual(post.kwargs["data"]["seDate"], "")

    def test_millisecond_string_and_seconds_retain_next_day_semantics(self):
        original = next_day_announcements()["announcements"][0]["announcementTime"]
        for value in (original, str(original), original // 1000, str(original // 1000)):
            data = next_day_announcements()
            data["announcements"][0]["announcementTime"] = value
            with self.subTest(value=value):
                rows = self.fetch([response(mapping()), response(data)])
                self.assertEqual(rows[0]["date"], "2026-09-08")
                self.assertTrue(rows[0]["nextDayDisclosure"])

    def test_same_day_disclosure_not_misclassified_after_midnight(self):
        self.now.return_value = dt.datetime(2026, 9, 8, 0, 0, 1, tzinfo=source.CST)
        rows = self.fetch([response(mapping()), response(next_day_announcements())])
        self.assertFalse(rows[0]["nextDayDisclosure"])
        self.assertNotIn("disclosureEvidence", rows[0])
        self.assertEqual(rows[0]["observedAt"], "2026-09-08T00:00:01+08:00")

    def test_two_or_more_days_ahead_remain_rejected_even_with_valid_attachment(self):
        for observed, day in ((OBSERVED_AT, "2026-09-09"),
                              (dt.datetime(2026, 9, 4, 22, 30, tzinfo=source.CST),
                               "2026-09-07")):
            self.now.return_value = observed
            with self.subTest(day=day), self.assertRaises(source.CninfoError) as caught:
                self.fetch([response(mapping()), response(next_day_announcements(day=day))])
            self.assertEqual(caught.exception.diagnostic["validationReason"], "date_out_of_range")

    def test_future_non_midnight_timestamp_is_not_a_disclosure_date_label(self):
        data = next_day_announcements()
        data["announcements"][0]["announcementTime"] += 1000
        with self.assertRaises(source.CninfoError) as caught:
            self.fetch([response(mapping()), response(data)])
        self.assertEqual(caught.exception.diagnostic["validationReason"],
                         "future_disclosure_evidence")

    def test_wrong_organization_is_rejected_even_with_right_security_code(self):
        for data in (announcements(), next_day_announcements()):
            data["announcements"][0]["orgId"] = "wrong"
            with self.subTest(nextDay="adjunctUrl" in data["announcements"][0]), \
                    self.assertRaises(source.CninfoError) as caught:
                self.fetch([response(mapping()), response(data)])
            self.assertEqual(caught.exception.diagnostic["errorType"], "SecurityMismatch")

    def test_next_day_requires_org_and_attachment_identity(self):
        for field in ("orgId", "announcementId", "adjunctUrl"):
            data = next_day_announcements()
            del data["announcements"][0][field]
            with self.subTest(field=field), self.assertRaises(source.CninfoError):
                self.fetch([response(mapping()), response(data)])

    def test_next_day_wrong_attachment_domain_path_date_or_id_is_rejected(self):
        for path in ("https://evil.example/finalpage/2026-09-08/1220000001.PDF",
                     "//evil.example/finalpage/2026-09-08/1220000001.PDF",
                     "finalpage/2026-09-07/1220000001.PDF",
                     "finalpage/2026-09-08/1220000002.PDF",
                     "finalpage/2026-09-08/../1220000001.PDF",
                     "finalpage/2026-09-08/1220000001.PDF?token=secret",
                     None, {"token": "secret"}):
            data = next_day_announcements()
            data["announcements"][0]["adjunctUrl"] = path
            with self.subTest(path=path), self.assertRaises(source.CninfoError) as caught:
                self.fetch([response(mapping()), response(data)])
            self.assertEqual(caught.exception.diagnostic["validationReason"],
                             "future_disclosure_evidence")
            self.assertNotIn("secret", str(caught.exception))

    def test_next_day_does_not_permit_incomplete_page(self):
        data = next_day_announcements()
        data["totalAnnouncement"] = 10
        with self.assertRaises(source.CninfoError) as caught:
            self.fetch([response(mapping()), response(data)])
        self.assertEqual(caught.exception.diagnostic["validationReason"], "page_incomplete")

    def test_schema_reason_survives_allowlist_without_untrusted_details(self):
        diagnostic = source.CninfoError("announcements", "InvalidSchema", "300033",
            validation_reason="future_disclosure_evidence").diagnostic
        diagnostic.update({"body": "secret", "url": "https://example.test/?token=secret"})
        self.assertEqual(source.safe_diagnostic(diagnostic)["validationReason"],
                         "future_disclosure_evidence")
        self.assertNotIn("secret", json.dumps(source.safe_diagnostic(diagnostic)))
        for value in ("secret", None, [], {}, True):
            diagnostic["validationReason"] = value
            self.assertNotIn("validationReason", source.safe_diagnostic(diagnostic))

    def test_untrusted_stage_or_error_type_is_rejected_without_type_error(self):
        for field in ("stage", "errorType"):
            diagnostic = source.CninfoError("announcements", "InvalidSchema", "300033").diagnostic
            diagnostic[field] = ["secret"]
            self.assertIsNone(source.safe_diagnostic(diagnostic))

    def test_unknown_security_does_not_guess_an_org_id(self):
        with self.assertRaises(source.CninfoError) as caught:
            self.fetch([response(mapping())], code="300033")
        self.assertEqual(caught.exception.diagnostic["errorType"], "UnknownSecurity")
        self.assertEqual(self.session.request.call_count, 1)

    def test_invalid_input_never_connects(self):
        with self.assertRaises(source.CninfoError):
            self.fetch([], code="002617?token=secret")
        self.session.request.assert_not_called()

    def test_invalid_or_conflicting_mapping_is_rejected(self):
        conflict = mapping()
        conflict["stockList"].append({"code": "002617", "orgId": "different"})
        for document in ({}, {"stockList": []}, {"stockList": [{}]}, conflict):
            with self.subTest(document=document), self.assertRaises(source.CninfoError):
                self.fetch([response(document)])

    def test_mapping_reused_and_successful_security_not_refetched(self):
        self.session.request.side_effect = [response(mapping()), response(announcements()),
                                            requests.ReadTimeout("private"),
                                            response(announcements("600875"))]
        result = daily_snapshot.collect_cninfo_announcements(["002617", "600875"])
        self.assertEqual(set(result), {"002617", "600875"})
        stocks = [call.kwargs["data"]["stock"].split(",")[0]
                  for call in self.session.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual(stocks, ["002617", "600875", "600875"])

    def test_one_failed_security_keeps_others_but_rejects_snapshot(self):
        self.session.request.side_effect = [response(mapping()), response(announcements())] + [
            requests.ReadTimeout("secret")
        ] * 3
        result = daily_snapshot.collect_cninfo_announcements(["002617", "600875"])
        self.assertIsInstance(result["002617"], list)
        with self.assertRaises(daily_snapshot.SnapshotValidationError) as caught:
            daily_snapshot.validate_source_completeness({"announcements": result}, [], [])
        self.assertIn("announcements.600875.error", str(caught.exception))
        self.assertEqual(caught.exception.details["sourceErrors"][0]["errorType"], "ReadTimeout")
        self.assertNotIn("secret", json.dumps(caught.exception.details))

    def test_untrusted_diagnostics_are_allowlisted(self):
        diagnostic = source.CninfoError("announcements", "ReadTimeout", "002617", 3).diagnostic
        diagnostic.update({"token": "secret", "url": "https://private/?key=secret"})
        self.assertNotIn("secret", json.dumps(source.safe_diagnostic(diagnostic)))
        altered = copy.deepcopy(diagnostic)
        altered["errorType"] = "secret"
        self.assertIsNone(source.safe_diagnostic(altered))

    def test_rejected_run_keeps_database_and_retains_target_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "research-data" / "config"
            config.mkdir(parents=True)
            (config / "stocks.json").write_text(json.dumps({"corePool": [{"code": "002617"}]}))
            db = root / "snapshot.db"
            daily_snapshot.store_snapshot({"date": "2026-08-28", "generatedAt": "old"}, str(db))
            original_hash = hashlib.sha256(db.read_bytes()).hexdigest()
            error = source.CninfoError("announcements", "ReadTimeout", "002617", 3)
            stage = {"akshare": {"activity": {"统计日期": "2026-09-04"}}}
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"HOME": directory}))
                stack.enter_context(mock.patch.object(sys, "argv", ["daily_snapshot.py"]))
                stack.enter_context(mock.patch.object(daily_snapshot, "qlib_market", return_value={
                    "date": "2026-09-04", "generation": {"tag": "2026-09-05"}}))
                stack.enter_context(mock.patch.object(daily_snapshot, "existing_snapshot", return_value=None))
                stack.enter_context(mock.patch.object(daily_snapshot, "load_akshare_market_stage", return_value=stage))
                stack.enter_context(mock.patch.object(daily_snapshot, "capture_current_akshare_market",
                    side_effect=daily_snapshot.SnapshotDeferredError("unavailable")))
                for name in ("akshare_lhb", "spot_market", "fund_flow_sina", "lhb_official",
                             "mootdx_quotes", "tencent_quote", "baostock_valuation", "cls_telegraph",
                             "northbound_flow", "concept_blocks", "insider_transactions",
                             "hot_stocks", "global_markets"):
                    stack.enter_context(mock.patch.object(daily_snapshot, name, return_value={}))
                stack.enter_context(mock.patch.object(daily_snapshot, "collect_cninfo_announcements",
                    return_value={"002617": {"error": error.diagnostic}}))
                stack.enter_context(mock.patch.object(daily_snapshot, "collect_shareholders",
                    return_value={"002617": {}}))
                store = stack.enter_context(mock.patch.object(daily_snapshot, "store_snapshot"))
                with self.assertRaises(daily_snapshot.SnapshotValidationError) as caught:
                    daily_snapshot.run_snapshot("offline-rejected", time.monotonic())
                store.assert_not_called()
            self.assertEqual(caught.exception.details["target"], "2026-09-04")
            self.assertEqual(caught.exception.details["qlibTag"], "2026-09-05")
            self.assertEqual(caught.exception.details["sourceErrors"], [error.diagnostic])
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
