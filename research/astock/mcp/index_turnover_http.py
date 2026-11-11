"""One index HTTP request in an exec-isolated, killable process.

Socket timeouts are only secondary guards. The parent owns the wall-clock deadline,
bounded IPC and process-group cleanup; a timed-out worker cannot keep requesting in
the background. This module has no imports from the provider and no business writes.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from typing import Any, Mapping

CLEANUP_RESERVE_SECONDS = 0.25
MAX_PROTOCOL_BYTES = 262_144
MAX_REQUEST_BYTES = 4096


class IndexFetchError(Exception):
    def __init__(self, stage: str, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail
        self.retryable = retryable


_YUAN_NAMES = frozenset({"CNY", "元", "人民币元"})
_UNIT_KEYS = frozenset({"turnover", "成交额", "amount"})


def validate_unit_declarations(data: Mapping[str, Any]) -> None:
    """Reject unclear/conflicting explicit units without inferring missing units.

    The real endpoint's absent unit field is not independent proof of CNY; that
    interpretation rests on its field contract. Any explicit declaration, whether
    in the response envelope or data object, must agree with that contract.
    This pure rule is shared by the isolated worker and offline provider fixtures.
    """
    def check(value: Any, label: str) -> None:
        if not isinstance(value, str) or value.strip() not in _YUAN_NAMES:
            raise IndexFetchError("unit", f"响应声明的单位不是人民币元（{label[:64]}）")

    for key, value in data.items():
        name = str(key)
        if name == "units":
            if not isinstance(value, Mapping):
                raise IndexFetchError("unit", "units 不是对象")
            for unit_key, unit_value in value.items():
                if unit_key not in _UNIT_KEYS:
                    raise IndexFetchError("unit", "units 含无法解释的键")
                check(unit_value, f"units.{unit_key}")
        elif "unit" in name.lower() or name.endswith("单位"):
            check(value, name)


def _worker_command() -> list[str]:
    # -I ignores PYTHONPATH/user-site injections; the production interpreter's venv
    # still supplies requests. Exec avoids inheriting locks from parent threads.
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"]


def _kill_and_reap(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Kill this owned session, including any descendants, and reap within budget."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise IndexFetchError("isolation", "无法终止取数进程组；本轮禁止继续请求") from error
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        raise IndexFetchError("isolation", "取数进程未在收尾期限内退出；本轮禁止继续请求") from error


def fetch_kline_isolated(
    secid: str,
    day_text: str,
    timeout: tuple[float, float],
    total_seconds: float,
    *,
    url: str,
    max_body_bytes: int,
    absolute_deadline: float | None = None,
) -> Mapping[str, Any]:
    """Return only a complete in-budget response from one non-redirecting request.

The supplied budget includes child startup, response/IPC and cleanup. A small part
is reserved for SIGKILL and bounded reap, rather than spending the entire budget
on HTTP then leaving a worker behind. OS inability to terminate/reap is an explicit
isolation failure, never a successful response or a reason to start another request.
The provider passes its shared absolute monotonic deadline: a delayed function
entry must not renew a previously computed remaining budget. Relative-only calls
remain supported for standalone transport tests, not the production provider path.
"""
    started = time.monotonic()
    if os.name != "posix":
        raise IndexFetchError("isolation", "当前平台不支持所需的进程组隔离")
    if type(total_seconds) not in (int, float) or not math.isfinite(total_seconds) or total_seconds <= 0:
        raise IndexFetchError("budget", "成交额补源总预算已用尽")
    deadline = started + total_seconds
    if absolute_deadline is not None:
        if type(absolute_deadline) not in (int, float) or not math.isfinite(absolute_deadline):
            raise IndexFetchError("budget", "成交额补源绝对截止无效")
        deadline = min(deadline, absolute_deadline)
    remaining = deadline - started
    if remaining <= 0:
        raise IndexFetchError("budget", "进入取数隔离前共享预算已用尽，未启动请求")
    reserve = min(CLEANUP_RESERVE_SECONDS, remaining / 4)
    request_deadline = deadline - reserve
    request = json.dumps({
        "secid": secid, "day": day_text, "timeout": timeout, "url": url,
        "maxBodyBytes": max_body_bytes, "deadline": request_deadline, "parentPid": os.getpid(),
    }, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        raise IndexFetchError("isolation", "取数控制消息超过上限")

    process: subprocess.Popen[bytes] | None = None
    output = bytearray()
    try:
        command = _worker_command()
        if time.monotonic() >= request_deadline:
            raise IndexFetchError("budget", "启动取数进程前共享预算已用尽，未启动请求")
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        assert process.stdin is not None and process.stdout is not None
        # A single <=4096-byte control frame cannot fill a POSIX pipe. Do not send
        # env credentials, inherited descriptors, archives or arbitrary callables.
        process.stdin.write(request)
        process.stdin.close()
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while not eof:
                remaining = request_deadline - time.monotonic()
                if remaining <= 0:
                    raise IndexFetchError("budget", "请求或响应未在总预算内结束，已终止并丢弃结果")
                ready = selector.select(remaining)
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    eof = True
                    continue
                output.extend(chunk)
                if len(output) > MAX_PROTOCOL_BYTES:
                    raise IndexFetchError("isolation", "取数进程响应超过通信上限")
        try:
            process.wait(timeout=max(0.0, request_deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise IndexFetchError("budget", "取数进程未在总预算内结束，已终止并丢弃结果") from error
        if time.monotonic() >= request_deadline:
            raise IndexFetchError("budget", "取数结果晚于总预算，已丢弃")
        if process.returncode != 0:
            raise IndexFetchError("isolation", "取数进程异常退出，未取得可信响应")
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise IndexFetchError("isolation", "取数进程响应不完整或格式错误") from error
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise IndexFetchError("isolation", "取数进程响应身份无效")
        if response["ok"] is False:
            stage = response.get("stage")
            detail = response.get("detail")
            retryable = response.get("retryable")
            if (stage not in {"budget", "kline_request", "kline_response", "isolation", "unit"}
                    or not isinstance(detail, str) or len(detail) > 240
                    or type(retryable) is not bool):
                raise IndexFetchError("isolation", "取数进程错误响应格式无效")
            raise IndexFetchError(stage, detail, retryable=retryable)
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise IndexFetchError("kline_response", "响应缺少 data 对象")
        return data
    except (OSError, BrokenPipeError) as error:
        raise IndexFetchError("isolation", "取数隔离进程或通信失败") from error
    finally:
        if process is not None:
            try:
                _kill_and_reap(process, deadline)
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()


def _fetch_once(request: Mapping[str, Any]) -> Mapping[str, Any]:
    import requests

    class NoRedirectSession(requests.Session):
        def resolve_redirects(self, *args: Any, **kwargs: Any):
            # Even allow_redirects=False normally prepares Response.next through
            # resolve_redirects, which consumes a redirect body first. A malicious
            # or endless 3xx body must not delay our header-only rejection.
            return iter(())

    remaining = float(request["deadline"]) - time.monotonic()
    if remaining <= 0:
        raise IndexFetchError("budget", "启动后预算已用尽，未发送请求")
    timeout = tuple(min(float(value), remaining) for value in request["timeout"])
    params = {
        "secid": request["secid"], "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101", "fqt": "0", "beg": request["day"], "end": request["day"],
    }
    try:
        with NoRedirectSession() as session:
            session.trust_env = False
            # No redirects, adapter retries or fallback endpoint: one HTTP attempt.
            with session.get(request["url"], params=params, allow_redirects=False,
                             stream=True, timeout=timeout) as response:
                if 300 <= response.status_code < 400:
                    raise IndexFetchError("kline_response", f"HTTP {response.status_code} 重定向被拒绝")
                if response.status_code >= 400:
                    raise IndexFetchError("kline_request", f"HTTP {response.status_code}",
                                          retryable=response.status_code == 429 or 500 <= response.status_code < 600)
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError as error:
                        raise IndexFetchError("kline_response", "响应长度声明无效") from error
                    if declared < 0 or declared > request["maxBodyBytes"]:
                        raise IndexFetchError("kline_response", "响应体超过大小上限")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=4096):
                    if time.monotonic() >= request["deadline"]:
                        raise IndexFetchError("budget", "响应超过总预算，已丢弃")
                    body.extend(chunk)
                    if len(body) > request["maxBodyBytes"]:
                        raise IndexFetchError("kline_response", "响应体超过大小上限")
                if time.monotonic() >= request["deadline"]:
                    raise IndexFetchError("budget", "响应超过总预算，已丢弃")
                def reject_nonfinite(value: str) -> None:
                    raise ValueError(f"non-finite JSON value: {value}")

                try:
                    document = json.loads(body, parse_constant=reject_nonfinite)
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise IndexFetchError("kline_response", "响应不是有效 JSON") from error
    except requests.exceptions.ContentDecodingError as error:
        raise IndexFetchError("kline_response", "响应内容解码失败", retryable=False) from error
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError) as error:
        # Class only: URLs, proxy credentials and server-provided body never enter
        # public failure diagnostics. Broken chunk framing is treated as interrupted
        # transfer; validly transferred but undecodable content is not retried above.
        raise IndexFetchError("kline_request", type(error).__name__, retryable=True) from error
    if isinstance(document, Mapping):
        validate_unit_declarations(document)
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(data, Mapping):
        raise IndexFetchError("kline_response", "响应缺少 data 对象")
    validate_unit_declarations(data)
    return data


def _arm_parent_death(parent_pid: int) -> None:
    """Production Linux: kill the HTTP worker if its owning process is killed.

    Set this inside the freshly exec'd child, never in a preexec_fn in the
    multithreaded provider. Recheck the parent afterwards to close the race.
    Non-Linux POSIX development hosts still use the parent-owned deadline cleanup.
    """
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise IndexFetchError("isolation", "无法建立取数进程父子生命周期保护")
    if os.getppid() != parent_pid:
        raise IndexFetchError("isolation", "取数父进程已改变，拒绝发送请求")


def _main() -> int:
    try:
        encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(encoded) > MAX_REQUEST_BYTES:
            raise IndexFetchError("kline_request", "控制消息超过上限")
        request = json.loads(encoded)
        _arm_parent_death(request["parentPid"])
        data = _fetch_once(request)
        result = {"ok": True, "data": data}
    except IndexFetchError as error:
        result = {"ok": False, "stage": error.stage, "detail": error.detail, "retryable": error.retryable}
    except Exception:
        result = {"ok": False, "stage": "kline_response", "detail": "取数进程内部失败", "retryable": False}
    sys.stdout.buffer.write(json.dumps(result, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main() if sys.argv[1:] == ["--worker"] else 2)
