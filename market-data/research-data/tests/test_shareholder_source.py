"""Synthetic HTML and mocked transport only; no live supplier or database."""
import copy
import socket
import subprocess
import unittest
from unittest import mock

from bs4 import BeautifulSoup

import shareholder_source as source


def evidence():
    return {"provider": "ths_f10", "sourceUrl": "https://basic.10jqka.com.cn/002617/holder.html",
            "code": "002617", "market": "SZ", "observedAt": "2026-09-10T00:00:00+00:00",
            "responseSha256": "a" * 64, "parserVersion": 1,
            "title": "测试公司(002617) 股东研究_F10_同花顺金融服务网",
            "timeKind": "observed_at", "sourcePublishedAt": None, "status": "validated"}


def section(prefix="fher", count=2, malformed_later=False, long_names=False):
    flow = prefix == "fher"
    ident, heading, date_class = ("flowholder", "十大流通股东", "fdates") if flow else ("tenholder", "十大股东", "tdates")
    dates = ["2026-06-30", "2026-03-31"] if flow else ["2026-08-21", "2026-06-30"]
    ratio = "占流通股比例" if flow else "占总股本比例"
    caption = "前十大流通股东累计持有" if flow else "主要股东持股变动公告"
    tabs = "".join(f'<li name="{prefix}_{i}" class="{"cur" if i == 1 else ""}"><a class="{date_class}" targ="{prefix}_{i}">{day}</a></li>' for i, day in enumerate(dates, 1))
    rows = []
    for index in range(count):
        name = ("示例机构" * 80 if long_names else "示例机构") + str(index)
        later = '<td>不变<td>流通A股</td><td>详情</td>' if malformed_later else '<td>不变</td><td>流通A股</td><td>详情</td>'
        rows.append(f'<tr><th class="holder_th"><a>{name}</a></th><td>9545.86万</td><td>不变</td><td>5.08%</td>{later}</tr>')
    return f'''<div id="{ident}"><h2>{heading}</h2><div class="m_tab"><ul>{tabs}</ul></div>
      <div id="{prefix}_1" class="m_tab_content2"><table class="m_table m_hl ggintro">
        <caption>{caption}</caption><thead><tr><th>机构或基金名称</th><th>持有数量(股)</th><th>持股变化(股)</th><th>{ratio}</th><th>变动比例</th><th>股份类型</th><th>详情</th></tr></thead>
        <tbody>{''.join(rows)}</tbody><div><tbody></tbody></div></table></div>
      <div id="{prefix}_2" class="m_tab_content2"><div>Historical content is not excerpted.</div></div></div>'''


def soup(**kwargs):
    return BeautifulSoup(section(**kwargs) + section("ther", count=1), "html.parser")


class ShareholderSourceTests(unittest.TestCase):
    def setUp(self):
        for obj, name in [(socket.socket, "connect"), (socket, "create_connection"), (subprocess, "run"), (subprocess, "Popen")]:
            patch = mock.patch.object(obj, name, side_effect=AssertionError("external operation forbidden"))
            patch.start(); self.addCleanup(patch.stop)

    def parse(self, html=None, meta=None):
        return source.ShareholderClient.parse(soup() if html is None else html, evidence() if meta is None else meta)

    def rejected(self, html=None, meta=None, error_type="InvalidSchema"):
        with self.assertRaises(source.SourceError) as caught:
            self.parse(html, meta)
        self.assertEqual(caught.exception.diagnostic["source"], "insiders")
        self.assertEqual(caught.exception.diagnostic["stage"], "holder_page")
        self.assertEqual(caught.exception.diagnostic["errorType"], error_type)
        return caught.exception

    def test_latest_two_tables_and_truthful_date_and_history_scope(self):
        parsed = self.parse()
        meta = parsed["evidence"]
        self.assertEqual(meta["reportDates"], ["2026-08-21", "2026-06-30"])
        self.assertEqual([item["rowCount"] for item in meta["tableSummaries"]], [2, 1])
        self.assertEqual(meta["tableSummaries"][1]["tableKind"], "holdings_change_announcement")
        self.assertEqual(meta["reportDateMeaning"], "source_table_date_not_first_observed_time")
        self.assertFalse(meta["completeHistory"])
        self.assertIsNone(meta["sourcePublishedAt"])
        self.assertFalse(meta["truncated"])
        self.assertEqual(meta["fullTextChars"], len(parsed["text"]))
        self.assertIn("主要股东持股变动公告", parsed["text"])
        self.assertNotIn("Historical", parsed["text"])

    def test_actual_later_unclosed_cell_and_nested_empty_tbody_are_ignored(self):
        result = self.parse(soup(malformed_later=True))
        self.assertIn("9545.86万 | 5.08%", result["text"])
        self.assertNotIn("股份类型", result["text"])

    def test_scripts_styles_and_comments_cannot_supply_content(self):
        value = soup()
        cell = value.select_one("#fher_1 tbody th")
        cell.append(BeautifulSoup('<script>token=SECRET</script><style>secret</style><!--secret-->', 'html.parser'))
        self.assertNotIn("SECRET", self.parse(value)["text"])
        self.assertNotIn("secret", self.parse(value)["text"])
        cell.clear(); cell.append(BeautifulSoup('<script>Fake shareholder</script>', 'html.parser'))
        self.rejected(value)

    def test_excerpt_limit_and_full_length_are_explicit(self):
        result = self.parse(soup(count=10, long_names=True))
        self.assertEqual(len(result["text"]), 3000)
        self.assertGreater(result["evidence"]["fullTextChars"], 3000)
        self.assertTrue(result["evidence"]["truncated"])

    def test_does_not_modify_soup_or_common_evidence(self):
        html, meta = soup(), evidence()
        original_html, original_meta = str(html), copy.deepcopy(meta)
        self.parse(html, meta)
        self.assertEqual(str(html), original_html)
        self.assertEqual(meta, original_meta)

    def test_missing_section_heading_tabs_panel_or_table_rejected(self):
        for selector in ("#flowholder", "#tenholder h2", "#flowholder .m_tab", "#fher_1", "#ther_1 table", "#fher_1 caption", "#fher_1 thead", "#fher_1 > table > tbody"):
            with self.subTest(selector=selector):
                html = soup(); html.select_one(selector).decompose(); self.rejected(html)

    def test_title_only_and_numeric_caption_do_not_prove_table(self):
        html = soup(); html.select_one("#fher_1 > table > tbody").clear()
        html.select_one("#fher_1 caption").string = "前十大流通股东累计持有 999999万股"
        self.rejected(html)

    def test_date_tab_binding_and_latest_selection_are_strict(self):
        for attr, value in (("name", "fher_2"), ("class", [])):
            html = soup(); html.select_one("#flowholder li")[attr] = value
            self.rejected(html)
        html = soup(); html.select_one("#flowholder a.fdates")["targ"] = "ther_1"
        self.rejected(html)
        html = soup(); html.select("#flowholder li")[1]["class"] = ["cur"]
        self.rejected(html)

    def test_dates_require_real_iso_dates_strict_order_and_no_future(self):
        for value in ("未知", "20260630", "2026-02-30"):
            html = soup(); html.select_one("a.fdates").string = value; self.rejected(html)
        for value in ("2026-06-30", "2026-07-01"):
            html = soup(); html.select("a.fdates")[1].string = value; self.rejected(html)
        html = soup(); html.select_one("a.tdates").string = "2026-09-11"
        self.rejected(html, error_type="FutureReportDate")

    def test_observed_date_is_aware_and_china_local(self):
        html = soup(); html.select_one("a.tdates").string = "2026-09-10"
        self.parse(html, dict(evidence(), observedAt="2026-09-09T16:00:00+00:00"))
        self.rejected(html, dict(evidence(), observedAt="2026-09-09T15:59:59+00:00"), "FutureReportDate")
        for when in ("2026-09-10T08:00:00", "bad", None):
            self.rejected(meta=dict(evidence(), observedAt=when))

    def test_header_order_and_ratio_kind_cannot_shift(self):
        for selector, text in (("#fher_1 thead th:nth-child(2)", "持股变化(股)"), ("#ther_1 thead th:nth-child(4)", "占流通股比例")):
            html = soup(); html.select_one(selector).string = text; self.rejected(html)

    def test_quantity_and_percentage_must_be_valid_finite_values(self):
        for position, values in ((2, ["--", "暂无", "0", "-5", "NaN", "1e9", "1,23万"]), (4, ["--", "1", "101%", "-1%", "nan%"])):
            for value in values:
                with self.subTest(position=position, value=value):
                    html = soup(); html.select_one(f"#fher_1 tbody tr > :nth-child({position})").string = value
                    self.rejected(html)

    def test_duplicate_names_or_summary_rows_and_excess_rows_rejected(self):
        html = soup(); cells = html.select("#fher_1 tbody th"); cells[1].string = cells[0].get_text()
        self.rejected(html)
        html = soup(); html.select_one("#fher_1 tbody th").string = "合计"; self.rejected(html)
        self.rejected(soup(count=11))

    def test_nested_or_spanned_selected_cells_are_ambiguous(self):
        html = soup(); html.select_one("#fher_1 tbody th")["colspan"] = "2"; self.rejected(html)
        html = soup(); html.select_one("#fher_1 tbody tr > td").append(BeautifulSoup("<td>6%</td>", "html.parser")); self.rejected(html)

    def test_duplicate_sections_panels_tables_are_rejected(self):
        for selector in ("#flowholder", "#fher_1", "#ther_1 table"):
            html = soup()
            parent = html.select_one("#ther_1") if selector.endswith("table") else html
            parent.append(copy.deepcopy(html.select_one(selector)))
            self.rejected(html)

    def test_identity_provenance_cannot_be_relabelled(self):
        for key, value in [("code", "600875"), ("market", "SH"), ("provider", "baidu"), ("sourceUrl", "https://example.test/holder"), ("title", "最新提示"), ("parserVersion", True), ("responseSha256", "bad"), ("status", "partial"), ("timeKind", "report_date")]:
            self.rejected(meta=dict(evidence(), **{key: value}))

    def test_error_does_not_expose_source_names_or_body(self):
        html = soup(); html.select_one("#fher_1 tbody tr > td").string = "token=SECRET"
        self.assertNotIn("SECRET", str(self.rejected(html)))

    def test_context_closes_session_on_success_and_parse_failure(self):
        for html in (soup(), BeautifulSoup("<h2>股东研究</h2>", "html.parser")):
            session = mock.Mock(headers={})
            with mock.patch("ths_source.requests.Session", return_value=session), mock.patch.object(source.ShareholderClient, "page", return_value=(html, evidence())) as page:
                try:
                    with source.ShareholderClient() as client:
                        self.assertEqual(client.source, "insiders")
                        client.shareholders("002617")
                except source.SourceError:
                    pass
                session.close.assert_called_once()
                page.assert_called_once_with("002617", "holder")

    def test_transport_error_is_preserved_and_session_closed(self):
        session = mock.Mock(headers={})
        error = source.SourceError("insiders", "holder_page", "HTTPStatus", "002617", http_status=403)
        with mock.patch("ths_source.requests.Session", return_value=session), mock.patch.object(source.ShareholderClient, "page", side_effect=error):
            with self.assertRaises(source.SourceError) as caught:
                with source.ShareholderClient() as client: client.shareholders("002617")
        self.assertIs(caught.exception, error)
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
