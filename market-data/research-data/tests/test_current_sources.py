import copy
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daily_snapshot as daily
from source_contract import validate_current_source_payloads
from ths_source import SourceError


def values(code="002617"):
    common = {"provider": "ths_f10", "status": "validated", "code": code, "market": "SZ",
              "parserVersion": 1, "responseSha256": "a" * 64, "timeKind": "observed_at",
              "collectionStartedAt": "2026-09-10T00:00:00+00:00",
              "observedAt": "2026-09-10T00:00:01+00:00"}
    concept = {"概念": ["样例概念"], "其他概念": ["其他样例"], "evidence": {
        **common, "sourceUrl": f"https://basic.10jqka.com.cn/{code}/concept.html",
        "taxonomy": "ths_f10_current_concepts", "truncated": False}}
    shareholder = {"text": "样例股东研究", "evidence": {
        **common, "sourceUrl": f"https://basic.10jqka.com.cn/{code}/holder.html",
        "fullTextChars": 6, "truncated": False, "sourceSection": "股东研究",
        "reportDates": ["2026-06-30"], "tableSummaries": [{"rowCount": 1}]}}
    return {code: concept}, {code: shareholder}


class CurrentSourceTests(unittest.TestCase):
    def test_valid_new_observations(self):
        validate_current_source_payloads(*values(), ["002617"])

    def test_empty_or_untyped_shareholder_is_not_success(self):
        for invalid in (None, "", "old untyped text", {}, {"text": ""}):
            concepts, insiders = values()
            insiders["002617"] = invalid
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_current_source_payloads(concepts, insiders, ["002617"])

    def test_missing_extra_wrong_security_and_no_provenance_reject(self):
        for key, value in (("code", "600875"), ("market", "SH"), ("provider", "baidu"),
                           ("sourceUrl", "http://untrusted"), ("responseSha256", "bad"),
                           ("timeKind", "trade_date"), ("observedAt", "2026-09-10T00:00:01"),
                           ("observedAt", "2026-09-09T00:00:00+00:00")):
            concepts, insiders = values()
            insiders["002617"]["evidence"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_current_source_payloads(concepts, insiders, ["002617"])
        concepts, insiders = values()
        insiders["600875"] = copy.deepcopy(insiders["002617"])
        with self.assertRaises(ValueError):
            validate_current_source_payloads(concepts, insiders, ["002617"])

    def test_truncation_and_concept_schema_cannot_be_faked(self):
        for key, value in (("truncated", True), ("fullTextChars", 3), ("reportDates", [])):
            concepts, insiders = values()
            insiders["002617"]["evidence"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_current_source_payloads(concepts, insiders, ["002617"])
        for names in ([], ["x", "x"], [None], "not-list"):
            concepts, insiders = values()
            concepts["002617"]["概念"] = names
            with self.assertRaises(ValueError):
                validate_current_source_payloads(concepts, insiders, ["002617"])

    def test_shareholder_failure_keeps_other_securities_and_closes(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        error = SourceError("insiders", "holder_page", "HTTPStatus", "600875", http_status=403)
        client.shareholders.side_effect = [{"text": "one"}, error, {"text": "three"}]
        with mock.patch.object(daily, "ShareholderClient", return_value=client):
            result = daily.collect_shareholders(["002617", "600875", "300033"])
        self.assertEqual(result["002617"], {"text": "one"})
        self.assertEqual(result["300033"], {"text": "three"})
        self.assertEqual(result["600875"], {"error": error.diagnostic})
        client.__exit__.assert_called_once()

    def test_both_new_source_errors_are_diagnostic_and_block_assembly(self):
        sources = {}
        for source, stage in (("concepts", "concept_page"), ("insiders", "holder_page")):
            diagnostic = SourceError(source, stage, "HTTPStatus", "002617", http_status=403).diagnostic
            diagnostic["rawBody"] = "SECRET"
            sources[source] = {"002617": {"error": diagnostic}}
        with self.assertRaises(daily.SnapshotValidationError) as caught:
            daily.validate_source_completeness(sources, ["002617"], [])
        self.assertEqual(len(caught.exception.details["sourceErrors"]), 2)
        self.assertNotIn("SECRET", repr(caught.exception.details))

    def test_early_stage_never_creates_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "stage"
            with mock.patch.object(daily, "validate_akshare_market_payload", return_value="2026-09-10"):
                with self.assertRaises(daily.SnapshotDeferredError):
                    daily.seal_akshare_market_stage({}, "2026-09-10T08:00:00+08:00",
                                                   "2026-09-10T08:00:01+08:00", str(directory))
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
