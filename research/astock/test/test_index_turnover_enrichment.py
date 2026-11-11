"""Offline contracts for same-index/date/unit amount-only enrichment (single kline request per attempt).

Network is never touched: the kline fetcher, clock and sleep are injected.
"""

from __future__ import annotations

import contextlib
import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))

import akshare_provider  # noqa: E402
from akshare_provider import A_SHARE_INDEXES, AkShareMorningProvider  # noqa: E402


SESSION = date(2026, 10, 8)
PREVIOUS = date(2026, 9, 30)
PRICES = {"sh000001": 3800, "sz399001": 12000, "sz399006": 2500}
AMOUNTS = {"000001": 801_000_000_000, "399001": 650_000_000_000, "399006": 301_000_000_000}
MARKETS = {"000001": 1, "399001": 0, "399006": 0}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ForbiddenFullMapApi:
    """Sina-only primary source. Any full market-map or akshare hist call is a test failure."""

    def __init__(self):
        self.primary_amounts = {}
        self.calls = []

    def stock_zh_index_daily(self, symbol):
        self.calls.append(symbol)
        price = PRICES[symbol]
        current = {"date": SESSION.isoformat(), "open": price - 1, "high": price + 2,
                   "low": price - 2, "close": price, "volume": 110}
        if symbol in self.primary_amounts:
            current["amount"] = self.primary_amounts[symbol]
        return [{"date": PREVIOUS.isoformat(), "open": price - 3, "high": price, "low": price - 4,
                 "close": price - 2, "volume": 100}, current]

    def index_code_id_map_em(self):
        raise AssertionError("full market-map download must not happen")

    def index_zh_a_hist(self, **kwargs):
        raise AssertionError("akshare index_zh_a_hist (hidden full-map download) must not be used")


class KlineFixture:
    """Scripted kline responses; ``scripts[code]`` is a list of behaviours consumed per attempt."""

    def __init__(self, clock=None, cost=0.0):
        self.requests = []
        self.scripts = {}
        self.clock = clock
        self.cost = cost
        self.data = {}
        for symbol, price in PRICES.items():
            code = symbol[2:]
            self.data[code] = {
                "code": code, "market": MARKETS[code], "name": A_SHARE_INDEXES[symbol],
                "klines": [f"{SESSION.isoformat()},{price - 1},{price},{price + 2},{price - 2},110,{AMOUNTS[code]}"],
            }

    def __call__(self, secid, day_text, timeout, total_seconds=None):
        market, code = secid.split(".")
        self.requests.append({"secid": secid, "day": day_text, "timeout": timeout, "total": total_seconds})
        if self.clock is not None:
            self.clock.now += self.cost
        script = self.scripts.get(code)
        if script:
            step = script.pop(0)
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                return step(dict(self.data[code]))
        return dict(self.data[code])

    def count(self, code=None):
        return len([r for r in self.requests if code is None or r["secid"].endswith("." + code)])


def connection_error():
    import requests
    return requests.exceptions.ConnectionError("Remote end closed connection without response")


def http_error(status):
    import requests
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(f"{status}", response=response)


class IndexTurnoverEnrichmentTests(unittest.TestCase):
    def collect(self, kline, api=None, clock=None):
        clock = clock or Clock()
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = AkShareMorningProvider(
                api=api or ForbiddenFullMapApi(), cache_dir=temp_dir, collect_documents=False,
                index_kline_fetcher=kline, index_clock=clock, sleep=clock.sleep)
            with patch("akshare_provider._requests_default_timeout", return_value=contextlib.nullcontext()):
                return provider._index_history(SESSION, PREVIOUS)

    def assert_primary_prices_retained(self, payload):
        self.assertEqual(len(payload.data), 3)
        for item in payload.data:
            self.assertEqual(item["close"], PRICES[item["code"]])
            self.assertEqual(item["open"], PRICES[item["code"]] - 1)
            self.assertEqual(item["volume"], 110)
            self.assertEqual(item["previousClose"], PRICES[item["code"]] - 2)

    # ---- request shape / duplicate download removal ----
    def test_missing_amount_is_enriched_with_exactly_one_request_per_index_and_no_full_map(self):
        kline = KlineFixture()
        payload = self.collect(kline)
        self.assert_primary_prices_retained(payload)
        for item in payload.data:
            self.assertEqual(item["turnover"], AMOUNTS[item["code"][2:]])
            self.assertEqual(item["units"]["turnover"], "CNY")
            self.assertNotEqual(item["priceSourceIds"], item["turnoverSourceIds"])
            self.assertEqual(item["fieldSourceIds"]["turnover"], item["turnoverSourceIds"])
        self.assertEqual(kline.count(), 3)  # first attempts: 3 requests in total
        self.assertEqual([r["secid"] for r in kline.requests], ["1.000001", "0.399001", "0.399006"])
        self.assertTrue(all(r["day"] == "20261008" for r in kline.requests))
        self.assertFalse(any("补源失败" in w for w in payload.warnings))
        self.assertFalse(any("已置为 null" in w for w in payload.warnings))

    def test_existing_primary_amount_is_preserved_without_any_supplement_request(self):
        api = ForbiddenFullMapApi()
        api.primary_amounts = {symbol: index + 1 for index, symbol in enumerate(A_SHARE_INDEXES)}
        kline = KlineFixture()
        payload = self.collect(kline, api=api)
        self.assertEqual(kline.count(), 0)
        for item in payload.data:
            self.assertEqual(item["turnover"], api.primary_amounts[item["code"]])
            self.assertEqual(item["turnoverSourceIds"], item["priceSourceIds"])

    def test_builtin_transport_receives_the_same_shared_absolute_deadline(self):
        clock = Clock()
        fixture = KlineFixture()
        seen = []

        def isolated_stub(secid, day, timeout, total_seconds, **kwargs):
            seen.append({"total": total_seconds, "absolute": kwargs.get("absolute_deadline")})
            return fixture(secid, day, timeout, total_seconds=total_seconds)

        with patch.object(akshare_provider, "fetch_kline_isolated", side_effect=isolated_stub):
            payload = self.collect(AkShareMorningProvider._fetch_index_kline, clock=clock)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(row["absolute"] == 1045.0 for row in seen), seen)
        self.assertTrue(all(row["total"] == 45.0 for row in seen), seen)
        self.assertTrue(all(row["turnover"] is not None for row in payload.data))

    def test_isolation_failure_reports_zero_attempts_for_unstarted_indexes(self):
        kline = KlineFixture()
        kline.scripts["000001"] = [akshare_provider.IndexTurnoverError("isolation", "固定回收故障")]
        payload = self.collect(kline)
        self.assertEqual(kline.count(), 1)
        self.assert_primary_prices_retained(payload)
        self.assertTrue(all(item["turnover"] is None for item in payload.data))
        for symbol in ("sz399001", "sz399006"):
            self.assertTrue(any(symbol in warning and "阶段=isolation，尝试=0次" in warning
                                for warning in payload.warnings), payload.warnings)

    def test_content_decoding_error_is_not_retried_as_a_connection_error(self):
        import requests
        kline = KlineFixture()
        kline.scripts["000001"] = [requests.exceptions.ContentDecodingError("fixed-invalid-content")]
        payload = self.collect(kline)
        self.assertEqual(kline.count("000001"), 1)
        self.assertIsNone(payload.data[0]["turnover"])
        self.assert_primary_prices_retained(payload)

    # ---- retained validations ----
    def test_wrong_date_is_not_used_and_other_indexes_continue(self):
        kline = KlineFixture()
        kline.data["000001"]["klines"] = [kline.data["000001"]["klines"][0].replace(SESSION.isoformat(), PREVIOUS.isoformat())]
        payload = self.collect(kline)
        self.assert_primary_prices_retained(payload)
        self.assertIsNone(payload.data[0]["turnover"])
        self.assertEqual(payload.data[0]["turnoverSourceIds"], [])
        self.assertEqual([i["turnover"] for i in payload.data[1:]], [AMOUNTS["399001"], AMOUNTS["399006"]])
        self.assertTrue(any("阶段=trading_day" in w and "缺少或重复当日" in w for w in payload.warnings))
        self.assertEqual(kline.count("000001"), 1)  # deterministic content error: no retry

    def test_duplicate_same_day_rows_are_rejected(self):
        kline = KlineFixture()
        kline.data["000001"]["klines"] = kline.data["000001"]["klines"] * 2
        payload = self.collect(kline)
        self.assertIsNone(payload.data[0]["turnover"])
        self.assert_primary_prices_retained(payload)

    def test_wrong_missing_or_boolean_identity_is_rejected_without_retry(self):
        cases = [{"market": 0}, {"market": None}, {"market": True}, {"market": 1.5}, {"market": "1%"},
                 {"code": "399006"}, {"code": None}]
        for override in cases:
            with self.subTest(override=override):
                kline = KlineFixture()
                kline.data["000001"].update(override)
                payload = self.collect(kline)
                self.assertIsNone(payload.data[0]["turnover"])
                self.assert_primary_prices_retained(payload)
                self.assertTrue(any("阶段=identity" in w for w in payload.warnings))
                self.assertEqual(kline.count("000001"), 1)

    def test_explicit_wrong_amount_unit_is_rejected_and_correct_unit_accepted(self):
        for fields in ({"amountUnit": "手"}, {"turnoverUnit": "万元"}, {"units": {"turnover": "USD"}},
                       {"amountUnit": "万元", "units": {"turnover": "CNY"}}):
            with self.subTest(fields=fields):
                kline = KlineFixture()
                kline.data["000001"].update(fields)
                payload = self.collect(kline)
                self.assertIsNone(payload.data[0]["turnover"])
                self.assertTrue(any("阶段=unit" in w and "单位不是" in w for w in payload.warnings))
        for fields in ({"units": {"turnover": "CNY"}}, {"units": {"成交额": "元"}}, {"units": {}}, {"amountUnit": "人民币元"}):
            with self.subTest(accepted=fields):
                kline = KlineFixture()
                kline.data["000001"].update(fields)
                self.assertEqual(self.collect(kline).data[0]["turnover"], AMOUNTS["000001"])

    def test_malformed_units_declarations_are_rejected_not_ignored(self):
        malformed = [
            {"units": "万元"}, {"units": "CNY"}, {"units": ["CNY"]}, {"units": None}, {"units": 5}, {"units": True},
            {"units": {"万元": "turnover"}}, {"units": {"turnoverUnit": "万元"}}, {"units": {"volume": "手", "turnover": "CNY"}},
            {"units": {"turnover": None}}, {"units": {"turnover": ["CNY"]}}, {"units": {"turnover": 1}},
            {"amountUnit": None}, {"amountUnit": 1}, {"turnover_unit": "亿元"}, {"成交额单位": "万元"}, {"unit": "手"},
        ]
        for fields in malformed:
            with self.subTest(fields=fields):
                kline = KlineFixture()
                kline.data["000001"].update(fields)
                payload = self.collect(kline)
                self.assertIsNone(payload.data[0]["turnover"], fields)
                self.assertTrue(any("阶段=unit" in w for w in payload.warnings), fields)
                self.assertEqual(kline.count("000001"), 1)  # deterministic content error: no retry
                self.assert_primary_prices_retained(payload)

    def test_amount_outside_the_cny_yuan_range_is_rejected_as_unit_error(self):
        for amount in (8.01e7, 8010, 8.01e15):  # 万元 / 亿元 / absurd
            with self.subTest(amount=amount):
                kline = KlineFixture()
                price = PRICES["sh000001"]
                kline.data["000001"]["klines"] = [f"{SESSION.isoformat()},{price - 1},{price},{price + 2},{price - 2},110,{amount}"]
                payload = self.collect(kline)
                self.assertIsNone(payload.data[0]["turnover"])
                self.assertTrue(any("阶段=unit" in w for w in payload.warnings))

    def test_secondary_price_mismatch_does_not_replace_primary_or_supply_amount(self):
        kline = KlineFixture()
        price = PRICES["sh000001"]
        kline.data["000001"]["klines"] = [f"{SESSION.isoformat()},{price - 1},{price + 100},{price + 2},{price - 2},110,{AMOUNTS['000001']}"]
        payload = self.collect(kline)
        self.assertIsNone(payload.data[0]["turnover"])
        self.assert_primary_prices_retained(payload)
        self.assertTrue(any("阶段=ohlc_crosscheck" in w for w in payload.warnings))

    def test_invalid_amount_remains_null_and_is_never_estimated(self):
        for amount in ("", "0", "-1", "nan", "inf", "not-an-amount"):
            with self.subTest(amount=amount):
                kline = KlineFixture()
                price = PRICES["sh000001"]
                kline.data["000001"]["klines"] = [f"{SESSION.isoformat()},{price - 1},{price},{price + 2},{price - 2},110,{amount}"]
                payload = self.collect(kline)
                self.assertIsNone(payload.data[0]["turnover"])
                self.assert_primary_prices_retained(payload)

    # ---- retries, request cap, budget ----
    def test_connection_closed_every_time_caps_at_three_attempts_per_index_nine_total(self):
        kline = KlineFixture()
        kline.scripts = {code: [connection_error() for _ in range(5)] for code in AMOUNTS}
        payload = self.collect(kline)
        self.assertEqual(kline.count(), 9)
        self.assertEqual([kline.count(c) for c in AMOUNTS], [3, 3, 3])
        self.assert_primary_prices_retained(payload)
        self.assertTrue(all(item["turnover"] is None for item in payload.data))
        failures = [w for w in payload.warnings if "补源失败" in w]
        self.assertEqual(len(failures), 3)
        self.assertTrue(all("阶段=kline_request" in w and "尝试=3次" in w and "ConnectionError" in w for w in failures))

    def test_transient_failure_then_success_uses_second_attempt_only(self):
        kline = KlineFixture()
        kline.scripts = {"000001": [connection_error()]}
        payload = self.collect(kline)
        self.assertEqual(payload.data[0]["turnover"], AMOUNTS["000001"])
        self.assertEqual([kline.count(c) for c in AMOUNTS], [2, 1, 1])
        self.assertFalse(any("补源失败" in w for w in payload.warnings))

    def test_only_connection_level_5xx_and_429_are_retried(self):
        for error, expected in ((http_error(503), 3), (http_error(429), 3), (http_error(404), 1),
                                (ValueError("bad json"), 1), (KeyError("x"), 1)):
            with self.subTest(error=repr(error)):
                kline = KlineFixture()
                kline.scripts = {"000001": [error, error, error]}
                payload = self.collect(kline)
                self.assertEqual(kline.count("000001"), expected)
                self.assertIsNone(payload.data[0]["turnover"])
                self.assertTrue(any("阶段=kline_request" in w and f"尝试={expected}次" in w for w in payload.warnings))

    def test_total_budget_stops_requests_for_remaining_indexes_and_names_the_stage(self):
        clock = Clock()
        kline = KlineFixture(clock=clock, cost=1.0)
        kline.scripts = {code: [connection_error() for _ in range(5)] for code in AMOUNTS}
        with patch.object(akshare_provider, "INDEX_TURNOVER_TOTAL_BUDGET_SECONDS", 3.0):
            payload = self.collect(kline, clock=clock)
        self.assertEqual(kline.count(), 2)  # budget, not the attempt cap, ended it
        self.assertEqual(kline.count("399001") + kline.count("399006"), 0)
        self.assertTrue(all(item["turnover"] is None for item in payload.data))
        self.assert_primary_prices_retained(payload)
        budget = [w for w in payload.warnings if "阶段=budget" in w]
        self.assertEqual(len(budget), 3)
        self.assertTrue(any("尝试=0次" in w for w in budget))
        self.assertTrue(all(r["timeout"][0] <= 5.0 and r["timeout"][1] <= 8.0 for r in kline.requests))
        self.assertTrue(all(r["total"] is not None and r["total"] <= 3.0 for r in kline.requests))

    def test_every_request_timeout_never_exceeds_remaining_budget(self):
        clock = Clock()
        kline = KlineFixture(clock=clock, cost=0.0)
        kline.scripts = {"000001": [connection_error()]}
        with patch.object(akshare_provider, "INDEX_TURNOVER_TOTAL_BUDGET_SECONDS", 4.0):
            self.collect(kline, clock=clock)
        self.assertTrue(all(r["timeout"][1] <= 4.0 for r in kline.requests))

    def test_a_value_returned_after_the_deadline_is_never_accepted(self):
        clock = Clock()
        kline = KlineFixture(clock=clock, cost=10.0)  # each call "takes" 10s of the fake clock, then returns VALID data
        with patch.object(akshare_provider, "INDEX_TURNOVER_TOTAL_BUDGET_SECONDS", 5.0):
            payload = self.collect(kline, clock=clock)
        self.assertEqual(kline.count(), 1)
        self.assertTrue(all(item["turnover"] is None for item in payload.data))
        self.assertTrue(any("阶段=budget" in w and "截止之后才返回" in w for w in payload.warnings))

    # ---- failure semantics ----
    def test_source_failure_keeps_prices_and_missing_amounts_visible(self):
        kline = KlineFixture()
        kline.scripts = {code: [TimeoutError("synthetic source timeout")] * 3 for code in AMOUNTS}
        payload = self.collect(kline)
        self.assert_primary_prices_retained(payload)
        self.assertTrue(all(item["turnover"] is None for item in payload.data))
        self.assertEqual(len(payload.sources), 3)  # only the three primary sources; nothing fabricated
        self.assertTrue(any("补源失败" in w for w in payload.warnings))

    def test_no_stale_or_other_day_value_is_ever_substituted(self):
        kline = KlineFixture()
        kline.scripts = {code: [connection_error() for _ in range(3)] for code in AMOUNTS}
        payload = self.collect(kline)
        for item in payload.data:
            self.assertIsNone(item["turnover"])
            self.assertEqual(item["turnoverSourceIds"], [])

    def test_field_provenance_resolves_to_independent_amount_source(self):
        payload = self.collect(KlineFixture())
        source_by_id = {source["id"]: source for source in payload.sources}
        self.assertEqual(len(source_by_id), 6)
        for item in payload.data:
            amount_source = source_by_id[item["turnoverSourceIds"][0]]
            price_source = source_by_id[item["priceSourceIds"][0]]
            self.assertEqual(amount_source["api"], "eastmoney_index_kline")
            self.assertEqual(amount_source["symbol"], item["code"])
            self.assertEqual(amount_source["sessionDate"], SESSION.isoformat())
            self.assertEqual(amount_source["units"]["turnover"], "CNY")
            self.assertEqual(amount_source["fields"], ["turnover"])
            self.assertEqual(price_source["publisher"], "新浪财经")
            self.assertTrue(set(item["sourceIds"]).issubset(source_by_id))


if __name__ == "__main__":
    unittest.main()
