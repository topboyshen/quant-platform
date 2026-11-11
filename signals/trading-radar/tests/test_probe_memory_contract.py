"""Offline lifecycle and safety regressions for the Quant HTTP probe.

Run a candidate without installing it or touching a server:
    OBSERVER_MODULE_PATH=/absolute/path/exporter.py python3 -m unittest \
        discover -s tests -p test_probe_memory_contract.py -v

The default is this task's candidate module. All probe transports are fakes;
socket connection and DNS entry points are additionally denied in every test.
These tests prove resource/control-flow contracts, not a production RSS bound.
"""

from __future__ import annotations

import gc
import importlib.util
import os
import socket
import ssl
import threading
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from http.client import BadStatusLine, HTTPException
from pathlib import Path
from unittest import mock

DEFAULT_MODULE = (
    Path(__file__).resolve().parents[1]
    / "deploy/runtime-observer/exporter.py"
)
MODULE_PATH = Path(os.environ.get("OBSERVER_MODULE_PATH", str(DEFAULT_MODULE)))
if not MODULE_PATH.is_file():
    raise FileNotFoundError(
        f"Candidate module not found: {MODULE_PATH}. Set OBSERVER_MODULE_PATH."
    )
SPEC = importlib.util.spec_from_file_location("observer_memory_contract", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)

EXPECTED_TARGETS = {
    "backend": "http://quantdinger-backend:5000/api/health",
    "frontend": "http://quantdinger-frontend:80/",
    "grafana": "http://grafana:3000/api/health",
}
EXPECTED_CONNECTIONS = {
    "backend": ("quantdinger-backend", 5000, "/api/health"),
    "frontend": ("quantdinger-frontend", 80, "/"),
    "grafana": ("grafana", 3000, "/api/health"),
}


class FakeResponse:
    def __init__(self, status=200, *, status_error=None):
        self._status = status
        self.status_error = status_error
        self.closed = False
        self.close_count = 0
        self.body_reads = 0
        self.headers = {"Location": "https://untrusted.invalid/"}

    @property
    def status(self):
        if self.status_error is not None:
            raise self.status_error
        return self._status

    def read(self, *args, **kwargs):
        self.body_reads += 1
        raise AssertionError("Health probes must not read response bodies")

    def close(self):
        self.closed = True
        self.close_count += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class FakeConnection:
    def __init__(
        self, host, port=None, *, timeout=None, response=None,
        request_error=None, getresponse_error=None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.response = response if response is not None else FakeResponse()
        self.request_error = request_error
        self.getresponse_error = getresponse_error
        self.requests = []
        self.getresponse_count = 0
        self.closed = False
        self.close_count = 0

    def request(self, method, path, body=None, headers=None, **kwargs):
        self.requests.append((method, path, body, dict(headers or {}), kwargs))
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        self.getresponse_count += 1
        if self.getresponse_error is not None:
            raise self.getresponse_error
        return self.response

    def close(self):
        self.closed = True
        self.close_count += 1


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        for method in ("create_connection", "getaddrinfo"):
            patcher = mock.patch.object(
                socket, method,
                side_effect=AssertionError("Real network and DNS are forbidden"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def probe_with(self, target="backend", **connection_options):
        created = []

        def factory(host, port=None, *, timeout=None):
            connection = FakeConnection(
                host, port, timeout=timeout, **connection_options
            )
            created.append(connection)
            return connection

        with mock.patch.object(observer, "HTTPConnection", side_effect=factory):
            metrics = observer.probe(target)
        self.assertEqual(len(created), 1)
        return metrics, created[0]

    def assert_failed_closed(self, metrics, connection, code=0):
        self.assertEqual(metrics["probe_success"], 0)
        self.assertEqual(metrics["probe_http_status_code"], code)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.close_count, 1)


class HttpResourceContractTests(OfflineTestCase):
    def test_three_fixed_target_urls_are_unchanged(self):
        self.assertEqual(observer.TARGETS, EXPECTED_TARGETS)

    def test_each_target_uses_its_fixed_host_port_path_and_timeout(self):
        for target, (host, port, path) in EXPECTED_CONNECTIONS.items():
            with self.subTest(target=target):
                metrics, connection = self.probe_with(target)
                self.assertEqual(
                    (connection.host, connection.port, connection.timeout),
                    (host, port, 2),
                )
                self.assertEqual(len(connection.requests), 1)
                method, actual_path, body, headers, extra = connection.requests[0]
                self.assertEqual((method, actual_path, body), ("GET", path, None))
                self.assertEqual(headers, {
                    "User-Agent": "quant-runtime-observer/1", "Connection": "close"})
                self.assertEqual(extra, {})
                self.assertEqual(metrics["probe_success"], 1)
                self.assertEqual(metrics["probe_http_status_code"], 200)
                self.assertGreaterEqual(metrics["probe_duration_seconds"], 0)
                self.assertEqual(set(metrics), {
                    "probe_success", "probe_http_status_code", "probe_duration_seconds"
                })

    def test_unknown_target_is_rejected_before_constructing_a_connection(self):
        values = ("", "BACKEND", "http://attacker.invalid/", "//127.0.0.1/", "../../")
        with mock.patch.object(observer, "HTTPConnection") as connection:
            for target in values:
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, "unknown target"):
                        observer.probe(target)
        connection.assert_not_called()

    def test_accidental_https_configuration_fails_closed_without_transport(self):
        with mock.patch.dict(observer.TARGETS, {"backend": "https://backend.invalid/"}), \
             mock.patch.object(observer, "HTTPConnection") as connection:
            metrics = observer.probe("backend")
        self.assertEqual(metrics["probe_success"], 0)
        self.assertEqual(metrics["probe_http_status_code"], 0)
        connection.assert_not_called()

    def test_success_closes_response_and_connection_without_reading_body(self):
        metrics, connection = self.probe_with()
        self.assertEqual(metrics["probe_success"], 1)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.close_count, 1)
        self.assertTrue(connection.response.closed)
        self.assertEqual(connection.response.close_count, 1)
        self.assertEqual(connection.response.body_reads, 0)

    def test_non_200_success_statuses_still_fail_and_close(self):
        for status in (201, 204, 206):
            with self.subTest(status=status):
                metrics, connection = self.probe_with(response=FakeResponse(status))
                self.assert_failed_closed(metrics, connection, status)
                self.assertTrue(connection.response.closed)

    def test_redirect_codes_never_create_a_followup_request(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                metrics, connection = self.probe_with(response=FakeResponse(status))
                self.assert_failed_closed(metrics, connection, status)
                self.assertEqual(len(connection.requests), 1)
                self.assertTrue(connection.response.closed)
                self.assertEqual(connection.response.body_reads, 0)

    def test_http_error_statuses_preserved_and_response_closed(self):
        for status in (400, 401, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                metrics, connection = self.probe_with(response=FakeResponse(status))
                self.assert_failed_closed(metrics, connection, status)
                self.assertTrue(connection.response.closed)

    def test_request_timeout_closes_connection_without_getresponse(self):
        metrics, connection = self.probe_with(request_error=TimeoutError("offline"))
        self.assert_failed_closed(metrics, connection)
        self.assertEqual(connection.getresponse_count, 0)

    def test_request_http_exception_closes_connection(self):
        metrics, connection = self.probe_with(request_error=HTTPException("offline"))
        self.assert_failed_closed(metrics, connection)

    def test_getresponse_timeout_closes_connection(self):
        metrics, connection = self.probe_with(getresponse_error=TimeoutError("offline"))
        self.assert_failed_closed(metrics, connection)

    def test_malformed_status_line_closes_connection(self):
        metrics, connection = self.probe_with(getresponse_error=BadStatusLine("bad"))
        self.assert_failed_closed(metrics, connection)

    def test_status_access_error_closes_response_and_connection(self):
        response = FakeResponse(status_error=OSError("offline status failure"))
        metrics, connection = self.probe_with(response=response)
        self.assert_failed_closed(metrics, connection)
        self.assertTrue(response.closed)
        self.assertEqual(response.close_count, 1)

    def test_constructor_failure_returns_failed_metrics(self):
        with mock.patch.object(observer, "HTTPConnection", side_effect=OSError("offline")):
            metrics = observer.probe("backend")
        self.assertEqual(metrics["probe_success"], 0)
        self.assertEqual(metrics["probe_http_status_code"], 0)

    def test_environment_proxies_are_not_used_or_changed(self):
        proxy_values = {
            "HTTP_PROXY": "http://forbidden.invalid:9999",
            "HTTPS_PROXY": "http://forbidden.invalid:9998",
            "ALL_PROXY": "socks5://forbidden.invalid:9997",
            "http_proxy": "http://forbidden.invalid:9996",
            "https_proxy": "http://forbidden.invalid:9995",
            "all_proxy": "socks5://forbidden.invalid:9994",
            "NO_PROXY": "",
            "no_proxy": "",
        }
        with mock.patch.dict(os.environ, proxy_values, clear=False):
            metrics, connection = self.probe_with()
            self.assertEqual(connection.host, "quantdinger-backend")
            self.assertEqual(metrics["probe_success"], 1)
            self.assertEqual({key: os.environ[key] for key in proxy_values}, proxy_values)

    def test_http_probes_do_not_build_tls_contexts_or_urllib_openers(self):
        with mock.patch.object(ssl, "SSLContext", side_effect=AssertionError("TLS forbidden")), \
             mock.patch("urllib.request.build_opener", side_effect=AssertionError("opener forbidden")), \
             mock.patch("urllib.request.getproxies", side_effect=AssertionError("proxy lookup forbidden")):
            for target in EXPECTED_TARGETS:
                metrics, _ = self.probe_with(target)
                self.assertEqual(metrics["probe_success"], 1)

    def test_concurrent_probes_do_not_share_connections_or_status(self):
        created = []
        lock = threading.Lock()
        codes = {"quantdinger-backend": 200, "quantdinger-frontend": 503, "grafana": 302}

        def factory(host, port=None, *, timeout=None):
            connection = FakeConnection(host, port, timeout=timeout,
                                        response=FakeResponse(codes[host]))
            with lock:
                created.append(connection)
            return connection

        targets = list(EXPECTED_TARGETS) * 8
        with mock.patch.object(observer, "HTTPConnection", side_effect=factory):
            with ThreadPoolExecutor(max_workers=4) as executor:
                metrics = list(executor.map(observer.probe, targets))
        self.assertEqual(len(created), len(targets))
        self.assertEqual(len({id(connection) for connection in created}), len(targets))
        for target, result in zip(targets, metrics):
            expected = codes[EXPECTED_CONNECTIONS[target][0]]
            self.assertEqual(result["probe_http_status_code"], expected)
            self.assertEqual(result["probe_success"], int(expected == 200))
        self.assertTrue(all(item.closed and item.response.closed for item in created))

    def test_repeated_success_and_failure_do_not_retain_connection_graphs(self):
        references = []
        count = 0

        def factory(host, port=None, *, timeout=None):
            nonlocal count
            count += 1
            connection = FakeConnection(host, port, timeout=timeout,
                                        response=FakeResponse(200 if count % 2 else 503))
            if count % 3 == 0:
                connection.getresponse_error = BadStatusLine("offline")
            references.extend((weakref.ref(connection), weakref.ref(connection.response)))
            return connection

        gc.collect()
        with mock.patch.object(observer, "HTTPConnection", side_effect=factory):
            for _ in range(256):
                observer.probe("backend")
        gc.collect()
        self.assertEqual(count, 256)
        self.assertTrue(all(reference() is None for reference in references))


def fake_handler(path, slots):
    handler = object.__new__(observer.Handler)
    handler.path = path
    handler.slots = slots
    handler.responses = []
    handler.respond = lambda status, body: handler.responses.append((status, body))
    return handler


class RequestGateContractTests(OfflineTestCase):
    def test_malformed_probe_queries_cannot_reach_probe(self):
        paths = (
            "/probe", "/probe?target=", "/probe?target=unknown",
            "/probe?target=backend&target=frontend",
            "/probe?target=backend&target=backend",
            "/probe?target=backend&url=http%3A%2F%2Funtrusted.invalid",
            "/probe?target=http%3A%2F%2F127.0.0.1%3A1%2F",
            "/probe?target=%2F%2Funtrusted.invalid%2F",
        )
        with mock.patch.object(observer, "probe") as probe:
            for path in paths:
                with self.subTest(path=path):
                    handler = fake_handler(path, threading.BoundedSemaphore(4))
                    handler.do_GET()
                    self.assertEqual(handler.responses, [(400, b"unknown target\n")])
        probe.assert_not_called()

    def test_unknown_paths_and_query_bearing_metrics_return_404(self):
        for path in ("/", "/secret", "/metrics?target=backend", "/health?extra=1"):
            with self.subTest(path=path):
                handler = fake_handler(path, threading.BoundedSemaphore(4))
                with mock.patch.object(observer, "probe") as probe, \
                     mock.patch.object(observer, "collect") as collect:
                    handler.do_GET()
                self.assertEqual(handler.responses, [(404, b"not found\n")])
                probe.assert_not_called()
                collect.assert_not_called()

    def test_unexpected_probe_exception_still_releases_slot(self):
        slots = threading.BoundedSemaphore(1)
        handler = fake_handler("/probe?target=backend", slots)
        with mock.patch.object(observer, "probe", side_effect=RuntimeError("test only")):
            with self.assertRaisesRegex(RuntimeError, "test only"):
                handler.do_GET()
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_response_write_exception_still_releases_slot(self):
        slots = threading.BoundedSemaphore(1)
        handler = fake_handler("/probe?target=backend", slots)
        handler.respond = mock.Mock(side_effect=BrokenPipeError("test only"))
        with mock.patch.object(observer, "probe", return_value={"probe_success": 1}):
            with self.assertRaises(BrokenPipeError):
                handler.do_GET()
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_four_active_probe_slots_reject_fifth_but_allow_health(self):
        slots = threading.BoundedSemaphore(4)
        condition = threading.Condition()
        release = threading.Event()
        errors = []
        active = 0

        def blocked_probe(target):
            nonlocal active
            with condition:
                active += 1
                condition.notify_all()
            if not release.wait(timeout=3):
                raise AssertionError("Test worker release timed out")
            return {"probe_success": 1}

        def execute(handler):
            try:
                handler.do_GET()
            except BaseException as exc:
                errors.append(exc)

        handlers = [fake_handler("/probe?target=backend", slots) for _ in range(4)]
        threads = [threading.Thread(target=execute, args=(handler,)) for handler in handlers]
        with mock.patch.object(observer, "probe", side_effect=blocked_probe) as probe:
            try:
                for thread in threads:
                    thread.start()
                with condition:
                    self.assertTrue(condition.wait_for(lambda: active == 4, timeout=2))
                rejected = fake_handler("/probe?target=backend", slots)
                rejected.do_GET()
                self.assertEqual(rejected.responses, [(503, b"busy\n")])
                health = fake_handler("/health", slots)
                health.do_GET()
                self.assertEqual(health.responses, [(200, b"ok\n")])
                self.assertEqual(probe.call_count, 4)
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertTrue(all(item.responses[0][0] == 200 for item in handlers))
        for _ in range(4):
            self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))
        for _ in range(4):
            slots.release()


if __name__ == "__main__":
    unittest.main()
