"""Load the tracked Radar config plus the local watchlist overlay."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from radar.config_schema import ConfigSchemaError, validate_monitor_config_dict
from radar.secrets import merge_env_into_config_data

_ALLOWED_LOCAL_KEYS = frozenset({"targets"})


def load_config(
    config_path: str | Path = "config.json",
    *,
    validate_monitor: bool = True,
) -> dict[str, Any]:
    """Load config and replace ``targets`` from ``config.local.json``.

    The local overlay is deliberately narrow: only the top-level ``targets``
    key is accepted and the array is replaced as a whole.
    """

    base_path = Path(config_path).resolve()
    raw = json.loads(base_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigSchemaError("根配置必须是 JSON 对象")

    configured_local_path = os.environ.get("RADAR_LOCAL_CONFIG", "").strip()
    local_path = (
        Path(configured_local_path).resolve()
        if configured_local_path
        else base_path.with_name("config.local.json")
    )
    if local_path.exists():
        local = json.loads(local_path.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            raise ConfigSchemaError("config.local.json 必须是 JSON 对象")
        extra = set(local) - _ALLOWED_LOCAL_KEYS
        if extra:
            raise ConfigSchemaError(
                f"config.local.json 仅允许顶层 targets，拒绝键: {sorted(extra)}"
            )
        if "targets" in local:
            if not isinstance(local["targets"], list):
                raise ConfigSchemaError("config.local.json targets 必须是数组")
            raw["targets"] = local["targets"]

    merged = merge_env_into_config_data(raw)
    if validate_monitor:
        validate_monitor_config_dict(merged)
    return merged
