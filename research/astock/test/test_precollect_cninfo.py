"""Offline regression for the 07:50 pre-collector's success path.

The staged times are real datetimes, so the summary line must serialize them as
ISO strings: a bare json.dumps used to raise after a valid file was already
written, which made the unit look failed even though the 08:00 run could still
consume the artifact. This drives main() end-to-end with fakes and asserts a
zero exit, valid JSON on stdout, and a readable staging file.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))


def configured_root(directory: str) -> Path:
    root = Path(directory)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "stocks.json").write_text(json.dumps({
        "schemaVersion": "1.0",
        "corePool": [
            {"code": "002617", "name": "露笑科技"},
            {"code": "600875", "name": "东方电气"},
            {"code": "300033", "name": "同花顺"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "config" / "workflow.json").write_text(json.dumps({
        "timezone": "Asia/Shanghai", "scheduledTime": "08:00",
    }), encoding="utf-8")
    return root


def announcement(code: str) -> dict:
    return {"title": f"{code} 半年度业绩说明会", "date": "2026-09-18",
            "dateKind": "source_disclosure_date",
            "observedAt": "2026-09-21T07:49:00+08:00", "nextDayDisclosure": False,
            "url": f"https://static.cninfo.com.cn/finalpage/2026-09-18/{code}.PDF"}


def summary_row() -> dict:
    return {"title": "2026年半年度报告摘要", "date": "2026-08-26",
            "dateKind": "source_disclosure_date",
            "observedAt": "2026-09-21T07:49:00+08:00", "nextDayDisclosure": False,
            "url": "https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF"}


def fake_document(code: str) -> dict:
    return {"status": "available", "kind": "financial_summary", "code": code,
            "title": "2026年半年度报告摘要",
            "url": "https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF",
            "disclosureDate": "2026-08-26", "reportDate": "2026-06-30",
            "observedAt": "2026-09-21T07:52:30+08:00",
            "dataAsOf": "2026-09-21T07:52:30+08:00", "pdfSha256": "b" * 64,
            "metrics": [{"field": "operatingCashFlow", "label": "经营活动产生的现金流量净额",
                         "currentToken": "1,000.00", "previousToken": "900.00",
                         "quote": "经营活动产生的现金流量净额 1,000.00 900.00",
                         "unitLabel": "元", "currency": "CNY", "pageNumber": 2,
                         "pageTextSha256": "c" * 64}]}


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def announcements(self, code, page_size=30, *, not_after=None, deadline=None):
        self.last_page_size = page_size
        rows = [announcement(code)]
        if code == "002617":
            rows.insert(0, summary_row())
        return rows

    def announcement_window(self, code, *, window_start, cutoff, page_size=30, max_pages=4, deadline=None):
        return {"rows": [], "queryCoverage": {
            "kind": "window", "windowStartAt": dt.datetime.combine(
                window_start, dt.time(0), cutoff.tzinfo).isoformat(),
            "cutoffAt": cutoff.isoformat(), "queriedAt": "2026-09-21T07:49:00+08:00",
            "observedThroughAt": "2026-09-21T07:49:00+08:00",
            "pageSize": page_size, "maxPages": max_pages, "pagesFetched": 1,
            "total": 0, "returned": 0, "complete": True,
            "reason": "all_query_pages_collected",
        }}


class PrecollectSuccessPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = configured_root(self.temp.name)
        base = dt.datetime(2026, 9, 21, 7, 50, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        times = iter([base, base, base, base,
                      base + dt.timedelta(minutes=1), base + dt.timedelta(minutes=1),
                      base + dt.timedelta(minutes=2), base + dt.timedelta(minutes=2),
                      base + dt.timedelta(minutes=3)])
        self.now = mock.Mock(side_effect=lambda: next(times))

    def run_main(self):
        import precollect_cninfo
        buffer = io.StringIO()
        with mock.patch.object(precollect_cninfo, "PROJECT_ROOT", self.root), \
                mock.patch.object(precollect_cninfo, "now_cst", self.now), \
                mock.patch.object(precollect_cninfo, "CninfoClient", FakeClient), \
                mock.patch.object(precollect_cninfo, "collect_summary",
                                  side_effect=lambda row, stock, cache, timeout_seconds: fake_document(stock["code"])), \
                contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = precollect_cninfo.main()
        return code, buffer.getvalue()

    def test_success_prints_valid_json_and_leaves_a_readable_artifact(self):
        import precollect_cninfo
        from cninfo_staging import load_staging
        code, output = self.run_main()
        self.assertEqual(code, 0, output)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "staged")
        self.assertEqual(summary["targetDate"], "2026-09-21")
        self.assertEqual(summary["batchCompletedAt"], "2026-09-21T07:53:00+08:00")
        # One real query instant per stock, serialized as strings.
        self.assertEqual(summary["queriedAt"], {
            "002617": "2026-09-21T07:50:00+08:00",
            "600875": "2026-09-21T07:51:00+08:00",
            "300033": "2026-09-21T07:52:00+08:00",
        })
        self.assertEqual(summary["documents"], ["002617"])
        # The written artifact is readable by the validator the 08:00 run uses.
        directory = precollect_cninfo.staging_directory(self.root)
        staged = load_staging(directory, "2026-09-21",
                              dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                              expected_codes=["002617", "600875", "300033"])
        self.assertIsNotNone(staged)
        self.assertEqual(staged["documents"]["002617"]["pdfSha256"], "b" * 64)
        self.assertTrue(staged["codes"]["002617"]["windowQuery"]["queryCoverage"]["complete"])
        self.assertEqual(staged["codes"]["002617"]["windowQuery"]["queryCoverage"]["cutoffAt"],
                         "2026-09-21T08:00:00+08:00")

    def test_weekend_still_exits_zero_without_writing(self):
        import precollect_cninfo
        saturday = dt.datetime(2026, 9, 19, 7, 50, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        buffer = io.StringIO()
        with mock.patch.object(precollect_cninfo, "PROJECT_ROOT", self.root), \
                mock.patch.object(precollect_cninfo, "now_cst", lambda: saturday), \
                mock.patch.object(precollect_cninfo, "CninfoClient", FakeClient), \
                contextlib.redirect_stdout(buffer):
            code = precollect_cninfo.main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "skipped_non_weekday")
        self.assertFalse(precollect_cninfo.staging_path(
            precollect_cninfo.staging_directory(self.root), "2026-09-19").exists())


class PrecollectBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = configured_root(self.temp.name)
        self.cutoff = dt.datetime(2026, 9, 21, 8, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    def run_main(self, times, monotonic=None):
        import precollect_cninfo
        client = FakeClient()
        client.announcements = mock.Mock(wraps=client.announcements)
        client.announcement_window = mock.Mock(wraps=client.announcement_window)
        buffer = io.StringIO()
        clock_context = (mock.patch.object(precollect_cninfo.time, "monotonic", side_effect=monotonic)
                         if monotonic is not None else contextlib.nullcontext())
        with mock.patch.object(precollect_cninfo, "PROJECT_ROOT", self.root), \
                mock.patch.object(precollect_cninfo, "now_cst", side_effect=times), \
                mock.patch.object(precollect_cninfo, "CninfoClient", return_value=client) as factory, \
                mock.patch.object(precollect_cninfo, "collect_summary",
                                  side_effect=lambda row, stock, cache, timeout_seconds: fake_document(stock["code"])) as document, \
                clock_context, contextlib.redirect_stdout(buffer):
            code = precollect_cninfo.main()
        return code, json.loads(buffer.getvalue()), client, document, factory

    def assert_no_artifact(self):
        import precollect_cninfo
        self.assertFalse(precollect_cninfo.staging_path(
            precollect_cninfo.staging_directory(self.root), "2026-09-21").exists())

    def test_start_at_cutoff_never_creates_a_client_or_starts_queries(self):
        code, output, client, document, factory = self.run_main([self.cutoff])
        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "held_after_cutoff")
        factory.assert_not_called()
        client.announcements.assert_not_called()
        document.assert_not_called()
        self.assert_no_artifact()

    def test_cutoff_reached_before_first_stock_does_not_start_history(self):
        early = self.cutoff - dt.timedelta(seconds=1)
        code, output, client, document, _ = self.run_main([early, self.cutoff])
        self.assertEqual(code, 1)
        client.announcements.assert_not_called()
        client.announcement_window.assert_not_called()
        document.assert_not_called()
        self.assert_no_artifact()

    def test_history_finishing_after_cutoff_starts_no_window_or_next_stock(self):
        early = self.cutoff - dt.timedelta(seconds=1)
        late = self.cutoff + dt.timedelta(seconds=1)
        code, output, client, document, _ = self.run_main([early, early, late])
        self.assertEqual(code, 1)
        self.assertEqual(client.announcements.call_count, 1)
        self.assertEqual(client.announcements.call_args.kwargs["not_after"], self.cutoff)
        self.assertIsInstance(client.announcements.call_args.kwargs["deadline"], float)
        client.announcement_window.assert_not_called()
        document.assert_not_called()
        self.assert_no_artifact()

    def test_window_finishing_after_cutoff_starts_no_pdf_or_next_stock(self):
        early = self.cutoff - dt.timedelta(seconds=1)
        late = self.cutoff + dt.timedelta(seconds=1)
        code, output, client, document, _ = self.run_main([early, early, early, late])
        self.assertEqual(code, 1)
        self.assertEqual(client.announcements.call_count, 1)
        self.assertEqual(client.announcement_window.call_count, 1)
        document.assert_not_called()
        self.assert_no_artifact()

    def test_monotonic_budget_exhaustion_starts_no_second_stock(self):
        early = self.cutoff - dt.timedelta(minutes=10)
        code, output, client, document, _ = self.run_main(
            [early] * 5, monotonic=[100.0, 101.0, 102.0, 103.0, 281.0])
        self.assertEqual(code, 1)
        self.assertEqual(client.announcements.call_count, 1)
        self.assertEqual(client.announcement_window.call_count, 1)
        self.assertEqual(document.call_count, 1)
        self.assert_no_artifact()

    def test_pdf_budget_is_limited_by_remaining_cutoff_seconds(self):
        offsets = [10, 10, 9, 5, 4, 3, 2, 1, 0.5]
        times = [self.cutoff - dt.timedelta(seconds=offset) for offset in offsets]
        code, output, client, document, _ = self.run_main(times)
        self.assertEqual(code, 0, output)
        self.assertEqual(document.call_args.kwargs["timeout_seconds"], 5.0)
        self.assertEqual(output["status"], "staged")


if __name__ == "__main__":
    unittest.main()
