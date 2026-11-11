"""Read the Trading Radar decision context through its private service API."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_TOKEN_BYTES = 4096
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True)
class RadarContextError(Exception):
    """A bounded, user-safe failure returned by the Radar adapter."""

    code: str
    message: str
    http_status: int = 502
    retriable: bool = True

    def __str__(self) -> str:
        return self.message


def _configured_url() -> str:
    url = (os.getenv("RADAR_CONTEXT_URL") or "").strip()
    parsed = urlsplit(url)
    if not url:
        raise RadarContextError(
            "radar_not_configured",
            "Trading Radar context is not configured",
            http_status=503,
            retriable=False,
        )
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RadarContextError(
            "radar_invalid_configuration",
            "Trading Radar context URL is invalid",
            http_status=503,
            retriable=False,
        )
    return url


def _configured_token() -> str:
    raw_path = (os.getenv("RADAR_CONTEXT_TOKEN_FILE") or "").strip()
    if not raw_path:
        raise RadarContextError(
            "radar_not_configured",
            "Trading Radar service credential is not configured",
            http_status=503,
            retriable=False,
        )
    try:
        path = Path(raw_path)
        if path.stat().st_size > MAX_TOKEN_BYTES:
            raise ValueError("token file is too large")
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError) as exc:
        raise RadarContextError(
            "radar_credential_unavailable",
            "Trading Radar service credential is unavailable",
            http_status=503,
            retriable=False,
        ) from exc
    if not token or any(character.isspace() for character in token):
        raise RadarContextError(
            "radar_credential_invalid",
            "Trading Radar service credential is invalid",
            http_status=503,
            retriable=False,
        )
    return token


def _timeout_seconds() -> float:
    try:
        configured = float(os.getenv("RADAR_CONTEXT_TIMEOUT_SECONDS", ""))
    except ValueError:
        configured = DEFAULT_TIMEOUT_SECONDS
    if not 0.25 <= configured <= 15.0:
        return DEFAULT_TIMEOUT_SECONDS
    return configured


def fetch_market_context() -> dict[str, Any]:
    """Fetch and validate one bounded Radar market-context response."""
    request = Request(
        _configured_url(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {_configured_token()}",
            "User-Agent": "QuantDinger-Radar-Adapter/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status = 503 if exc.code in {401, 403} else 502
        code = "radar_auth_failed" if status == 503 else "radar_upstream_error"
        raise RadarContextError(
            code,
            "Trading Radar rejected the internal request"
            if status == 503
            else "Trading Radar returned an upstream error",
            http_status=status,
            retriable=exc.code >= 500,
        ) from exc
    except (TimeoutError, socket.timeout, URLError, OSError) as exc:
        raise RadarContextError(
            "radar_unavailable",
            "Trading Radar is temporarily unavailable",
            http_status=502,
            retriable=True,
        ) from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise RadarContextError(
            "radar_response_too_large",
            "Trading Radar returned an oversized response",
            http_status=502,
            retriable=False,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RadarContextError(
            "radar_invalid_response",
            "Trading Radar returned an invalid response",
            http_status=502,
            retriable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise RadarContextError(
            "radar_invalid_response",
            "Trading Radar returned an invalid response",
            http_status=502,
            retriable=True,
        )
    return payload
