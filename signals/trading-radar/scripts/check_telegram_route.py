#!/usr/bin/env python3
"""Keep the Mihomo Telegram selector on a node that reaches Telegram."""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path("/home/ubuntu/mihomo/config.yaml")
# unify-phase1 2026-11-11: single host mihomo (controller 127.0.0.1:9090, no secret); radar-mihomo retired.
DEFAULT_CONTROLLER = "http://127.0.0.1:9090"
PROBE_URL = "https://api.telegram.org"
PROBE_TIMEOUT_MS = 5000
LOGGER = logging.getLogger(__name__)


def _read_secret(path: Path) -> str:
    """Read only the top-level controller secret without parsing proxy entries."""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("secret:"):
            return raw_line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _headers(secret: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _get_proxies(controller: str, secret: str) -> dict:
    request = urllib.request.Request(
        f"{controller.rstrip('/')}/proxies",
        headers=_headers(secret),
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response).get("proxies", {})


def _find_telegram_group(proxies: dict) -> tuple[str, dict]:
    groups = [
        (name, detail)
        for name, detail in proxies.items()
        if "telegram" in name.casefold() and detail.get("all")
    ]
    if len(groups) != 1:
        raise RuntimeError(f"Telegram 选择组数量异常: {len(groups)}")
    return groups[0]


def _probe(
    controller: str,
    secret: str,
    proxy_name: str,
) -> Optional[int]:
    encoded_name = urllib.parse.quote(proxy_name, safe="")
    query = urllib.parse.urlencode(
        {"url": PROBE_URL, "timeout": PROBE_TIMEOUT_MS}
    )
    request = urllib.request.Request(
        f"{controller.rstrip('/')}/proxies/{encoded_name}/delay?{query}",
        headers=_headers(secret),
    )
    try:
        with urllib.request.urlopen(request, timeout=7) as response:
            delay = int(json.load(response).get("delay", 0))
        return delay if delay > 0 else None
    except Exception:
        return None


def _choose_fastest(delays: list[Optional[int]]) -> Optional[int]:
    working = [(delay, index) for index, delay in enumerate(delays) if delay]
    return min(working)[1] if working else None


def _select(controller: str, secret: str, group_name: str, member: str) -> None:
    encoded_group = urllib.parse.quote(group_name, safe="")
    body = json.dumps({"name": member}).encode("utf-8")
    request = urllib.request.Request(
        f"{controller.rstrip('/')}/proxies/{encoded_group}",
        data=body,
        headers=_headers(secret, json_body=True),
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Mihomo 选择节点失败: HTTP {response.status}")


def check_route() -> int:
    config_path = Path(os.getenv("MIHOMO_CONFIG", str(DEFAULT_CONFIG))).expanduser()
    controller = os.getenv("MIHOMO_CONTROLLER_URL", DEFAULT_CONTROLLER).strip()
    secret = _read_secret(config_path)
    proxies = _get_proxies(controller, secret)
    group_name, group = _find_telegram_group(proxies)
    members = list(group.get("all") or [])
    current = group.get("now")
    current_index = members.index(current) if current in members else -1

    if current and _probe(controller, secret, current) is not None:
        LOGGER.info("Telegram 路由健康: current_index=%s", current_index)
        return 0

    LOGGER.warning(
        "Telegram 当前路由不可达，开始探测 %s 个候选节点",
        len(members),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        delays = list(
            pool.map(lambda member: _probe(controller, secret, member), members)
        )
    target_index = _choose_fastest(delays)
    if target_index is None:
        LOGGER.error("Telegram 所有候选路由均不可达")
        return 1

    target = members[target_index]
    if target != current:
        _select(controller, secret, group_name, target)
    if _probe(controller, secret, target) is None:
        if current and target != current:
            _select(controller, secret, group_name, current)
        LOGGER.error("Telegram 新路由复核失败，已恢复原选择")
        return 1

    LOGGER.info(
        "Telegram 路由自愈完成: %s -> %s, delay_ms=%s",
        current_index,
        target_index,
        delays[target_index],
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    lock_path = PROJECT_ROOT / "data" / ".telegram_route_health.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, flags, 0o640)
    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.info("Telegram 路由检查已在运行，本次跳过")
            return 0
        return check_route()


if __name__ == "__main__":
    raise SystemExit(main())
