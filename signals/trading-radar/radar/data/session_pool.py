"""Shared HTTP session helpers with explicit connection pooling."""

from __future__ import annotations

import os
from typing import Mapping, Optional

import requests
from requests.adapters import HTTPAdapter


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _configured_proxies() -> dict[str, str]:
    proxies: dict[str, str] = {}
    http_proxy = os.getenv("RADAR_HTTP_PROXY")
    https_proxy = os.getenv("RADAR_HTTPS_PROXY")
    all_proxy = os.getenv("RADAR_ALL_PROXY")

    if all_proxy:
        proxies["http"] = all_proxy
        proxies["https"] = all_proxy
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    return proxies


def create_pooled_session(
    headers: Optional[Mapping[str, str]] = None,
    *,
    pool_connections: int = 16,
    pool_maxsize: int = 32,
    max_retries: int = 0,
    trust_env: Optional[bool] = None,
) -> requests.Session:
    """Create a requests Session with mounted HTTP/HTTPS connection pools.

    Market-data requests should not silently inherit unrelated shell/Cursor proxy
    variables. Set ``RADAR_HTTP_PROXY``/``RADAR_HTTPS_PROXY``/``RADAR_ALL_PROXY``
    when a stable proxy is explicitly required.
    """
    session = requests.Session()
    session.trust_env = _env_flag("RADAR_REQUESTS_TRUST_ENV", False) if trust_env is None else trust_env
    proxies = _configured_proxies()
    if proxies:
        session.proxies.update(proxies)
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=max_retries,
        pool_block=False,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if headers:
        session.headers.update(dict(headers))
    return session
