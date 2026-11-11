from __future__ import annotations

import io
import json
import math
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker
import requests
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))

from akshare_bridge import run_bridge  # noqa: E402
from akshare_provider import (  # noqa: E402
    CACHE_FORMAT_VERSION,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    MAX_WARNING_ERROR_DETAIL_CHARS,
    AkShareMorningProvider,
    _warning_error_detail,
    normalize_stock_code,
    sanitize_json,
)


CN = ZoneInfo("Asia/Shanghai")


def request_fixture() -> dict:
    return {
        "schemaVersion": "1.0",
        "task": "a_share_morning_context",
        "targetDate": "2026-08-02",
        "timezone": "Asia/Shanghai",
        "requestedAt": "2026-08-02T08:00:00+08:00",
        "universe": [
            {"code": "002617", "exchange": "SZ", "symbol": "002617.SZ", "name": "露笑科技"},
            {"code": "600875", "exchange": "SH", "symbol": "600875.SH", "name": "东方电气"},
            {"code": "300033", "exchange": "SZ", "symbol": "300033.SZ", "name": "同花顺"},
        ],
        "requiredSections": [
            "trading_calendar",
            "overnight_global",
            "previous_a_share_session",
            "policy_and_calendar",
            "sectors",
            "capital_flows",
            "watchlist_market_data",
            "announcements_and_news",
        ],
    }


class FakeAkShare:
    def tool_trade_date_hist_sina(self):
        return [
            {"trade_date": "2026-07-30"},
            {"trade_date": "2026-07-31"},
            {"trade_date": "2026-08-03"},
            {"trade_date": "2026-08-04"},
        ]

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        self.stock_history_args = (symbol, start_date, end_date, adjust)
        series = {
            "sz002617": [
                {"date": "2026-07-30", "open": 11.9, "high": 12.1, "low": 11.8, "close": 12.0, "volume": 900, "amount": 9_000},
                {"date": "2026-07-31", "open": 12.1, "high": 12.5, "low": 11.9, "close": 12.3, "volume": 1_000, "amount": 12_000},
                {"date": "2026-08-03", "open": 12.2, "high": 12.25, "low": 12.0, "close": 12.1, "volume": 800, "amount": 9_700},
            ],
            "sh600875": [
                {"date": "2026-07-30", "open": 19.8, "high": 20.1, "low": 19.7, "close": 20.0, "volume": 1_900, "amount": 38_000},
                {"date": "2026-07-31", "open": 20.0, "high": 20.4, "low": 19.9, "close": 20.2, "volume": 2_000, "amount": 40_400},
                {"date": "2026-08-03", "open": 20.1, "high": 20.2, "low": 19.6, "close": 19.8, "volume": 2_100, "amount": 41_600},
            ],
            "sz300033": [
                {"date": "2026-07-30", "open": 99.0, "high": 100.5, "low": 98.5, "close": 100.0, "volume": 2_900, "amount": 290_000},
                {"date": "2026-07-31", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 3_000, "amount": 303_000},
                {"date": "2026-08-03", "open": 100.5, "high": 101.0, "low": 98.0, "close": 99.0, "volume": 3_100, "amount": 307_000},
            ],
        }
        return series[symbol]

    def stock_zh_index_daily(self, symbol):
        series = {
            "sh000001": [
                {"date": "2026-07-30", "open": 3790.0, "high": 3810.0, "low": 3780.0, "close": 3804.86, "volume": 10},
                {"date": "2026-07-31", "open": 3810.0, "high": 3840.0, "low": 3805.0, "close": 3832.262, "volume": 11},
                {"date": "2026-08-03", "open": 3825.0, "high": 3830.0, "low": 3795.0, "close": 3809.663, "volume": 12},
            ],
            "sz399001": [
                {"date": "2026-07-30", "open": 11950, "high": 12020, "low": 11900, "close": 12000, "volume": 20},
                {"date": "2026-07-31", "open": 12010, "high": 12150, "low": 12000, "close": 12120, "volume": 21},
                {"date": "2026-08-03", "open": 12100, "high": 12110, "low": 12000, "close": 12040, "volume": 22},
            ],
            "sz399006": [
                {"date": "2026-07-30", "open": 2480, "high": 2510, "low": 2470, "close": 2500, "volume": 30},
                {"date": "2026-07-31", "open": 2510, "high": 2550, "low": 2505, "close": 2540, "volume": 31},
                {"date": "2026-08-03", "open": 2530, "high": 2535, "low": 2490, "close": 2505, "volume": 32},
            ],
        }
        return series[symbol]

    def stock_sse_deal_daily(self, date):
        self.sse_summary_date = date
        return [{"单日情况": "成交金额", "主板A": 4_000, "科创板": 1_000, "主板B": 10}]

    def stock_szse_summary(self, date):
        self.szse_summary_date = date
        return [
            {"证券类别": "股票", "成交金额": 600_100_000_000},
            {"证券类别": "主板A股", "成交金额": 400_000_000_000},
            {"证券类别": "主板B股", "成交金额": 100_000_000},
            {"证券类别": "创业板A股", "成交金额": 200_000_000_000},
        ]

    def stock_market_activity_legu(self):
        return [
            {"item": "上涨", "value": 3_000},
            {"item": "下跌", "value": 2_000},
            {"item": "平盘", "value": 100},
            {"item": "统计日期", "value": "2026-07-31 15:00:00"},
        ]

    def stock_zh_a_spot(self):
        return [
            {"代码": "sz002617", "名称": "露笑科技", "最新价": 12.3, "涨跌额": 0.3, "涨跌幅": 2.5, "昨收": 12, "今开": 12.1, "最高": 12.5, "最低": 11.9, "成交量": 100, "成交额": 1_000, "时间戳": "15:30:00"},
            {"代码": "sh600875", "名称": "东方电气", "最新价": float("nan"), "涨跌额": -0.2, "涨跌幅": -1, "昨收": 20, "今开": 20, "最高": 20.1, "最低": 19.5, "成交量": 200, "成交额": 2_000, "时间戳": "15:30:00"},
            {"代码": "sz300033", "名称": "同花顺", "最新价": 100, "涨跌额": 0, "涨跌幅": 0, "昨收": 100, "今开": 100, "最高": 101, "最低": 99, "成交量": 300, "成交额": 3_000, "时间戳": "15:30:00"},
        ]

    def stock_zh_index_spot_sina(self):
        return [
            {"代码": "sh000001", "名称": "上证指数", "最新价": 3800, "涨跌额": 10, "涨跌幅": 0.3, "昨收": 3790, "今开": 3795, "最高": 3810, "最低": 3780, "成交量": 1, "成交额": 10},
            {"代码": "sz399001", "名称": "深证成指", "最新价": 12000, "涨跌额": 50, "涨跌幅": 0.4, "昨收": 11950, "今开": 11960, "最高": 12020, "最低": 11900, "成交量": 2, "成交额": 20},
            {"代码": "sz399006", "名称": "创业板指", "最新价": 2500, "涨跌额": -5, "涨跌幅": -0.2, "昨收": 2505, "今开": 2500, "最高": 2510, "最低": 2490, "成交量": 3, "成交额": 30},
        ]

    def stock_board_industry_summary_ths(self):
        return [
            {"序号": 1, "板块": "软件开发", "涨跌幅": 5.2, "总成交量": 20, "总成交额": 30, "净流入": 4, "上涨家数": 50, "下跌家数": 2, "领涨股": "示例A", "领涨股-最新价": 10, "领涨股-涨跌幅": 10},
            {"序号": 2, "板块": "煤炭", "涨跌幅": -1.2, "总成交量": 10, "总成交额": 15, "净流入": -2, "上涨家数": 2, "下跌家数": 30, "领涨股": "示例B", "领涨股-最新价": 8, "领涨股-涨跌幅": 1},
        ]

    def stock_lhb_detail_em(self, start_date, end_date):
        self.lhb_dates = (start_date, end_date)
        return [{"代码": 2617, "名称": "露笑科技", "上榜日": "2026-07-31", "解读": "测试", "收盘价": 12.3, "涨跌幅": 2.5, "龙虎榜净买额": 100, "龙虎榜买入额": 200, "龙虎榜卖出额": 100, "上榜原因": "测试原因"}]

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        code = symbol[:6]
        return [
            {
                "SECURITY_CODE": code,
                "REPORT_DATE": "2026-06-30",
                "REPORT_DATE_NAME": "2026中报",
                "NOTICE_DATE": "2026-08-02 07:00:00",
                "UPDATE_DATE": "2026-08-02 08:00:01",
                "CURRENCY": "CNY",
                "TOTALOPERATEREVE": 9999,
                "TOTALOPERATEREVETZ": 99,
                "PARENTNETPROFIT": 999,
                "PARENTNETPROFITTZ": 99,
                "KCFJCXSYJLR": 888,
                "KCFJCXSYJLRTZ": 88,
                "XSMLL": 55,
                "XSJLL": 44,
                "ROEJQ": 9,
                "EPSJB": 9,
            },
            {
                "SECURITY_CODE": code,
                "REPORT_DATE": "2026-03-31",
                "REPORT_DATE_NAME": "2026一季报",
                "NOTICE_DATE": "2026-04-29 18:00:00",
                "UPDATE_DATE": "2026-04-29 18:30:00",
                "CURRENCY": "CNY",
                "TOTALOPERATEREVE": 1000,
                "TOTALOPERATEREVETZ": 12,
                "PARENTNETPROFIT": 100,
                "PARENTNETPROFITTZ": 20,
                "KCFJCXSYJLR": 90,
                "KCFJCXSYJLRTZ": 18,
                "XSMLL": 30,
                "XSJLL": 10,
                "ROEJQ": 3,
                "EPSJB": 0.1,
            },
        ]

    def stock_news_em(self, symbol):
        return [
            {"新闻标题": f"{symbol} 截止前新闻", "新闻内容": "可用", "发布时间": "2026-08-02 07:59:59", "文章来源": "测试媒体", "新闻链接": f"https://finance.eastmoney.com/a/{symbol}-past.html"},
            {"新闻标题": f"{symbol} 截止后新闻", "新闻内容": "不得使用", "发布时间": "2026-08-02 08:00:01", "文章来源": "测试媒体", "新闻链接": f"https://example.com/{symbol}/future"},
        ]

    def stock_info_global_em(self):
        return [
            {"标题": "截止前政策新闻", "摘要": "可用", "发布时间": "2026-08-02 07:00:00", "来源": "新华社", "链接": "https://finance.eastmoney.com/a/policy-past.html"},
            {"标题": "截止后政策新闻", "摘要": "不得使用", "发布时间": "2026-08-02 09:00:00", "链接": "https://example.com/policy/future"},
        ]

    def index_us_stock_sina(self, symbol):
        return [
            {"date": "2026-07-30", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10, "amount": 1000},
            {"date": "2026-07-31", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 20, "amount": 2000},
        ]


class FailingAkShare:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RuntimeError(f"forced failure: {name}")

        return fail


class HttpProbeAkShare:
    def probe(self):
        session = requests.Session()
        session.request("GET", "https://example.invalid/default")
        session.request("GET", "https://example.invalid/explicit", timeout=2.5)
        session.request(
            "GET", "https://example.invalid/explicit-positional",
            None, None, None, None, None, None, 3.5,
        )
        worker = threading.Thread(
            target=lambda: session.request("GET", "https://example.invalid/unrelated-thread")
        )
        worker.start()
        worker.join(timeout=2)
        if worker.is_alive():
            raise RuntimeError("并发请求测试线程未结束")


class TimeoutCalendarAkShare(FakeAkShare):
    def tool_trade_date_hist_sina(self):
        return requests.Session().request("GET", "https://example.invalid/calendar")


class TimeoutAllMediaAkShare(FakeAkShare):
    def __init__(self):
        self.media_calls: list[str] = []
        self.financial_calls = 0

    def stock_info_global_em(self):
        self.media_calls.append("global_news")
        raise requests.ReadTimeout("simulated Eastmoney media timeout")

    def stock_news_em(self, symbol):
        self.media_calls.append(f"stock_news_{symbol}")
        raise AssertionError("media circuit should have skipped this upstream call")

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        self.financial_calls += 1
        return super().stock_financial_analysis_indicator_em(symbol, indicator)


class ContentErrorGlobalMediaAkShare(FakeAkShare):
    def __init__(self):
        self.media_calls: list[str] = []

    def stock_info_global_em(self):
        self.media_calls.append("global_news")
        raise ValueError("malformed media payload")

    def stock_news_em(self, symbol):
        self.media_calls.append(f"stock_news_{symbol}")
        return super().stock_news_em(symbol)


class TrackingMediaAkShare(FakeAkShare):
    def __init__(self):
        self.media_calls: list[str] = []

    def stock_info_global_em(self):
        self.media_calls.append("global_news")
        return super().stock_info_global_em()

    def stock_news_em(self, symbol):
        self.media_calls.append(f"stock_news_{symbol}")
        return super().stock_news_em(symbol)


class ManualMonotonicClock:
    def __init__(self, initial: float = 0.0):
        self.value = initial

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


class OverBudgetFirstMediaAkShare(FakeAkShare):
    def __init__(self, monotonic_clock):
        self.monotonic_clock = monotonic_clock
        self.media_calls: list[str] = []

    def stock_info_global_em(self):
        self.media_calls.append("global_news")
        self.monotonic_clock.advance(16.0)
        return super().stock_info_global_em()

    def stock_news_em(self, symbol):
        self.media_calls.append(f"stock_news_{symbol}")
        raise AssertionError("over-budget first loader should open the media circuit")


class TimeoutSwallowingMediaAkShare(FakeAkShare):
    def __init__(self, marker_path):
        self.marker_path = Path(marker_path)

    def stock_info_global_em(self):
        with self.marker_path.open("wb", buffering=0) as marker:
            marker.write(f"{os.getpid()}\n".encode("ascii"))
            # This reproduces the bypass that defeated a one-shot SIGALRM: the
            # loader catches TimeoutError repeatedly and keeps doing work.  A
            # process boundary must stop it regardless of exception handling.
            while True:
                try:
                    raise TimeoutError("caught by hostile loader")
                except TimeoutError:
                    marker.write(b"x")
                    time.sleep(0.002)


class SlowCoreTrackingMediaAkShare(TrackingMediaAkShare):
    def __init__(self, monotonic_clock):
        super().__init__()
        self.monotonic_clock = monotonic_clock

    def stock_lhb_detail_em(self, start_date, end_date):
        self.monotonic_clock.advance(20.0)
        return super().stock_lhb_detail_em(start_date, end_date)

    def index_us_stock_sina(self, symbol):
        self.monotonic_clock.advance(20.0)
        return super().index_us_stock_sina(symbol)

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        self.monotonic_clock.advance(20.0)
        return super().stock_financial_analysis_indicator_em(symbol, indicator)


class SensitiveTimeoutAllMediaAkShare(TimeoutAllMediaAkShare):
    def stock_info_global_em(self):
        self.media_calls.append("global_news")
        raise requests.ReadTimeout(
            'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 '
            'sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 '
            'AKIAABCDEFGHIJKLMNOP '
            '-----BEGIN PRIVATE KEY-----private-key-material-----END PRIVATE KEY----- '
            'endpoint=https://alice:user-pass@example.test/news?api_key=query-secret&safe=kept '
            '"client_secret": "json secret value" '
            "'api_key' = 'quoted-key-secret' "
            'password = spaced-assignment-secret '
            '"cookie":"session=json-cookie-secret" '
            "Authorization: Bearer another-secret\n"
            + "x" * 1000
        )


class PartialFinancialAkShare(FakeAkShare):
    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        return [
            {
                "SECURITY_CODE": symbol[:6],
                "REPORT_DATE": "2026-03-31",
                "REPORT_DATE_NAME": "2026一季报",
                "NOTICE_DATE": "2026-04-29 18:00:00",
                "UPDATE_DATE": "2026-04-29 18:30:00",
                "CURRENCY": "CNY",
                "TOTALOPERATEREVE": 1000,
                "PARENTNETPROFIT": 100,
            }
        ]


class DateOnlyCutoffFinancialAkShare(FakeAkShare):
    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        return [
            {
                "SECURITY_CODE": symbol[:6],
                "REPORT_DATE": "2026-06-30",
                "REPORT_DATE_NAME": "2026中报",
                "NOTICE_DATE": "2026-08-02",
                "UPDATE_DATE": "2026-08-02",
                "CURRENCY": "CNY",
                "TOTALOPERATEREVE": 9999,
                "PARENTNETPROFIT": 999,
            }
        ]


class August3HistoryAkShare(FakeAkShare):
    def __init__(self):
        self.spot_calls: list[str] = []

    def stock_zh_a_spot(self):
        self.spot_calls.append("stock_zh_a_spot")
        return super().stock_zh_a_spot()

    def stock_zh_index_spot_sina(self):
        self.spot_calls.append("stock_zh_index_spot_sina")
        return [
            {
                "代码": "sh000001",
                "最新价": 3809.663,
                "昨收": 3809.6629,
                "涨跌额": -0.004,
                "涨跌幅": 0,
                "成交量": 0,
                "成交额": 0,
            }
        ]

    def stock_zh_index_daily(self, symbol):
        rows = [dict(row) for row in super().stock_zh_index_daily(symbol)]
        if symbol == "sh000001":
            rows[-1]["change"] = -0.004
            rows[-1]["change_percent"] = 0
        return rows

    def stock_market_activity_legu(self):
        return [
            {"item": "上涨", "value": 2_100},
            {"item": "下跌", "value": 2_900},
            {"item": "平盘", "value": 100},
            {"item": "统计日期", "value": "2026-08-03 15:00:00"},
        ]


class IncompleteMarketHistoryAkShare(FakeAkShare):
    def stock_zh_index_daily(self, symbol):
        if symbol != "sh000001":
            return []
        return super().stock_zh_index_daily(symbol)

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        rows = [dict(row) for row in super().stock_zh_a_daily(symbol, start_date, end_date, adjust)]
        if symbol == "sh600875":
            rows[1]["volume"] = 0
            rows[1]["amount"] = 0
        if symbol == "sz300033":
            rows = [row for row in rows if row["date"] != "2026-07-31"]
        return rows

    def stock_sse_deal_daily(self, date):
        return [{"单日情况": "成交金额", "主板A": 0, "科创板": 0}]

    def stock_market_activity_legu(self):
        return [
            {"item": "上涨", "value": 0},
            {"item": "下跌", "value": 0},
            {"item": "平盘", "value": 0},
            {"item": "统计日期", "value": "2026-07-31 15:00:00"},
        ]


class MissingImmediatePreviousDayAkShare(August3HistoryAkShare):
    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        rows = super().stock_zh_a_daily(symbol, start_date, end_date, adjust)
        return [row for row in rows if row["date"] != "2026-07-31"]

    def stock_zh_index_daily(self, symbol):
        rows = super().stock_zh_index_daily(symbol)
        return [row for row in rows if row["date"] != "2026-07-31"]


class BrokenSzseComponentsAkShare(FakeAkShare):
    def __init__(self, mode):
        self.mode = mode

    def stock_szse_summary(self, date):
        rows = [dict(row) for row in super().stock_szse_summary(date)]
        if self.mode == "missing_main":
            return [row for row in rows if row["证券类别"] != "主板A股"]
        if self.mode == "renamed_chinext":
            for row in rows:
                if row["证券类别"] == "创业板A股":
                    row["证券类别"] = "创业板"
            return rows
        if self.mode == "mismatched_total":
            for row in rows:
                if row["证券类别"] == "股票":
                    row["成交金额"] += 1_000_000_000
            return rows
        raise AssertionError(f"unknown test mode: {self.mode}")


class AkShareProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.now = lambda: datetime(2026, 8, 2, 8, 0, tzinfo=CN)
        self.cninfo_patch = patch.object(
            AkShareMorningProvider,
            "_fetch_cninfo_rows",
            side_effect=RuntimeError("cninfo disabled in unit test"),
        )
        self.cninfo_patch.start()
        self.addCleanup(self.cninfo_patch.stop)

    def provider(self, api):
        return AkShareMorningProvider(
            api,
            cache_dir=self.temp_dir.name,
            clock=self.now,
            isolate_media_loaders=False,
            collect_documents=False,
            cninfo_window_fetcher=lambda *args, **kwargs: None,
            staging_dir=Path(self.temp_dir.name) / "staging",
        )

    def assert_market_arithmetic(self, item, close_field):
        close = item[close_field]
        previous_close = item["previousClose"]
        self.assertGreater(close, 0)
        self.assertGreater(previous_close, 0)
        self.assertAlmostEqual(item["change"], close - previous_close, places=8)
        self.assertAlmostEqual(
            item["changePercent"],
            (close - previous_close) / previous_close * 100,
            places=8,
        )

    def test_builds_all_sections_and_filters_future_news(self):
        response = self.provider(FakeAkShare()).get_morning_context(request_fixture())

        self.assertEqual(response["schemaVersion"], "1.0")
        self.assertEqual(response["meta"]["provider"], "akshare")
        self.assertEqual(set(response["meta"]["dataCompleteness"]), {"market", "capital", "news", "financial", "overall"})
        self.assertEqual(set(response["data"]["sectionCompleteness"]), {
            "tradingCalendar", "overnightGlobal", "previousAShareSession", "policyAndCalendar",
            "sectors", "capitalFlows", "watchlist", "announcementsAndNews",
        })
        self.assertTrue(all(0 <= value <= 100 for value in response["meta"]["dataCompleteness"].values()))
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertFalse(response["meta"]["isLive"])
        self.assertEqual(response["data"]["tradingCalendar"]["previousTradingDate"], "2026-07-31")
        self.assertEqual(response["data"]["previousAShareSession"]["sessionDate"], "2026-07-31")
        self.assertIsNotNone(response["data"]["previousAShareSession"]["dataAsOf"])
        self.assertEqual(response["data"]["previousAShareSession"]["units"]["totalTurnover"], "CNY")
        self.assertEqual(
            len(response["data"]["previousAShareSession"]["turnoverSourceIds"]),
            2,
        )
        self.assertEqual(
            len(response["data"]["previousAShareSession"]["marketBreadth"]["sourceIds"]),
            1,
        )
        index_units = response["data"]["previousAShareSession"]["indices"][0]["units"]
        self.assertEqual(index_units["close"], "index_point")
        self.assertEqual(index_units["volume"], "provider_native_unspecified")
        self.assertTrue(all(item["turnover"] is None for item in response["data"]["previousAShareSession"]["indices"]))
        self.assertEqual(len(response["data"]["policyAndCalendar"]), 1)
        self.assertEqual(len(response["data"]["announcementsAndNews"]), 3)
        self.assertFalse(any("截止后" in item["title"] for item in response["data"]["announcementsAndNews"]))
        self.assertEqual(response["data"]["watchlist"][0]["code"], "002617")
        self.assertEqual(response["data"]["watchlist"][1]["marketData"]["last"], 20.2)
        market_units = response["data"]["watchlist"][0]["marketData"]["units"]
        self.assertEqual(market_units["last"], "CNY_per_share")
        self.assertEqual(market_units["turnover"], "CNY")
        self.assertEqual(market_units["volume"], "provider_native_unspecified")
        sector_units = response["data"]["sectors"][0]["units"]
        self.assertEqual(sector_units["turnover"], "CNY_100_million")
        self.assertEqual(sector_units["netInflow"], "CNY_100_million")
        self.assertEqual(sector_units["volume"], "ten_thousand_lots")
        financials = response["data"]["watchlist"][0]["financials"]
        self.assertEqual(financials["reportDate"], "2026-03-31")
        self.assertEqual(financials["seasonLabel"], "2026一季报")
        self.assertEqual(financials["deductedNetProfit"], 90)
        self.assertEqual(financials["deductedNetProfitYoY"], 18)
        self.assertEqual(financials["grossMargin"], 30)
        self.assertEqual(financials["netMargin"], 10)
        self.assertEqual(financials["roeWeighted"], 3)
        self.assertIsNone(financials["roeDiluted"])
        self.assertEqual(financials["dataAsOf"], "2026-04-29T18:30:00+08:00")
        self.assertEqual(financials["units"]["revenue"], "CNY")
        self.assertEqual(financials["revenueBasis"], {
            "provider": "eastmoney", "amountField": "TOTALOPERATEREVE",
            "yoyField": "TOTALOPERATEREVETZ", "verification": "unverified_against_filing",
        })
        self.assertEqual(financials["units"]["grossMargin"], "percentage_point")
        self.assertEqual(financials["units"]["eps"], "CNY_per_share")
        self.assertEqual(response["meta"]["dataCompleteness"]["financial"], 100)
        self.assertEqual(response["data"]["capitalFlows"][0]["status"], "unavailable")
        self.assertIn("北向", response["data"]["capitalFlows"][0]["reason"])
        lhb_units = response["data"]["capitalFlows"][1]["entries"][0]["units"]
        self.assertEqual(lhb_units["netBuy"], "CNY")
        self.assertEqual(lhb_units["close"], "CNY_per_share")

        source_ids = {source["id"] for source in response["sources"]}
        self.assertTrue(source_ids)
        self.assertTrue(all(source["url"].startswith(("http://", "https://")) for source in response["sources"]))
        source_types = {source["sourceType"] for source in response["sources"]}
        self.assertTrue({
            "trading_calendar_data",
            "market_data",
            "industry_market_data",
            "dragon_tiger_list_aggregation",
            "financial_data_aggregation",
            "media_aggregation",
        }.issubset(source_types))
        aggregated_media = [source for source in response["sources"] if source["sourceType"] == "media_aggregation"]
        self.assertEqual(len(aggregated_media), 4)
        self.assertTrue(all(source["aggregator"] == "东方财富" for source in aggregated_media))
        self.assertEqual({source["originalPublisher"] for source in aggregated_media}, {"新华社", "测试媒体"})
        self.assertTrue(all("eastmoney.com" in source["url"] for source in aggregated_media))
        self.assertNotIn("official_announcement", source_types)
        self.assertEqual(response["data"]["policyAndCalendar"][0]["sourceType"], "media_aggregation")
        self.assertEqual(response["data"]["policyAndCalendar"][0]["originalPublisher"], "新华社")
        self.assertEqual(response["data"]["policyAndCalendar"][0]["aggregator"], "东方财富")
        self.assertTrue(all(item["sourceType"] == "media_aggregation" for item in response["data"]["announcementsAndNews"]))
        self.assertTrue(all(item["type"] == "news" for item in response["data"]["announcementsAndNews"]))
        for item in response["data"]["watchlist"]:
            self.assertTrue(set(item["sourceIds"]).issubset(source_ids))
        referenced_ids: set[str] = set()

        def collect_references(value):
            if isinstance(value, dict):
                referenced_ids.update(value.get("sourceIds") or [])
                if value.get("sourceId"):
                    referenced_ids.add(value["sourceId"])
                for child in value.values():
                    collect_references(child)
            elif isinstance(value, list):
                for child in value:
                    collect_references(child)

        collect_references(response["data"])
        self.assertEqual(source_ids, referenced_ids)

        component_times: list[datetime] = []

        def collect_times(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"dataAsOf", "publishedAt"} and child:
                        component_times.append(datetime.fromisoformat(child))
                    else:
                        collect_times(child)
            elif isinstance(value, list):
                for child in value:
                    collect_times(child)

        collect_times({"data": response["data"], "sources": response["sources"]})
        self.assertGreaterEqual(datetime.fromisoformat(response["meta"]["asOf"]), max(component_times))
        self.assertEqual(response["meta"]["dataCompleteness"]["market"], 96)
        self.assertTrue(any("指数" in warning and "成交额缺失" in warning for warning in response["meta"]["warnings"]))
        json.dumps(response, ensure_ascii=False, allow_nan=False)

    def test_previous_session_uses_dated_daily_history_for_july31_and_august3(self):
        cases = [
            ("2026-08-03", "2026-07-31", "2026-07-30", FakeAkShare(), 3832.262, 3804.86),
            ("2026-08-04", "2026-08-03", "2026-07-31", August3HistoryAkShare(), 3809.663, 3832.262),
        ]
        for target_date, session_date, previous_close_date, api, expected_close, expected_previous in cases:
            with self.subTest(session_date=session_date):
                request = request_fixture()
                request["targetDate"] = target_date
                request["requestedAt"] = f"{target_date}T08:00:00+08:00"
                response = self.provider(api).get_morning_context(request)
                previous_session = response["data"]["previousAShareSession"]

                self.assertEqual(previous_session["sessionDate"], session_date)
                self.assertEqual(len(previous_session["indices"]), 3)
                shanghai = next(item for item in previous_session["indices"] if item["code"] == "sh000001")
                self.assertEqual(shanghai["close"], expected_close)
                self.assertEqual(shanghai["previousClose"], expected_previous)
                self.assertEqual(shanghai["previousCloseDate"], previous_close_date)
                self.assert_market_arithmetic(shanghai, "close")
                for index in previous_session["indices"]:
                    self.assert_market_arithmetic(index, "close")
                    self.assertTrue(index["dataAsOf"].startswith(session_date))
                for stock in response["data"]["watchlist"]:
                    self.assertIsNotNone(stock["marketData"])
                    self.assert_market_arithmetic(stock["marketData"], "last")
                    self.assertEqual(stock["marketData"]["previousCloseDate"], previous_close_date)
                    self.assertTrue(stock["marketData"]["dataAsOf"].startswith(session_date))
                self.assertGreater(previous_session["totalTurnover"], 0)
                self.assertGreater(previous_session["marketBreadth"]["advancers"], 0)
                self.assertEqual(response["meta"]["dataCompleteness"]["market"], 96)
                if isinstance(api, August3HistoryAkShare):
                    self.assertEqual(api.spot_calls, [])
                    self.assertAlmostEqual(shanghai["change"], -22.599, places=8)
                    self.assertAlmostEqual(shanghai["changePercent"], -0.5897039399, places=8)
                    self.assertTrue(any("上游涨跌额" in warning for warning in response["meta"]["warnings"]))

    def test_missing_indices_turnover_breadth_and_zero_activity_reduce_market_completeness(self):
        response = self.provider(IncompleteMarketHistoryAkShare()).get_morning_context(request_fixture())
        previous_session = response["data"]["previousAShareSession"]

        self.assertEqual([item["code"] for item in previous_session["indices"]], ["sh000001"])
        self.assertIsNone(previous_session["totalTurnover"])
        self.assertIsNone(previous_session["marketBreadth"])
        self.assertLess(response["meta"]["dataCompleteness"]["market"], 100)
        self.assertLess(response["data"]["sectionCompleteness"]["previousAShareSession"], 100)
        second_quote = response["data"]["watchlist"][1]["marketData"]
        self.assertIsNone(second_quote["volume"])
        self.assertIsNone(second_quote["turnover"])
        self.assertIsNone(response["data"]["watchlist"][2]["marketData"])
        self.assertTrue(any("缺失或不大于 0" in warning for warning in response["meta"]["warnings"]))
        self.assertTrue(any("a_share_market_breadth 不可用" in warning for warning in response["meta"]["warnings"]))

    def test_missing_immediate_previous_trading_day_never_uses_older_close(self):
        request = request_fixture()
        request["targetDate"] = "2026-08-04"
        request["requestedAt"] = "2026-08-04T08:00:00+08:00"
        api = MissingImmediatePreviousDayAkShare()
        response = self.provider(api).get_morning_context(request)

        calendar = response["data"]["tradingCalendar"]
        self.assertEqual(calendar["previousTradingDate"], "2026-08-03")
        self.assertEqual(calendar["previousSessionPreviousTradingDate"], "2026-07-31")
        self.assertEqual(response["data"]["previousAShareSession"]["indices"], [])
        self.assertTrue(all(item["marketData"] is None for item in response["data"]["watchlist"]))
        self.assertLess(response["meta"]["dataCompleteness"]["market"], 50)
        self.assertTrue(
            any(
                "紧邻前序交易日 2026-07-31" in warning and "不得使用更老日期" in warning
                for warning in response["meta"]["warnings"]
            )
        )
        self.assertEqual(api.spot_calls, [])

    def test_szse_turnover_requires_exact_components_and_closed_total_minus_b_share(self):
        cases = {
            "missing_main": "主板A股",
            "renamed_chinext": "创业板A股",
            "mismatched_total": "口径不闭合",
        }
        for mode, expected_warning in cases.items():
            with self.subTest(mode=mode):
                response = self.provider(BrokenSzseComponentsAkShare(mode)).get_morning_context(request_fixture())
                previous_session = response["data"]["previousAShareSession"]

                self.assertIsNone(previous_session["totalTurnover"])
                self.assertIsNone(previous_session["turnoverByExchange"])
                self.assertEqual(response["meta"]["dataCompleteness"]["market"], 86)
                self.assertTrue(
                    any(
                        "a_share_market_turnover 不可用" in warning and expected_warning in warning
                        for warning in response["meta"]["warnings"]
                    )
                )

    def test_request_and_response_validate_against_contract_schema(self):
        schema = json.loads((PROJECT_ROOT / "mcp" / "contract.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.check_schema(schema)

        request = request_fixture()
        response = self.provider(FakeAkShare()).get_morning_context(request)
        validator.validate(request)
        validator.validate(response)

    def test_financial_completeness_reflects_missing_required_fields(self):
        response = self.provider(PartialFinancialAkShare()).get_morning_context(request_fixture())

        self.assertGreater(response["meta"]["dataCompleteness"]["financial"], 0)
        self.assertLess(response["meta"]["dataCompleteness"]["financial"], 100)
        self.assertLess(response["data"]["sectionCompleteness"]["watchlist"], 100)

    def test_date_only_disclosure_on_cutoff_day_is_not_treated_as_morning_available(self):
        response = self.provider(DateOnlyCutoffFinancialAkShare()).get_morning_context(request_fixture())

        self.assertEqual(response["meta"]["dataCompleteness"]["financial"], 0)
        self.assertTrue(all(item["financials"] is None for item in response["data"]["watchlist"]))

    def test_future_financial_cache_is_rejected_for_earlier_cutoff(self):
        later_request = request_fixture()
        later_request["requestedAt"] = "2026-08-02T09:00:00+08:00"
        later = self.provider(FakeAkShare()).get_morning_context(later_request)
        self.assertEqual(later["data"]["watchlist"][0]["financials"]["reportDate"], "2026-06-30")

        earlier = self.provider(FailingAkShare()).get_morning_context(request_fixture())
        self.assertTrue(all(item["financials"] is None for item in earlier["data"]["watchlist"]))
        self.assertEqual(earlier["meta"]["dataCompleteness"]["financial"], 0)
        self.assertFalse(earlier["meta"]["isLive"])

    def test_module_failure_uses_last_good_cache(self):
        request = request_fixture()
        first = self.provider(FakeAkShare()).get_morning_context(request)
        second = self.provider(FailingAkShare()).get_morning_context(request)

        self.assertTrue(second["meta"]["cacheHits"])
        self.assertEqual(second["meta"]["status"], "partial")
        self.assertFalse(second["meta"]["isLive"])
        self.assertEqual(second["data"]["watchlist"][0]["marketData"]["last"], first["data"]["watchlist"][0]["marketData"]["last"])
        self.assertTrue(any("last-good" in warning for warning in second["meta"]["warnings"]))

    def test_cache_format_version_rejects_legacy_source_provenance(self):
        request = request_fixture()
        self.provider(FakeAkShare()).get_morning_context(request)
        cache_path = Path(self.temp_dir.name) / "trading_calendar.json"
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["cacheFormatVersion"], CACHE_FORMAT_VERSION)

        cached.pop("cacheFormatVersion")
        cache_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        second = self.provider(FailingAkShare()).get_morning_context(request)

        self.assertNotIn("trading_calendar", second["meta"]["cacheHits"])
        self.assertIsNone(second["data"]["tradingCalendar"])

    def test_bridge_emits_exactly_one_strict_json_value(self):
        input_stream = io.StringIO(json.dumps(request_fixture(), ensure_ascii=False))
        output_stream = io.StringIO()
        run_bridge(input_stream, output_stream, provider=self.provider(FakeAkShare()))
        raw = output_stream.getvalue()
        parsed = json.loads(raw)
        self.assertEqual(parsed["schemaVersion"], "1.0")
        # Nested versioned quality metadata is permitted; the bridge must emit
        # one JSON document, not one occurrence of a particular key name.
        _, consumed = json.JSONDecoder().raw_decode(raw)
        self.assertEqual(raw[consumed:].strip(), "")
        self.assertNotIn("NaN", raw)

    def test_http_default_timeout_respects_explicit_timeout_and_other_threads(self):
        observed: list[tuple[str, float | None]] = []

        def fake_request(
            session, method, url, params=None, data=None, headers=None, cookies=None,
            files=None, auth=None, timeout=None, **kwargs,
        ):
            observed.append((url, timeout))
            return {"ok": True}

        with patch.object(requests.sessions.Session, "request", new=fake_request):
            original = requests.sessions.Session.request
            self.provider(HttpProbeAkShare())._call("probe")
            self.assertIs(requests.sessions.Session.request, original)

        self.assertEqual(observed[0][1], DEFAULT_HTTP_TIMEOUT_SECONDS)
        self.assertEqual(observed[1][1], 2.5)
        self.assertEqual(observed[2][1], 3.5)
        self.assertIsNone(observed[3][1])

    def test_http_timeout_restores_requests_and_module_uses_last_good_cache(self):
        request = request_fixture()
        self.provider(FakeAkShare()).get_morning_context(request)

        def timeout_request(session, method, url, **kwargs):
            self.assertEqual(kwargs["timeout"], DEFAULT_HTTP_TIMEOUT_SECONDS)
            raise requests.Timeout("simulated timeout")

        with patch.object(requests.sessions.Session, "request", new=timeout_request):
            original = requests.sessions.Session.request
            response = self.provider(TimeoutCalendarAkShare()).get_morning_context(request)
            self.assertIs(requests.sessions.Session.request, original)

        self.assertIn("trading_calendar", response["meta"]["cacheHits"])
        self.assertIsNotNone(response["data"]["tradingCalendar"])
        self.assertTrue(any("Timeout" in warning for warning in response["meta"]["warnings"]))

    def test_first_media_timeout_opens_circuit_and_core_modules_continue(self):
        api = TimeoutAllMediaAkShare()
        started = time.perf_counter()
        response = self.provider(api).get_morning_context(request_fixture())
        elapsed = time.perf_counter() - started

        self.assertEqual(api.media_calls, ["global_news"])
        self.assertLess(elapsed, 1.0)
        self.assertEqual(api.financial_calls, 3)
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertEqual(response["data"]["sectionCompleteness"]["policyAndCalendar"], 0)
        self.assertEqual(response["meta"]["dataCompleteness"]["news"], 0)
        self.assertEqual(len(response["data"]["previousAShareSession"]["indices"]), 3)
        self.assertTrue(all(item["marketData"] is not None for item in response["data"]["watchlist"]))
        self.assertTrue(all(item["financials"] is not None for item in response["data"]["watchlist"]))
        self.assertTrue(any("东方财富媒体熔断已开启" in warning for warning in response["meta"]["warnings"]))
        self.assertTrue(any("标记 unavailable" in warning for warning in response["meta"]["warnings"]))

    def test_media_circuit_reuses_only_fresh_point_in_time_cache(self):
        request = request_fixture()
        seeded = self.provider(FakeAkShare()).get_morning_context(request)
        self.assertEqual(len(seeded["data"]["announcementsAndNews"]), 3)

        api = TimeoutAllMediaAkShare()
        response = self.provider(api).get_morning_context(request)

        expected_cache_hits = {
            "global_news",
            "stock_news_002617",
            "stock_news_600875",
            "stock_news_300033",
        }
        self.assertEqual(api.media_calls, ["global_news"])
        self.assertTrue(expected_cache_hits.issubset(set(response["meta"]["cacheHits"])))
        self.assertEqual(len(response["data"]["policyAndCalendar"]), 1)
        self.assertEqual(len(response["data"]["announcementsAndNews"]), 3)
        self.assertTrue(
            any("未超过 72 小时阈值的媒体缓存" in warning for warning in response["meta"]["warnings"])
        )

    def test_media_circuit_rejects_cache_older_than_news_threshold(self):
        request = request_fixture()
        self.provider(FakeAkShare()).get_morning_context(request)
        cache_dir = Path(self.temp_dir.name)
        for module in (
            "global_news",
            "stock_news_002617",
            "stock_news_600875",
            "stock_news_300033",
        ):
            cache_path = cache_dir / f"{module}.json"
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            for item in document["data"]:
                item["publishedAt"] = "2026-07-29T07:00:00+08:00"
                item["dataAsOf"] = "2026-07-29T07:00:00+08:00"
            for source in document["sources"]:
                source["publishedAt"] = "2026-07-29T07:00:00+08:00"
                source["dataAsOf"] = "2026-07-29T07:00:00+08:00"
            cache_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        response = self.provider(TimeoutAllMediaAkShare()).get_morning_context(request)

        self.assertFalse(
            {
                "global_news",
                "stock_news_002617",
                "stock_news_600875",
                "stock_news_300033",
            }.intersection(response["meta"]["cacheHits"])
        )
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])

    def test_non_transport_media_content_error_does_not_open_circuit(self):
        api = ContentErrorGlobalMediaAkShare()
        response = self.provider(api).get_morning_context(request_fixture())

        self.assertEqual(
            api.media_calls,
            [
                "global_news",
                "stock_news_002617",
                "stock_news_600875",
                "stock_news_300033",
            ],
        )
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(len(response["data"]["announcementsAndNews"]), 3)
        self.assertFalse(any("东方财富媒体熔断已开启" in warning for warning in response["meta"]["warnings"]))

    def test_shared_media_deadline_skips_remaining_endpoints(self):
        ticks = iter((0.0, 0.0, 1.0, 16.0))
        api = TrackingMediaAkShare()
        provider = AkShareMorningProvider(
            api,
            cache_dir=self.temp_dir.name,
            clock=self.now,
            monotonic_clock=lambda: next(ticks, 16.0),
            media_source_budget_seconds=15.0,
            isolate_media_loaders=False,
        )

        response = provider.get_morning_context(request_fixture())

        self.assertEqual(api.media_calls, ["global_news"])
        self.assertEqual(len(response["data"]["policyAndCalendar"]), 1)
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertTrue(any("共享媒体时间预算已耗尽" in warning for warning in response["meta"]["warnings"]))

    def test_first_media_loader_over_budget_is_discarded_without_cache_or_live_status(self):
        monotonic_clock = ManualMonotonicClock()
        api = OverBudgetFirstMediaAkShare(monotonic_clock)
        provider = AkShareMorningProvider(
            api,
            cache_dir=self.temp_dir.name,
            clock=self.now,
            monotonic_clock=monotonic_clock,
            media_source_budget_seconds=15.0,
            isolate_media_loaders=False,
        )

        response = provider.get_morning_context(request_fixture())

        self.assertEqual(api.media_calls, ["global_news"])
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertNotIn("global_news", response["meta"]["cacheHits"])
        self.assertFalse((Path(self.temp_dir.name) / "global_news.json").exists())
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertTrue(
            any(
                "结果已丢弃且不会写入缓存或标记 live" in warning
                for warning in response["meta"]["warnings"]
            )
        )

    def test_posix_process_boundary_kills_loader_that_repeatedly_swallows_timeout(self):
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
            or "fork" not in multiprocessing.get_all_start_methods()
        ):
            self.skipTest("POSIX main-thread fork boundary is unavailable")

        marker_path = Path(self.temp_dir.name) / "swallowing-loader.marker"
        api = TimeoutSwallowingMediaAkShare(marker_path)
        before_children = {child.pid for child in multiprocessing.active_children()}
        provider = AkShareMorningProvider(
            api,
            cache_dir=self.temp_dir.name,
            clock=self.now,
            media_source_budget_seconds=0.15,
        )

        started = time.monotonic()
        response = provider.get_morning_context(request_fixture())
        elapsed = time.monotonic() - started

        marker = marker_path.read_text(encoding="ascii")
        child_pid_text, caught_markers = marker.split("\n", 1)
        child_pid = int(child_pid_text)
        self.assertGreater(len(caught_markers), 2)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertNotIn("global_news", response["meta"]["cacheHits"])
        self.assertFalse((Path(self.temp_dir.name) / "global_news.json").exists())
        self.assertEqual(response["meta"]["status"], "partial")
        self.assertTrue(any("共享媒体时间预算已耗尽" in item for item in response["meta"]["warnings"]))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()} - before_children,
            set(),
        )

    def test_production_media_process_boundary_returns_bounded_payload_for_parent_cache(self):
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
            or "fork" not in multiprocessing.get_all_start_methods()
        ):
            self.skipTest("POSIX main-thread fork boundary is unavailable")

        response = AkShareMorningProvider(
            FakeAkShare(),
            cache_dir=self.temp_dir.name,
            clock=self.now,
        ).get_morning_context(request_fixture())

        self.assertEqual(len(response["data"]["policyAndCalendar"]), 1)
        self.assertEqual(len(response["data"]["announcementsAndNews"]), 3)
        self.assertTrue((Path(self.temp_dir.name) / "global_news.json").is_file())
        self.assertTrue((Path(self.temp_dir.name) / "stock_news_002617.json").is_file())

    def test_media_fails_closed_when_safe_process_boundary_is_unavailable(self):
        api = TrackingMediaAkShare()
        provider = AkShareMorningProvider(
            api,
            cache_dir=self.temp_dir.name,
            clock=self.now,
        )

        with patch("akshare_provider.multiprocessing.get_all_start_methods", return_value=[]):
            response = provider.get_morning_context(request_fixture())

        self.assertEqual(api.media_calls, [])
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertTrue(
            any("fail-closed" in warning for warning in response["meta"]["warnings"])
        )

    def test_slow_core_modules_do_not_consume_contiguous_media_phase_budget(self):
        monotonic_clock = ManualMonotonicClock()
        api = SlowCoreTrackingMediaAkShare(monotonic_clock)
        provider = AkShareMorningProvider(
            api,
            cache_dir=self.temp_dir.name,
            clock=self.now,
            monotonic_clock=monotonic_clock,
            media_source_budget_seconds=15.0,
            isolate_media_loaders=False,
        )

        response = provider.get_morning_context(request_fixture())

        self.assertGreaterEqual(monotonic_clock.value, 140.0)
        self.assertEqual(
            api.media_calls,
            [
                "global_news",
                "stock_news_002617",
                "stock_news_600875",
                "stock_news_300033",
            ],
        )
        self.assertEqual(len(response["data"]["policyAndCalendar"]), 1)
        self.assertEqual(len(response["data"]["announcementsAndNews"]), 3)
        self.assertEqual(len(response["data"]["previousAShareSession"]["indices"]), 3)
        self.assertTrue(
            all(item["financials"] is not None for item in response["data"]["watchlist"])
        )

    def test_bad_media_cache_and_transport_failure_keep_context_and_bound_warning_detail(self):
        request = request_fixture()
        self.provider(FakeAkShare()).get_morning_context(request)
        malformed_source_ids = (None, 7, ["valid-looking", 7], ["duplicate", "duplicate"])
        media_modules = (
            "global_news",
            "stock_news_002617",
            "stock_news_600875",
            "stock_news_300033",
        )
        for module, bad_value in zip(media_modules, malformed_source_ids):
            cache_path = Path(self.temp_dir.name) / f"{module}.json"
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            for item in document["data"]:
                item["sourceIds"] = bad_value
            cache_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        api = SensitiveTimeoutAllMediaAkShare()
        response = self.provider(api).get_morning_context(request)

        self.assertEqual(api.media_calls, ["global_news"])
        self.assertFalse(set(media_modules).intersection(response["meta"]["cacheHits"]))
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertEqual(len(response["data"]["previousAShareSession"]["indices"]), 3)
        self.assertTrue(
            all(item["marketData"] is not None for item in response["data"]["watchlist"])
        )
        self.assertTrue(
            all(item["financials"] is not None for item in response["data"]["watchlist"])
        )
        warnings = response["meta"]["warnings"]
        self.assertTrue(any("ReadTimeout" in warning for warning in warnings))
        joined_warnings = " ".join(warnings)
        for secret in (
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "AKIAABCDEFGHIJKLMNOP",
            "private-key-material",
            "alice",
            "user-pass",
            "query-secret",
            "json secret value",
            "quoted-key-secret",
            "spaced-assignment-secret",
            "json-cookie-secret",
            "another-secret",
        ):
            self.assertNotIn(secret, joined_warnings)
        self.assertIn("example.test/news", joined_warnings)
        self.assertIn("safe=kept", joined_warnings)
        self.assertIn("[REDACTED]", joined_warnings)
        self.assertTrue(all("\n" not in warning and "\r" not in warning for warning in warnings))
        self.assertTrue(
            all(
                len(warning) <= MAX_WARNING_ERROR_DETAIL_CHARS + 220
                for warning in warnings
                if "ReadTimeout" in warning
            )
        )

    def test_warning_detail_redacts_each_supported_secret_syntax_before_bounding(self):
        cases = (
            (
                'failure {"client_secret": "json secret value", '
                '"api_key" = "quoted-key-secret", "cookie":"cookie-secret"} tail=kept',
                ("json secret value", "quoted-key-secret", "cookie-secret"),
                "tail=kept",
            ),
            (
                "Authorization: Bearer bearer-secret; authorization = Basic basic-secret tail=kept",
                ("bearer-secret", "basic-secret"),
                "tail=kept",
            ),
            (
                "https://alice:user-pass@example.test/news?token=query-secret&safe=kept",
                ("alice", "user-pass", "query-secret"),
                "safe=kept",
            ),
            (
                "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 "
                "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 "
                "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 AKIAABCDEFGHIJKLMNOP tail=kept",
                (
                    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                    "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                    "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                    "AKIAABCDEFGHIJKLMNOP",
                ),
                "tail=kept",
            ),
            (
                "-----BEGIN PRIVATE KEY-----private-key-material-----END PRIVATE KEY----- tail=kept",
                ("private-key-material",),
                "tail=kept",
            ),
        )
        for raw, secrets, useful_tail in cases:
            with self.subTest(raw=raw):
                detail = _warning_error_detail(RuntimeError(raw))
                self.assertLessEqual(len(detail), MAX_WARNING_ERROR_DETAIL_CHARS)
                self.assertIn("[REDACTED", detail)
                self.assertIn(useful_tail, detail)
                for secret in secrets:
                    self.assertNotIn(secret, detail)

    def test_non_string_media_cache_source_types_are_rejected_without_breaking_context(self):
        request = request_fixture()
        self.provider(FakeAkShare()).get_morning_context(request)
        media_modules = (
            "global_news",
            "stock_news_002617",
            "stock_news_600875",
            "stock_news_300033",
        )
        source_type_mutations = (
            ("item", []),
            ("item", {}),
            ("source", []),
            ("source", {}),
        )
        for module, (target, bad_value) in zip(media_modules, source_type_mutations):
            cache_path = Path(self.temp_dir.name) / f"{module}.json"
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            records = document["data"] if target == "item" else document["sources"]
            for record in records:
                record["sourceType"] = bad_value
            cache_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        api = SensitiveTimeoutAllMediaAkShare()
        response = self.provider(api).get_morning_context(request)

        self.assertEqual(api.media_calls, ["global_news"])
        self.assertFalse(set(media_modules).intersection(response["meta"]["cacheHits"]))
        self.assertEqual(response["data"]["policyAndCalendar"], [])
        self.assertEqual(response["data"]["announcementsAndNews"], [])
        self.assertEqual(len(response["data"]["previousAShareSession"]["indices"]), 3)
        self.assertTrue(
            all(item["marketData"] is not None for item in response["data"]["watchlist"])
        )
        self.assertTrue(
            all(item["financials"] is not None for item in response["data"]["watchlist"])
        )

    def test_media_http_timeout_caps_upstream_explicit_values(self):
        observed: list[tuple[str, float | None]] = []

        def fake_request(
            session, method, url, params=None, data=None, headers=None, cookies=None,
            files=None, auth=None, timeout=None, **kwargs,
        ):
            observed.append((url, timeout))
            return {"ok": True}

        with patch.object(requests.sessions.Session, "request", new=fake_request):
            self.provider(HttpProbeAkShare())._call(
                "probe",
                _http_timeout_seconds=1.25,
                _cap_http_timeout=True,
            )

        self.assertEqual(observed[0][1], 1.25)
        self.assertEqual(observed[1][1], 1.25)
        self.assertEqual(observed[2][1], 1.25)
        self.assertIsNone(observed[3][1])

    def test_cninfo_filings_are_windowed_and_kept_apart_from_media(self):
        self.cninfo_patch.stop()
        rows = {
            "002617": [{
                "title": "关于召开董事会的公告",
                "date": "2026-07-31",
                "url": "https://static.cninfo.com.cn/finalpage/2026-07-31/1.PDF",
            }, {
                "title": "过早的旧公告",
                "date": "2026-07-01",
                "url": "https://static.cninfo.com.cn/finalpage/2026-07-01/2.PDF",
            }],
            "600875": [],
            "300033": [],
        }
        self.cninfo_patch = patch.object(
            AkShareMorningProvider,
            "_fetch_cninfo_rows",
            side_effect=lambda code: rows[code],
        )
        self.cninfo_patch.start()
        self.addCleanup(self.cninfo_patch.stop)
        response = self.provider(FakeAkShare()).get_morning_context(request_fixture())
        official = [item for item in response["data"]["announcementsAndNews"] if item["type"] == "official_announcement"]
        media = [item for item in response["data"]["announcementsAndNews"] if item["type"] == "news"]
        self.assertEqual([item["title"] for item in official], ["关于召开董事会的公告"])
        self.assertEqual(
            [item["title"] for item in response["data"]["watchlist"][0]["officialDisclosure"]["recentBackground"]],
            ["过早的旧公告"],
        )
        self.assertTrue(all(item["sourceType"] == "official_announcement" for item in official))
        self.assertTrue(all(item["sourceType"] == "media_aggregation" for item in media))
        self.assertEqual(response["data"]["watchlist"][0]["officialDisclosure"]["status"], "available")
        self.assertEqual(response["data"]["watchlist"][0]["officialDisclosure"]["latestDisclosureDate"], "2026-07-31")
        self.assertEqual(response["data"]["watchlist"][0]["officialDisclosure"]["queriedAt"], "2026-08-02T08:00:00+08:00")
        query_sources = [
            source for source in response["sources"]
            if source.get("sourceType") == "official_disclosure_query"
            and "信息披露查询" in str(source.get("title") or "")
        ]
        self.assertTrue(query_sources)
        self.assertTrue(all(source.get("dataAsOf") == "2026-08-02T08:00:00+08:00" for source in query_sources))
        self.assertEqual(official[0]["dataAsOf"], "2026-08-02T08:00:00+08:00")
        self.assertIsNone(official[0]["publishedAt"])
        self.assertEqual(official[0]["timePrecision"], "date")
        self.assertEqual(official[0]["disclosureDate"], "2026-07-31")
        self.assertIn("official_announcement", {source["sourceType"] for source in response["sources"]})
        self.assertTrue(any("巨潮官方公告与媒体新闻分栏" in warning for warning in response["meta"]["warnings"]))

    def test_cninfo_records_actual_clock_and_does_not_invent_publication_time(self):
        self.now = lambda: datetime(2026, 8, 2, 17, 8, tzinfo=CN)
        rows = [
            {"title": "昨日披露", "date": "2026-08-01", "url": "https://static.cninfo.com.cn/old.PDF"},
            {"title": "今日仅日期", "date": "2026-08-02", "url": "https://static.cninfo.com.cn/today.PDF"},
        ]
        provider = self.provider(FakeAkShare())
        with patch.object(provider, "_fetch_cninfo_rows", return_value=rows):
            payload = provider._official_announcements(request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertEqual(payload.data["queriedAt"], "2026-08-02T17:08:00+08:00")
        self.assertEqual(payload.data["queryCutoffAt"], "2026-08-02T08:00:00+08:00")
        self.assertEqual([row["title"] for row in payload.data["items"]], ["昨日披露"])
        self.assertIsNone(payload.data["items"][0]["publishedAt"])
        self.assertFalse(payload.data["queryCoverage"]["complete"])
        self.assertEqual(payload.sources[0]["sourceType"], "official_disclosure_query")

    def test_precollected_query_before_cutoff_keeps_its_real_time_and_window_filing(self):
        from cninfo_staging import write_staging
        staging_dir = Path(self.temp_dir.name) / "staging"
        rows = [{"title": "昨日披露", "date": "2026-08-01",
                 "url": "https://static.cninfo.com.cn/old.PDF",
                 "dateKind": "source_disclosure_date",
                 "observedAt": "2026-08-02T07:50:00+08:00", "nextDayDisclosure": False}]
        write_staging(staging_dir, "2026-08-02", datetime(2026, 8, 2, 7, 52, tzinfo=CN),
                      {"002617": {"queriedAt": "2026-08-02T07:50:00+08:00", "rows": rows}},
                      {}, "a" * 64)
        self.now = lambda: datetime(2026, 8, 2, 8, 1, tzinfo=CN)
        provider = self.provider(FakeAkShare())
        provider._request_codes = ["002617"]
        with patch.object(provider, "_fetch_cninfo_rows",
                          side_effect=AssertionError("live query must not run")):
            payload = provider._official_announcements(
                request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        # The recorded query instant is that stock's real 07:50 pre-collection,
        # and the previous-day filing observed then is a judgeable window item.
        self.assertEqual(payload.data["queriedAt"], "2026-08-02T07:50:00+08:00")
        self.assertEqual([row["title"] for row in payload.data["items"]], ["昨日披露"])
        self.assertEqual(payload.data["queryCutoffAt"], "2026-08-02T08:00:00+08:00")

    def test_precollected_batch_time_does_not_overstate_an_early_stock(self):
        from cninfo_staging import write_staging
        staging_dir = Path(self.temp_dir.name) / "staging"
        write_staging(staging_dir, "2026-08-02", datetime(2026, 8, 2, 7, 59, tzinfo=CN),
                      {"002617": {"queriedAt": "2026-08-02T07:45:00+08:00", "rows": []},
                       "600875": {"queriedAt": "2026-08-02T07:58:00+08:00", "rows": []}},
                      {}, "a" * 64)
        self.now = lambda: datetime(2026, 8, 2, 8, 30, tzinfo=CN)
        provider = self.provider(FakeAkShare())
        provider._request_codes = ["002617", "600875"]
        with patch.object(provider, "_fetch_cninfo_rows",
                          side_effect=AssertionError("live query must not run")):
            first = provider._official_announcements(
                request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
            second = provider._official_announcements(
                request_fixture()["universe"][1], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertEqual(first.data["queriedAt"], "2026-08-02T07:45:00+08:00")
        self.assertEqual(second.data["queriedAt"], "2026-08-02T07:58:00+08:00")

    def test_precollected_query_after_cutoff_falls_back_to_live_query(self):
        from cninfo_staging import write_staging
        staging_dir = Path(self.temp_dir.name) / "staging"
        write_staging(staging_dir, "2026-08-02", datetime(2026, 8, 2, 8, 31, tzinfo=CN),
                      {"002617": {"queriedAt": "2026-08-02T08:30:00+08:00", "rows": []}},
                      {}, "a" * 64)
        self.now = lambda: datetime(2026, 8, 2, 9, 0, tzinfo=CN)
        provider = self.provider(FakeAkShare())
        provider._request_codes = ["002617"]
        with patch.object(provider, "_fetch_cninfo_rows", return_value=[]) as live:
            payload = provider._official_announcements(
                request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertTrue(live.called)
        self.assertEqual(payload.data["queriedAt"], "2026-08-02T09:00:00+08:00")

    def test_precollected_watchlist_must_cover_the_request_pool(self):
        from cninfo_staging import write_staging
        staging_dir = Path(self.temp_dir.name) / "staging"
        write_staging(staging_dir, "2026-08-02", datetime(2026, 8, 2, 7, 52, tzinfo=CN),
                      {"002617": {"queriedAt": "2026-08-02T07:50:00+08:00", "rows": []}},
                      {}, "a" * 64)
        self.now = lambda: datetime(2026, 8, 2, 8, 1, tzinfo=CN)
        provider = self.provider(FakeAkShare())
        provider._request_codes = ["002617", "600875", "300033"]
        with patch.object(provider, "_fetch_cninfo_rows", return_value=[]) as live:
            payload = provider._official_announcements(
                request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertTrue(live.called)
        self.assertEqual(payload.data["queriedAt"], "2026-08-02T08:01:00+08:00")

    def test_precollected_pdf_evidence_is_used_without_a_live_fetch(self):
        from cninfo_staging import write_staging
        staging_dir = Path(self.temp_dir.name) / "staging"
        row = {"title": "2026年半年度报告摘要", "date": "2026-08-01",
               "url": "https://static.cninfo.com.cn/finalpage/2026-08-01/0.PDF",
               "dateKind": "source_disclosure_date",
               "observedAt": "2026-08-02T07:50:00+08:00", "nextDayDisclosure": False}
        document = {"status": "available", "kind": "financial_summary", "code": "002617",
                    "title": row["title"], "url": row["url"],
                    "disclosureDate": row["date"], "reportDate": "2026-06-30",
                    "observedAt": "2026-08-02T07:55:00+08:00",
                    "dataAsOf": "2026-08-02T07:55:00+08:00", "pdfSha256": "b" * 64,
                    "metrics": [{"field": "operatingCashFlow",
                                 "label": "经营活动产生的现金流量净额",
                                 "currentToken": "1,000.00", "previousToken": "900.00",
                                 "quote": "经营活动产生的现金流量净额 1,000.00 900.00",
                                 "unitLabel": "元", "currency": "CNY", "pageNumber": 2,
                                 "pageTextSha256": "c" * 64}]}
        write_staging(staging_dir, "2026-08-02", datetime(2026, 8, 2, 7, 56, tzinfo=CN),
                      {"002617": {"queriedAt": "2026-08-02T07:50:00+08:00", "rows": [row]}},
                      {"002617": document}, "a" * 64)
        self.now = lambda: datetime(2026, 8, 2, 9, 0, tzinfo=CN)
        provider = self.provider(FakeAkShare())
        provider._request_codes = ["002617"]
        provider.collect_documents = True
        provider.document_fetcher = lambda *_args: (_ for _ in ()).throw(AssertionError("live pdf fetch"))
        with patch.object(provider, "_fetch_cninfo_rows",
                          side_effect=AssertionError("live query must not run")):
            payload = provider._official_announcements(
                request_fixture()["universe"][0], datetime(2026, 8, 2, 9, 0, tzinfo=CN), date(2026, 7, 31))
        self.assertEqual(payload.data["financialSummary"]["observedAt"], "2026-08-02T07:55:00+08:00")
        self.assertEqual(payload.data["financialSummary"]["pdfSha256"], "b" * 64)
        self.assertEqual([source["url"] for source in payload.sources].count(row["url"]), 1)

    def test_cninfo_empty_query_still_has_actual_completion_time(self):
        self.now = lambda: datetime(2026, 8, 2, 17, 8, tzinfo=CN)
        provider = self.provider(FakeAkShare())
        with patch.object(provider, "_fetch_cninfo_rows", return_value=[]):
            payload = provider._official_announcements(request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertEqual(payload.data["queriedAt"], "2026-08-02T17:08:00+08:00")
        self.assertEqual(payload.data["items"], [])

    def test_pdf_observation_is_separate_and_does_not_duplicate_filing_source(self):
        self.now = lambda: datetime(2026, 8, 2, 8, 30, tzinfo=CN)
        row = {"title": "2026年半年度报告摘要", "date": "2026-07-31", "url": "https://static.cninfo.com.cn/finalpage/2026-07-31/0.PDF", "observedAt": "2026-08-02T08:29:00+08:00", "dateKind": "source_disclosure_date"}
        provider = self.provider(FakeAkShare())
        provider.collect_documents = True
        provider.document_fetcher = lambda *_args: {
            "status": "available", "title": row["title"], "url": row["url"], "disclosureDate": row["date"],
            "observedAt": "2026-08-02T08:31:00+08:00", "pdfSha256": "a" * 64,
        }
        with patch.object(provider, "_fetch_cninfo_rows", return_value=[row]):
            payload = provider._official_announcements(request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertEqual(payload.data["queriedAt"], "2026-08-02T08:30:00+08:00")
        self.assertEqual(payload.data["financialSummary"]["observedAt"], "2026-08-02T08:31:00+08:00")
        self.assertEqual(payload.data["items"][0]["observedAt"], "2026-08-02T08:31:00+08:00")
        self.assertEqual(sum(source["url"] == row["url"] for source in payload.sources), 1)
        self.assertEqual(payload.data["queryCoverage"]["limit"], 30)
        self.assertFalse(payload.data["queryCoverage"]["complete"])

    def test_pdf_failure_is_a_document_gap_not_an_empty_disclosure_query(self):
        row = {"title": "2026年半年度报告摘要", "date": "2026-07-31", "url": "https://static.cninfo.com.cn/finalpage/2026-07-31/0.PDF"}
        provider = self.provider(FakeAkShare())
        provider.collect_documents = True
        def fail(*_args):
            raise ValueError("pdf_extraction_failed")
        provider.document_fetcher = fail
        with patch.object(provider, "_fetch_cninfo_rows", return_value=[row]):
            payload = provider._official_announcements(request_fixture()["universe"][0], datetime(2026, 8, 2, 8, tzinfo=CN), date(2026, 7, 31))
        self.assertEqual(payload.data["status"], "available")
        self.assertEqual(len(payload.data["items"]), 1)
        self.assertEqual(payload.data["financialSummary"], {"status": "unavailable", "reason": "pdf_extraction_failed"})

    def test_normalization_helpers(self):
        self.assertEqual(normalize_stock_code("sz2617"), "002617")
        self.assertEqual(normalize_stock_code(600875), "600875")
        self.assertIsNone(sanitize_json(float("nan")))
        self.assertIsNone(sanitize_json(float("inf")))


if __name__ == "__main__":
    unittest.main()
