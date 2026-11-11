from __future__ import annotations

import io
import json
import math
import sys
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = PROJECT_ROOT / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from stock_research_provider import (  # noqa: E402
    MAX_CAPITAL_FLOW_LIMIT,
    MAX_NEWS_LIMIT,
    MAX_PRICE_LIMIT,
    MAX_SECTOR_LIMIT,
    StockResearchProvider,
    normalize_stock_symbol,
    sanitize_json,
)


CN = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


class Scalar:
    """Small numpy-like scalar used to test JSON cleaning without numpy."""

    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class FakeAkShare:
    def __init__(self):
        self.price_call = None
        self.financial_call = None
        self.institution_call = None
        self.flow_call = None

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        self.price_call = {
            "symbol": symbol,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
        }
        first = date(2024, 1, 1)
        rows = []
        for index in range(70):
            day = first + timedelta(days=index)
            close = 10 + index / 10
            rows.append(
                {
                    "日期": day,
                    "股票代码": symbol,
                    "开盘": close - 0.1,
                    "收盘": Scalar(close),
                    "最高": close + 0.2,
                    "最低": close - 0.2,
                    "成交量": 1_000 + index,
                    "成交额": (1_000 + index) * close,
                    "振幅": 2.0,
                    "涨跌幅": 1.0,
                    "涨跌额": 0.1,
                    "换手率": float("nan") if index == 69 else 0.5,
                }
            )
        print("fake-akshare-diagnostic")
        return rows

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        self.financial_call = (symbol, indicator)
        return [
            {
                "REPORT_DATE": "2024-06-30",
                "REPORT_DATE_NAME": "2024中报",
                "NOTICE_DATE": "2024-08-30 18:00:00",
                "UPDATE_DATE": "2024-08-30 18:30:00",
                "TOTALOPERATEREVE": 2_000,
                "PARENTNETPROFIT": 200,
                "EPSJB": 0.2,
            },
            {
                "REPORT_DATE": "2024-03-31",
                "REPORT_DATE_NAME": "2024一季报",
                "NOTICE_DATE": "2024-04-20 18:00:00",
                "UPDATE_DATE": "2024-04-20 18:30:00",
                "TOTALOPERATEREVE": 1_000,
                "TOTALOPERATEREVETZ": 10,
                "PARENTNETPROFIT": 100,
                "PARENTNETPROFITTZ": 12,
                "KCFJCXSYJLR": 90,
                "KCFJCXSYJLRTZ": 11,
                "EPSJB": 0.1,
                "ROEJQ": 3,
                "XSMLL": 30,
                "XSJLL": 10,
                "ZCFZL": 40,
            },
        ]

    def stock_institute_hold_detail(self, stock, quarter):
        self.institution_call = (stock, quarter)
        return [
            {
                "持股机构类型": "基金",
                "持股机构代码": "000001",
                "持股机构简称": "示例基金",
                "持股机构全称": "示例基金管理有限公司",
                "持股数": 100,
                "最新持股数": 110,
                "持股比例": 1.1,
                "最新持股比例": 1.2,
                "占流通股比例": 1.3,
                "最新占流通股比例": 1.4,
                "持股比例增幅": 0.1,
                "占流通股比例增幅": 0.1,
            }
        ]

    def stock_main_stock_holder(self, stock):
        return [
            {
                "编号": 1,
                "股东名称": "第一大股东",
                "持股数量": 1_000_000,
                "持股比例": 20,
                "股本性质": "流通A股",
                "截至日期": "2024-03-31",
                "公告日期": "2024-04-22",
                "股东总数": 10_000,
                "平均持股数": 100,
            },
            {
                "编号": 1,
                "股东名称": "未来股东",
                "持股数量": 2_000_000,
                "持股比例": 30,
                "股本性质": "流通A股",
                "截至日期": "2024-06-30",
                "公告日期": "2024-08-31",
            },
        ]

    def stock_news_em(self, symbol):
        return [
            {
                "新闻标题": "截止前新闻",
                "新闻内容": "媒体报道内容",
                "发布时间": "2024-04-30 09:00:00",
                "文章来源": "测试媒体",
                "新闻链接": "https://finance.eastmoney.com/a/past.html",
            },
            {
                "新闻标题": "截止后新闻",
                "新闻内容": "未来内容",
                "发布时间": "2024-05-02 09:00:00",
                "文章来源": "测试媒体",
                "新闻链接": "https://example.com/future",
            },
            {
                "新闻标题": "无时间新闻",
                "新闻内容": "不可核验",
                "发布时间": None,
                "文章来源": "测试媒体",
                "新闻链接": "javascript:alert(1)",
            },
        ]

    def stock_board_industry_name_em(self):
        return [
            {
                "排名": 2,
                "板块名称": "软件开发",
                "板块代码": "BK0737",
                "最新价": 100,
                "涨跌额": 2,
                "涨跌幅": 2,
                "总市值": 1_000,
                "换手率": 3,
                "上涨家数": 20,
                "下跌家数": 2,
                "领涨股票": "示例A",
                "领涨股票-涨跌幅": 10,
            },
            {
                "排名": 1,
                "板块名称": "银行",
                "板块代码": "BK0475",
                "最新价": 90,
                "涨跌额": 3,
                "涨跌幅": 3,
                "总市值": 2_000,
                "换手率": 1,
                "上涨家数": 30,
                "下跌家数": 1,
                "领涨股票": "示例B",
                "领涨股票-涨跌幅": 8,
            },
        ]

    def stock_board_concept_name_em(self):
        return [{"排名": 1, "板块名称": "人工智能", "板块代码": "BK0001", "涨跌幅": 5}]

    def stock_individual_fund_flow(self, stock, market):
        self.flow_call = (stock, market)
        return [
            {
                "日期": "2024-04-29",
                "收盘价": 10,
                "涨跌幅": 1,
                "主力净流入-净额": 100,
                "主力净流入-净占比": 2,
                "超大单净流入-净额": 30,
                "超大单净流入-净占比": 0.6,
                "大单净流入-净额": 70,
                "大单净流入-净占比": 1.4,
                "中单净流入-净额": -50,
                "中单净流入-净占比": -1,
                "小单净流入-净额": -50,
                "小单净流入-净占比": -1,
            },
            {
                "日期": "2024-04-30",
                "收盘价": 11,
                "涨跌幅": 10,
                "主力净流入-净额": 200,
                "主力净流入-净占比": 4,
            },
        ]

    def stock_individual_fund_flow_rank(self, indicator):
        return [
            {
                "序号": 1,
                "代码": 1,
                "名称": "平安银行",
                "最新价": 11,
                f"{indicator}涨跌幅": 2,
                f"{indicator}主力净流入-净额": 1_000,
                f"{indicator}主力净流入-净占比": 3,
            }
        ]


class FailingAkShare:
    def __getattr__(self, name):
        def fail(**kwargs):
            raise RuntimeError(f"forced failure: {name}")

        return fail


class TxFallbackAkShare(FakeAkShare):
    def __init__(self):
        super().__init__()
        self.tx_price_call = None

    def stock_zh_a_hist(self, **kwargs):
        raise ConnectionError("forced primary failure")

    def stock_zh_a_hist_tx(self, symbol, start_date, end_date, adjust):
        self.tx_price_call = {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
        }
        first = date(2024, 1, 1)
        return [
            {
                "date": first + timedelta(days=index),
                "open": 10 + index / 10,
                "close": 10.1 + index / 10,
                "high": 10.2 + index / 10,
                "low": 9.9 + index / 10,
                "volume": 100_000 + index * 100,
                "turnover": 0.005,
                "amount": 1_000_000 + index,
            }
            for index in range(70)
        ]


class StockResearchProviderTests(unittest.TestCase):
    def setUp(self):
        # The injected fake API must stay usable in a minimal offline Python
        # without requests installed.  Production keeps the real 15-second
        # timeout context; its implementation is covered by the morning
        # provider tests.
        timeout_patch = patch(
            "akshare_provider._requests_default_timeout",
            side_effect=lambda: nullcontext(),
        )
        timeout_patch.start()
        self.addCleanup(timeout_patch.stop)
        self.now = lambda: datetime(2025, 1, 15, 8, 0, tzinfo=CN)
        self.api = FakeAkShare()
        self.provider = StockResearchProvider(self.api, clock=self.now)

    def assert_envelope(self, response, tool):
        self.assertEqual(response["schemaVersion"], "1.0")
        self.assertEqual(response["tool"], tool)
        self.assertEqual(response["provider"], "akshare")
        self.assertIn(response["status"], {"live", "partial", "unavailable"})
        for key in ("source", "dataAsOf", "units", "data", "sources", "warnings"):
            self.assertIn(key, response)
        json.dumps(response, ensure_ascii=False, allow_nan=False)

        source_ids = {source["id"] for source in response["sources"]}
        referenced = set()

        def collect(value):
            if isinstance(value, dict):
                referenced.update(value.get("sourceIds") or [])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(response["data"])
        self.assertEqual(referenced, source_ids)

    def test_price_returns_bounded_k_lines_and_indicators_from_full_sample(self):
        protocol_stdout = io.StringIO()
        diagnostics = io.StringIO()
        with redirect_stdout(protocol_stdout), redirect_stderr(diagnostics):
            response = self.provider.get_stock_price(
                "600519.SH",
                start_date="2024-01-01",
                end_date="2024-04-30",
                limit=5,
                adjust="qfq",
            )

        self.assert_envelope(response, "get_stock_price")
        self.assertEqual(protocol_stdout.getvalue(), "")
        self.assertIn("fake-akshare-diagnostic", diagnostics.getvalue())
        self.assertEqual(self.api.price_call["symbol"], "600519")
        self.assertEqual(self.api.price_call["start_date"], "20240101")
        self.assertEqual(response["status"], "live")
        self.assertEqual(response["data"]["count"], 5)
        self.assertEqual(len(response["data"]["technicalIndicators"]["series"]), 5)
        latest = response["data"]["technicalIndicators"]["latest"]
        self.assertIsNotNone(latest["ma60"])
        self.assertIsNotNone(latest["rsi14"])
        self.assertIsNone(response["data"]["prices"][-1]["turnoverRate"])
        self.assertEqual(response["units"]["prices"]["volume"], "lot_100_shares")

    def test_current_unfinished_daily_bar_is_timestamped_and_marked_partial(self):
        provider = StockResearchProvider(
            self.api,
            clock=lambda: datetime(2024, 3, 10, 10, 0, tzinfo=CN),
        )
        response = provider.get_stock_price(
            "600519.SH",
            start_date="2024-01-01",
            end_date="2024-03-10",
            limit=120,
        )
        latest = response["data"]["prices"][-1]
        self.assertEqual(response["status"], "partial")
        self.assertEqual(latest["sessionStatus"], "intraday_partial")
        self.assertEqual(latest["dataAsOf"], "2024-03-10T10:00:00+08:00")
        self.assertTrue(any("盘中未收盘" in warning for warning in response["warnings"]))

    def test_price_falls_back_to_tencent_with_explicit_partial_provenance(self):
        api = TxFallbackAkShare()
        provider = StockResearchProvider(api, clock=self.now)
        response = provider.get_stock_price(
            "600519.SH",
            start_date="2024-01-01",
            end_date="2024-04-30",
            limit=5,
            adjust="qfq",
        )

        self.assert_envelope(response, "get_stock_price")
        self.assertEqual(response["status"], "partial")
        self.assertEqual(api.tx_price_call["symbol"], "sh600519")
        self.assertEqual(api.tx_price_call["start_date"], "20240101")
        self.assertEqual(response["source"], "AkShare.stock_zh_a_hist_tx")
        self.assertIn("gu.qq.com", response["sources"][0]["url"])
        self.assertEqual(response["data"]["prices"][-1]["volume"], 1_069)
        self.assertEqual(response["data"]["prices"][-1]["turnover"], 1_000_069)
        self.assertIsNone(response["data"]["prices"][-1]["turnoverRate"])
        self.assertTrue(any("回退到" in warning for warning in response["warnings"]))
        self.assertTrue(any("由股换算为手" in warning for warning in response["warnings"]))

    def test_strict_symbol_date_enum_and_limit_validation(self):
        self.assertEqual(normalize_stock_symbol("sh600519"), ("600519", "sh"))
        self.assertEqual(normalize_stock_symbol("000001.SZ"), ("000001", "sz"))
        self.assertEqual(normalize_stock_symbol("920001"), ("920001", "bj"))
        bad_symbols = ["600519;whoami", "../600519", " 600519", "600519.SH.exe", 600519, "123456", "sz600519"]
        for bad_symbol in bad_symbols:
            with self.subTest(symbol=bad_symbol), self.assertRaises(ValueError):
                self.provider.get_stock_price(bad_symbol)

        invalid_calls = [
            lambda: self.provider.get_stock_price("600519", start_date="2024/01/01"),
            lambda: self.provider.get_stock_price("600519", start_date="2024-02-01", end_date="2024-01-01"),
            lambda: self.provider.get_stock_price("600519", limit=True),
            lambda: self.provider.get_stock_price("600519", limit=MAX_PRICE_LIMIT + 1),
            lambda: self.provider.get_stock_price("600519", adjust="raw"),
            lambda: self.provider.get_news("600519", limit=MAX_NEWS_LIMIT + 1),
            lambda: self.provider.get_sector_rank("path/to/sector"),
            lambda: self.provider.get_sector_rank(limit=MAX_SECTOR_LIMIT + 1),
            lambda: self.provider.get_capital_flow(indicator="7日"),
            lambda: self.provider.get_capital_flow(limit=MAX_CAPITAL_FLOW_LIMIT + 1),
            lambda: self.provider.get_financial_report("600519", quarter="2024-01"),
        ]
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_financial_report_filters_point_in_time_and_labels_holdings_availability(self):
        response = self.provider.get_financial_report("600519", as_of="2024-05-01", quarter="2024Q1")

        self.assert_envelope(response, "get_financial_report")
        self.assertEqual(response["status"], "partial")
        self.assertEqual(self.api.financial_call, ("600519.SH", "按报告期"))
        self.assertEqual(self.api.institution_call, ("600519", "20241"))
        self.assertEqual(len(response["data"]["reports"]), 1)
        self.assertEqual(response["data"]["reports"][0]["reportDate"], "2024-03-31")
        self.assertEqual(response["data"]["reports"][0]["revenue"], 1_000)
        holding = response["data"]["institutionHoldings"][0]
        self.assertEqual(holding["dataAsOf"], "2024-03-31T15:00:00+08:00")
        self.assertEqual(holding["dateMeaning"], "holding_period_end")
        self.assertIsNone(holding["availabilityAsOf"])
        self.assertEqual(holding["availabilityStatus"], "unknown_no_publication_timestamp")
        self.assertTrue(any("不能证明" in warning for warning in response["warnings"]))
        self.assertEqual([item["name"] for item in response["data"]["majorShareholders"]], ["第一大股东"])

    def test_news_filters_future_and_undated_items_and_marks_aggregation(self):
        response = self.provider.get_news("600519", limit=10, as_of="2024-05-01")

        self.assert_envelope(response, "get_news")
        self.assertEqual(response["status"], "partial")
        self.assertEqual([item["title"] for item in response["data"]["items"]], ["截止前新闻"])
        self.assertFalse(response["data"]["officialAnnouncementsAvailable"])
        self.assertEqual(response["sources"][0]["sourceType"], "media_aggregation")
        self.assertEqual(response["sources"][0]["aggregator"], "东方财富")
        self.assertTrue(any("不等同于" in warning for warning in response["warnings"]))

    def test_sector_rank_and_both_capital_flow_modes(self):
        sector = self.provider.get_sector_rank("industry", limit=1)
        self.assert_envelope(sector, "get_sector_rank")
        self.assertEqual(sector["data"]["rankings"][0]["name"], "银行")

        history = self.provider.get_capital_flow("000001", indicator="5日", limit=1)
        self.assert_envelope(history, "get_capital_flow")
        self.assertEqual(self.api.flow_call, ("000001", "sz"))
        self.assertEqual(history["data"]["mode"], "stock_history")
        self.assertFalse(history["data"]["indicatorApplied"])
        self.assertEqual(history["data"]["items"][0]["date"], "2024-04-30")

        rank = self.provider.get_capital_flow(indicator="3日", limit=1)
        self.assert_envelope(rank, "get_capital_flow")
        self.assertEqual(rank["data"]["mode"], "market_ranking")
        self.assertTrue(rank["data"]["indicatorApplied"])
        self.assertEqual(rank["data"]["items"][0]["symbol"], "000001")
        self.assertEqual(rank["data"]["items"][0]["mainNetInflow"], 1_000)

    def test_upstream_failures_are_explicit_unavailable_responses(self):
        provider = StockResearchProvider(FailingAkShare(), clock=self.now)
        responses = [
            provider.get_stock_price("600519", start_date="2024-01-01", end_date="2024-02-01"),
            provider.get_financial_report("600519", as_of="2024-05-01", quarter="2024Q1"),
            provider.get_news("600519", as_of="2024-05-01"),
            provider.get_sector_rank(),
            provider.get_capital_flow("600519"),
        ]
        for response in responses:
            with self.subTest(tool=response["tool"]):
                self.assertEqual(response["status"], "unavailable")
                self.assertEqual(response["sources"], [])
                self.assertTrue(response["warnings"])
                json.dumps(response, allow_nan=False)

    def test_json_sanitizer_handles_non_finite_and_numpy_like_scalars(self):
        cleaned = sanitize_json(
            {
                "nan": float("nan"),
                "positiveInfinity": float("inf"),
                "negativeInfinity": -math.inf,
                "integer": Scalar(7),
                "date": date(2024, 1, 1),
            }
        )
        self.assertEqual(
            cleaned,
            {
                "nan": None,
                "positiveInfinity": None,
                "negativeInfinity": None,
                "integer": 7,
                "date": "2024-01-01",
            },
        )


if __name__ == "__main__":
    unittest.main()
