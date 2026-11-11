"""Synthetic parser/transport boundaries, not historical evidence."""
import datetime as dt
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from cninfo_evidence import collect_summary, _collect_summary, digest, normalize_text, parse_financial_summary, MAX_BYTES


def pages_fixture():
    texts = [
        "证券代码：002617 露笑科技2026年半年度报告摘要 本报告期 上年同期 单位：元 币种：人民币",
        "经营活动产生的现金流量净额 1,000.00 900.00 营业收入 2,000.00 1,800.00",
    ]
    return [{"pageNumber": i + 1, "text": t, "textSha256": digest(t.encode())} for i, t in enumerate(texts)]


def parse(pages):
    return parse_financial_summary(pages, "002617", "露笑科技", "2026年半年度报告摘要", "2026-08-26")


class EvidenceTests(unittest.TestCase):
    def test_literal_row_and_real_comparative_period(self):
        result = parse(pages_fixture())
        self.assertEqual(result["reportDate"], "2026-06-30")
        self.assertEqual(result["comparisonReportDate"], "2025-06-30")
        self.assertEqual(result["metrics"][0]["currentToken"], "1,000.00")
        self.assertEqual(result["metrics"][0]["pageNumber"], 2)

    def test_chinese_layout_whitespace_does_not_merge_amount_cells(self):
        text = normalize_text("经 营活动产生的现\n金流量净额 1,000.00\n900.00")
        self.assertEqual(text, "经营活动产生的现金流量净额 1,000.00 900.00")

    def test_inline_yuan_is_not_silently_relabelled_as_verified_currency(self):
        pages = pages_fixture()
        pages[0]["text"] = pages[0]["text"].replace("单位：元 币种：人民币", "")
        pages[1]["text"] = pages[1]["text"].replace("现金流量净额", "现金流量净额（元）")
        self.assertEqual(parse(pages)["metrics"][0]["currency"], "not_separately_stated")

    def test_non_yuan_header_without_row_unit_is_rejected(self):
        pages = pages_fixture(); pages[0]["text"] = pages[0]["text"].replace("单位：元", "单位：万元")
        with self.assertRaisesRegex(ValueError, "cash_flow_row_unverified"):
            parse(pages)

    def test_foreign_currency_is_not_mislabeled_as_absent_or_cny(self):
        pages = pages_fixture(); pages[0]["text"] = pages[0]["text"].replace("人民币", "美元")
        with self.assertRaisesRegex(ValueError, "unsupported_declared_currency"):
            parse(pages)

    def test_reversed_or_missing_comparative_header_is_rejected(self):
        pages = pages_fixture(); pages[0]["text"] = pages[0]["text"].replace("本报告期 上年同期", "上年同期 本报告期")
        with self.assertRaisesRegex(ValueError, "comparative_columns_unverified"):
            parse(pages)

    def test_restatement_columns_are_not_silently_read_as_two_periods(self):
        pages = pages_fixture()
        pages[0]["text"] += " 调整前 调整后"
        with self.assertRaisesRegex(ValueError, "unsupported_restatement_columns"):
            parse(pages)

    def test_whole_collection_timeout_kills_the_entire_worker_group(self):
        row = {"url": "https://static.cninfo.com.cn/finalpage/2026-08-26/0.PDF", "date": "2026-08-26"}
        process = MagicMock(); process.__enter__.return_value = process; process.pid = 12345
        process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 1), ("", "")]
        with patch("cninfo_evidence.subprocess.Popen", return_value=process) as spawn, patch("cninfo_evidence.os.killpg") as kill:
            with self.assertRaisesRegex(ValueError, "pdf_collection_timeout"):
                collect_summary(row, {"code": "002617", "name": "露笑科技"}, "/unused-fixture", timeout_seconds=1)
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            kill.assert_called_once_with(12345, signal.SIGKILL)

    def test_duplicate_cash_flow_rows_are_not_arbitrarily_selected(self):
        pages = pages_fixture(); pages.append(pages[1])
        with self.assertRaisesRegex(ValueError, "cash_flow_row_unverified"):
            parse(pages)

    def test_wrong_security_or_document_period_is_rejected(self):
        for old, new in [("002617", "300033"), ("露笑科技", "同花顺"), ("2026年", "2025年")]:
            pages = pages_fixture(); pages[0]["text"] = pages[0]["text"].replace(old, new)
            with self.subTest(old=old), self.assertRaises(ValueError):
                parse(pages)

    def test_future_report_period_and_invalid_disclosure_date_are_rejected(self):
        for date in ["2026-05-26", "2026-99-99"]:
            with self.subTest(date=date), self.assertRaises(ValueError):
                parse_financial_summary(pages_fixture(), "002617", "露笑科技", "2026年半年度报告摘要", date)

    def test_non_allowlisted_pdf_never_reaches_network(self):
        with tempfile.TemporaryDirectory() as root, patch("cninfo_evidence.build_cninfo_session") as session:
            for url in ["http://static.cninfo.com.cn/finalpage/2026-08-26/0.PDF", "https://example.com/0.PDF", "https://static.cninfo.com.cn@127.0.0.1/0.PDF"]:
                with self.subTest(url=url), self.assertRaisesRegex(ValueError, "untrusted_pdf_url"):
                    collect_summary({"url": url, "date": "2026-08-26"}, {}, root)
            session.assert_not_called()

    def test_download_keeps_actual_time_bytes_hash_and_query_observation_separate(self):
        row = {"title": "2026年半年度报告摘要", "date": "2026-08-26", "url": "https://static.cninfo.com.cn/finalpage/2026-08-26/0.PDF", "observedAt": "2026-08-26T08:00:00+08:00", "dateKind": "source_disclosure_date"}
        body = b"%PDF-1.7\nsynthetic transport fixture\n"
        response = MagicMock(); response.__enter__.return_value = response
        response.status_code = 200; response.headers = {}; response.iter_content.return_value = [body]
        session = MagicMock(); session.__enter__.return_value = session; session.get.return_value = response
        worker = MagicMock(returncode=0, stdout=json.dumps({"parserVersion": "cninfo-pdf-1", "libraryVersion": "6.18.1", "pages": pages_fixture()}))
        before = dt.datetime.now(dt.timezone.utc)
        with tempfile.TemporaryDirectory() as root, patch("cninfo_evidence.build_cninfo_session", return_value=session), patch("cninfo_evidence.subprocess.run", return_value=worker):
            doc = _collect_summary(row, {"code": "002617", "name": "露笑科技"}, root)
            self.assertEqual((Path(root) / f"{doc['pdfSha256']}.pdf").read_bytes(), body)
            self.assertEqual(doc["pdfSha256"], digest(body))
            self.assertEqual(doc["queryRecord"]["observedAt"], row["observedAt"])
            self.assertGreaterEqual(dt.datetime.fromisoformat(doc["observedAt"]), before)
            self.assertIsNone(doc["publishedAt"])
            self.assertFalse(session.trust_env)
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
            response.headers = {"Content-Length": str(MAX_BYTES + 1)}
            with self.assertRaisesRegex(ValueError, "pdf_byte_limit"):
                _collect_summary(row, {"code": "002617", "name": "露笑科技"}, root)


if __name__ == "__main__":
    unittest.main()
