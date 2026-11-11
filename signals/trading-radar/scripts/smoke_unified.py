#!/usr/bin/env python3
"""End-to-end smoke test for the unified Radar + QuantDinger deployment."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


def _env_value(path: Path, name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"missing {name} in protected environment file")


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    token: str = "",
) -> tuple[int, Any]:
    headers = {"Accept": "application/json", "User-Agent": "unified-deploy-smoke/1.0"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            raw = response.read(1024 * 1024)
    except HTTPError as exc:
        status = exc.code
        raw = exc.read(1024 * 1024)
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"request failed for {path}: {type(exc).__name__}") from exc
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"non-JSON response for {path} (HTTP {status})") from exc


def run(base_url: str, backend_env: Path) -> None:
    username = _env_value(backend_env, "ADMIN_USER")
    password = _env_value(backend_env, "ADMIN_PASSWORD")

    health_status, health = _request(base_url, "/api/health")
    if health_status != 200 or health.get("status") != "healthy":
        raise RuntimeError("QuantDinger health contract failed")

    unauth_status, _unauth = _request(base_url, "/api/radar/market-context")
    if unauth_status != 401:
        raise RuntimeError("Radar facade did not reject an unauthenticated request")

    login_status, login = _request(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
    )
    login_data = login.get("data") if isinstance(login, dict) else None
    token = login_data.get("token") if isinstance(login_data, dict) else None
    userinfo = login_data.get("userinfo") if isinstance(login_data, dict) else None
    if login_status != 200 or login.get("code") != 1 or not token:
        raise RuntimeError("QuantDinger admin login failed")
    role = userinfo.get("role") if isinstance(userinfo, dict) else None
    if not isinstance(role, dict) or role.get("id") != "admin":
        raise RuntimeError("QuantDinger initialized user is not an administrator")

    context_status, context = _request(
        base_url,
        "/api/radar/market-context",
        token=token,
    )
    context_data = context.get("data") if isinstance(context, dict) else None
    required = {"schema_version", "status", "risk", "signals", "quality"}
    if (
        context_status != 200
        or context.get("code") != 1
        or not isinstance(context_data, dict)
        or not required.issubset(context_data)
    ):
        safe_error = context.get("error") if isinstance(context, dict) else None
        safe_keys = sorted(context_data) if isinstance(context_data, dict) else []
        raise RuntimeError(
            "authenticated Radar market-context contract failed: "
            f"http={context_status} code={context.get('code') if isinstance(context, dict) else None} "
            f"error={safe_error!r} data_keys={safe_keys!r}"
        )

    logger.info("quantdinger_health=ok")
    logger.info("quantdinger_unauth_guard=ok")
    logger.info("quantdinger_admin_login=ok")
    logger.info("radar_context_facade=ok")
    logger.info("credentials_or_tokens_printed=false")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8888")
    parser.add_argument(
        "--backend-env",
        type=Path,
        default=Path("/home/ubuntu/QuantDinger/backend.env"),
    )
    args = parser.parse_args()
    run(args.base_url, args.backend_env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
