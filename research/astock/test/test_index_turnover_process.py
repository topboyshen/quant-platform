"""Killable index HTTP boundary: local process faults, never external traffic.

The 45-second case intentionally keeps the production budget unchanged. Other
cases shorten only the budget, not the worker/cleanup path. The injected worker
command is a fixed local program, not a production API or an Eastmoney probe.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import akshare_provider  # noqa: E402
import index_turnover_http  # noqa: E402
from akshare_provider import AkShareMorningProvider  # noqa: E402
from test_index_turnover_enrichment import (  # noqa: E402
    PRICES, PREVIOUS, SESSION, ForbiddenFullMapApi, KlineFixture)
from test_index_turnover_http import FakeEastmoney  # noqa: E402


class IndexTurnoverDeadlineContractTests(unittest.TestCase):
    """Fixed clocks and a forbidden spawn: no HTTP or real worker is involved."""

    def capture_request(self, *, started, absolute_deadline, total_seconds=45.0):
        captured = []
        dumps = json.dumps

        def record(value, **kwargs):
            captured.append(dict(value))
            return dumps(value, **kwargs)

        with patch.object(index_turnover_http.time, "monotonic", return_value=started), \
                patch.object(index_turnover_http.json, "dumps", side_effect=record), \
                patch.object(index_turnover_http.subprocess, "Popen",
                             side_effect=OSError("fixed stop before spawn")) as popen:
            with self.assertRaises(index_turnover_http.IndexFetchError) as caught:
                index_turnover_http.fetch_kline_isolated(
                    "1.000001", "20261008", (5.0, 8.0), total_seconds,
                    url="https://fixed.invalid", max_body_bytes=65_536,
                    absolute_deadline=absolute_deadline,
                )
        return captured, popen, caught.exception

    def test_delayed_transport_entry_cannot_extend_shared_absolute_deadline(self):
        # Provider computed remaining=45 at t=100, then was descheduled until
        # transport entry t=105. The common cutoff remains 145, not 150.
        captured, popen, error = self.capture_request(started=105.0, absolute_deadline=145.0)
        self.assertEqual(error.stage, "isolation")  # injected pre-spawn stop only
        popen.assert_called_once()
        self.assertEqual(captured[0]["deadline"], 144.75)

    def test_absolute_deadline_cannot_expand_a_shorter_relative_cap(self):
        captured, popen, _ = self.capture_request(started=100.0, absolute_deadline=200.0)
        popen.assert_called_once()
        self.assertEqual(captured[0]["deadline"], 144.75)

    def test_expired_absolute_deadline_never_starts_a_worker(self):
        for deadline in (144.0, 145.0):
            with self.subTest(deadline=deadline):
                captured, popen, error = self.capture_request(started=145.0, absolute_deadline=deadline)
                self.assertEqual(error.stage, "budget")
                self.assertEqual(captured, [])
                popen.assert_not_called()

    def test_invalid_absolute_deadline_is_rejected_before_spawn(self):
        for deadline in (True, False, "145", float("nan"), float("inf"), float("-inf")):
            with self.subTest(deadline=deadline):
                captured, popen, error = self.capture_request(started=100.0, absolute_deadline=deadline)
                self.assertEqual(error.stage, "budget")
                self.assertEqual(captured, [])
                popen.assert_not_called()

    def test_deadline_crossed_during_preparation_never_starts_a_worker(self):
        with patch.object(index_turnover_http.time, "monotonic", side_effect=[100.0, 144.75]), \
                patch.object(index_turnover_http.subprocess, "Popen") as popen:
            with self.assertRaises(index_turnover_http.IndexFetchError) as caught:
                index_turnover_http.fetch_kline_isolated(
                    "1.000001", "20261008", (5.0, 8.0), 45.0,
                    url="https://fixed.invalid", max_body_bytes=65_536,
                    absolute_deadline=145.0,
                )
        self.assertEqual(caught.exception.stage, "budget")
        popen.assert_not_called()


@unittest.skipUnless(os.name == "posix", "The production process-group boundary requires POSIX")
class IndexTurnoverProcessTests(unittest.TestCase):
    def setUp(self):
        work = PROJECT_ROOT / "work"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="turnover-process-test-", dir=work)
        self.addCleanup(self.temp.cleanup)
        self.spawned = []
        self.spawn_started = []

    def collect(self, worker_source, *, budget=0.8, fake=None, expected_stage="budget"):
        provider = AkShareMorningProvider(
            api=ForbiddenFullMapApi(), cache_dir=self.temp.name, collect_documents=False,
            index_kline_fetcher=AkShareMorningProvider._fetch_index_kline,
            sleep=lambda seconds: time.sleep(min(seconds, 0.01)))
        original_popen = subprocess.Popen

        def recorded_popen(*args, **kwargs):
            self.spawn_started.append(time.monotonic())
            process = original_popen(*args, **kwargs)
            self.spawned.append(process)
            self.addCleanup(self.reap_test_worker, process)
            return process

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(index_turnover_http, "_worker_command",
                                            return_value=[sys.executable, "-I", "-B", "-c", worker_source]))
            stack.enter_context(patch.object(index_turnover_http.subprocess, "Popen", side_effect=recorded_popen))
            stack.enter_context(patch("akshare_provider._requests_default_timeout", return_value=contextlib.nullcontext()))
            if budget is not None:
                stack.enter_context(patch.object(akshare_provider, "INDEX_TURNOVER_TOTAL_BUDGET_SECONDS", budget))
            if fake is not None:
                stack.enter_context(patch.object(akshare_provider, "INDEX_TURNOVER_KLINE_URL", fake.url))
            started = time.monotonic()
            payload = provider._index_history(SESSION, PREVIOUS)
            elapsed = time.monotonic() - started
        self.assertTrue(self.spawned, "The injected fault never entered the process boundary")
        self.assertLessEqual(len(self.spawned), 3, "A budget timeout is not a retryable transport error")
        if expected_stage == "isolation":
            self.assertEqual(len(self.spawned), 1, "Unverified isolation must stop subsequent index requests")
        total_budget = budget if budget is not None else akshare_provider.INDEX_TURNOVER_TOTAL_BUDGET_SECONDS
        self.assertTrue(all(at < started + total_budget for at in self.spawn_started),
                        "A worker was started after the shared hard deadline")
        for process in self.spawned:
            self.assertIsNotNone(process.poll(), f"worker {process.pid} survived the deadline")
        self.assertTrue(all(row["turnover"] is None for row in payload.data))
        self.assertTrue(any(f"阶段={expected_stage}" in warning for warning in payload.warnings), payload.warnings)
        for row in payload.data:
            self.assertEqual(row["close"], PRICES[row["code"]])
        return payload, elapsed

    @staticmethod
    def reap_test_worker(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    @staticmethod
    def worker_prelude():
        # Consume the real parent's request envelope, then inject only the fault.
        return "import json,sys,time,os,signal\njson.loads(sys.stdin.readline())\n"

    def test_nonreturning_worker_is_terminated_not_abandoned(self):
        source = self.worker_prelude() + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"
        _, elapsed = self.collect(source)
        self.assertLess(elapsed, 2.0)

    def test_blocked_resolver_cannot_outlive_the_same_process_deadline(self):
        source = self.worker_prelude() + (
            "import socket\n"
            "def blocked_resolver(*args, **kwargs):\n    time.sleep(120)\n"
            "socket.getaddrinfo=blocked_resolver\n"
            "socket.getaddrinfo('fixed-dns-fault.invalid',443)\n")
        _, elapsed = self.collect(source)
        self.assertLess(elapsed, 2.0)

    def test_partial_ipc_reply_cannot_block_the_parent_past_deadline(self):
        source = self.worker_prelude() + "sys.stdout.write('{\"ok\":true,');sys.stdout.flush()\ntime.sleep(120)\n"
        _, elapsed = self.collect(source)
        self.assertLess(elapsed, 2.0)

    def test_oversize_ipc_response_is_rejected_and_stops_later_requests(self):
        source = self.worker_prelude() + (
            f"sys.stdout.write('x'*{index_turnover_http.MAX_PROTOCOL_BYTES + 8192});sys.stdout.flush()\n"
            "time.sleep(120)\n")
        _, elapsed = self.collect(source, expected_stage="isolation")
        self.assertLess(elapsed, 2.0)

    def test_unconfirmed_cleanup_never_accepts_a_valid_value_or_starts_another_worker(self):
        # Perform real cleanup for test hygiene, but inject the parent's failure
        # to confirm it. This tests fail-closed state propagation, not kill success.
        original_cleanup = index_turnover_http._kill_and_reap

        def unconfirmed_cleanup(process, deadline):
            original_cleanup(process, deadline)
            raise index_turnover_http.IndexFetchError("isolation", "固定故障桩：无法确认回收")

        value = json.dumps({"ok": True, "data": KlineFixture().data["000001"]})
        source = self.worker_prelude() + f"print({value!r},flush=True)\n"
        with patch.object(index_turnover_http, "_kill_and_reap", side_effect=unconfirmed_cleanup):
            _, elapsed = self.collect(source, expected_stage="isolation")
        self.assertLess(elapsed, 2.0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "PDEATHSIG is Linux-specific")
    def test_linux_parent_sigkill_terminates_real_http_worker_without_late_requests(self):
        # Import only the standalone transport in the disposable owner process.
        # No provider/business entry point or production data is involved.
        pid_file = Path(self.temp.name) / "parent-death-worker.pid"
        worker_pid = None
        with FakeEastmoney("slow_headers") as fake:
            owner_source = (
                "import sys,subprocess\n"
                f"sys.path.insert(0,{str(PROJECT_ROOT / 'mcp')!r})\n"
                "import index_turnover_http as transport\n"
                "original_popen=subprocess.Popen\n"
                "def record_worker(*args,**kwargs):\n"
                "    child=original_popen(*args,**kwargs)\n"
                f"    with open({str(pid_file)!r},'w') as out: out.write(str(child.pid))\n"
                "    return child\n"
                "transport.subprocess.Popen=record_worker\n"
                f"transport.fetch_kline_isolated('1.000001','20261008',(5,8),30,url={fake.url!r},max_body_bytes=65536)\n")
            owner = subprocess.Popen([sys.executable, "-I", "-B", "-c", owner_source],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, start_new_session=True,
                                     env={"PATH": os.defpath, "LANG": "C.UTF-8"})
            try:
                startup_deadline = time.monotonic() + 6
                while time.monotonic() < startup_deadline:
                    with fake.lock:
                        hits = len(fake.hits)
                    if hits == 1 and pid_file.exists():
                        worker_pid = int(pid_file.read_text())
                        break
                    if owner.poll() is not None:
                        self.fail(f"Disposable owner exited before HTTP fault: {owner.stderr.read(2048)!r}")
                    time.sleep(0.02)
                self.assertIsNotNone(worker_pid, "Real isolated worker never reached the local server")
                self.assertIsNone(owner.poll())
                os.kill(owner.pid, signal.SIGKILL)
                owner.wait(timeout=2)

                # A zombie is no longer executing or holding a socket; reaping an
                # orphan is init's responsibility, not proof of parent cleanup.
                stop_deadline = time.monotonic() + 2
                worker_state = None
                active = 1
                while time.monotonic() < stop_deadline:
                    try:
                        stat = Path(f"/proc/{worker_pid}/stat").read_text()
                        worker_state = stat.rsplit(")", 1)[1].strip().split()[0]
                    except FileNotFoundError:
                        worker_state = None
                    with fake.lock:
                        active = fake.open_requests
                    if worker_state in (None, "Z") and active == 0:
                        break
                    time.sleep(0.02)
                self.assertIn(worker_state, (None, "Z"), "HTTP worker survived its owner's SIGKILL")
                self.assertEqual(active, 0, "HTTP socket survived the owning process")
                time.sleep(0.3)
                self.assertEqual(len(fake.hits), 1, "A request was issued after owner termination")
            finally:
                if owner.poll() is None:
                    os.killpg(owner.pid, signal.SIGKILL)
                    owner.wait(timeout=2)
                if worker_pid is not None:
                    try:
                        os.killpg(worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if owner.stderr is not None:
                    owner.stderr.close()

    def test_late_valid_result_and_late_http_are_prevented_not_just_ignored(self):
        with FakeEastmoney("ok") as fake:
            response = json.dumps({"ok": True, "data": KlineFixture().data["000001"]})
            source = self.worker_prelude() + (
                "time.sleep(1.4)\n"
                "import http.client\n"
                f"c=http.client.HTTPConnection('127.0.0.1',{fake.server.server_address[1]},timeout=1)\n"
                "c.request('GET','/late');c.getresponse().read();c.close()\n"
                f"print({response!r},flush=True)\n")
            payload, elapsed = self.collect(source, fake=fake)
            self.assertLess(elapsed, 2.0)
            frozen_result = json.dumps(payload.data, sort_keys=True)
            time.sleep(1.5)
            self.assertEqual(fake.hits, [], "Abandoned worker issued HTTP after caller returned")
            self.assertEqual(json.dumps(payload.data, sort_keys=True), frozen_result)

    def test_helper_process_is_killed_with_worker_and_cannot_issue_late_http(self):
        pid_file = Path(self.temp.name) / "helper.pid"
        with FakeEastmoney("ok") as fake:
            helper = (
                "import time,http.client,os\n"
                f"open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
                "time.sleep(1.4)\n"
                f"c=http.client.HTTPConnection('127.0.0.1',{fake.server.server_address[1]},timeout=1)\n"
                "c.request('GET','/late-helper');c.getresponse().read();c.close()\n"
                "time.sleep(120)\n")
            source = self.worker_prelude() + (
                "import subprocess\n"
                f"subprocess.Popen([sys.executable,'-I','-B','-c',{helper!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "time.sleep(120)\n")
            _, elapsed = self.collect(source, fake=fake)
            self.assertLess(elapsed, 2.0)
            self.assertTrue(pid_file.exists(), "No descendant was created; fault case was not exercised")
            helper_pid = int(pid_file.read_text())
            try:
                time.sleep(1.5)
                self.assertEqual(fake.hits, [], "A helper survived termination and performed HTTP")
                probe = subprocess.run(["ps", "-o", "stat=", "-p", str(helper_pid)],
                                       capture_output=True, text=True, timeout=2, check=False)
                self.assertTrue(not probe.stdout.strip() or probe.stdout.strip().startswith("Z"),
                                f"helper {helper_pid} is still executing: {probe.stdout!r}")
            finally:
                # Failure cleanup is limited to the exact PID created by this
                # test; never leave an intentionally injected helper alive.
                try:
                    os.kill(helper_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(os.environ.get("LONG_INDEX_DEADLINE_TEST") == "1",
                         "Explicit 45-second acceptance: set LONG_INDEX_DEADLINE_TEST=1")
    def test_real_production_45_second_budget_terminates_a_nonreturning_worker(self):
        self.assertEqual(akshare_provider.INDEX_TURNOVER_TOTAL_BUDGET_SECONDS, 45.0)
        source = self.worker_prelude() + "time.sleep(120)\n"
        _, elapsed = self.collect(source, budget=None)
        self.assertGreaterEqual(elapsed, 44.0, "Did not exercise the unchanged production 45-second budget")
        self.assertLess(elapsed, 46.5, f"45-second hard budget was exceeded: {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
