"""Contract tests for the read-only Quant runtime observer.

The suite intentionally uses only the Python standard library and local
temporary files.  It never reaches a production host or a real HTTP target.
"""

from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "runtime-observer" / "exporter.py"
SPEC = importlib.util.spec_from_file_location("quant_runtime_observer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)

TZ = observer.TZ
CALENDAR_SOURCE = "akshare.tool_trade_date_hist_sina"
INDEX_SCHEMA = "quant.index-close-cache.v1"
SYMBOLS = ("sh000001", "sh000300", "sz399001", "sz399006")


def shanghai(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ)


def weekday_dates(start, end, excluded=()):
    excluded = set(excluded)
    cursor = start
    result = []
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in excluded:
            result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def calendar_payload(
    *,
    start=date(2026, 1, 1),
    end=date(2026, 12, 31),
    excluded=(),
    generated_at="2026-09-01T12:00:00+08:00",
):
    sessions = weekday_dates(start, end, excluded)
    return {
        "schema_version": 1,
        "source": CALENDAR_SOURCE,
        "generated_at": generated_at,
        "count": len(sessions),
        "min_date": sessions[0],
        "max_date": sessions[-1],
        "trading_dates": sessions,
    }


def close_rows(trade_day="2026-09-04"):
    return [
        {
            "symbol": symbol,
            "close": 3900.0 + offset,
            "trade_date": trade_day,
            "source": "tencent",
        }
        for offset, symbol in enumerate(SYMBOLS)
    ]


def index_payload(
    trade_day="2026-09-04",
    *,
    fetched_at="2026-09-04T15:36:00+08:00",
    stale=False,
):
    return {
        "schema_version": INDEX_SCHEMA,
        "trade_date": trade_day,
        "expected_as_of": trade_day,
        "stale": stale,
        "source": "tencent",
        "fetched_at": fetched_at,
        "closes": close_rows(trade_day),
    }


def digest_payload(
    trade_day="2026-09-04",
    *,
    generated_at="2026-09-06T11:00:00+08:00",
    fetched_at="2026-09-04T15:36:00+08:00",
    expected_as_of=None,
    stale=False,
):
    return {
        "generated_at": generated_at,
        "index_closes": close_rows(trade_day),
        "index_close_cache": {
            "available": True,
            "trade_date": trade_day,
            "expected_as_of": expected_as_of or trade_day,
            "stale": stale,
            "source": "tencent",
            "fetched_at": fetched_at,
        },
    }


class Dataset:
    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name)

    def close(self):
        self._temporary.cleanup()

    def write(self, filename, payload):
        (self.path / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def complete(self, *, calendar=None, index=None, digest=None):
        if calendar is not None:
            self.write("trading_calendar.json", calendar)
        if index is not None:
            self.write("index_closes_latest.json", index)
        if digest is not None:
            self.write("research_digest.json", digest)
        return self.path


class DatasetTestCase(unittest.TestCase):
    def setUp(self):
        self.dataset = Dataset()

    def tearDown(self):
        self.dataset.close()


class TradingCalendarTests(DatasetTestCase):
    def test_sunday_uses_friday_without_marking_friday_cache_stale(self):
        now = shanghai(2026, 9, 6, 12)
        path = self.dataset.complete(
            calendar=calendar_payload(),
            index=index_payload(),
            digest=digest_payload(),
        )

        metrics = observer.collect(path, now)

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 1)
        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 1)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 0)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 0)

    def test_friday_calendar_boundary_covers_the_immediately_following_weekend(self):
        calendar = calendar_payload(end=date(2026, 9, 4))

        actual = observer.expected_trade_date(calendar, shanghai(2026, 9, 6, 12))

        self.assertEqual(actual, date(2026, 9, 4))

    def test_weekday_after_friday_boundary_is_outside_known_coverage(self):
        path = self.dataset.complete(
            calendar=calendar_payload(end=date(2026, 9, 4)),
            index=index_payload(),
        )

        metrics = observer.collect(path, shanghai(2026, 9, 7, 10))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)

    def test_explicit_holiday_uses_the_last_session_in_the_calendar(self):
        holiday = {
            date(2026, 10, 1),
            date(2026, 10, 2),
            date(2026, 10, 5),
            date(2026, 10, 6),
            date(2026, 10, 7),
        }
        calendar = calendar_payload(excluded=holiday)

        actual = observer.expected_trade_date(calendar, shanghai(2026, 10, 5, 12))

        self.assertEqual(actual, date(2026, 9, 30))

    def test_trading_day_switches_at_exactly_1530(self):
        calendar = calendar_payload()

        before = observer.expected_trade_date(
            calendar, shanghai(2026, 9, 7, 15, 29, 59)
        )
        at_cutoff = observer.expected_trade_date(
            calendar, shanghai(2026, 9, 7, 15, 30, 0)
        )

        self.assertEqual(before, date(2026, 9, 4))
        self.assertEqual(at_cutoff, date(2026, 9, 7))

    def test_missing_calendar_keeps_freshness_unknown_and_fail_closed(self):
        path = self.dataset.complete(index=index_payload(), digest=digest_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 12))

        self.assertEqual(metrics['radar_runtime_file_read_success{file="calendar"}'], 0)
        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)
        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 1)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)
        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)

    def test_wrong_calendar_schema_fails_closed(self):
        calendar = calendar_payload()
        calendar["schema_version"] = 2
        path = self.dataset.complete(calendar=calendar, index=index_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 12))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)

    def test_unknown_calendar_source_fails_closed(self):
        calendar = calendar_payload()
        calendar["source"] = "untrusted.source"
        path = self.dataset.complete(calendar=calendar, index=index_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 12))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)

    def test_calendar_generated_too_far_in_the_future_fails_closed(self):
        calendar = calendar_payload(generated_at="2026-09-06T12:06:00+08:00")
        path = self.dataset.complete(calendar=calendar, index=index_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 12))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)

    def test_old_but_covering_calendar_stays_usable_and_exports_age(self):
        calendar = calendar_payload(generated_at="2026-08-13T00:00:00+08:00")
        path = self.dataset.complete(calendar=calendar, index=index_payload())
        now = shanghai(2026, 9, 6, 12)

        metrics = observer.collect(path, now)

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 1)
        self.assertGreater(
            metrics["radar_runtime_calendar_age_seconds"], 14 * 24 * 60 * 60
        )
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 0)

    def test_calendar_more_than_550_days_into_future_fails_closed(self):
        calendar = calendar_payload(end=date(2028, 12, 29))
        path = self.dataset.complete(calendar=calendar, index=index_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 12))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)

    def test_implausible_recent_calendar_gap_fails_closed(self):
        sessions = weekday_dates(date(2026, 1, 1), date(2026, 7, 31))
        sessions.extend(["2026-08-03", "2026-09-30"])
        calendar = {
            "schema_version": 1,
            "source": CALENDAR_SOURCE,
            "generated_at": "2026-09-01T12:00:00+08:00",
            "count": len(sessions),
            "min_date": sessions[0],
            "max_date": sessions[-1],
            "trading_dates": sessions,
        }
        path = self.dataset.complete(calendar=calendar, index=index_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 12))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)

    def test_current_weekend_corruption_outside_future_boundary_window_fails_closed(self):
        calendar = calendar_payload()
        calendar["trading_dates"].append("2026-09-06")
        calendar["trading_dates"].sort()
        calendar["count"] = len(calendar["trading_dates"])
        path = self.dataset.complete(calendar=calendar, index=index_payload())

        metrics = observer.collect(path, shanghai(2026, 9, 6, 16))

        self.assertEqual(metrics["radar_runtime_expected_date_known"], 0)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)


class IndexCacheTests(DatasetTestCase):
    NOW = shanghai(2026, 9, 6, 12)

    def collect_index(self, payload, now=None):
        path = self.dataset.complete(calendar=calendar_payload(), index=payload)
        return observer.collect(path, now or self.NOW)

    def test_valid_cache_requires_the_known_schema_and_all_four_indices(self):
        metrics = self.collect_index(index_payload())

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 1)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 0)

    def test_unknown_cache_schema_is_invalid(self):
        payload = index_payload()
        payload["schema_version"] = "quant.index-close-cache.v2"

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)

    def test_declared_stale_cache_is_invalid_even_when_its_date_matches(self):
        metrics = self.collect_index(index_payload(stale=True))

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_expected_as_of_must_equal_cache_trade_date(self):
        payload = index_payload()
        payload["expected_as_of"] = "2026-09-03"

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_missing_or_duplicate_index_fails_the_complete_batch(self):
        missing = index_payload()
        missing["closes"].pop()
        duplicate = index_payload()
        duplicate["closes"][-1]["symbol"] = duplicate["closes"][0]["symbol"]

        for payload in (missing, duplicate):
            with self.subTest(rows=payload["closes"]):
                metrics = self.collect_index(payload)
                self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_nonpositive_nonfinite_boolean_string_closes_fail_closed(self):
        invalid_values = (0, -1, math.inf, math.nan, True, "3900")
        for value in invalid_values:
            with self.subTest(value=value):
                payload = index_payload()
                payload["closes"][0]["close"] = value
                metrics = self.collect_index(payload)
                self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_mixed_trade_dates_fail_the_complete_batch(self):
        payload = index_payload()
        payload["closes"][0]["trade_date"] = "2026-09-03"

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_valid_older_cache_is_structurally_valid_but_stale_after_cutoff(self):
        metrics = self.collect_index(
            index_payload(), now=shanghai(2026, 9, 7, 16, 10)
        )

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 1)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)

    def test_future_cache_trade_date_is_invalid(self):
        payload = index_payload(
            "2026-09-07", fetched_at="2026-09-07T00:00:00+08:00"
        )

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_cache_fetch_before_its_trade_day_is_invalid(self):
        payload = index_payload(fetched_at="2026-09-03T23:59:59+08:00")

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_cache_fetch_more_than_five_minutes_in_future_is_invalid(self):
        payload = index_payload(fetched_at="2026-09-06T12:05:01+08:00")

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)

    def test_non_string_cache_timestamp_fails_closed_without_escaping_collect(self):
        payload = index_payload()
        payload["fetched_at"] = 1780000000

        metrics = self.collect_index(payload)

        self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)
        self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)

    def test_missing_and_malformed_index_files_fail_closed(self):
        path = self.dataset.complete(calendar=calendar_payload())
        missing_metrics = observer.collect(path, self.NOW)
        (path / "index_closes_latest.json").write_text("{", encoding="utf-8")
        malformed_metrics = observer.collect(path, self.NOW)

        for metrics in (missing_metrics, malformed_metrics):
            self.assertEqual(metrics["radar_runtime_index_cache_valid"], 0)
            self.assertEqual(metrics["radar_runtime_index_cache_stale"], 1)


class DigestTests(DatasetTestCase):
    NOW = shanghai(2026, 9, 6, 12)

    def collect_digest(self, payload, now=None):
        path = self.dataset.complete(calendar=calendar_payload(), digest=payload)
        return observer.collect(path, now or self.NOW)

    def test_valid_digest_with_friday_inputs_is_fresh_on_sunday(self):
        metrics = self.collect_digest(digest_payload())

        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 0)

    def test_digest_age_over_90_minutes_is_separate_from_structural_validity(self):
        generated = self.NOW - timedelta(seconds=5401)
        payload = digest_payload(generated_at=generated.isoformat())

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertGreater(metrics["radar_runtime_digest_age_seconds"], 5400)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 0)

    def test_digest_more_than_five_minutes_in_future_is_invalid(self):
        payload = digest_payload(generated_at="2026-09-06T12:05:01+08:00")

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)

    def test_digest_at_five_minute_future_tolerance_is_accepted_with_zero_age(self):
        payload = digest_payload(generated_at="2026-09-06T12:05:00+08:00")

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertEqual(metrics["radar_runtime_digest_age_seconds"], 0)

    def test_digest_generated_before_its_input_fetch_is_invalid(self):
        payload = digest_payload(
            generated_at="2026-09-04T15:30:00+08:00",
            fetched_at="2026-09-04T15:36:00+08:00",
        )

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)

    def test_digest_with_previous_trade_day_inputs_is_stale_after_cutoff(self):
        payload = digest_payload(generated_at="2026-09-07T16:00:00+08:00")

        metrics = self.collect_digest(payload, shanghai(2026, 9, 7, 16, 10))

        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)

    def test_digest_unavailable_cache_is_invalid(self):
        unavailable = digest_payload()
        unavailable["index_close_cache"]["available"] = False

        metrics = self.collect_digest(unavailable)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)

    def test_declared_stale_digest_is_structurally_valid(self):
        payload = digest_payload(stale=True)

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 0)

    def test_declared_stale_digest_after_cutoff_is_valid_with_stale_inputs(self):
        payload = digest_payload(
            trade_day="2026-09-09",
            generated_at="2026-09-10T15:40:01+08:00",
            fetched_at="2026-09-09T15:45:01+08:00",
            expected_as_of="2026-09-10",
            stale=True,
        )

        metrics = self.collect_digest(payload, shanghai(2026, 9, 10, 15, 46))

        self.assertEqual(metrics["radar_runtime_digest_valid"], 1)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)

    def test_fresh_digest_cache_with_mismatched_expected_date_is_invalid(self):
        payload = digest_payload(expected_as_of="2026-09-10", stale=False)

        metrics = self.collect_digest(payload, shanghai(2026, 9, 10, 15, 46))

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)

    def test_digest_requires_four_valid_index_rows(self):
        payload = digest_payload()
        payload["index_closes"].pop()

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)

    def test_non_string_digest_timestamp_fails_closed_without_escaping_collect(self):
        payload = digest_payload()
        payload["generated_at"] = 1780000000

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)

    def test_non_string_digest_input_fetch_timestamp_fails_closed(self):
        payload = digest_payload()
        payload["index_close_cache"]["fetched_at"] = {"bad": "type"}

        metrics = self.collect_digest(payload)

        self.assertEqual(metrics["radar_runtime_digest_valid"], 0)
        self.assertEqual(metrics["radar_runtime_digest_inputs_stale"], 1)


class ReadJsonTests(DatasetTestCase):
    def test_non_object_json_is_rejected(self):
        path = self.dataset.path / "array.json"
        path.write_text("[]", encoding="utf-8")

        with self.assertRaises(ValueError):
            observer.read_json(path)

    def test_oversize_json_is_rejected_before_parsing(self):
        path = self.dataset.path / "oversize.json"
        path.write_bytes(b"{" + b" " * observer.MAX_BYTES + b"}")

        with self.assertRaisesRegex(ValueError, "oversize"):
            observer.read_json(path)


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True


class FakeConnection:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.closed = False

    def request(self, method, path, headers):
        self.calls.append((method, path, headers))

    def getresponse(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


class ProbeTests(unittest.TestCase):
    def run_probe(self, result, target="backend"):
        connection = FakeConnection(result)
        with mock.patch.object(observer, "HTTPConnection", return_value=connection) as build:
            metrics = observer.probe(target)
        return metrics, connection, build

    def test_target_allowlist_is_fixed_and_unknown_target_never_opens_a_url(self):
        self.assertEqual(set(observer.TARGETS), {"backend", "frontend", "grafana"})
        with mock.patch.object(observer, "HTTPConnection") as build:
            with self.assertRaisesRegex(ValueError, "unknown target"):
                observer.probe("http://attacker.invalid/")
        build.assert_not_called()

    def test_http_200_is_success_and_uses_fixed_url_and_two_second_timeout(self):
        response = FakeResponse(200)

        metrics, connection, build = self.run_probe(response)

        self.assertEqual(metrics["probe_success"], 1)
        self.assertEqual(metrics["probe_http_status_code"], 200)
        self.assertGreaterEqual(metrics["probe_duration_seconds"], 0)
        build.assert_called_once_with("quantdinger-backend", 5000, timeout=2)
        self.assertEqual(connection.calls, [("GET", "/api/health", {
            "User-Agent": "quant-runtime-observer/1", "Connection": "close"})])
        self.assertTrue(response.closed)
        self.assertTrue(connection.closed)

    def test_http_500_preserves_status_and_fails_probe(self):
        response = FakeResponse(500)
        metrics, connection, _build = self.run_probe(response)

        self.assertEqual(metrics["probe_success"], 0)
        self.assertEqual(metrics["probe_http_status_code"], 500)
        self.assertTrue(response.closed)
        self.assertTrue(connection.closed)

    def test_redirect_is_not_treated_as_success(self):
        response = FakeResponse(302)
        metrics, connection, _build = self.run_probe(response)

        self.assertEqual(metrics["probe_success"], 0)
        self.assertEqual(metrics["probe_http_status_code"], 302)
        self.assertEqual(len(connection.calls), 1)
        self.assertTrue(response.closed)
        self.assertTrue(connection.closed)

    def test_timeout_fails_closed_with_zero_status(self):
        metrics, connection, _build = self.run_probe(TimeoutError("timed out"))

        self.assertEqual(metrics["probe_success"], 0)
        self.assertEqual(metrics["probe_http_status_code"], 0)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
