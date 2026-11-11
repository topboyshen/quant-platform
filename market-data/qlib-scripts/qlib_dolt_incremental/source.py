"""Bounded, read-only, commit-pinned DoltHub reader. No CLI or import side effects.

query() accepts application-owned SELECT / fixed-schema inspection SQL, not an
arbitrary SQL sandbox. Domain checks (dates, market identities, units and price
coverage) remain the caller's responsibility. No response body or URL is logged.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from urllib.parse import urlencode, urlsplit


ENDPOINT = "https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master"
HEAD_SQL = "SELECT DOLT_HASHOF('HEAD') AS head_hash"
REF_RE = re.compile(r"[0-9a-v]{32}\Z")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TABLE_KEYS = {
    "final_a_stock_eod_price": (("tradedate", "symbol"),),
    "ts_index_weight": (("index_code", "stock_code", "trade_date"),),
    "ts_trade_day_calendar": (("id",), ("exchange", "date")),
    "ts_a_stock_list": (("ts_code",),),
}
TABLE_COLUMNS = {
    "final_a_stock_eod_price": {"tradedate", "symbol", "high", "low", "open", "close", "adjclose", "volume", "amount"},
    "ts_index_weight": {"index_code", "stock_code", "trade_date", "weight"},
    "ts_trade_day_calendar": {"id", "exchange", "date", "is_open"},
    "ts_a_stock_list": {"ts_code", "symbol", "exchange", "list_date", "delist_date"},
}


class SourceError(RuntimeError):
    """Only safe machine codes; never raw server text, URLs or subprocess output."""


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise SourceError("invalid_" + name)
    return value


def _ref(value):
    if not isinstance(value, str) or REF_RE.fullmatch(value) is None:
        raise SourceError("invalid_ref")
    return value


def _identifier(value):
    if not isinstance(value, str) or IDENT_RE.fullmatch(value) is None:
        raise SourceError("invalid_identifier")
    return value


def _unquote_sql(sql):
    """Blank string literals, preserving offsets; reject unsupported escapes."""
    if not isinstance(sql, str) or not sql or len(sql) > 16000 or "\\" in sql or "\x00" in sql:
        raise SourceError("invalid_sql")
    result = list(sql)
    i = 0
    while i < len(sql):
        if sql[i] != "'":
            i += 1
            continue
        start = i
        i += 1
        while i < len(sql):
            if sql[i] == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                i += 1
                result[start:i] = " " * (i - start)
                break
            i += 1
        else:
            raise SourceError("unterminated_sql_literal")
    return "".join(result)


def _validate_sql(sql, ref):
    scrubbed = _unquote_sql(sql)
    if any(item in scrubbed for item in (";", "--", "/*", "*/", "#", '"', "@")):
        raise SourceError("unsupported_sql_syntax")
    if re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|CALL|SET|INTO|OUTFILE|DUMPFILE|LOAD|LOCK|UNLOCK|GET_LOCK|RELEASE_LOCK|SLEEP|BENCHMARK)\b", scrubbed, re.I):
        if not re.fullmatch(r"\s*SHOW\s+CREATE\s+TABLE\s+`?[A-Za-z_][A-Za-z0-9_]*`?\s+AS\s+OF\s+\s*", scrubbed, re.I):
            raise SourceError("non_readonly_sql")
    if re.match(r"\s*SELECT\b", scrubbed, re.I):
        matches = list(re.finditer(r"\b(?:FROM|JOIN)\s+(`?)([A-Za-z_][A-Za-z0-9_]*)\1", scrubbed, re.I))
        if not matches:
            raise SourceError("select_requires_pinned_table")
        for match in matches:
            if match.group(2) not in TABLE_KEYS:
                raise SourceError("unsupported_table")
            suffix = sql[match.end():]
            pin = re.match(r"\s+AS\s+OF\s+'([0-9a-v]{32})'", suffix, re.I)
            if pin is None or pin.group(1) != ref:
                raise SourceError("query_not_pinned")
    elif re.match(r"\s*SHOW\s+CREATE\s+TABLE\b", scrubbed, re.I):
        match = re.fullmatch(r"\s*SHOW\s+CREATE\s+TABLE\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s+AS\s+OF\s+'([0-9a-v]{32})'\s*", sql, re.I)
        if match is None or match.group(1) not in TABLE_KEYS or match.group(2) != ref:
            raise SourceError("query_not_pinned")
    else:
        raise SourceError("unsupported_query")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError("duplicate_json_key")
        result[key] = value
    return result


def _query_diagnostic(sql, page_index=None):
    """Return only fixed labels from validated SQL, never SQL or its literals.

    ``page_index`` is supplied only by the internal pagination loop. Arbitrary
    SELECT callers remain ``select``; a LIMIT alone does not claim a page index.
    """
    if sql == HEAD_SQL:
        return {"queryKind": "head"}
    scrubbed = _unquote_sql(sql)
    diagnostic = {"queryKind": "select"}
    if re.match(r"\s*SHOW\s+CREATE\s+TABLE\b", scrubbed, re.I):
        diagnostic["queryKind"] = "schema"
    elif re.match(r"\s*SELECT\s+COUNT\s*\(\s*\*\s*\)", scrubbed, re.I):
        diagnostic["queryKind"] = "count"
    elif type(page_index) is int and page_index >= 0:
        diagnostic.update(queryKind="page", pageIndex=page_index)
    tables = {
        match.group(2)
        for match in re.finditer(r"\b(?:FROM|JOIN|TABLE)\s+(`?)([A-Za-z_][A-Za-z0-9_]*)\1", scrubbed, re.I)
        if match.group(2) in TABLE_KEYS
    }
    if len(tables) == 1:
        diagnostic["table"] = next(iter(tables))
    return diagnostic


class CurlTransport:
    """At most four attempts: direct/SOCKS, then one repeat of the same routes.

    stdin closed; no curl config, auth, output file, inherited proxy selection or
    TLS relaxation. stdout is bounded in memory; stderr is discarded. HTTP 4xx
    (including quota/rate-limit errors) are not retried through another route.
    Only explicit transient curl errors or HTTP 502/503/504 are retried. The URL
    is unchanged, so a pinned AS OF query cannot repin or move its page cursor.
    All attempts share the ORIGINAL two-route wall budget (44 seconds by default),
    also clipped to the DoltClient deadline. No sleep or unbounded curl retry.
    """

    _NETWORK_RCS = frozenset({5, 6, 7, 18, 28, 35, 52, 55, 56, 92})
    _SAFE_EXCEPTION_CODES = frozenset({"transport_timeout", "transport_process_failed", "response_too_large",
        "unsupported_endpoint", "transport_budget_exhausted", "invalid_transport_deadline"})
    _FRAME_BYTES = 256
    _TIMING_FIELDS = ("nameLookupMs", "connectMs", "appConnectMs", "startTransferMs", "transferTotalMs")
    _WRITE_OUT = ("\nQLIB_TRANSPORT_METRICS:%{time_namelookup},%{time_connect},"
                  "%{time_appconnect},%{time_starttransfer},%{time_total},%{num_redirects}"
                  "\nQLIB_HTTP_STATUS:%{http_code}")

    def __init__(self, *, max_bytes=2 * 1024 * 1024, timeout=20, connect_timeout=8,
                 curl="curl", proxy="socks5h://127.0.0.1:7891", total_timeout=None, clock=None):
        self.max_bytes = _positive(max_bytes, "max_bytes")
        self.timeout = _positive(timeout, "timeout")
        self.connect_timeout = _positive(connect_timeout, "connect_timeout")
        if proxy != "socks5h://127.0.0.1:7891":
            raise SourceError("unsupported_proxy")
        original_budget = 2 * (self.timeout + 2)
        self.total_timeout = original_budget if total_timeout is None else _positive(total_timeout, "total_timeout")
        if self.total_timeout > original_budget:
            raise SourceError("transport_budget_above_original_bound")
        self._clock = time.monotonic if clock is None else clock
        if not callable(self._clock):
            raise SourceError("invalid_transport_clock")
        self.curl, self.proxy = curl, proxy
        self.events = []
        self.last_events = []
        self._attempt_diagnostic = {"diagnosticStatus": "unavailable"}

    def _command(self, url, route, *, timeout=None):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "www.dolthub.com" or parts.path != urlsplit(ENDPOINT).path:
            raise SourceError("unsupported_endpoint")
        timeout = self.timeout if timeout is None else timeout
        if type(timeout) not in (int, float) or not 0 < timeout <= self.timeout:
            raise SourceError("invalid_transport_deadline")
        args = [self.curl, "-q", "--silent", "--show-error", "--location", "--max-redirs", "2",
                "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
                "--connect-timeout", str(min(self.connect_timeout, timeout)), "--max-time", str(timeout),
                "--max-filesize", str(self.max_bytes), "--write-out", self._WRITE_OUT]
        args += ["--noproxy", "*"] if route == "direct" else ["--proxy", self.proxy, "--noproxy", ""]
        return args + [url]

    def _decode_output(self, output, rc):
        """Parse only curl's fixed numeric trailer; never retain body/URL/IP.

        Timings are cumulative elapsed values reported by curl, not proof of a
        DNS/TCP/TLS failure cause. Missing/invalid telemetry never promotes or
        rejects a response that the existing HTTP/content gates would accept.
        """
        self._attempt_diagnostic = {"diagnosticStatus": "unavailable"}
        body, separator, status = output.rpartition(b"\nQLIB_HTTP_STATUS:")
        if not separator or re.fullmatch(rb"[0-9]{3}", status) is None:
            return rc, 0, b""
        plain, metrics_separator, metrics = body.rpartition(b"\nQLIB_TRANSPORT_METRICS:")
        if metrics_separator:
            body = plain
            self._attempt_diagnostic = {"diagnosticStatus": "invalid"}
            values = metrics.split(b",")
            if (len(values) == 6
                    and all(re.fullmatch(rb"[0-9]{1,5}\.[0-9]{6}", value) for value in values[:5])
                    and re.fullmatch(rb"[0-9]{1,2}", values[5])):
                times = [int(value.split(b".")[0]) * 1000 + int(value.split(b".")[1]) // 1000
                         for value in values[:5]]
                if all(value <= 86400000 for value in times):
                    self._attempt_diagnostic = {"diagnosticStatus": "available",
                        **dict(zip(self._TIMING_FIELDS, times)), "numRedirects": int(values[5])}
        if len(body) > self.max_bytes:
            raise SourceError("response_too_large")
        return rc, int(status), body

    def _once(self, url, route, *, timeout=None, deadline=None):
        proc = None
        self._attempt_diagnostic = {"diagnosticStatus": "unavailable"}
        try:
            timeout = self.timeout if timeout is None else timeout
            proc = subprocess.Popen(self._command(url, route, timeout=timeout), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            local_deadline = self._clock() + timeout + 2
            deadline = local_deadline if deadline is None else min(deadline, local_deadline)
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise SourceError("transport_timeout")
                    if not selector.select(min(remaining, 0.25)):
                        continue
                    chunk = os.read(proc.stdout.fileno(), min(65536, self.max_bytes + self._FRAME_BYTES + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > self.max_bytes + self._FRAME_BYTES:
                        raise SourceError("response_too_large")
            rc = proc.wait(timeout=max(0.01, deadline - self._clock()))
            return self._decode_output(bytes(output), rc)
        except (OSError, subprocess.SubprocessError):
            raise SourceError("transport_process_failed") from None
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
                if proc.stdout is not None:
                    proc.stdout.close()

    def __call__(self, url, *, deadline=None):
        self.last_events = []
        now = self._clock()
        request_deadline = now + self.total_timeout
        if deadline is not None:
            if type(deadline) not in (int, float) or not (-float("inf") < deadline < float("inf")):
                raise SourceError("invalid_transport_deadline")
            request_deadline = min(request_deadline, deadline)
        for attempt, route in enumerate(("direct", "proxy", "direct", "proxy"), start=1):
            remaining = request_deadline - self._clock()
            # Reserve the existing two-second kill/reap bound inside the shared
            # deadline. Never start a fresh full timeout after budget exhaustion.
            if remaining <= 2:
                raise SourceError("transport_budget_exhausted")
            attempt_timeout = min(self.timeout, remaining - 2)
            attempt_started = self._clock()
            self._attempt_diagnostic = {"diagnosticStatus": "unavailable"}
            metadata = {"transport": route, "attempt": attempt,
                        "attemptTimeoutMs": int(attempt_timeout * 1000),
                        "remainingBudgetMs": int(remaining * 1000)}
            def record(**values):
                event = {**metadata, **values, **self._attempt_diagnostic,
                         "attemptElapsedMs": max(0, int((self._clock() - attempt_started) * 1000))}
                self.events.append(event)
                self.last_events.append(event)
            try:
                rc, status, body = self._once(url, route, timeout=attempt_timeout, deadline=request_deadline - 2)
            except SourceError as exc:
                code = str(exc) if str(exc) in self._SAFE_EXCEPTION_CODES else "transport_internal_error"
                record(status="failed", code=code)
                if code == "transport_timeout":
                    continue
                raise SourceError(code) from None
            if type(rc) is not int or type(status) is not int or not isinstance(body, bytes):
                raise SourceError("invalid_transport_result")
            record(curl_rc=rc, http=status, bytes=len(body))
            if self._clock() > request_deadline:
                raise SourceError("transport_budget_exhausted")
            if rc == 63:
                raise SourceError("response_too_large")
            if rc == 0 and status == 200:
                return status, body
            if 400 <= status < 500 or (status and status not in {200, 502, 503, 504}):
                raise SourceError("http_rejected")
            if rc != 0 and rc not in self._NETWORK_RCS:
                # E.g. 51/60 certificate verification, local IO, malformed URL,
                # redirect limit and unsupported protocol are not transient.
                raise SourceError("curl_nonretryable_error")
            if status not in {502, 503, 504} and rc not in self._NETWORK_RCS:
                raise SourceError("invalid_transport_response")
        raise SourceError("transport_exhausted")


class DoltClient:
    def __init__(self, transport=None, ref=None, *, page_size=200, max_rows=100000,
                 max_requests=256, max_bytes=2 * 1024 * 1024, max_seconds=900):
        self.page_size = _positive(page_size, "page_size")
        if self.page_size > 1000:
            raise SourceError("page_size_above_safety_bound")
        self.max_rows = _positive(max_rows, "max_rows")
        self.max_requests = _positive(max_requests, "max_requests")
        self.max_bytes = _positive(max_bytes, "max_bytes")
        self.max_seconds = _positive(max_seconds, "max_seconds")
        self._ref = _ref(ref) if ref is not None else None
        self.transport = transport if transport is not None else CurlTransport(max_bytes=max_bytes)
        self.requests, self.events = 0, []
        self.last_transport_events = []
        self.last_source_event = None
        self._started_at = time.monotonic()

    @property
    def ref(self):
        return self._ref

    def _query(self, sql, row_bound):
        self.last_transport_events = []
        self.last_source_event = None
        if self.requests >= self.max_requests:
            raise SourceError("request_budget_exhausted")
        request_started_at = time.monotonic()
        if request_started_at - self._started_at > self.max_seconds:
            raise SourceError("time_budget_exhausted")
        self.requests += 1
        page_context = getattr(self, "_page_query_context", None)
        page_index = (page_context[1] if isinstance(page_context, tuple) and len(page_context) == 2
                      and page_context[0] == sql else None)
        event = {"request_i": self.requests, "http": None, "rows": 0, "status": "failed",
                 "elapsedMs": 0, **_query_diagnostic(sql, page_index)}
        self.events.append(event)
        self.last_source_event = event
        transport_started = False
        try:
            url = ENDPOINT + "?" + urlencode({"q": sql})
            if isinstance(self.transport, CurlTransport):
                transport_started = True
                status, raw = self.transport(url, deadline=self._started_at + self.max_seconds)
            else:
                status, raw = self.transport(url)
            event["http"] = status if type(status) is int else None
            if type(status) is not int or status != 200:
                raise SourceError("http_rejected")
            if not isinstance(raw, bytes) or len(raw) > self.max_bytes:
                raise SourceError("response_too_large_or_invalid")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object,
                                 parse_constant=lambda _: (_ for _ in ()).throw(SourceError("nonfinite_json")))
            if not isinstance(payload, dict):
                raise SourceError("invalid_response_root")
            if payload.get("query_execution_status") != "Success":
                raise SourceError("query_not_success")
            if payload.get("query_execution_message") != "":
                raise SourceError("query_message_not_empty")
            if (payload.get("repository_owner"), payload.get("repository_name"), payload.get("commit_ref")) != ("chenditc", "investment_data", "master"):
                raise SourceError("response_identity_mismatch")
            if payload.get("sql_query") != sql:
                raise SourceError("query_echo_mismatch")
            schema, rows = payload.get("schema"), payload.get("rows")
            if not isinstance(schema, list) or not schema or not isinstance(rows, list) or len(rows) > row_bound:
                raise SourceError("invalid_schema_or_row_bound")
            names = []
            for column in schema:
                if not isinstance(column, dict) or not isinstance(column.get("columnName"), str) or not isinstance(column.get("columnType"), str):
                    raise SourceError("invalid_schema")
                names.append(column["columnName"])
            if len(set(names)) != len(names):
                raise SourceError("duplicate_schema_column")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(names):
                    raise SourceError("row_shape_mismatch")
                if any(value is not None and not isinstance(value, str) for value in row.values()):
                    raise SourceError("invalid_cell_type")
            event.update(rows=len(rows), status="Success")
            return rows
        except SourceError:
            raise
        except Exception:
            raise SourceError("invalid_transport_or_response") from None
        finally:
            event["elapsedMs"] = max(0, int((time.monotonic() - request_started_at) * 1000))
            if transport_started:
                identity = {"request_i": self.requests, **_query_diagnostic(sql, page_index)}
                self.last_transport_events = [{**item, **identity} for item in self.transport.last_events]

    def get_head(self):
        rows = self._query(HEAD_SQL, 1)
        if len(rows) != 1 or set(rows[0]) != {"head_hash"}:
            raise SourceError("invalid_head_result")
        return _ref(rows[0]["head_hash"])

    def latest_ref(self):
        if self._ref is None:
            self._ref = self.get_head()
        return self._ref

    def table(self, name):
        if name not in TABLE_KEYS:
            raise SourceError("unsupported_table")
        return f"`{name}` AS OF '{self.latest_ref()}'"

    def query(self, sql):
        ref = self.latest_ref()
        self.last_transport_events = []
        self.last_source_event = None
        _validate_sql(sql, ref)
        return self._query(sql, self.page_size)

    def _query_page(self, sql, page_index):
        # Keep query(sql) as the public dispatch point for subclasses and test
        # doubles. Only the exact internally constructed page gets this label;
        # a nested or rewritten query must not inherit another page's identity.
        previous = getattr(self, "_page_query_context", None)
        self._page_query_context = (sql, page_index)
        try:
            return self.query(sql)
        finally:
            self._page_query_context = previous

    def _count(self, table_sql, where):
        rows = self.query(f"SELECT COUNT(*) AS row_count FROM {table_sql} WHERE ({where})")
        if len(rows) != 1 or set(rows[0]) != {"row_count"} or not isinstance(rows[0]["row_count"], str) or re.fullmatch(r"0|[1-9][0-9]{0,19}", rows[0]["row_count"]) is None:
            raise SourceError("invalid_count")
        count = int(rows[0]["row_count"])
        if count > self.max_rows:
            raise SourceError("row_budget_exhausted")
        return count

    @staticmethod
    def _literal(value):
        if not isinstance(value, str) or any(ord(char) < 32 for char in value) or "\\" in value:
            raise SourceError("invalid_cursor")
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _sort_key(table, fields, row):
        values = []
        for field in fields:
            value = row[field]
            if not isinstance(value, str):
                raise SourceError("null_or_invalid_key")
            if table == "ts_trade_day_calendar" and field == "id":
                if re.fullmatch(r"[0-9]+", value) is None:
                    raise SourceError("invalid_numeric_key")
                value = int(value)
            values.append(value)
        return tuple(values)

    def paginated_rows(self, table, columns, where="1=1", order_fields=None):
        if table not in TABLE_KEYS:
            raise SourceError("unsupported_table")
        if not isinstance(columns, (tuple, list)) or not columns or isinstance(order_fields, str):
            raise SourceError("invalid_columns_or_order")
        columns = tuple(_identifier(column) for column in columns)
        fields = tuple(_identifier(field) for field in (order_fields or TABLE_KEYS[table][0]))
        if (len(set(columns)) != len(columns) or len(set(fields)) != len(fields)
                or not set(columns) <= TABLE_COLUMNS[table] or not set(fields) <= set(columns)
                or not any(set(key) <= set(fields) for key in TABLE_KEYS[table])):
            raise SourceError("non_unique_or_missing_order_key")
        if not isinstance(where, str) or not where:
            raise SourceError("invalid_where")
        table_sql = self.table(table)
        expected = self._count(table_sql, where)
        result, previous, cursor, page_index = [], None, None, 0
        while True:
            predicate = f"({where})"
            if cursor is not None:
                terms = []
                for i, field in enumerate(fields):
                    prefix = [f"`{prior}` = {self._literal(cursor[prior])}" for prior in fields[:i]]
                    terms.append("(" + " AND ".join(prefix + [f"`{field}` > {self._literal(cursor[field])}"]) + ")")
                predicate += " AND (" + " OR ".join(terms) + ")"
            sql = ("SELECT " + ",".join(f"`{column}`" for column in columns) + f" FROM {table_sql} WHERE {predicate}"
                   + " ORDER BY " + ",".join(f"`{field}`" for field in fields) + f" LIMIT {self.page_size}")
            page = self._query_page(sql, page_index)
            for row in page:
                if set(row) != set(columns):
                    raise SourceError("page_columns_mismatch")
                key = self._sort_key(table, fields, row)
                if previous is not None and key <= previous:
                    raise SourceError("non_increasing_or_duplicate_key")
                previous = key
                result.append(row)
                if len(result) > expected or len(result) > self.max_rows:
                    raise SourceError("count_mismatch")
            if len(page) < self.page_size:
                break
            cursor = page[-1]
            page_index += 1
        if len(result) != expected or self._count(table_sql, where) != expected:
            raise SourceError("count_mismatch")
        return result
