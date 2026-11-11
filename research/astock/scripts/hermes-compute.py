#!/usr/bin/env python3
"""Fixed, least-privilege Hermes compute adapter for AStock reports."""

from __future__ import annotations

import ctypes
import importlib.metadata
import json
import logging
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import urlsplit


EXPECTED_HERMES_VERSION = "0.21.0"
ALLOWED_PROVIDER = "kimi-coding"
ALLOWED_MODEL = "k3-256k"
ALLOWED_BASE_URL = "https://api.kimi.com/coding"
ALLOWED_API_MODE = "anthropic_messages"
ALLOWED_PHASES = {"generate", "review", "analysis", "smoke"}
ALLOWED_REASONING = {"low", "medium", "high"}
MAX_INPUT_BYTES = 2 * 1024 * 1024 + 64 * 1024
MAX_INTERNAL_INPUT_BYTES = MAX_INPUT_BYTES + 64 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MAX_TURNS = 12
MAX_ANALYSIS_OUTPUT_BYTES = 64 * 1024
ISOLATED_WORKER_ARGUMENT = "--isolated-worker"
MEMFD_NAME = "astock-hermes-worker-input"
MEMFD_CLOEXEC = 0x0001
MEMFD_CHECK_SENTINEL = b"astock-hermes-memfd-check"
MANAGED_HERMES_ROOT = Path("/etc/hermes")
HERMES_PROJECT_ENV = Path("/home/ubuntu/.hermes/hermes-agent/.env")
ALLOWED_PROJECT_ENV_KEYS = frozenset(
    {
        "TERMINAL_MODAL_IMAGE",
        "TERMINAL_TIMEOUT",
        "TERMINAL_LIFETIME_SECONDS",
        "BROWSERBASE_PROXIES",
        "BROWSERBASE_ADVANCED_STEALTH",
        "BROWSER_SESSION_TIMEOUT",
        "BROWSER_INACTIVITY_TIMEOUT",
        "WEB_TOOLS_DEBUG",
        "VISION_TOOLS_DEBUG",
        "MOA_TOOLS_DEBUG",
        "IMAGE_TOOLS_DEBUG",
    }
)
DISABLED_TOOLSETS = [
    "terminal",
    "file",
    "web",
    "browser",
    "code_execution",
    "delegation",
    "cronjob",
    "memory",
    "session_search",
    "skills",
    "computer_use",
    "image_gen",
    "tts",
]
MINIMAL_HERMES_CONFIG = """\
context:
  engine: compressor
agent:
  environment_probe: false
  api_max_retries: 2
compression:
  enabled: false
"""


def _fail(message: str, code: int = 2) -> int:
    sys.stderr.write(f"Hermes compute adapter: {message}\n")
    return code


def _hermes_version() -> str:
    for distribution_name in ("hermes-agent", "hermes_agent"):
        try:
            return importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "missing"


def _prepare_isolation() -> None:
    os.environ["HERMES_SAFE_MODE"] = "1"
    os.environ["HERMES_IGNORE_RULES"] = "1"
    os.environ["HERMES_IGNORE_USER_CONFIG"] = "1"
    os.environ["HERMES_SESSION_SOURCE"] = "tool"
    os.environ["NO_COLOR"] = "1"
    os.environ["FORCE_COLOR"] = "0"
    os.environ.pop("HERMES_ACCEPT_HOOKS", None)
    os.environ.pop("HERMES_YOLO_MODE", None)


def _resolve_runtime():
    from hermes_cli.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider(
        requested=ALLOWED_PROVIDER,
        target_model=ALLOWED_MODEL,
    )


def _sanitize_runtime(runtime: dict) -> dict:
    base_url = runtime.get("base_url")
    parsed = urlsplit(base_url or "")
    valid_endpoint = (
        base_url == ALLOWED_BASE_URL
        and parsed.scheme == "https"
        and parsed.hostname == "api.kimi.com"
        and parsed.port is None
        and parsed.path == "/coding"
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
    api_key = runtime.get("api_key")
    if (
        runtime.get("provider") != ALLOWED_PROVIDER
        or runtime.get("requested_provider") != ALLOWED_PROVIDER
        or runtime.get("api_mode") != ALLOWED_API_MODE
        or not valid_endpoint
        or not isinstance(api_key, str)
        or not api_key
    ):
        raise RuntimeError("runtime provider does not match the pinned contract")
    return {
        "api_key": api_key,
        "provider": ALLOWED_PROVIDER,
        "requested_provider": ALLOWED_PROVIDER,
        "api_mode": ALLOWED_API_MODE,
        "base_url": ALLOWED_BASE_URL,
    }


def _validated_runtime() -> dict:
    return _sanitize_runtime(_resolve_runtime())


def _ensure_no_managed_scope() -> None:
    if MANAGED_HERMES_ROOT.exists():
        raise RuntimeError("managed Hermes scope is not allowed for compute")


def _ensure_project_env_allowlist() -> None:
    if not HERMES_PROJECT_ENV.exists():
        return
    discovered = set()
    with HERMES_PROJECT_ENV.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key = line.split("=", 1)[0].strip()
            if not key:
                raise RuntimeError("Hermes project env contains an invalid key")
            discovered.add(key)
    unexpected = discovered - ALLOWED_PROJECT_ENV_KEYS
    if unexpected:
        raise RuntimeError("Hermes project env contains unapproved keys")


def _fixed_environment(home: Path, hermes_home: Path) -> dict:
    return {
        "HOME": str(home),
        "USER": "ubuntu",
        "LOGNAME": "ubuntu",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/home/ubuntu/.hermes/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": "/home/ubuntu/.hermes/hermes-agent/venv",
        "HERMES_HOME": str(hermes_home),
        "HERMES_SAFE_MODE": "1",
        "HERMES_IGNORE_RULES": "1",
        "HERMES_IGNORE_USER_CONFIG": "1",
        "HERMES_SESSION_SOURCE": "tool",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "PYTHONUNBUFFERED": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }


def _enter_isolated_home(root: str) -> None:
    home = Path(root)
    hermes_home = home / ".hermes"
    xdg_config = home / ".config"
    xdg_data = home / ".local" / "share"
    xdg_cache = home / ".cache"
    for directory in (hermes_home, xdg_config, xdg_data, xdg_cache):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    config_path = hermes_home / "config.yaml"
    config_path.write_text(MINIMAL_HERMES_CONFIG, encoding="utf-8")
    config_path.chmod(0o600)
    # Rebuild the already-clean exec environment before importing run_agent.
    # The bootstrap's Hermes config modules and unrelated secrets were removed
    # by execve and cannot enter this worker's module heap.
    os.environ.clear()
    os.environ.update(_fixed_environment(home, hermes_home))


def _check() -> int:
    _prepare_isolation()
    try:
        _ensure_no_managed_scope()
        _ensure_project_env_allowlist()
    except RuntimeError:
        return _fail("Hermes configuration isolation check failed", code=3)
    version = _hermes_version()
    if version != EXPECTED_HERMES_VERSION:
        return _fail(
            f"expected Hermes {EXPECTED_HERMES_VERSION}, found {version}",
            code=3,
        )
    try:
        descriptor = _anonymous_input_fd()
        try:
            _write_all(descriptor, MEMFD_CHECK_SENTINEL)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, len(MEMFD_CHECK_SENTINEL)) != MEMFD_CHECK_SENTINEL:
                raise RuntimeError("anonymous worker input is unavailable")
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError):
        return _fail("anonymous worker input is unavailable", code=3)
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                runtime = _validated_runtime()
        credential_ready = bool(runtime.get("api_key"))
    except Exception:
        return _fail("runtime provider configuration is unavailable", code=3)
    print(
        json.dumps(
            {
                "ok": credential_ready,
                "version": version,
                "provider": ALLOWED_PROVIDER,
                "model": ALLOWED_MODEL,
                "credentialReady": credential_ready,
                "enabledToolsets": ["todo"],
                "safeMode": True,
            },
            ensure_ascii=False,
        )
    )
    return 0 if credential_ready else 3


def _validate_request_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("stdin payload must be a JSON object")
    allowed_keys = {
        "schemaVersion",
        "phase",
        "provider",
        "model",
        "reasoning",
        "maxTurns",
        "prompt",
    }
    if set(payload) != allowed_keys:
        raise ValueError("stdin payload keys do not match the fixed contract")
    if payload["schemaVersion"] != "1.0":
        raise ValueError("unsupported schemaVersion")
    if payload["phase"] not in ALLOWED_PHASES:
        raise ValueError("unsupported phase")
    if payload["provider"] != ALLOWED_PROVIDER or payload["model"] != ALLOWED_MODEL:
        raise ValueError("provider/model do not match the pinned Hermes backend")
    if payload["reasoning"] not in ALLOWED_REASONING:
        raise ValueError("unsupported reasoning level")
    if not isinstance(payload["maxTurns"], int) or not 1 <= payload["maxTurns"] <= MAX_TURNS:
        raise ValueError("maxTurns is outside the fixed range")
    prompt = payload["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt exceeds 2 MiB")
    return payload


def _read_payload() -> dict:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("stdin payload exceeds the fixed limit")
    return _validate_request_payload(json.loads(raw.decode("utf-8")))


def _read_internal_payload() -> tuple[dict, dict]:
    raw = sys.stdin.buffer.read(MAX_INTERNAL_INPUT_BYTES + 1)
    if len(raw) > MAX_INTERNAL_INPUT_BYTES:
        raise ValueError("internal payload exceeds the fixed limit")
    internal = json.loads(raw.decode("utf-8"))
    if not isinstance(internal, dict) or set(internal) != {
        "schemaVersion",
        "request",
        "runtime",
    }:
        raise ValueError("internal payload keys do not match the fixed contract")
    if internal["schemaVersion"] != "1.0":
        raise ValueError("unsupported internal schemaVersion")
    runtime = internal["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {
        "api_key",
        "provider",
        "requested_provider",
        "api_mode",
        "base_url",
    }:
        raise ValueError("internal runtime keys do not match the fixed contract")
    return _validate_request_payload(internal["request"]), _sanitize_runtime(runtime)


def _completed_response(result: dict, phase: str = "generate") -> str:
    if not isinstance(result, dict):
        raise ValueError("agent result is not an object")
    response = str(result.get("final_response") or "")
    if (
        result.get("completed") is not True
        or result.get("failed") is True
        or result.get("partial") is True
        or result.get("interrupted") is True
        or not response.strip()
    ):
        raise ValueError("agent did not produce a complete final response")
    if phase == "analysis" and len(response.encode("utf-8")) > MAX_ANALYSIS_OUTPUT_BYTES:
        raise ValueError("structured analysis response exceeds 64 KiB")
    return response


def _write_all(file_descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise RuntimeError("unable to prepare isolated worker input")
        view = view[written:]


def _libc_memfd_create() -> int:
    """Use the fixed libc wrapper when Python omits os.memfd_create."""
    # Keep the credential-bearing worker envelope in kernel memory. A named or
    # unnamed filesystem temporary file is not an acceptable fallback here.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        creator = getattr(libc, "memfd_create")
        creator.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        creator.restype = ctypes.c_int
        ctypes.set_errno(0)
        descriptor = creator(MEMFD_NAME.encode("ascii"), MEMFD_CLOEXEC)
    except (AttributeError, OSError, TypeError):
        raise RuntimeError("anonymous worker input is unavailable") from None
    if not isinstance(descriptor, int) or descriptor < 0:
        raise RuntimeError("anonymous worker input is unavailable")
    return descriptor


def _anonymous_input_fd() -> int:
    native_creator = getattr(os, "memfd_create", None)
    if callable(native_creator):
        try:
            descriptor = native_creator(MEMFD_NAME, flags=MEMFD_CLOEXEC)
        except OSError:
            raise RuntimeError("anonymous worker input is unavailable") from None
        if not isinstance(descriptor, int) or descriptor < 0:
            raise RuntimeError("anonymous worker input is unavailable")
        return descriptor
    return _libc_memfd_create()


def _exec_isolated_worker(payload: dict, runtime: dict) -> None:
    internal = json.dumps(
        {
            "schemaVersion": "1.0",
            "request": payload,
            "runtime": runtime,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(internal) > MAX_INTERNAL_INPUT_BYTES:
        raise RuntimeError("isolated worker input exceeds the fixed limit")

    input_fd = _anonymous_input_fd()
    try:
        _write_all(input_fd, internal)
        os.lseek(input_fd, 0, os.SEEK_SET)
        os.dup2(input_fd, 0, inheritable=True)
        # dup2(fd, fd, inheritable=True) is a no-op on some Python builds. If
        # stdin was closed and memfd_create returned 0, clear CLOEXEC explicitly
        # so the isolated worker still receives its anonymous input.
        os.set_inheritable(0, True)
        if input_fd != 0:
            os.close(input_fd)
        placeholder_home = Path("/nonexistent/astock-hermes-worker")
        worker_environment = _fixed_environment(
            placeholder_home,
            placeholder_home / ".hermes",
        )
        script_path = str(Path(__file__).resolve())
        os.execve(
            sys.executable,
            [sys.executable, "-I", script_path, ISOLATED_WORKER_ARGUMENT],
            worker_environment,
        )
    except BaseException:
        try:
            if input_fd not in (0,):
                os.close(input_fd)
        except OSError:
            pass
        raise


def _bootstrap_run(payload: dict) -> int:
    _prepare_isolation()
    version = _hermes_version()
    if version != EXPECTED_HERMES_VERSION:
        return _fail(
            f"expected Hermes {EXPECTED_HERMES_VERSION}, found {version}",
            code=3,
        )

    try:
        _ensure_no_managed_scope()
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                runtime = _validated_runtime()
        _exec_isolated_worker(payload, runtime)
        return _fail("isolated worker did not start", code=4)
    except BaseException as exc:  # noqa: BLE001 - keep secrets out of stderr
        return _fail(f"unable to start isolated worker ({type(exc).__name__})", code=4)


def _run_isolated(payload: dict, runtime: dict) -> int:
    _prepare_isolation()
    logging.disable(logging.CRITICAL)
    agent = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="astock-hermes-compute-",
            dir="/tmp",
        ) as isolated_home:
            _enter_isolated_home(isolated_home)
            _ensure_project_env_allowlist()
            try:
                with open(os.devnull, "w", encoding="utf-8") as sink:
                    with redirect_stdout(sink), redirect_stderr(sink):
                        from run_agent import AIAgent

                        agent = AIAgent(
                            api_key=runtime["api_key"],
                            base_url=ALLOWED_BASE_URL,
                            provider=ALLOWED_PROVIDER,
                            requested_provider=ALLOWED_PROVIDER,
                            api_mode=ALLOWED_API_MODE,
                            model=ALLOWED_MODEL,
                            max_iterations=payload["maxTurns"],
                            enabled_toolsets=["todo"],
                            disabled_toolsets=DISABLED_TOOLSETS,
                            quiet_mode=True,
                            platform="tool",
                            skip_context_files=True,
                            load_soul_identity=False,
                            skip_memory=True,
                            session_db=None,
                            fallback_model=None,
                            credential_pool=None,
                            checkpoints_enabled=False,
                            pass_session_id=False,
                            max_tokens=32768,
                            reasoning_config={"effort": payload["reasoning"]},
                        )
                        agent.suppress_status_output = True
                        agent.stream_delta_callback = None
                        agent.tool_gen_callback = None
                        result = agent.run_conversation(payload["prompt"])
            finally:
                if agent is not None:
                    with open(os.devnull, "w", encoding="utf-8") as sink:
                        with redirect_stdout(sink), redirect_stderr(sink):
                            try:
                                agent.shutdown_memory_provider()
                            except Exception:
                                pass
                            try:
                                agent.close()
                            except Exception:
                                pass
                    agent = None
        try:
            response = _completed_response(result, payload["phase"])
        except ValueError:
            return _fail("agent did not produce a complete final response", code=4)
        sys.stdout.write(response)
        if not response.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    except BaseException as exc:  # noqa: BLE001 - keep secrets out of stderr
        return _fail(f"agent execution failed ({type(exc).__name__})", code=4)


def _isolated_worker_main() -> int:
    _prepare_isolation()
    version = _hermes_version()
    if version != EXPECTED_HERMES_VERSION:
        return _fail(
            f"expected Hermes {EXPECTED_HERMES_VERSION}, found {version}",
            code=3,
        )
    try:
        _ensure_no_managed_scope()
        payload, runtime = _read_internal_payload()
        sys.stdin.close()
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        return _fail(str(exc))
    return _run_isolated(payload, runtime)


def main() -> int:
    if sys.argv[1:] == ["--check"]:
        return _check()
    if sys.argv[1:] == [ISOLATED_WORKER_ARGUMENT]:
        return _isolated_worker_main()
    if sys.argv[1:]:
        return _fail("no command-line arguments are accepted")
    try:
        payload = _read_payload()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _fail(str(exc))
    return _bootstrap_run(payload)


if __name__ == "__main__":
    raise SystemExit(main())
