import json
import time
import unittest
from contextlib import ExitStack
from unittest import mock

import daily_snapshot as daily
from cninfo_source import CninfoError
from concept_source import ConceptError


class SourceIntegrationTests(unittest.TestCase):
    def test_concept_failure_is_per_security_and_preserves_successes(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        failure = ConceptError("related_blocks", "ReadTimeout", "600875", 3)
        client.concepts.side_effect = [{"概念": ["sample"]}, failure, {"概念": ["sample2"]}]
        with mock.patch.object(daily, "ConceptClient", return_value=client):
            result = daily.collect_concepts(["002617", "600875", "300033"])
        self.assertEqual(result["002617"], {"概念": ["sample"]})
        self.assertEqual(result["300033"], {"概念": ["sample2"]})
        self.assertEqual(result["600875"], {"error": failure.diagnostic})
        self.assertEqual(client.concepts.call_args_list,
                         [mock.call("002617"), mock.call("600875"), mock.call("300033")])
        client.__exit__.assert_called_once()

    def test_combined_diagnostics_keep_both_sources_and_remove_unknown_fields(self):
        announcement = CninfoError("announcements", "InvalidSchema", "300033",
                                  validation_reason="future_disclosure_evidence").diagnostic
        concept = ConceptError("related_blocks", "HTTPStatus", "600875", 1, 403).diagnostic
        concept["body"] = "secret-token"
        sources = {"announcements": {"300033": {"error": announcement}},
                   "concepts": {"600875": {"error": concept}}}
        with self.assertRaises(daily.SnapshotValidationError) as caught:
            daily.validate_source_completeness(sources, ["300033", "600875"], [])
        self.assertIn("announcements.300033.error", str(caught.exception))
        self.assertIn("concepts.600875.error", str(caught.exception))
        details = caught.exception.details["sourceErrors"]
        self.assertEqual({d["source"] for d in details}, {"cninfo", "concepts"})
        self.assertNotIn("secret", json.dumps(details))

    def test_malformed_diagnostic_still_rejects_without_crashing_sanitizer(self):
        sources = {"concepts": {"300033": {"error": {
            "source": "concepts", "stage": [], "errorType": {"private": "value"}}}}}
        with self.assertRaises(daily.SnapshotValidationError) as caught:
            daily.validate_source_completeness(sources, ["300033"], [])
        self.assertIn("concepts.300033.error", str(caught.exception))
        self.assertNotIn("sourceErrors", caught.exception.details)

    def test_concept_failure_rejects_full_assembly_before_store_with_target(self):
        stage = {"status": "existing", "tradeDate": "2026-09-04", "payloadSha256": "0" * 64,
                 "akshare": {"sectors": [{"name": "sample"}],
                             "activity": {"统计日期": "2026-09-04 15:00:00"}},
                 "collectionWindow": {"startedAt": "2026-09-04T22:30:00+08:00",
                                      "completedAt": "2026-09-04T22:30:01+08:00"}}
        failure = ConceptError("related_blocks", "ReadTimeout", "300033", 3)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(daily.sys, "argv", ["daily_snapshot.py"]))
            stack.enter_context(mock.patch.object(daily, "qlib_market", return_value={
                "date": "2026-09-04", "generation": {"tag": "2026-09-05"}}))
            stack.enter_context(mock.patch.object(daily, "existing_snapshot", return_value=None))
            stack.enter_context(mock.patch.object(daily, "load_akshare_market_stage", return_value=stage))
            stack.enter_context(mock.patch.object(daily, "capture_current_akshare_market", return_value=stage))
            stack.enter_context(mock.patch("builtins.open", mock.mock_open(read_data=json.dumps({
                "corePool": [{"code": "300033"}]}))))
            stack.enter_context(mock.patch.object(daily, "spot_market", return_value={"limitUp": []}))
            for name in ("akshare_lhb", "fund_flow_sina", "lhb_official", "mootdx_quotes",
                         "tencent_quote", "baostock_valuation", "cls_telegraph", "northbound_flow",
                         "insider_transactions", "hot_stocks", "global_markets"):
                stack.enter_context(mock.patch.object(daily, name, return_value={}))
            stack.enter_context(mock.patch.object(daily, "collect_cninfo_announcements",
                                                 return_value={"300033": []}))
            stack.enter_context(mock.patch.object(daily, "collect_concepts",
                                                 return_value={"300033": {"error": failure.diagnostic}}))
            store = stack.enter_context(mock.patch.object(daily, "store_snapshot"))
            with self.assertRaises(daily.SnapshotValidationError) as caught:
                daily.run_snapshot("offline-concepts-rejected", time.monotonic())
            store.assert_not_called()
        self.assertEqual(caught.exception.details["target"], "2026-09-04")
        self.assertEqual(caught.exception.details["qlibTag"], "2026-09-05")
        self.assertEqual(caught.exception.details["sourceErrors"], [failure.diagnostic])


if __name__ == "__main__":
    unittest.main()
