"""Synthetic THS pages and mocked HTTP only; never contact an upstream site."""
import datetime as dt
import hashlib
import unittest
from unittest import mock

import requests

import ths_source
from ths_concept_source import ThsConceptClient
from ths_source import F10Client, SourceError, security_market, safe_source_diagnostic


def concept_row(number=1, name="清洁能源", source_id="000001", *, other=False):
    cls, attr = ("gnStockList", "cid") if other else ("gnName", "clid")
    leader = "" if other else "<td>示例龙头</td>"
    return (f'<tr><td>{number}</td><td class="{cls}" {attr}="{source_id}">{name}</td>'
            f'{leader}<td>公开分类说明</td></tr>')


def concept_section(*, other=False, rows=None, headers=None):
    ident, heading = ("other", "其他概念") if other else ("concept", "常规概念")
    if rows is None:
        rows = concept_row(name="融资融券", source_id="000201", other=True) if other else concept_row()
    if headers is None:
        headers = ["序号", "概念名称", "概念解析"] if other else ["序号", "概念名称", "龙头股", "概念解析"]
    head = "".join(f"<th>{item}</th>" for item in headers)
    return (f'<section id="{ident}"><h2>{heading}</h2><table class="gnContent">'
            f'<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></section>')


def page_html(code="600875", *, kind="concept", market_id=None, content=None,
              charset="utf-8", extra_head="", extra_body=""):
    if market_id is None:
        market_id = "17" if code.startswith(("60", "68")) else "33"
    title_kind = "概念题材" if kind == "concept" else "股东研究"
    if content is None:
        content = concept_section() + concept_section(other=True) if kind == "concept" else "<p>股东研究资料</p>"
    return (f'<!doctype html><html><head><meta charset="{charset}">'
            f'<title>示例公司({code}) {title_kind}_F10_同花顺金融服务网</title>{extra_head}</head>'
            f'<body><input type="hidden" id="stockCode" value="{code}">'
            f'<input type="hidden" id="marketId" value="{market_id}">'
            f'{content}{extra_body}</body></html>')


class FakeResponse:
    def __init__(self, body=None, *, status=200, headers=None, chunks=None):
        self.body = page_html().encode("utf-8") if body is None else body
        self.status_code = status
        self.headers = {"Content-Type": "text/html; charset=UTF-8"} if headers is None else headers
        self.chunks = [self.body] if chunks is None else chunks
        self.read_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        self.read_calls += 1
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


class MockHttpTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.session.headers = {}
        self.session.trust_env = True
        self.session.verify = True
        self.session.get.return_value = FakeResponse()
        factory_patch = mock.patch.object(ths_source.requests, "Session", return_value=self.session)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        sleep_patch = mock.patch.object(ths_source.time, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def get_page(self, code="600875", kind="concept"):
        with F10Client("concepts" if kind == "concept" else "insiders") as client:
            return client.page(code, kind)

    def assert_error(self, error_type, func=None, *, attempt=1):
        with self.assertRaises(SourceError) as caught:
            (func or self.get_page)()
        self.assertEqual(caught.exception.diagnostic["errorType"], error_type)
        self.assertEqual(caught.exception.diagnostic["attempt"], attempt)
        return caught.exception


class TransportTests(MockHttpTests):
    def test_https_verification_default_and_no_proxy_override(self):
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://invalid.example:9999"}):
            _, evidence = self.get_page()
        self.assertFalse(self.session.trust_env)
        self.assertIs(self.session.verify, True)
        self.session.get.assert_called_once_with(
            "https://basic.10jqka.com.cn/600875/concept.html", timeout=(5, 12),
            stream=True, allow_redirects=False)
        kwargs = self.session.get.call_args.kwargs
        self.assertNotIn("verify", kwargs)
        self.assertNotIn("proxies", kwargs)
        self.assertNotIn("cookies", kwargs)
        self.assertEqual(evidence["transportSecurity"], "https_verified")
        self.session.close.assert_called_once()

    def test_forbidden_does_not_retry_or_read_body(self):
        response = FakeResponse(b"private upstream error text", status=403)
        self.session.get.return_value = response
        error = self.assert_error("HTTPStatus")
        self.assertEqual(error.diagnostic["httpStatus"], 403)
        self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(response.closed)
        self.assertNotIn("private", str(error))

    def test_redirects_are_not_followed_or_retried(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.session.get.return_value = FakeResponse(status=status, headers={"Location": "https://other.example/"})
                error = self.assert_error("HTTPStatus")
                self.assertEqual(error.diagnostic["httpStatus"], status)
                self.assertEqual(self.session.get.call_count, 1)
                self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.sleep.assert_not_called()

    def test_other_nonretryable_statuses(self):
        for status in (204, 400, 401, 404, 410):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.session.get.return_value = FakeResponse(status=status)
                self.assert_error("HTTPStatus")
                self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_timeout_retries_have_three_attempt_limit(self):
        self.session.get.side_effect = requests.ReadTimeout("secret timeout details")
        error = self.assert_error("ReadTimeout", attempt=3)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertNotIn("secret", str(error))
        self.assertEqual({call.args[0] for call in self.session.get.call_args_list},
                         {"https://basic.10jqka.com.cn/600875/concept.html"})

    def test_429_retries_have_three_attempt_limit(self):
        responses = [FakeResponse(status=429) for _ in range(3)]
        self.session.get.side_effect = responses
        error = self.assert_error("HTTPStatus", attempt=3)
        self.assertEqual(error.diagnostic["httpStatus"], 429)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertTrue(all(response.closed for response in responses))

    def test_5xx_then_success_retries_same_endpoint(self):
        self.session.get.side_effect = [FakeResponse(status=503), FakeResponse()]
        self.get_page()
        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(self.session.get.call_args_list[0], self.session.get.call_args_list[1])

    def test_connection_timeout_and_connection_error_can_retry(self):
        self.session.get.side_effect = [requests.ConnectTimeout(), requests.ConnectionError(), FakeResponse()]
        self.get_page()
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_certificate_error_does_not_retry(self):
        self.session.get.side_effect = requests.exceptions.SSLError("private certificate details")
        error = self.assert_error("SSLError")
        self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()
        self.assertNotIn("private", str(error))

    def test_generic_request_exception_does_not_retry(self):
        self.session.get.side_effect = requests.RequestException("private query string")
        self.assert_error("RequestException")
        self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_stream_read_timeout_closes_response_before_retry(self):
        first = FakeResponse(chunks=[b"partial", requests.ReadTimeout("private stream")])
        self.session.get.side_effect = [first, FakeResponse()]
        self.get_page()
        self.assertTrue(first.closed)
        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_declared_oversize_rejected_before_read(self):
        response = FakeResponse(headers={"Content-Type": "text/html",
                                         "Content-Length": str(ths_source.MAX_RESPONSE_BYTES + 1)})
        self.session.get.return_value = response
        self.assert_error("ResponseTooLarge")
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_stream_size_limit_not_only_content_length(self):
        self.session.get.return_value = FakeResponse(
            headers={"Content-Type": "text/html", "Content-Length": "1"},
            chunks=[b"a" * ths_source.MAX_RESPONSE_BYTES, b"b"])
        self.assert_error("ResponseTooLarge")
        self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_exact_response_limit_is_allowed_by_transport(self):
        raw = page_html().encode("utf-8")
        raw += b" " * (ths_source.MAX_RESPONSE_BYTES - len(raw))
        self.session.get.return_value = FakeResponse(raw)
        _, evidence = self.get_page()
        self.assertEqual(evidence["responseSha256"], hashlib.sha256(raw).hexdigest())

    def test_invalid_content_length_rejected(self):
        for declared in ("-1", "1.5", " 1", "10x", 12):
            with self.subTest(declared=declared):
                self.session.get.return_value = FakeResponse(headers={"Content-Type": "text/html", "Content-Length": declared})
                self.assert_error("InvalidSchema")
        self.sleep.assert_not_called()

    def test_non_html_content_type_rejected(self):
        for headers in ({}, {"Content-Type": "application/json"}, {"Content-Type": "text/plain"}):
            with self.subTest(headers=headers):
                self.session.get.return_value = FakeResponse(headers=headers)
                self.assert_error("InvalidSchema")

    def test_unexpectedly_large_dom_rejected(self):
        self.session.get.return_value = FakeResponse(page_html(extra_body="<i></i>" * 20001).encode())
        self.assert_error("ResponseTooLarge")


class IdentityTests(MockHttpTests):
    def test_supported_security_markets(self):
        for code, expected in (("600875", ("SH", "17")), ("688001", ("SH", "17")),
                               ("002617", ("SZ", "33")), ("300033", ("SZ", "33"))):
            with self.subTest(code=code):
                self.assertEqual(security_market(code), expected)
                self.session.get.return_value = FakeResponse(page_html(code).encode())
                _, evidence = self.get_page(code)
                self.assertEqual(evidence["code"], code)
                self.assertEqual(evidence["market"], expected[0])

    def test_invalid_or_out_of_scope_code_rejected_before_http(self):
        for code in (None, True, 600875, "60087", "6008750", "600875 ", "../600875", "600875?x=1",
                     "430047", "920001", "900901", "00700", "６００８７５"):
            with self.subTest(code=code):
                self.assert_error("InvalidInput", lambda: self.get_page(code))
        self.session.get.assert_not_called()

    def test_invalid_page_kind_rejected_before_http(self):
        with F10Client("concepts") as client:
            self.assert_error("InvalidInput", lambda: client.page("600875", "../holder"))
        self.session.get.assert_not_called()

    def test_holder_identity_uses_holder_title(self):
        self.session.get.return_value = FakeResponse(page_html(kind="holder").encode())
        _, evidence = self.get_page(kind="holder")
        self.assertIn("股东研究", evidence["title"])
        self.assertEqual(evidence["sourceUrl"], "https://basic.10jqka.com.cn/600875/holder.html")

    def test_title_code_mismatch_rejected(self):
        text = page_html().replace("示例公司(600875)", "示例公司(600876)")
        self.session.get.return_value = FakeResponse(text.encode())
        self.assert_error("SecurityMismatch")

    def test_title_kind_mismatch_rejected(self):
        self.session.get.return_value = FakeResponse(page_html().replace("概念题材_F10", "股东研究_F10").encode())
        self.assert_error("SecurityMismatch")

    def test_duplicate_titles_rejected(self):
        self.session.get.return_value = FakeResponse(page_html(extra_head="<title>another title</title>").encode())
        self.assert_error("SecurityMismatch")

    def test_missing_title_rejected(self):
        text = page_html().replace("<title>示例公司(600875) 概念题材_F10_同花顺金融服务网</title>", "")
        self.session.get.return_value = FakeResponse(text.encode())
        self.assert_error("SecurityMismatch")

    def test_hidden_code_mismatch_rejected(self):
        text = page_html().replace('id="stockCode" value="600875"', 'id="stockCode" value="600876"')
        self.session.get.return_value = FakeResponse(text.encode())
        self.assert_error("SecurityMismatch")

    def test_hidden_market_mismatch_rejected(self):
        self.session.get.return_value = FakeResponse(page_html(market_id="33").encode())
        self.assert_error("SecurityMismatch")

    def test_duplicate_security_identity_inputs_rejected(self):
        for ident, value in (("stockCode", "600875"), ("marketId", "17")):
            with self.subTest(ident=ident):
                extra = f'<input id="{ident}" value="{value}">'
                self.session.get.return_value = FakeResponse(page_html(extra_body=extra).encode())
                self.assert_error("SecurityMismatch")

    def test_identity_must_be_input_not_similar_element(self):
        text = page_html().replace('<input type="hidden" id="stockCode" value="600875">',
                                   '<div id="stockCode" value="600875"></div>')
        self.session.get.return_value = FakeResponse(text.encode())
        self.assert_error("SecurityMismatch")

    def test_missing_identity_input_rejected(self):
        text = page_html().replace('<input type="hidden" id="marketId" value="17">', "")
        self.session.get.return_value = FakeResponse(text.encode())
        self.assert_error("SecurityMismatch")

    def test_gbk_gb2312_and_gb18030_pages_decode_without_replacement(self):
        for charset in ("gbk", "gb2312", "gb18030"):
            with self.subTest(charset=charset):
                self.session.get.return_value = FakeResponse(page_html(charset=charset).encode(charset))
                _, evidence = self.get_page()
                self.assertIn("示例公司", evidence["title"])

    def test_missing_or_unknown_charset_rejected(self):
        for text in (page_html().replace('<meta charset="utf-8">', ""), page_html(charset="latin1")):
            with self.subTest(text=text[:60]):
                self.session.get.return_value = FakeResponse(text.encode())
                self.assert_error("InvalidEncoding")

    def test_malformed_utf8_rejected(self):
        self.session.get.return_value = FakeResponse(page_html().encode() + b"\xff")
        self.assert_error("InvalidEncoding")

    def test_malformed_gb18030_rejected(self):
        self.session.get.return_value = FakeResponse(page_html(charset="gb18030").encode("gb18030") + b"\x81")
        self.assert_error("InvalidEncoding")

    def test_embedded_nul_rejected(self):
        self.session.get.return_value = FakeResponse(page_html(extra_body="\x00").encode())
        self.assert_error("InvalidSchema")

    def test_provenance_keeps_true_observation_not_report_date(self):
        started = dt.datetime(2026, 9, 10, 0, 0, 0, tzinfo=dt.timezone.utc)
        observed = dt.datetime(2026, 9, 10, 0, 0, 2, tzinfo=dt.timezone.utc)
        clock = mock.Mock()
        clock.timezone = dt.timezone
        clock.datetime.now.side_effect = [started, observed]
        raw = page_html().encode()
        self.session.get.return_value = FakeResponse(raw)
        with mock.patch.object(ths_source, "datetime", clock):
            _, evidence = self.get_page()
        self.assertEqual(evidence["collectionStartedAt"], started.isoformat())
        self.assertEqual(evidence["observedAt"], observed.isoformat())
        self.assertEqual(evidence["timeKind"], "observed_at")
        self.assertIsNone(evidence["sourcePublishedAt"])
        self.assertEqual(evidence["responseSha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(evidence["provider"], "ths_f10")
        self.assertEqual(evidence["status"], "validated")
        self.assertEqual(evidence["parserVersion"], 1)

    def test_diagnostic_whitelist_drops_untrusted_fields(self):
        safe = safe_source_diagnostic({"source": "concepts", "stage": "concept_page", "errorType": "HTTPStatus",
                                       "code": "600875", "attempt": 1, "httpStatus": 403,
                                       "provider": "fake", "body": "private", "token": "private",
                                       "url": "https://private.example/?secret=value"})
        self.assertEqual(safe, {"source": "concepts", "stage": "concept_page", "errorType": "HTTPStatus",
                               "code": "600875", "attempt": 1, "httpStatus": 403, "provider": "ths_f10"})


class ConceptParserTests(MockHttpTests):
    def get_concepts(self, text=None):
        if text is not None:
            self.session.get.return_value = FakeResponse(text.encode("utf-8"))
        with ThsConceptClient() as client:
            return client.concepts("600875")

    def reject_concepts(self, text):
        self.assert_error("InvalidSchema", lambda: self.get_concepts(text))

    def test_all_rows_categories_and_provenance_preserved(self):
        rows = concept_row() + concept_row(2, "高端装备", "000002")
        result = self.get_concepts(page_html(content=concept_section(rows=rows) + concept_section(other=True)))
        self.assertEqual(result["概念"], ["清洁能源", "高端装备"])
        self.assertEqual(result["其他概念"], ["融资融券"])
        evidence = result["evidence"]
        self.assertEqual(evidence["categoryIds"], {"概念": ["000001", "000002"], "其他概念": ["000201"]})
        self.assertEqual(evidence["taxonomy"], "ths_f10_current_concepts")
        self.assertEqual(evidence["categoryMapping"], {"概念": "常规概念", "其他概念": "其他概念"})
        self.assertEqual(evidence["scope"], "regular_and_other_concepts_only")
        self.assertEqual(evidence["provider"], "ths_f10")
        self.assertFalse(evidence["historicalMembershipVerified"])
        self.assertFalse(evidence["truncated"])
        self.assertNotIn("baidu", str(evidence).lower())

    def test_missing_category_section_rejected(self):
        self.reject_concepts(page_html(content=concept_section()))

    def test_duplicate_section_id_rejected(self):
        self.reject_concepts(page_html(content=concept_section() * 2 + concept_section(other=True)))

    def test_wrong_section_heading_rejected(self):
        self.reject_concepts(page_html().replace("<h2>常规概念</h2>", "<h2>最新新闻</h2>"))

    def test_duplicate_section_heading_rejected(self):
        self.reject_concepts(page_html().replace("<h2>常规概念</h2>", "<h2>常规概念</h2><h2>常规概念</h2>"))

    def test_missing_required_table_header_rejected(self):
        regular = concept_section(headers=["序号", "概念名称", "未知列"])
        self.reject_concepts(page_html(content=regular + concept_section(other=True)))

    def test_duplicate_header_must_not_appear_complete(self):
        row = concept_row().replace("公开分类说明</td>", "公开分类说明</td><td>多余重复名称</td>")
        regular = concept_section(rows=row, headers=["序号", "概念名称", "龙头股", "概念解析", "概念名称"])
        self.reject_concepts(page_html(content=regular + concept_section(other=True)))

    def test_row_cell_count_mismatch_rejected(self):
        row = concept_row().replace("<td>公开分类说明</td>", "")
        self.reject_concepts(page_html(content=concept_section(rows=row) + concept_section(other=True)))

    def test_nonsequential_row_numbers_rejected(self):
        rows = concept_row() + concept_row(3, "高端装备", "000002")
        self.reject_concepts(page_html(content=concept_section(rows=rows) + concept_section(other=True)))

    def test_duplicate_concept_names_rejected(self):
        rows = concept_row() + concept_row(2, "清洁能源", "000002")
        self.reject_concepts(page_html(content=concept_section(rows=rows) + concept_section(other=True)))

    def test_duplicate_concept_ids_rejected(self):
        rows = concept_row() + concept_row(2, "高端装备", "000001")
        self.reject_concepts(page_html(content=concept_section(rows=rows) + concept_section(other=True)))

    def test_duplicate_other_category_ids_rejected(self):
        rows = concept_row(1, "融资融券", "000201", other=True) + concept_row(2, "其他分类", "000201", other=True)
        self.reject_concepts(page_html(content=concept_section() + concept_section(other=True, rows=rows)))

    def test_missing_or_noncanonical_concept_id_rejected(self):
        for value in ("", "1", "1234567", "12ab56", "１２３４５６"):
            with self.subTest(value=value):
                self.reject_concepts(page_html(content=concept_section(rows=concept_row(source_id=value)) + concept_section(other=True)))

    def test_empty_and_control_character_names_rejected(self):
        for name in ("", "   ", "a\x01b", "a" * 129):
            with self.subTest(name=name):
                self.reject_concepts(page_html(content=concept_section(rows=concept_row(name=name)) + concept_section(other=True)))

    def test_200_row_limit_accepts_exact_limit(self):
        rows = "".join(concept_row(i, f"分类{i}", f"{i:06d}") for i in range(1, 201))
        result = self.get_concepts(page_html(content=concept_section(rows=rows) + concept_section(other=True)))
        self.assertEqual(len(result["概念"]), 200)
        self.assertFalse(result["evidence"]["truncated"])

    def test_over_200_rows_rejected_not_truncated(self):
        rows = "".join(concept_row(i, f"分类{i}", f"{i:06d}") for i in range(1, 202))
        self.reject_concepts(page_html(content=concept_section(rows=rows) + concept_section(other=True)))

    def test_expansion_detail_row_does_not_count_as_membership(self):
        detail = '<tr class="extend_content"><td colspan="4">附加说明</td></tr>'
        result = self.get_concepts(page_html(content=concept_section(rows=concept_row() + detail) + concept_section(other=True)))
        self.assertEqual(result["概念"], ["清洁能源"])

    def test_membership_disguised_as_expansion_must_not_be_silently_dropped(self):
        hidden = concept_row(2, "被漏掉的分类", "000002").replace("<tr>", '<tr class="extend_content">', 1)
        self.reject_concepts(page_html(content=concept_section(rows=concept_row() + hidden) + concept_section(other=True)))

    def test_empty_category_table_rejected(self):
        self.reject_concepts(page_html(content=concept_section(rows="") + concept_section(other=True)))

    def test_only_detail_rows_do_not_prove_empty_membership(self):
        detail = '<tr class="extend_content"><td colspan="4">暂无明细</td></tr>'
        self.reject_concepts(page_html(content=concept_section(rows=detail) + concept_section(other=True)))

    def test_missing_tbody_rejected(self):
        text = page_html().replace("<tbody>", "").replace("</tbody>", "")
        self.reject_concepts(text)

    def test_duplicate_direct_tbody_rejected(self):
        text = page_html().replace("</tbody>", "</tbody><tbody></tbody>", 1)
        self.reject_concepts(text)

    def test_second_table_in_section_rejected(self):
        text = page_html().replace("</table>", '</table><table class="gnContent"></table>', 1)
        self.reject_concepts(text)

    def test_name_cell_in_wrong_column_rejected(self):
        row = concept_row().replace('<td>1</td><td class="gnName"', '<td class="gnName"')
        self.reject_concepts(page_html(content=concept_section(rows=row) + concept_section(other=True)))

    def test_duplicate_name_cells_rejected(self):
        row = concept_row().replace("<td>公开分类说明</td>", '<td class="gnName" clid="000002">其他名称</td>')
        self.reject_concepts(page_html(content=concept_section(rows=row) + concept_section(other=True)))

    def test_valid_identity_but_error_page_body_rejected(self):
        self.reject_concepts(page_html(content="<p>访问失败，请稍后重试</p>"))

    def test_script_only_tables_are_not_accepted(self):
        content = "<script>" + concept_section() + concept_section(other=True) + "</script>"
        self.reject_concepts(page_html(content=content))

    def test_template_and_noscript_tables_are_not_accepted(self):
        for tag in ("template", "noscript"):
            with self.subTest(tag=tag):
                content = f"<{tag}>" + concept_section() + concept_section(other=True) + f"</{tag}>"
                self.reject_concepts(page_html(content=content))

    def test_script_injection_not_included_in_concept_name(self):
        name = "清洁能源<script>private injected text</script>"
        result = self.get_concepts(page_html(content=concept_section(rows=concept_row(name=name)) + concept_section(other=True)))
        self.assertEqual(result["概念"], ["清洁能源"])
        self.assertNotIn("private injected", str(result))


if __name__ == "__main__":
    unittest.main()
