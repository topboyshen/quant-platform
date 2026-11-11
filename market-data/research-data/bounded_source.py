"""A bounded, JSON-only collector child; the outer supervisor owns its group.

The child cannot create subprocesses and never inherits the snapshot lock. A
timeout is terminal for this run, not permission to start another collector.
No endpoint response, exception text, credentials or request URL is exposed.
"""
import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

MAX_BYTES = 4 * 1024 * 1024
CALL_SECONDS = 75


class CollectionBoundaryError(RuntimeError):
    pass


class CollectionFailure(RuntimeError):
    def __init__(self, error_type, endpoint=None):
        self.error_type, self.endpoint = error_type, endpoint
        super().__init__("collector_failed:" + (endpoint or "source"))


class _Discard(io.TextIOBase):
    """Discard vendor diagnostics immediately instead of retaining them in RAM."""
    def write(self, text):
        return len(text)


def _decode_result(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise CollectionBoundaryError("collector_result_invalid") from None
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise CollectionBoundaryError("collector_result_invalid")
    if value["ok"]:
        if set(value) != {"ok", "value"}:
            raise CollectionBoundaryError("collector_result_invalid")
        return value["value"]
    if not {"ok", "errorType"} <= set(value) <= {"ok", "errorType", "endpoint"}:
        raise CollectionBoundaryError("collector_result_invalid")
    error_type = value["errorType"]
    if not isinstance(error_type, str) or error_type not in {
        "ValueError", "TimeoutError", "ConnectionError", "RuntimeError", "HTTPError", "OtherError", "ResponseTooLarge"
    }:
        raise CollectionBoundaryError("collector_result_invalid")
    endpoint = value.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or endpoint not in {
        "stock_board_industry_summary_ths", "stock_market_activity_legu"
    }):
        raise CollectionBoundaryError("collector_result_invalid")
    raise CollectionFailure(error_type, endpoint)


def _child(func, args, kwargs, destination, lock_fd):
    if lock_fd is not None:
        os.close(lock_fd)
    def no_helpers(event, _args):
        if event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn"}:
            raise CollectionBoundaryError("collector_helper_forbidden")
    sys.addaudithook(no_helpers)
    try:
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            result = func(*args, **kwargs)
        data = {"ok": True, "value": result}
    except Exception as exc:
        error_type = getattr(exc, "error_type", type(exc).__name__)
        data = {"ok": False, "errorType": error_type if error_type in {
            "ValueError", "TimeoutError", "ConnectionError", "RuntimeError", "HTTPError"} else "OtherError"}
        endpoint = getattr(exc, "endpoint", None)
        if endpoint in {"stock_board_industry_summary_ths", "stock_market_activity_legu"}:
            data["endpoint"] = endpoint
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        encoded = b'{"ok":false,"errorType":"ResponseTooLarge"}'
    with open(destination, "xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def call(func, args=(), kwargs=None, *, timeout=CALL_SECONDS, state_dir=None):
    if timeout <= 0:
        raise CollectionBoundaryError("collector_budget_exhausted")
    # Fork preserves fixed call arguments without serializing callable source or
    # importing a business module in a second, unbounded interpreter.
    context = multiprocessing.get_context("fork")
    directory = Path(tempfile.mkdtemp(prefix="source-", dir=state_dir))
    path = directory / "result.json"
    descriptor = os.environ.get("RESEARCH_DATA_SUPERVISOR_LOCK_FD")
    lock_fd = int(descriptor) if descriptor is not None else None
    process = context.Process(target=_child, args=(func, args, kwargs or {}, str(path), lock_fd))
    started = time.monotonic()
    joined = False
    try:
        process.start()
        process.join(timeout)
        if process.is_alive():
            # This Process owns an unreaped child, not an arbitrary numeric
            # PGID. The pinned outer watchdog drains any remaining group.
            process.kill()
            process.join(2)
            joined = not process.is_alive()
            raise CollectionBoundaryError("collector_deadline_exceeded" if joined else "collector_cleanup_uncertain")
        joined = True
        if process.exitcode != 0 or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise CollectionBoundaryError("collector_result_unavailable")
        result = _decode_result(path.read_bytes())
        if time.monotonic() - started > timeout + 2:
            raise CollectionBoundaryError("collector_budget_exceeded")
        return result
    finally:
        if joined or process.pid is None:
            shutil.rmtree(directory)
        # Uncertain cleanup leaves private evidence for the outer supervisor;
        # never race a still-live child by deleting its working directory.
