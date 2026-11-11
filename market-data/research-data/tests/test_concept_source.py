import copy
import json
import unittest
from unittest import mock

import requests

import concept_source as source


def document(code="002617"):
    return {"QueryID": "not-needed", "ResultCode": "0", "Result": {code: [
        {"name": "行业", "list": [{"name": "示例行业", "ratio": "1.0"}]},
        {"name": "概念", "list": [{"name": "示例概念"}]},
        {"name": "地域", "list": []},
    ]}}


def response(data=None, status=200, raw=None, headers=None, chunks=None):
    result = mock.Mock(status_code=status, headers=headers or {})
    result.__enter__ = mock.Mock(return_value=result)
    result.__exit__ = mock.Mock(return_value=False)
    encoded = json.dumps(data, ensure_ascii=False).encode() if raw is None else raw
    result.iter_content.return_value = chunks if chunks is not None else [encoded]
    return result


class ConceptSourceTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock(headers={}, trust_env=True)
        patch = mock.patch.object(source.requests, "Session", return_value=self.session)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(source.time, "sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)

    def fetch(self, effects, code="002617"):
        self.session.get.side_effect = effects
        with source.ConceptClient() as client:
            return client.concepts(code)

    def test_valid_response_preserves_output_scope_and_closes_session(self):
        result = self.fetch([response(document())])
        self.assertEqual(result, {"行业": ["示例行业"], "概念": ["示例概念"], "地域": []})
        self.session.close.assert_called_once()
        self.assertFalse(self.session.trust_env)
        call = self.session.get.call_args
        self.assertEqual(call.args, (source.CONCEPT_URL,))
        self.assertTrue(call.args[0].startswith("https://"))
        self.assertEqual(json.loads(call.kwargs["params"]["stock"]),
                         [{"code": "002617", "market": "ab", "type": "stock"}])
        self.assertEqual(call.kwargs["params"]["finClientType"], "pc")
        self.assertEqual(call.kwargs["timeout"], (5, 15))
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])
        self.assertNotIn("verify", call.kwargs)  # requests default certificate verification
        prepared = requests.Request("GET", source.CONCEPT_URL,
                                    params=call.kwargs["params"]).prepare()
        self.assertIn("%7B", prepared.url)
        self.assertNotIn("{", prepared.url)
        self.assertNotIn('"', prepared.url)

    def test_numeric_zero_result_code_is_supported_but_not_boolean(self):
        value = document()
        value["ResultCode"] = 0
        self.fetch([response(value)])
        value["ResultCode"] = False
        with self.assertRaises(source.ConceptError):
            self.fetch([response(value)])

    def test_timeout_retries_and_recovers(self):
        self.fetch([requests.ReadTimeout("secret URL"), response(document())])
        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_connection_failure_retries(self):
        self.fetch([requests.ConnectionError("secret"), response(document())])
        self.assertEqual(self.session.get.call_count, 2)

    def test_exhausted_timeout_is_bounded_and_redacted(self):
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([requests.ReadTimeout("token=secret")] * 3)
        self.assertEqual(caught.exception.diagnostic, {
            "source": "concepts", "stage": "related_blocks", "errorType": "ReadTimeout",
            "code": "002617", "attempt": 3,
        })
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.session.close.assert_called_once()

    def test_retryable_http_statuses(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                failed = response(status=status)
                self.fetch([failed, response(document())])
                self.assertEqual(self.session.get.call_count, 2)
                failed.__exit__.assert_called_once()

    def test_http_retries_stop_after_three_attempts(self):
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([response(status=503)] * 3)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(caught.exception.diagnostic["attempt"], 3)

    def test_denials_redirects_and_not_found_fail_closed_without_retry(self):
        for status in (301, 302, 307, 401, 403, 404):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                with self.assertRaises(source.ConceptError) as caught:
                    self.fetch([response(status=status)])
                self.assertEqual(caught.exception.diagnostic["httpStatus"], status)
                self.assertEqual(self.session.get.call_count, 1)

    def test_tls_failure_is_not_retried_or_verification_disabled(self):
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([requests.exceptions.SSLError("private")])
        self.assertEqual(caught.exception.diagnostic["errorType"], "SSLError")
        self.assertEqual(self.session.get.call_count, 1)

    def test_unknown_request_exception_is_sanitized(self):
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([requests.exceptions.InvalidURL("secret")])
        self.assertEqual(caught.exception.diagnostic["errorType"], "RequestException")
        self.assertNotIn("secret", str(caught.exception))

    def test_invalid_input_never_connects(self):
        for code in (None, 2617, "002617?token=secret", "00001", "１２３４５６"):
            with self.subTest(code=code), self.assertRaises(source.ConceptError):
                self.fetch([], code=code)
        self.session.get.assert_not_called()

    def test_bad_json_and_invalid_utf8_fail_without_body_logging(self):
        for raw in (b"<html>secret</html>", b"\xff", b"", b"{\"private\": NaN}"):
            with self.subTest(raw=raw), self.assertRaises(source.ConceptError) as caught:
                self.fetch([response(raw=raw)])
            self.assertEqual(caught.exception.diagnostic["errorType"], "InvalidJSON")
            self.assertNotIn("secret", str(caught.exception))

    def test_duplicate_json_keys_are_rejected(self):
        raw = b'{"ResultCode":"0","ResultCode":"1","Result":{}}'
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([response(raw=raw)])
        self.assertEqual(caught.exception.diagnostic["errorType"], "InvalidJSON")

    def test_missing_and_wrong_result_structures_are_not_empty_success(self):
        values = [{}, [], None, {"ResultCode": "0"},
                  {"ResultCode": "0", "Result": []},
                  {"ResultCode": "0", "Result": {"002617": []}},
                  {"ResultCode": "0", "Result": {"002617": None}}]
        for data in values:
            with self.subTest(data=data), self.assertRaises(source.ConceptError):
                self.fetch([response(data)])

    def test_upstream_error_is_not_logged_or_treated_as_empty(self):
        value = document()
        value.update(ResultCode="403", ResultMsg="secret server message")
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([response(value)])
        self.assertEqual(caught.exception.diagnostic["errorType"], "UpstreamFailure")
        self.assertNotIn("secret", str(caught.exception))

    def test_wrong_security_or_extra_security_is_rejected(self):
        value = document()
        value["Result"]["600875"] = value["Result"]["002617"]
        for data in (document("600875"), value, {"ResultCode": "0", "Result": {}}):
            with self.subTest(data=data), self.assertRaises(source.ConceptError) as caught:
                self.fetch([response(data)])
            self.assertEqual(caught.exception.diagnostic["errorType"], "SecurityMismatch")

    def test_category_schema_is_strict_and_duplicates_do_not_overwrite(self):
        for categories in ([{}], [None], [{"name": "", "list": []}],
                           [{"name": "概念", "list": None}],
                           [{"name": "概念", "list": [{"name": "A"}]},
                            {"name": "概念", "list": [{"name": "B"}]}]):
            value = document()
            value["Result"]["002617"] = categories
            with self.subTest(categories=categories), self.assertRaises(source.ConceptError):
                self.fetch([response(value)])

    def test_item_names_are_required_and_must_be_safe_strings(self):
        for item in ({}, None, {"name": None}, {"name": ""}, {"name": " "},
                     {"name": "secret\nheader"}, {"name": "x" * 257}):
            value = document()
            value["Result"]["002617"][0]["list"] = [item]
            with self.subTest(item=item), self.assertRaises(source.ConceptError):
                self.fetch([response(value)])

    def test_completely_empty_categories_are_not_valid_evidence(self):
        value = document()
        for category in value["Result"]["002617"]:
            category["list"] = []
        with self.assertRaises(source.ConceptError):
            self.fetch([response(value)])

    def test_missing_concept_category_is_not_silent_empty_membership(self):
        value = document()
        value["Result"]["002617"] = [
            {"name": "行业", "list": [{"name": "示例行业"}]},
            {"name": "地域", "list": [{"name": "示例地域"}]},
        ]
        with self.assertRaises(source.ConceptError) as caught:
            self.fetch([response(value)])
        self.assertEqual(caught.exception.diagnostic["errorType"], "InvalidSchema")

    def test_explicit_empty_concept_category_is_preserved(self):
        value = document()
        value["Result"]["002617"][1]["list"] = []
        result = self.fetch([response(value)])
        self.assertEqual(result, {"行业": ["示例行业"], "概念": [], "地域": []})

    def test_size_limits_apply_to_declared_and_decoded_response(self):
        for item in (response(headers={"Content-Length": str(source.MAX_RESPONSE_BYTES + 1)}),
                     response(chunks=[b"x" * (source.MAX_RESPONSE_BYTES + 1)])):
            with self.subTest(item=item), self.assertRaises(source.ConceptError) as caught:
                self.fetch([item])
            self.assertEqual(caught.exception.diagnostic["errorType"], "ResponseTooLarge")
            item.__exit__.assert_called_once()

    def test_malformed_content_length_fails_closed(self):
        for length in ("private", "-1"):
            with self.subTest(length=length), self.assertRaises(source.ConceptError):
                self.fetch([response(document(), headers={"Content-Length": length})])

    def test_category_and_item_count_limits(self):
        for field in ("categories", "items"):
            value = document()
            if field == "categories":
                value["Result"]["002617"] = [
                    {"name": str(i), "list": [{"name": "A"}]}
                    for i in range(source.MAX_CATEGORIES + 1)]
            else:
                value["Result"]["002617"][0]["list"] = [
                    {"name": str(i)} for i in range(source.MAX_ITEMS_PER_CATEGORY + 1)]
            with self.subTest(field=field), self.assertRaises(source.ConceptError):
                self.fetch([response(value)])

    def test_stream_failure_retries_after_closing_failed_response(self):
        first = response(document())
        first.iter_content.side_effect = requests.ConnectionError("private stream")
        self.fetch([first, response(document())])
        first.__exit__.assert_called_once()
        self.assertEqual(self.session.get.call_count, 2)

    def test_successful_security_is_not_refetched_on_later_retry(self):
        self.session.get.side_effect = [response(document()), requests.ReadTimeout("private"),
                                        response(document("600875"))]
        with source.ConceptClient() as client:
            first = client.concepts("002617")
            second = client.concepts("600875")
        self.assertEqual(first, second)
        codes = [json.loads(call.kwargs["params"]["stock"])[0]["code"]
                 for call in self.session.get.call_args_list]
        self.assertEqual(codes, ["002617", "600875", "600875"])

    def test_diagnostic_allowlist_rejects_or_strips_untrusted_fields(self):
        valid = {"source": "concepts", "stage": "related_blocks", "errorType": "HTTPStatus",
                 "code": "002617", "attempt": 1, "httpStatus": 403}
        unsafe = dict(valid, rawBody="secret", headers={"Authorization": "secret"})
        self.assertEqual(source.safe_concept_diagnostic(unsafe), valid)
        for field, replacement in (("source", "other"), ("stage", "secret"),
                                   ("errorType", "token=secret"),
                                   ("stage", []), ("errorType", {})):
            changed = dict(valid, **{field: replacement})
            self.assertIsNone(source.safe_concept_diagnostic(changed))
        for field, replacement in (("code", "secret"), ("attempt", True),
                                   ("httpStatus", 999)):
            changed = dict(valid, **{field: replacement})
            self.assertNotIn(field, source.safe_concept_diagnostic(changed))
        self.assertIsNone(source.safe_concept_diagnostic("secret"))

    def test_input_document_not_mutated(self):
        original = document()
        cloned = copy.deepcopy(original)
        source.ConceptClient._parse(original, "002617", 1)
        self.assertEqual(original, cloned)


if __name__ == "__main__":
    unittest.main()
