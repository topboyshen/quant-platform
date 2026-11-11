"""Real HTTP against a LOCAL fake server (127.0.0.1 only; no external network, no eastmoney).

Proves what unit fakes cannot: the wall-clock deadline holds against slow headers and slow-drip bodies,
redirects are never followed, and the number of ACTUAL HTTP requests never exceeds the cap.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import akshare_provider  # noqa: E402
from akshare_provider import AkShareMorningProvider, IndexTurnoverError  # noqa: E402
from test_index_turnover_enrichment import (  # noqa: E402
    AMOUNTS, PRICES, PREVIOUS, SESSION, ForbiddenFullMapApi, KlineFixture)


class FakeEastmoney:
    def __init__(self, mode):
        self.mode = mode
        self.hits = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.open_requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.close_connection = True
                with outer.lock:
                    outer.hits.append(self.path)
                    outer.open_requests += 1
                try:
                    outer.respond(self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # An intentionally killed client must close its socket.
                finally:
                    with outer.lock:
                        outer.open_requests -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/kline"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def body(self, path):
        code = path.split("secid=")[1].split("&")[0].split(".")[1] if "secid=" in path else "000001"
        data = KlineFixture().data[code]
        if self.mode == "wrong_unit" and code == "000001":
            data["amountUnit"] = "万元"
        document = {"data": data}
        if self.mode.startswith("envelope_unit_"):
            data["units"] = {"turnover": "CNY"}
            if self.mode == "envelope_unit_wrong":
                document["amountUnit"] = "万元"
            elif self.mode == "envelope_unit_malformed":
                document["units"] = {"amount": None}
            elif self.mode == "envelope_unit_yuan":
                document["amountUnit"] = "人民币元"
        return json.dumps(document).encode()

    def drip(self, h):
        while not self.stopped.is_set():
            h.wfile.write(b" ")
            h.wfile.flush()
            self.stopped.wait(0.02)

    def wait_for_peer_close(self, h):
        # No headers ever arrive. Notice socket closure so tests can distinguish
        # terminating the HTTP worker from merely abandoning its result.
        while not self.stopped.wait(0.02):
            readable, _, _ = select.select([h.connection], [], [], 0)
            if readable and h.connection.recv(1, socket.MSG_PEEK) == b"":
                return

    def respond(self, h):
        if h.path.startswith("/other"):
            payload = self.body("secid=1.000001")
            h.send_response(200); h.send_header("Content-Length", str(len(payload))); h.end_headers(); h.wfile.write(payload); return
        if self.mode in ("ok", "wrong_unit") or self.mode.startswith("envelope_unit_"):
            payload = self.body(h.path)
            h.send_response(200); h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(payload))); h.end_headers(); h.wfile.write(payload)
        elif self.mode == "slow_headers":
            self.wait_for_peer_close(h)
        elif self.mode == "drip":
            h.send_response(200); h.send_header("Content-Length", "65536"); h.end_headers()
            self.drip(h)
        elif self.mode == "redirect_self":
            h.send_response(302); h.send_header("Location", self.url + "?again=1"); h.send_header("Content-Length", "0"); h.end_headers()
        elif self.mode == "redirect_other":
            h.send_response(302); h.send_header("Location", self.url.rsplit("/", 1)[0] + "/other"); h.send_header("Content-Length", "0"); h.end_headers()
        elif self.mode == "redirect_drip":
            h.send_response(302); h.send_header("Location", self.url.rsplit("/", 1)[0] + "/other")
            h.send_header("Content-Length", "100000"); h.end_headers(); self.drip(h)
        elif self.mode in ("http429", "http503"):
            h.send_response(int(self.mode[4:])); h.send_header("Content-Length", "0"); h.end_headers()
        elif self.mode in ("oversize_declared", "oversize_streamed"):
            payload = b" " * (akshare_provider.INDEX_TURNOVER_MAX_BODY_BYTES + 1)
            h.send_response(200)
            if self.mode == "oversize_declared":
                h.send_header("Content-Length", str(len(payload)))
            else:
                h.send_header("Connection", "close")
                h.close_connection = True
            h.end_headers(); h.wfile.write(payload)
        else:
            raise AssertionError(self.mode)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.server.shutdown(); self.server.server_close()
        self.thread.join(timeout=1)

    def kline_hits(self):
        return [p for p in self.hits if p.startswith("/kline")]


class LocalServerTests(unittest.TestCase):
    def collect(self, fake, *, budget=None, fetcher=None):
        work = PROJECT_ROOT / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="turnover-http-test-", dir=work) as temp_dir:
            provider = AkShareMorningProvider(
                api=ForbiddenFullMapApi(), cache_dir=temp_dir, collect_documents=False,
                index_kline_fetcher=fetcher or AkShareMorningProvider._fetch_index_kline, sleep=lambda s: time.sleep(min(s, 0.02)))
            patches = [patch.object(akshare_provider, "INDEX_TURNOVER_KLINE_URL", fake.url),
                       patch.object(akshare_provider, "INDEX_TURNOVER_RETRY_DELAY_SECONDS", 0.02),
                       patch("akshare_provider._requests_default_timeout", return_value=contextlib.nullcontext())]
            if budget is not None:
                patches.append(patch.object(akshare_provider, "INDEX_TURNOVER_TOTAL_BUDGET_SECONDS", budget))
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                started = time.monotonic()
                payload = provider._index_history(SESSION, PREVIOUS)
                return payload, time.monotonic() - started

    def assert_clients_closed(self, fake):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with fake.lock:
                active = fake.open_requests
            if active == 0:
                return
            time.sleep(0.02)
        self.assertEqual(active, 0, "HTTP client remained active after its deadline")

    def assert_prices_unchanged(self, payload):
        for item in payload.data:
            self.assertEqual(item["close"], PRICES[item["code"]])
            self.assertEqual(item["volume"], 110)

    def test_success_over_real_http_is_one_request_per_index_single_day_and_ignores_env_proxy(self):
        with FakeEastmoney("ok") as fake, patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
            payload, _ = self.collect(fake)
        self.assertEqual([i["turnover"] for i in payload.data], [AMOUNTS["000001"], AMOUNTS["399001"], AMOUNTS["399006"]])
        self.assertEqual(len(fake.kline_hits()), 3)
        for path, secid in zip(fake.kline_hits(), ("1.000001", "0.399001", "0.399006")):
            self.assertIn(f"secid={secid}", path)
            self.assertIn("beg=20261008", path)
            self.assertIn("end=20261008", path)

    def test_slow_response_headers_end_at_the_overall_deadline(self):
        with FakeEastmoney("slow_headers") as fake:
            payload, elapsed = self.collect(fake, budget=1.5)
            self.assert_clients_closed(fake)
        self.assertLess(elapsed, 1.5 + 1.5, f"took {elapsed:.2f}s")
        self.assertTrue(all(i["turnover"] is None for i in payload.data))
        self.assertLessEqual(len(fake.kline_hits()), 3)
        self.assertTrue(any("补源失败" in w for w in payload.warnings))

    def test_slow_drip_body_cannot_outlive_the_deadline(self):
        with FakeEastmoney("drip") as fake:
            payload, elapsed = self.collect(fake, budget=1.5)
            self.assert_clients_closed(fake)
        self.assertLess(elapsed, 1.5 + 1.5, f"took {elapsed:.2f}s")
        self.assertTrue(all(i["turnover"] is None for i in payload.data))
        self.assertTrue(any("阶段=budget" in w for w in payload.warnings))

    def test_default_fetcher_alone_enforces_its_total_seconds_against_a_drip(self):
        with FakeEastmoney("drip") as fake, patch.object(akshare_provider, "INDEX_TURNOVER_KLINE_URL", fake.url):
            started = time.monotonic()
            with self.assertRaises(IndexTurnoverError) as caught:
                AkShareMorningProvider._fetch_index_kline("1.000001", "20261008", (5.0, 8.0), total_seconds=1.0)
            elapsed = time.monotonic() - started
            self.assert_clients_closed(fake)
        self.assertEqual(caught.exception.stage, "budget")
        self.assertLess(elapsed, 1.0 + 1.5, f"took {elapsed:.2f}s")

    def test_redirects_are_not_followed_and_do_not_add_requests(self):
        for mode in ("redirect_self", "redirect_other", "redirect_drip"):
            with self.subTest(mode=mode):
                with FakeEastmoney(mode) as fake:
                    payload, elapsed = self.collect(fake, budget=4)
                    self.assert_clients_closed(fake)
                self.assertLess(elapsed, 3, "Redirect body was consumed before rejection")
                self.assertEqual(len(fake.hits), 3, fake.hits)          # exactly one request per index, no hops
                self.assertFalse(any(p.startswith("/other") for p in fake.hits))
                self.assertTrue(all(i["turnover"] is None for i in payload.data))
                self.assertEqual(len([w for w in payload.warnings if "重定向被拒绝" in w and "阶段=kline_response" in w]), 3)

    def test_actual_http_requests_never_exceed_the_cap_of_nine(self):
        for mode in ("http429", "http503"):
            with self.subTest(mode=mode), FakeEastmoney(mode) as fake:
                payload, _ = self.collect(fake)
                self.assertEqual(len(fake.hits), 9)
                self.assertTrue(all(i["turnover"] is None for i in payload.data))
                self.assertTrue(all("尝试=3次" in w for w in payload.warnings if "补源失败" in w))
                self.assert_prices_unchanged(payload)

    def test_body_limit_rejects_announced_and_streamed_oversize_without_retry(self):
        for mode in ("oversize_declared", "oversize_streamed"):
            with self.subTest(mode=mode), FakeEastmoney(mode) as fake:
                payload, _ = self.collect(fake)
                self.assertEqual(len(fake.hits), 3)
                self.assertTrue(all(i["turnover"] is None for i in payload.data))
                self.assertEqual(len([w for w in payload.warnings if "阶段=kline_response" in w]), 3)
                self.assert_prices_unchanged(payload)

    def test_explicit_wrong_unit_over_real_http_is_not_retried_or_used(self):
        with FakeEastmoney("wrong_unit") as fake:
            payload, _ = self.collect(fake)
        self.assertEqual(len(fake.hits), 3)
        self.assertIsNone(payload.data[0]["turnover"])
        self.assertEqual([i["turnover"] for i in payload.data[1:]], [AMOUNTS["399001"], AMOUNTS["399006"]])
        self.assertTrue(any("阶段=unit" in w and "尝试=1次" in w for w in payload.warnings))
        self.assert_prices_unchanged(payload)

    def test_envelope_unit_cannot_be_overridden_by_valid_data_unit(self):
        for mode in ("envelope_unit_wrong", "envelope_unit_malformed", "envelope_unit_yuan"):
            with self.subTest(mode=mode), FakeEastmoney(mode) as fake:
                payload, _ = self.collect(fake)
                self.assertEqual(len(fake.hits), 3, "Explicit unit decisions must never trigger retries")
                self.assert_prices_unchanged(payload)
                if mode == "envelope_unit_yuan":
                    self.assertEqual([row["turnover"] for row in payload.data],
                                     [AMOUNTS["000001"], AMOUNTS["399001"], AMOUNTS["399006"]])
                    self.assertFalse(any("补源失败" in warning for warning in payload.warnings))
                else:
                    self.assertTrue(all(row["turnover"] is None for row in payload.data))
                    failures = [warning for warning in payload.warnings if "补源失败" in warning]
                    self.assertEqual(len(failures), 3)
                    self.assertTrue(all("阶段=unit" in warning and "尝试=1次" in warning
                                        for warning in failures), failures)


if __name__ == "__main__":
    unittest.main()
