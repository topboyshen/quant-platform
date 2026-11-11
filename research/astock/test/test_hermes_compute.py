"""Offline security-contract tests for the Hermes compute adapter."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = PROJECT_ROOT / "scripts" / "hermes-compute.py"
SPEC = importlib.util.spec_from_file_location("astock_hermes_compute", ADAPTER_PATH)
ADAPTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ADAPTER)


def valid_runtime():
    return {
        "api_key": "test-only-key",
        "base_url": ADAPTER.ALLOWED_BASE_URL,
        "provider": ADAPTER.ALLOWED_PROVIDER,
        "requested_provider": ADAPTER.ALLOWED_PROVIDER,
        "api_mode": ADAPTER.ALLOWED_API_MODE,
        "credential_pool": object(),
    }


class HermesComputeSecurityTests(unittest.TestCase):
    def test_analysis_phase_is_allowed_but_unknown_phases_fail_closed(self):
        payload = {
            "schemaVersion": "1.0",
            "phase": "analysis",
            "provider": ADAPTER.ALLOWED_PROVIDER,
            "model": ADAPTER.ALLOWED_MODEL,
            "reasoning": "high",
            "maxTurns": 2,
            "prompt": "fixed structured prompt",
        }
        self.assertEqual(ADAPTER._validate_request_payload(payload), payload)
        with self.assertRaisesRegex(ValueError, "unsupported phase"):
            ADAPTER._validate_request_payload({**payload, "phase": "shell"})

    def test_version_gate_accepts_pinned_version_and_rejects_0191(self):
        with (
            mock.patch.object(ADAPTER, "_ensure_no_managed_scope"),
            mock.patch.object(ADAPTER, "_ensure_project_env_allowlist"),
            mock.patch.object(ADAPTER, "_hermes_version", return_value=ADAPTER.EXPECTED_HERMES_VERSION),
            mock.patch.object(
                ADAPTER,
                "_validated_runtime",
                return_value={"api_key": "test-only-key"},
            ),
            mock.patch.object(ADAPTER, "_anonymous_input_fd", return_value=87),
            mock.patch.object(ADAPTER, "_write_all") as write_all,
            mock.patch.object(ADAPTER.os, "lseek") as seek_descriptor,
            mock.patch.object(
                ADAPTER.os,
                "read",
                return_value=ADAPTER.MEMFD_CHECK_SENTINEL,
            ),
            mock.patch.object(ADAPTER.os, "close") as close_descriptor,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(ADAPTER._check(), 0)
            result = json.loads(stdout.getvalue())

        write_all.assert_called_once_with(87, ADAPTER.MEMFD_CHECK_SENTINEL)
        seek_descriptor.assert_called_once_with(87, 0, ADAPTER.os.SEEK_SET)
        close_descriptor.assert_called_once_with(87)
        self.assertNotEqual(ADAPTER.EXPECTED_HERMES_VERSION, "0.19.1")
        self.assertEqual(result["version"], ADAPTER.EXPECTED_HERMES_VERSION)
        self.assertEqual(result["enabledToolsets"], ["todo"])
        self.assertTrue(result["safeMode"])

        with (
            mock.patch.object(ADAPTER, "_ensure_no_managed_scope"),
            mock.patch.object(ADAPTER, "_ensure_project_env_allowlist"),
            mock.patch.object(ADAPTER, "_hermes_version", return_value="0.19.1"),
            mock.patch.object(ADAPTER, "_validated_runtime") as runtime,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(ADAPTER._check(), 3)

        runtime.assert_not_called()
        self.assertIn(
            f"expected Hermes {ADAPTER.EXPECTED_HERMES_VERSION}, found 0.19.1",
            stderr.getvalue(),
        )

    def test_check_rejects_a_runtime_without_anonymous_worker_input(self):
        with (
            mock.patch.object(ADAPTER, "_ensure_no_managed_scope"),
            mock.patch.object(ADAPTER, "_ensure_project_env_allowlist"),
            mock.patch.object(ADAPTER, "_hermes_version", return_value=ADAPTER.EXPECTED_HERMES_VERSION),
            mock.patch.object(
                ADAPTER,
                "_anonymous_input_fd",
                side_effect=RuntimeError("anonymous worker input is unavailable"),
            ),
            mock.patch.object(ADAPTER, "_validated_runtime") as runtime,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(ADAPTER._check(), 3)

        runtime.assert_not_called()
        self.assertIn("anonymous worker input is unavailable", stderr.getvalue())

    def test_runtime_contract_is_pinned_and_drops_credential_pool(self):
        with mock.patch.object(ADAPTER, "_resolve_runtime", return_value=valid_runtime()):
            runtime = ADAPTER._validated_runtime()
        self.assertEqual(runtime["base_url"], "https://api.kimi.com/coding")
        self.assertEqual(runtime["api_key"], "test-only-key")
        self.assertNotIn("credential_pool", runtime)

        bad = valid_runtime()
        bad["base_url"] = "https://api.kimi.com/coding?redirect=attacker"
        with mock.patch.object(ADAPTER, "_resolve_runtime", return_value=bad):
            with self.assertRaisesRegex(RuntimeError, "pinned contract"):
                ADAPTER._validated_runtime()

    def test_installed_hermes_dotenv_is_fail_closed_to_known_nonsecret_keys(self):
        with tempfile.TemporaryDirectory() as root:
            dotenv = Path(root) / ".env"
            dotenv.write_text("TERMINAL_TIMEOUT=30\nWEB_TOOLS_DEBUG=false\n", encoding="utf-8")
            with mock.patch.object(ADAPTER, "HERMES_PROJECT_ENV", dotenv):
                self.assertIsNone(ADAPTER._ensure_project_env_allowlist())

            dotenv.write_text(
                "TERMINAL_TIMEOUT=30\nTELEGRAM_BOT_TOKEN=must-not-load\n",
                encoding="utf-8",
            )
            with mock.patch.object(ADAPTER, "HERMES_PROJECT_ENV", dotenv):
                with self.assertRaisesRegex(RuntimeError, "unapproved keys"):
                    ADAPTER._ensure_project_env_allowlist()

    def test_isolated_home_scrubs_telegram_and_unrelated_credentials(self):
        original = dict(os.environ)
        try:
            os.environ["TELEGRAM_BOT_TOKEN"] = "must-not-survive"
            os.environ["UNRELATED_API_KEY"] = "must-not-survive"
            with tempfile.TemporaryDirectory() as root:
                ADAPTER._enter_isolated_home(root)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
                self.assertNotIn("UNRELATED_API_KEY", os.environ)
                self.assertEqual(os.environ["HERMES_SAFE_MODE"], "1")
                self.assertEqual(os.environ["HERMES_IGNORE_USER_CONFIG"], "1")
                config = Path(os.environ["HERMES_HOME"]) / "config.yaml"
                text = config.read_text(encoding="utf-8")
                self.assertIn("environment_probe: false", text)
                self.assertIn("compression:\n  enabled: false", text)
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_bootstrap_execs_a_fresh_isolated_python_worker(self):
        captured_input = bytearray()

        def fake_write(_fd, data):
            chunk = bytes(data)
            captured_input.extend(chunk)
            return len(chunk)

        request = {
            "schemaVersion": "1.0",
            "phase": "generate",
            "provider": ADAPTER.ALLOWED_PROVIDER,
            "model": ADAPTER.ALLOWED_MODEL,
            "reasoning": "high",
            "maxTurns": 2,
            "prompt": "fixed prompt",
        }
        with (
            mock.patch.object(ADAPTER.os, "memfd_create", return_value=91, create=True),
            mock.patch.object(ADAPTER.os, "write", side_effect=fake_write),
            mock.patch.object(ADAPTER.os, "lseek"),
            mock.patch.object(ADAPTER.os, "dup2") as duplicate,
            mock.patch.object(ADAPTER.os, "set_inheritable") as set_inheritable,
            mock.patch.object(ADAPTER.os, "close"),
            mock.patch.object(ADAPTER.os, "execve", side_effect=OSError("test stop")) as execute,
        ):
            with self.assertRaisesRegex(OSError, "test stop"):
                ADAPTER._exec_isolated_worker(request, ADAPTER._sanitize_runtime(valid_runtime()))

        duplicate.assert_called_once_with(91, 0, inheritable=True)
        set_inheritable.assert_called_once_with(0, True)
        executable, arguments, environment = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(arguments[1], "-I")
        self.assertEqual(arguments[-1], ADAPTER.ISOLATED_WORKER_ARGUMENT)
        self.assertEqual(
            environment["HERMES_HOME"].replace("\\", "/"),
            "/nonexistent/astock-hermes-worker/.hermes",
        )
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        internal = json.loads(captured_input.decode("utf-8"))
        self.assertEqual(internal["runtime"]["api_key"], "test-only-key")
        self.assertEqual(internal["request"], request)

    def test_fd_zero_clears_close_on_exec_before_worker_exec(self):
        request = {
            "schemaVersion": "1.0",
            "phase": "generate",
            "provider": ADAPTER.ALLOWED_PROVIDER,
            "model": ADAPTER.ALLOWED_MODEL,
            "reasoning": "high",
            "maxTurns": 2,
            "prompt": "fixed prompt",
        }
        with (
            mock.patch.object(ADAPTER, "_anonymous_input_fd", return_value=0),
            mock.patch.object(ADAPTER, "_write_all"),
            mock.patch.object(ADAPTER.os, "lseek"),
            mock.patch.object(ADAPTER.os, "dup2") as duplicate,
            mock.patch.object(ADAPTER.os, "set_inheritable") as set_inheritable,
            mock.patch.object(ADAPTER.os, "close") as close_descriptor,
            mock.patch.object(ADAPTER.os, "execve", side_effect=OSError("test stop")),
        ):
            with self.assertRaisesRegex(OSError, "test stop"):
                ADAPTER._exec_isolated_worker(
                    request,
                    ADAPTER._sanitize_runtime(valid_runtime()),
                )

        duplicate.assert_called_once_with(0, 0, inheritable=True)
        set_inheritable.assert_called_once_with(0, True)
        close_descriptor.assert_not_called()

    def test_anonymous_input_uses_fixed_libc_memfd_when_python_wrapper_is_absent(self):
        creator_calls = []

        class FakeCreator:
            argtypes = None
            restype = None

            def __call__(self, name, flags):
                creator_calls.append((name, flags))
                return 92

        class FakeLibc:
            memfd_create = FakeCreator()

        with (
            mock.patch.object(ADAPTER.os, "memfd_create", None, create=True),
            mock.patch.object(ADAPTER.ctypes, "CDLL", return_value=FakeLibc()),
        ):
            self.assertEqual(ADAPTER._anonymous_input_fd(), 92)

        self.assertEqual(
            creator_calls,
            [(b"astock-hermes-worker-input", ADAPTER.MEMFD_CLOEXEC)],
        )
        self.assertEqual(
            FakeLibc.memfd_create.argtypes,
            [ADAPTER.ctypes.c_char_p, ADAPTER.ctypes.c_uint],
        )
        self.assertIs(FakeLibc.memfd_create.restype, ADAPTER.ctypes.c_int)

    def test_anonymous_input_prefers_native_memfd_with_close_on_exec(self):
        with (
            mock.patch.object(ADAPTER.os, "memfd_create", return_value=93, create=True) as native,
            mock.patch.object(ADAPTER, "_libc_memfd_create") as fallback,
        ):
            self.assertEqual(ADAPTER._anonymous_input_fd(), 93)

        native.assert_called_once_with(
            "astock-hermes-worker-input",
            flags=ADAPTER.MEMFD_CLOEXEC,
        )
        fallback.assert_not_called()

    def test_anonymous_input_fails_closed_when_native_and_libc_memfd_are_unavailable(self):
        with (
            mock.patch.object(ADAPTER.os, "memfd_create", None, create=True),
            mock.patch.object(ADAPTER.ctypes, "CDLL", side_effect=OSError("unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "anonymous worker input is unavailable"):
                ADAPTER._anonymous_input_fd()

        class FailedCreator:
            argtypes = None
            restype = None

            def __call__(self, _name, _flags):
                return -1

        class FailedLibc:
            memfd_create = FailedCreator()

        with (
            mock.patch.object(ADAPTER.os, "memfd_create", None, create=True),
            mock.patch.object(ADAPTER.ctypes, "CDLL", return_value=FailedLibc()),
        ):
            with self.assertRaisesRegex(RuntimeError, "anonymous worker input is unavailable"):
                ADAPTER._anonymous_input_fd()

    def test_incomplete_or_interrupted_agent_result_is_rejected(self):
        for result in (
            {
                "completed": False,
                "interrupted": True,
                "final_response": "incomplete candidate",
            },
            {
                "completed": True,
                "partial": True,
                "final_response": "partial candidate",
            },
            {
                "completed": True,
                "failed": True,
                "final_response": "failed candidate",
            },
        ):
            with self.assertRaisesRegex(ValueError, "complete final response"):
                ADAPTER._completed_response(result)

        self.assertEqual(
            ADAPTER._completed_response(
                {
                    "completed": True,
                    "failed": False,
                    "partial": False,
                    "interrupted": False,
                    "final_response": "complete report",
                }
            ),
            "complete report",
        )

    def test_analysis_final_response_is_bounded_by_utf8_bytes(self):
        def completed(response):
            return {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "final_response": response,
            }

        exact = "x" * ADAPTER.MAX_ANALYSIS_OUTPUT_BYTES
        self.assertEqual(ADAPTER._completed_response(completed(exact), "analysis"), exact)
        for oversized in (
            exact + "x",
            "中" * (ADAPTER.MAX_ANALYSIS_OUTPUT_BYTES // 3 + 1),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds 64 KiB"):
                ADAPTER._completed_response(completed(oversized), "analysis")

        # Markdown report phases retain their separate outer process bound.
        self.assertEqual(
            ADAPTER._completed_response(completed(exact + "x"), "generate"),
            exact + "x",
        )


if __name__ == "__main__":
    unittest.main()
