"""Minimal rate-limited HTTP client (stdlib). Outbound goes through the host mihomo proxy
(QP_HTTP_PROXY, default http://127.0.0.1:7890) unless a source is direct in the legacy code too."""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

PROXY = os.getenv("QP_HTTP_PROXY", "http://127.0.0.1:7890")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
MIN_INTERVAL_S = float(os.getenv("QP_HTTP_MIN_INTERVAL", "1.0"))
_last: dict[str, float] = {}


class FetchError(RuntimeError):
    """Safe machine-readable error code; never includes response bodies or secrets."""


@dataclass
class Resp:
    status: int
    body: bytes
    ms: float
    via_proxy: bool
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self, enc: str = "utf-8") -> str:
        return self.body.decode(enc, errors="replace")


def _opener(proxy: bool):
    handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY} if proxy and PROXY else {})]
    return urllib.request.build_opener(*handlers)


def fetch(url: str, *, params: dict | None = None, data: dict | None = None, headers: dict | None = None,
          proxy: bool = True, timeout: float = 20, max_bytes: int = 8_000_000) -> Resp:
    """GET (or POST form when data is given). Enforces a per-host minimum interval (rate limit)."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    host = urllib.parse.urlsplit(url).netloc
    wait = MIN_INTERVAL_S - (time.monotonic() - _last.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    t0 = time.monotonic()
    try:
        with _opener(proxy).open(req, timeout=timeout) as r:
            raw = r.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise FetchError("response_too_large")
            return Resp(r.status, raw, round((time.monotonic() - t0) * 1000, 1), proxy,
                        {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        raise FetchError(f"http_{e.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise FetchError(type(getattr(e, "reason", e)).__name__) from None
    finally:
        _last[host] = time.monotonic()


def tcp_probe(host: str, port: int, timeout: float = 5) -> tuple[bool, float, str | None]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, round((time.monotonic() - t0) * 1000, 1), None
    except OSError as e:
        return False, round((time.monotonic() - t0) * 1000, 1), type(e).__name__
