"""Offline tests only: injected transports; real network/process creation blocked."""

import json
import socket
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from source import CurlTransport, DoltClient, HEAD_SQL, SourceError


REF = "80q7jtqjrga15h61uufm60h6uqirk6iq"
OTHER = "11111111111111111111111111111111"


def payload(sql, rows, columns=None, **changes):
    names = columns or (list(rows[0]) if rows else ["tradedate", "symbol"])
    result = {
        "query_execution_status": "Success", "query_execution_message": "",
        "repository_owner": "chenditc", "repository_name": "investment_data",
        "commit_ref": "master", "sql_query": sql,
        "schema": [{"columnName": name, "columnType": "text"} for name in names], "rows": rows,
    }
    result.update(changes)
    return 200, json.dumps(result).encode()


class Fake:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sql = []

    def __call__(self, url):
        self.sql.append(parse_qs(urlsplit(url).query)["q"][0])
        response = self.responses.pop(0)
        if callable(response):
            return response(self.sql[-1])
        return payload(self.sql[-1], response)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.process = patch("source.subprocess.Popen", side_effect=AssertionError("process forbidden"))
        self.network.start()
        self.process.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.process.stop)

    def test_pins_once_and_observing_head_never_repins(self):
        fake = Fake([[{"head_hash": REF}], [{"head_hash": OTHER}]])
        client = DoltClient(fake)
        self.assertEqual(client.latest_ref(), REF)
        self.assertEqual(client.latest_ref(), REF)
        self.assertEqual(client.requests, 1)
        self.assertEqual(client.get_head(), OTHER)
        self.assertEqual(client.ref, REF)
        self.assertIn(REF, client.table("final_a_stock_eod_price"))
        self.assertEqual(fake.sql, [HEAD_SQL, HEAD_SQL])

    def test_keyset_pagination_counted_before_and_after(self):
        rows = [{"tradedate": "2026-09-08", "symbol": f"BJ92000{i}"} for i in range(3)]
        fake = Fake([[{"row_count": "3"}], rows[:2], rows[2:], [{"row_count": "3"}]])
        client = DoltClient(fake, REF, page_size=2)
        self.assertEqual(client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol"), "tradedate='2026-09-08'"), rows)
        self.assertIn("`symbol` > 'BJ920001'", fake.sql[2])
        self.assertTrue(all("AS OF '" + REF + "'" in sql for sql in fake.sql))
        self.assertEqual(client.requests, 4)
        common = {"request_i", "http", "rows", "status", "elapsedMs", "queryKind", "table"}
        self.assertTrue(all(set(event) == common | ({"pageIndex"} if event["queryKind"] == "page" else set())
                            for event in client.events))
        self.assertEqual([event["queryKind"] for event in client.events], ["count", "page", "page", "count"])
        self.assertEqual([event["pageIndex"] for event in client.events if "pageIndex" in event], [0, 1])
        self.assertTrue(all(event["table"] == "final_a_stock_eod_price" for event in client.events))
        self.assertTrue(all(type(event["elapsedMs"]) is int and event["elapsedMs"] >= 0 for event in client.events))
        self.assertNotIn("sql", json.dumps(client.events))

    def test_request_diagnostics_head_select_schema_and_elapsed(self):
        now = [100.0]
        def response(sql, rows):
            now[0] += 1.25
            return payload(sql, rows)
        fake = Fake([lambda sql: response(sql, [{"head_hash": REF}]),
                     lambda sql: response(sql, [{"symbol": "SH600000"}]),
                     lambda sql: response(sql, [{"Table": "final_a_stock_eod_price", "Create Table": "DDL"}])])
        with patch("source.time.monotonic", side_effect=lambda: now[0]):
            client = DoltClient(fake)
            table = client.table("final_a_stock_eod_price")
            client.query("SELECT symbol FROM " + table + " LIMIT 1")
            client.query("SHOW CREATE TABLE " + table)
        self.assertEqual([event["queryKind"] for event in client.events], ["head", "select", "schema"])
        self.assertEqual([event["elapsedMs"] for event in client.events], [1250, 1250, 1250])
        self.assertNotIn("table", client.events[0])
        self.assertTrue(all("pageIndex" not in event for event in client.events))

    def test_failed_page_keeps_safe_page_identity_and_elapsed(self):
        now = [100.0]
        row = {"tradedate": "2026-10-15", "symbol": "SECRET_CURSOR_VALUE"}
        def timeout(sql):
            now[0] += 84
            raise SourceError("transport_budget_exhausted")
        fake = Fake([[{"row_count": "2"}], [row], timeout])
        with patch("source.time.monotonic", side_effect=lambda: now[0]):
            client = DoltClient(fake, REF, page_size=1)
            with self.assertRaisesRegex(SourceError, "transport_budget_exhausted"):
                client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol"),
                                      "symbol != 'SECRET_WHERE_VALUE'")
        self.assertEqual(client.requests, 3)
        self.assertEqual(client.events[-1], {
            "request_i": 3, "http": None, "rows": 0, "status": "failed", "elapsedMs": 84000,
            "queryKind": "page", "table": "final_a_stock_eod_price", "pageIndex": 1,
        })
        serialized = json.dumps(client.events)
        for forbidden in ("SECRET", REF, "2026-10-15", "SELECT", "https:", "cursor", "url"):
            self.assertNotIn(forbidden, serialized)

    def test_diagnostics_do_not_log_select_literals_or_server_failure_text(self):
        fake = Fake([lambda sql: payload(sql, [], query_execution_message="SECRET_SERVER_TEXT")])
        client = DoltClient(fake, REF)
        sql = "SELECT 'FROM ts_a_stock_list SECRET_TOKEN' AS value FROM " + client.table("final_a_stock_eod_price")
        with self.assertRaisesRegex(SourceError, "query_message_not_empty"):
            client.query(sql)
        self.assertEqual(client.events[-1]["table"], "final_a_stock_eod_price")
        self.assertEqual(client.events[-1]["queryKind"], "select")
        self.assertEqual(client.events[-1]["status"], "failed")
        self.assertNotIn("SECRET", json.dumps(client.events))
        self.assertNotIn("ts_a_stock_list", json.dumps(client.events))

    def test_multi_table_diagnostic_does_not_misattribute_single_table(self):
        fake = Fake([[{"symbol": "SH600000"}]])
        client = DoltClient(fake, REF)
        client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price")
                     + " JOIN " + client.table("ts_index_weight") + " ON 1=1")
        self.assertEqual(client.events[-1]["queryKind"], "select")
        self.assertNotIn("table", client.events[-1])

    def test_pagination_keeps_public_query_override_on_success_and_failure(self):
        class ObservedClient(DoltClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.public_queries = []

            def query(self, sql):
                self.public_queries.append(sql)
                return super().query(sql)

        row = {"tradedate": "2026-10-15", "symbol": "SH600000"}
        client = ObservedClient(Fake([[{"row_count": "1"}], [row], [], [{"row_count": "1"}]]), REF, page_size=1)
        self.assertEqual(client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol")), [row])
        self.assertEqual(len(client.public_queries), 4)
        self.assertEqual(sum("LIMIT" in sql for sql in client.public_queries), 2)
        self.assertIsNone(client._page_query_context)

        def fail(sql):
            raise SourceError("transport_budget_exhausted")
        client = ObservedClient(Fake([[{"row_count": "2"}], [row], fail]), REF, page_size=1)
        with self.assertRaisesRegex(SourceError, "transport_budget_exhausted"):
            client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol"))
        self.assertEqual(len(client.public_queries), 3)
        self.assertEqual(client.events[-1]["pageIndex"], 1)
        self.assertEqual(client.events[-1]["status"], "failed")
        self.assertIsNone(client._page_query_context)

    def test_page_diagnostic_does_not_label_nested_query_or_later_direct_query(self):
        class NestedClient(DoltClient):
            def query(self, sql):
                if "LIMIT" in sql:
                    super().query("SELECT symbol FROM " + self.table("ts_a_stock_list"))
                return super().query(sql)

        fake = Fake([[{"row_count": "0"}], [{"symbol": "SH600000"}], [], [{"row_count": "0"}],
                     [{"symbol": "SH600000"}]])
        client = NestedClient(fake, REF)
        self.assertEqual(client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol")), [])
        client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price"))
        self.assertEqual([event["queryKind"] for event in client.events], ["count", "select", "page", "count", "select"])
        self.assertNotIn("pageIndex", client.events[1])
        self.assertEqual(client.events[2]["pageIndex"], 0)
        self.assertNotIn("pageIndex", client.events[-1])

    def test_exact_multiple_requires_terminal_empty_page(self):
        row = {"tradedate": "2026-09-08", "symbol": "SH600000"}
        fake = Fake([[{"row_count": "1"}], [row], [], [{"row_count": "1"}]])
        client = DoltClient(fake, REF, page_size=1)
        self.assertEqual(client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol")), [row])
        self.assertEqual(client.requests, 4)
        self.assertEqual([(event["pageIndex"], event["rows"]) for event in client.events if event["queryKind"] == "page"],
                         [(0, 1), (1, 0)])

    def test_empty_dataset_is_verified(self):
        fake = Fake([[{"row_count": "0"}], [], [{"row_count": "0"}]])
        client = DoltClient(fake, REF)
        self.assertEqual(client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol")), [])

    def test_http_200_error_and_rowlimit_are_not_empty_success(self):
        for status in ("Error", "RowLimit", "success", None):
            with self.subTest(status=status):
                fake = Fake([lambda sql: payload(sql, [], query_execution_status=status)])
                client = DoltClient(fake, REF)
                with self.assertRaisesRegex(SourceError, "query_not_success"):
                    client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price"))

    def test_message_identity_echo_and_row_shape_fail_closed(self):
        mutations = [
            {"query_execution_message": "warning"}, {"repository_owner": "elsewhere"},
            {"repository_name": "other"}, {"commit_ref": REF}, {"sql_query": "SELECT 1"},
            {"rows": [{}]}, {"rows": [{"symbol": 12}]}, {"rows": [{"symbol": {}}]},
            {"schema": [{"columnName": "symbol", "columnType": "text"}] * 2},
        ]
        for change in mutations:
            with self.subTest(change=change):
                fake = Fake([lambda sql, change=change: payload(sql, [{"symbol": "SH600000"}], **change)])
                client = DoltClient(fake, REF)
                with self.assertRaises(SourceError):
                    client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price"))

    def test_http_utf8_json_duplicates_nonfinite_and_size_fail(self):
        for response in [(403, b"secret"), (True, b"{}"), (200, b"\xff"), (200, b"[]"),
                         (200, b'{"x":1,"x":2}'), (200, b'{"x":NaN}'), (200, b"x" * 1025), (200, "not bytes")]:
            with self.subTest(response=response[:1]):
                fake = Fake([lambda sql, response=response: response])
                client = DoltClient(fake, REF, max_bytes=1024)
                with self.assertRaises(SourceError) as caught:
                    client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price"))
                self.assertNotIn("secret", str(caught.exception))

    def test_unpinned_other_ref_mutating_unknown_view_and_join_rejected(self):
        fake = Fake([])
        client = DoltClient(fake, REF)
        bad = [
            "SELECT * FROM final_a_stock_eod_price", "SELECT * FROM final_a_stock_eod_price AS OF '" + OTHER + "'",
            "DELETE FROM final_a_stock_eod_price", "SELECT * FROM max_index_date AS OF '" + REF + "'",
            "SELECT * FROM " + client.table("final_a_stock_eod_price") + "; SELECT 1",
            "SELECT * FROM " + client.table("final_a_stock_eod_price") + " INTO OUTFILE 'x'",
            "SELECT * FROM " + client.table("final_a_stock_eod_price") + " JOIN ts_index_weight ON 1=1",
            "SELECT * FROM " + client.table("final_a_stock_eod_price") + " /* ignored */",
        ]
        for sql in bad:
            with self.subTest(sql=sql), self.assertRaises(SourceError):
                client.query(sql)
        self.assertEqual(client.requests, 0)

    def test_show_create_base_table_pinned(self):
        fake = Fake([[{"Table": "final_a_stock_eod_price", "Create Table": "DDL"}]])
        client = DoltClient(fake, REF)
        self.assertEqual(len(client.query("SHOW CREATE TABLE final_a_stock_eod_price AS OF '" + REF + "'")), 1)

    def test_ref_and_limit_validation(self):
        for ref in ("master", REF + "\n", "'", None, 2):
            if ref is None:
                continue
            with self.subTest(ref=ref), self.assertRaises(SourceError):
                DoltClient(Fake([]), ref)
        for kwargs in ({"page_size": True}, {"page_size": 1001}, {"max_rows": 0}, {"max_requests": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                DoltClient(Fake([]), REF, **kwargs)

    def test_duplicates_unordered_and_count_mismatch_reject(self):
        a = {"tradedate": "2026-09-08", "symbol": "A"}
        b = {"tradedate": "2026-09-08", "symbol": "B"}
        cases = [([[a, a]], "2"), ([[b, a]], "2"), ([[a]], "2"), ([[a, b]], "1")]
        for pages, count in cases:
            with self.subTest(count=count, pages=pages):
                fake = Fake([[{"row_count": count}]] + pages + [[{"row_count": count}]])
                client = DoltClient(fake, REF, page_size=3)
                with self.assertRaises(SourceError):
                    client.paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol"))

    def test_count_change_and_request_budget_fail(self):
        row = {"tradedate": "2026-09-08", "symbol": "A"}
        fake = Fake([[{"row_count": "1"}], [row], [{"row_count": "2"}]])
        with self.assertRaisesRegex(SourceError, "count_mismatch"):
            DoltClient(fake, REF).paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol"))
        client = DoltClient(Fake([[{"head_hash": REF}]]), max_requests=1)
        client.latest_ref()
        with self.assertRaisesRegex(SourceError, "request_budget"):
            client.get_head()

    def test_page_and_total_bounds(self):
        fake = Fake([[{"row_count": "2"}]])
        with self.assertRaisesRegex(SourceError, "row_budget"):
            DoltClient(fake, REF, max_rows=1).paginated_rows("final_a_stock_eod_price", ("tradedate", "symbol"))
        fake = Fake([[{"symbol": "A"}, {"symbol": "B"}]])
        client = DoltClient(fake, REF, page_size=1)
        with self.assertRaises(SourceError):
            client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price"))

    def test_key_requires_unique_complete_projected_fields(self):
        client = DoltClient(Fake([]), REF)
        for columns, order in [(("symbol",), ("symbol",)), (("tradedate", "symbol"), ("symbol",)),
                               (("tradedate", "symbol"), ("tradedate", "symbol", "high"))]:
            with self.subTest(columns=columns, order=order), self.assertRaises(SourceError):
                client.paginated_rows("final_a_stock_eod_price", columns, order_fields=order)
        self.assertEqual(client.requests, 0)

    def test_numeric_calendar_key_and_safe_literal(self):
        self.assertLess(DoltClient._sort_key("ts_trade_day_calendar", ("id",), {"id": "2"}),
                        DoltClient._sort_key("ts_trade_day_calendar", ("id",), {"id": "10"}))
        self.assertEqual(DoltClient._literal("a'b"), "'a''b'")
        with self.assertRaises(SourceError):
            DoltClient._literal("bad\\value")

    def test_curl_commands_have_no_writes_or_inherited_proxy_route(self):
        transport = CurlTransport()
        for route in ("direct", "proxy"):
            command = transport._command("https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master?q=SELECT", route)
            self.assertEqual(command[:2], ["curl", "-q"])
            self.assertIn("--max-filesize", command)
            self.assertNotIn("--insecure", command)
            self.assertNotIn("--output", command)
            self.assertEqual(command[command.index("--noproxy") + 1], "*" if route == "direct" else "")

    def test_curl_bounded_fallback_and_no_quota_retry(self):
        transport = CurlTransport()
        with patch.object(transport, "_once", side_effect=[(28, 0, b""), (0, 200, b"{}")] ) as once:
            self.assertEqual(transport("url"), (200, b"{}"))
            self.assertEqual(once.call_count, 2)
        for status in (400, 401, 403, 429):
            transport = CurlTransport()
            with patch.object(transport, "_once", return_value=(0, status, b"secret")) as once:
                with self.assertRaisesRegex(SourceError, "http_rejected"):
                    transport("url")
                self.assertEqual(once.call_count, 1)
                self.assertNotIn("secret", json.dumps(transport.events))

    def test_curl_oversize_does_not_retry(self):
        transport = CurlTransport()
        with patch.object(transport, "_once", side_effect=SourceError("response_too_large")) as once:
            with self.assertRaises(SourceError):
                transport("url")
            self.assertEqual(once.call_count, 1)

    def test_one_additional_route_cycle_keeps_exact_same_pinned_url(self):
        transport = CurlTransport()
        url = "https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master?q=AS+OF+" + REF
        with patch.object(transport, "_once", side_effect=[(28, 0, b""), (56, 0, b""), (0, 200, b"ok")]) as once:
            self.assertEqual(transport(url), (200, b"ok"))
        self.assertEqual([call.args for call in once.call_args_list],
                         [(url, "direct"), (url, "proxy"), (url, "direct")])
        self.assertNotIn(REF, json.dumps(transport.events))
        self.assertNotIn(url, json.dumps(transport.events))

    def test_temporary_gateway_errors_exhaust_at_four_attempts(self):
        transport = CurlTransport()
        with patch.object(transport, "_once", side_effect=[(0, 502, b"secret"), (0, 503, b"secret"),
                                                         (0, 504, b"secret"), (0, 502, b"secret")]) as once:
            with self.assertRaisesRegex(SourceError, "transport_exhausted"):
                transport("url")
        self.assertEqual(once.call_count, 4)
        self.assertEqual([event["transport"] for event in transport.events], ["direct", "proxy", "direct", "proxy"])
        self.assertEqual([event["attempt"] for event in transport.events], [1, 2, 3, 4])
        self.assertNotIn("secret", json.dumps(transport.events))

    def test_only_explicit_transient_curl_codes_retry(self):
        for code in (5, 6, 7, 18, 28, 35, 52, 55, 56, 92):
            transport = CurlTransport()
            with self.subTest(code=code), patch.object(transport, "_once", side_effect=[(code, 0, b""), (0, 200, b"ok")]) as once:
                self.assertEqual(transport("url"), (200, b"ok"))
                self.assertEqual(once.call_count, 2)
        for code in (1, 2, 3, 23, 26, 27, 47, 51, 58, 60, 77, 99):
            transport = CurlTransport()
            with self.subTest(code=code), patch.object(transport, "_once", return_value=(code, 0, b"secret")) as once:
                with self.assertRaisesRegex(SourceError, "curl_nonretryable_error"):
                    transport("url")
                self.assertEqual(once.call_count, 1)

    def test_permission_quota_and_other_server_errors_stop_after_prior_transient(self):
        for status in (400, 401, 403, 404, 429, 500, 501, 507):
            transport = CurlTransport()
            with self.subTest(status=status), patch.object(transport, "_once", side_effect=[(7, 0, b""), (0, status, b"secret")]) as once:
                with self.assertRaisesRegex(SourceError, "http_rejected"):
                    transport("url")
                self.assertEqual(once.call_count, 2)

    def test_timeout_exception_retries_but_process_or_unknown_error_does_not(self):
        transport = CurlTransport()
        with patch.object(transport, "_once", side_effect=[SourceError("transport_timeout"), (0, 200, b"ok")]) as once:
            self.assertEqual(transport("url"), (200, b"ok"))
            self.assertEqual(once.call_count, 2)
        for code, expected in [("transport_process_failed", "transport_process_failed"),
                               ("SELECT secret_url_and_body", "transport_internal_error")]:
            transport = CurlTransport()
            with self.subTest(code=code), patch.object(transport, "_once", side_effect=SourceError(code)) as once:
                with self.assertRaisesRegex(SourceError, expected) as caught:
                    transport("url")
                self.assertEqual(once.call_count, 1)
                self.assertNotIn("secret", str(caught.exception) + json.dumps(transport.events))

    def test_http200_invalid_json_or_rowlimit_is_not_retried(self):
        for invalid in ("json", "RowLimit", "Error"):
            transport = CurlTransport()
            def response(url, route, **kwargs):
                sql = parse_qs(urlsplit(url).query)["q"][0]
                if invalid == "json":
                    return 0, 200, b"{secret_invalid_json"
                status, body = payload(sql, [], query_execution_status=invalid)
                return 0, status, body
            with self.subTest(invalid=invalid), patch.object(transport, "_once", side_effect=response) as once:
                client = DoltClient(transport, REF)
                with self.assertRaises(SourceError) as caught:
                    client.query("SELECT symbol FROM " + client.table("final_a_stock_eod_price"))
                self.assertEqual(once.call_count, 1)
                self.assertNotIn("secret", str(caught.exception))

    def test_four_attempt_plan_cannot_expand_original_44_second_budget(self):
        now = [0.0]
        transport = CurlTransport(clock=lambda: now[0])
        self.assertEqual(transport.total_timeout, 44)
        def response(url, route, **kwargs):
            now[0] += kwargs["timeout"]
            return 28, 0, b""
        with patch.object(transport, "_once", side_effect=response) as once:
            with self.assertRaisesRegex(SourceError, "transport_budget_exhausted"):
                transport("url")
        self.assertEqual(once.call_count, 3)
        self.assertEqual([call.kwargs["timeout"] for call in once.call_args_list], [20, 20, 2])
        self.assertTrue(all(call.kwargs["deadline"] == 42 for call in once.call_args_list))
        self.assertLessEqual(now[0], 44)

    def test_configured_40_second_attempt_accepts_observed_slow_count(self):
        now = [0.0]
        transport = CurlTransport(timeout=40, clock=lambda: now[0])
        def response(url, route, **kwargs):
            self.assertEqual(kwargs["timeout"], 40)
            now[0] += 23.35
            return 0, 200, b"ok"
        with patch.object(transport, "_once", side_effect=response) as once:
            self.assertEqual(transport("url"), (200, b"ok"))
            self.assertEqual(once.call_count, 1)
        self.assertEqual(transport.total_timeout, 84)

    def test_configured_slow_attempt_cannot_extend_global_deadline(self):
        transport = CurlTransport(timeout=40, clock=lambda: 100.0)
        with patch.object(transport, "_once", return_value=(0, 200, b"ok")) as once:
            self.assertEqual(transport("url", deadline=109), (200, b"ok"))
        self.assertEqual(once.call_args.kwargs["timeout"], 7)
        self.assertEqual(once.call_args.kwargs["deadline"], 107)

    def test_client_deadline_clips_transport_and_connect_timeouts(self):
        transport = CurlTransport(clock=lambda: 100.0)
        with patch("source.time.monotonic", return_value=100.0):
            client = DoltClient(transport, REF, max_seconds=9)
            sql = "SELECT symbol FROM " + client.table("final_a_stock_eod_price")
            status, body = payload(sql, [{"symbol": "SH600000"}])
            with patch.object(transport, "_once", return_value=(0, status, body)) as once:
                self.assertEqual(client.query(sql), [{"symbol": "SH600000"}])
        self.assertEqual(once.call_args.kwargs["timeout"], 7)
        self.assertEqual(once.call_args.kwargs["deadline"], 107)
        url = "https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master?q=SELECT"
        command = transport._command(url, "direct", timeout=2.5)
        self.assertEqual(command[command.index("--connect-timeout") + 1], "2.5")
        self.assertEqual(command[command.index("--max-time") + 1], "2.5")
        self.assertNotIn("--retry", command)

    def test_exhausted_deadline_does_not_spawn_or_accept_late_success(self):
        now = [100.0]
        transport = CurlTransport(clock=lambda: now[0])
        with patch.object(transport, "_once") as once:
            with self.assertRaisesRegex(SourceError, "transport_budget_exhausted"):
                transport("url", deadline=101)
            once.assert_not_called()
        def late(url, route, **kwargs):
            now[0] += 45
            return 0, 200, b"ok"
        with patch.object(transport, "_once", side_effect=late) as once:
            with self.assertRaisesRegex(SourceError, "transport_budget_exhausted"):
                transport("url")
            self.assertEqual(once.call_count, 1)

    def test_transport_budget_and_response_frame_fail_closed(self):
        with self.assertRaisesRegex(SourceError, "above_original_bound"):
            CurlTransport(total_timeout=45)
        transport = CurlTransport()
        for deadline in (float("inf"), float("nan"), True):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(SourceError, "invalid_transport_deadline"):
                transport("url", deadline=deadline)
        with patch.object(transport, "_once", return_value=(0, 0, b"")) as once:
            with self.assertRaisesRegex(SourceError, "invalid_transport_response"):
                transport("url")
            self.assertEqual(once.call_count, 1)
        with patch.object(transport, "_once", return_value=(63, 200, b"")) as once:
            with self.assertRaisesRegex(SourceError, "response_too_large"):
                transport("url")
            self.assertEqual(once.call_count, 1)

    def test_numeric_curl_trailer_is_removed_from_body_and_bounded(self):
        transport = CurlTransport()
        raw = (b'{"secret_body":"never logged"}\nQLIB_TRANSPORT_METRICS:'
               b'0.001234,0.020000,0.125999,23.350000,23.360000,2\nQLIB_HTTP_STATUS:200')
        self.assertEqual(transport._decode_output(raw, 0), (0, 200, b'{"secret_body":"never logged"}'))
        self.assertEqual(transport._attempt_diagnostic, {
            "diagnosticStatus": "available", "nameLookupMs": 1, "connectMs": 20,
            "appConnectMs": 125, "startTransferMs": 23350, "transferTotalMs": 23360, "numRedirects": 2})
        self.assertNotIn("secret", json.dumps(transport._attempt_diagnostic))
        command = transport._command("https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master?q=x", "direct")
        trailer = command[command.index("--write-out") + 1]
        for forbidden in ("url_effective", "remote_ip", "local_ip", "stderr", "header"):
            self.assertNotIn(forbidden, trailer)
        self.assertEqual(command[command.index("--max-redirs") + 1], "2")

    def test_invalid_or_missing_diagnostic_trailer_does_not_change_http_body_gate(self):
        transport = CurlTransport(max_bytes=2)
        for metrics in (b'SECRET,https://private,127.0.0.1,user:pass,token=x,0',
                        b'NaN,0.000000,0.000000,0.000000,0.000000,0',
                        b'-1.000000,0.000000,0.000000,0.000000,0.000000,0',
                        b'99999.000000,0.000000,0.000000,0.000000,0.000000,0',
                        b'0.000000,0.000000,0.000000,0.000000,0.000000,true'):
            with self.subTest(metrics=metrics):
                self.assertEqual(transport._decode_output(b'{}\nQLIB_TRANSPORT_METRICS:' + metrics + b'\nQLIB_HTTP_STATUS:200', 0), (0, 200, b'{}'))
                self.assertEqual(transport._attempt_diagnostic, {"diagnosticStatus": "invalid"})
        self.assertEqual(transport._decode_output(b'{}\nQLIB_HTTP_STATUS:200', 0), (0, 200, b'{}'))
        self.assertEqual(transport._attempt_diagnostic, {"diagnosticStatus": "unavailable"})
        self.assertEqual(transport._decode_output(b'{}\nQLIB_HTTP_STATUS:secret', 28), (28, 0, b''))
        with self.assertRaisesRegex(SourceError, "response_too_large"):
            transport._decode_output(b'LONG\nQLIB_TRANSPORT_METRICS:bad\nQLIB_HTTP_STATUS:200', 0)

    def test_parent_timeout_cannot_inherit_prior_attempt_metrics(self):
        now = [0.0]
        transport = CurlTransport(timeout=40, clock=lambda: now[0])
        def response(url, route, **kwargs):
            now[0] += kwargs["timeout"]
            if not transport.events:
                return transport._decode_output(b'\nQLIB_TRANSPORT_METRICS:0.001000,0.020000,0.100000,0.000000,40.000000,0\nQLIB_HTTP_STATUS:000', 28)
            raise SourceError("transport_timeout")
        with patch.object(transport, "_once", side_effect=response):
            with self.assertRaisesRegex(SourceError, "transport_budget_exhausted"):
                transport("url")
        self.assertEqual(transport.last_events[0]["diagnosticStatus"], "available")
        self.assertEqual(transport.last_events[1]["diagnosticStatus"], "unavailable")
        self.assertNotIn("nameLookupMs", transport.last_events[1])
        self.assertEqual(transport.total_timeout, 84)
        self.assertEqual(now[0], 82)

    def test_transport_failure_is_bound_to_current_page_not_prior_success(self):
        transport = CurlTransport(timeout=40)
        client = DoltClient(transport, REF)
        first = "SELECT symbol FROM " + client.table("final_a_stock_eod_price")
        def response(url, route, **kwargs):
            sql = parse_qs(urlsplit(url).query)["q"][0]
            if sql == first:
                status, body = payload(sql, [{"symbol": "SH600000"}])
                return 0, status, body
            return 28, 0, b''
        with patch.object(transport, "_once", side_effect=response):
            self.assertEqual(client.query(first), [{"symbol": "SH600000"}])
            second = first + " ORDER BY symbol LIMIT 500"
            with self.assertRaisesRegex(SourceError, "transport_exhausted"):
                client._query_page(second, 27)
        self.assertEqual(len(transport.events), 5)
        self.assertEqual(len(client.last_transport_events), 4)
        self.assertTrue(all(e["request_i"] == 2 and e["pageIndex"] == 27 and e["queryKind"] == "page"
                            and e["table"] == "final_a_stock_eod_price" and e["http"] == 0
                            for e in client.last_transport_events))
        self.assertNotIn(REF, json.dumps(client.last_transport_events))
        self.assertNotIn("SELECT", json.dumps(client.last_transport_events))

    def test_pre_request_budget_failure_clears_last_request_diagnostics(self):
        transport = CurlTransport()
        client = DoltClient(transport, REF, max_requests=1)
        sql = "SELECT symbol FROM " + client.table("final_a_stock_eod_price")
        status, body = payload(sql, [{"symbol": "SH600000"}])
        with patch.object(transport, "_once", return_value=(0, status, body)) as once:
            client.query(sql)
            self.assertEqual(len(client.last_transport_events), 1)
            with self.assertRaisesRegex(SourceError, "request_budget_exhausted"):
                client.query(sql)
        self.assertEqual(once.call_count, 1)
        self.assertEqual(client.last_transport_events, [])
        self.assertIsNone(client.last_source_event)

    def test_fake_curl_pipe_frame_flows_through_real_once_and_client_without_network(self):
        transport = CurlTransport(timeout=40)
        client = DoltClient(transport, REF)
        sql = "SELECT symbol FROM " + client.table("final_a_stock_eod_price")
        _, body = payload(sql, [{"symbol": "SH600000"}])
        framed = body + b'\nQLIB_TRANSPORT_METRICS:0.001000,0.020000,0.100000,0.200000,0.210000,1\nQLIB_HTTP_STATUS:200'
        process, selector = MagicMock(), MagicMock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        process.stdout.fileno.return_value = 71
        selector.__enter__.return_value = selector
        selector.select.return_value = [(object(), None)]
        with (patch("source.subprocess.Popen", return_value=process) as popen,
              patch("source.selectors.DefaultSelector", return_value=selector),
              patch("source.os.read", side_effect=[framed[:30], framed[30:], b""])):
            self.assertEqual(client.query(sql), [{"symbol": "SH600000"}])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(client.last_transport_events[0]["request_i"], 1)
        self.assertEqual(client.last_transport_events[0]["startTransferMs"], 200)
        self.assertEqual(client.last_transport_events[0]["numRedirects"], 1)
        self.assertEqual(client.last_transport_events[0]["bytes"], len(body))
        process.stdout.close.assert_called_once()
        process.kill.assert_not_called()

    def test_rejected_query_cannot_reuse_previous_success_diagnostics(self):
        transport = CurlTransport()
        client = DoltClient(transport, REF)
        sql = "SELECT symbol FROM " + client.table("final_a_stock_eod_price")
        status, body = payload(sql, [{"symbol": "SH600000"}])
        with patch.object(transport, "_once", return_value=(0, status, body)) as once:
            client.query(sql)
            with self.assertRaisesRegex(SourceError, "query_not_pinned"):
                client.query(sql.replace(REF, OTHER))
        self.assertEqual(once.call_count, 1)
        self.assertEqual(client.last_transport_events, [])
        self.assertIsNone(client.last_source_event)

    def test_diagnostic_frame_allowance_cannot_expand_response_body_bound(self):
        transport = CurlTransport(max_bytes=2)
        process, selector = MagicMock(), MagicMock()
        process.wait.return_value = 0
        process.poll.return_value = None
        process.stdout.fileno.return_value = 71
        selector.__enter__.return_value = selector
        selector.select.return_value = [(object(), None)]
        with (patch("source.subprocess.Popen", return_value=process),
              patch("source.selectors.DefaultSelector", return_value=selector),
              patch("source.os.read", return_value=b"x" * (transport.max_bytes + transport._FRAME_BYTES + 1))):
            with self.assertRaisesRegex(SourceError, "response_too_large"):
                transport._once("https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master?q=x", "direct")
        process.kill.assert_called_once()
        process.stdout.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
