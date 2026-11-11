from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.runtime-cleanup.yml")
_RETIRED_SERVICES = {"redis", "monitor", "ml-serve", "dashboard"}


@lru_cache(maxsize=1)
def _compose_command() -> tuple[str, ...]:
    candidates: list[tuple[str, ...]] = []

    if docker := shutil.which("docker"):
        candidates.append((docker, "compose"))
    if standalone := shutil.which("docker-compose"):
        candidates.append((standalone,))

    for candidate in candidates:
        result = subprocess.run(
            [*candidate, "version"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate

    pytest.fail("Docker Compose is required for the offline runtime-cleanup contract tests")


def _compose_config(*options: str) -> str:
    command = [*_compose_command()]
    for compose_file in _COMPOSE_FILES:
        command.extend(("-f", compose_file))
    command.extend(options)

    env = os.environ.copy()
    env["TELEGRAM_CHAT_ID"] = "contract-test"
    result = subprocess.run(
        command,
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Compose config command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout


def _configured_services(*profiles: str) -> set[str]:
    options: list[str] = []
    for profile in profiles:
        options.extend(("--profile", profile))
    options.extend(("config", "--services"))
    return set(_compose_config(*options).splitlines())


def test_default_runtime_excludes_retired_services():
    default_services = _configured_services()

    assert _RETIRED_SERVICES.isdisjoint(default_services)
    assert "macro-risk-producer" in default_services


def test_retired_profile_restores_the_complete_legacy_runtime():
    retired_services = _configured_services("retired")

    assert _RETIRED_SERVICES <= retired_services


def test_legacy_ui_profile_remains_an_alias_for_the_retired_runtime():
    legacy_ui_services = _configured_services("legacy-ui")
    retired_services = _configured_services("retired")

    assert _RETIRED_SERVICES <= legacy_ui_services
    assert legacy_ui_services == retired_services


def test_macro_risk_default_runtime_contract_is_preserved():
    rendered = json.loads(_compose_config("config", "--format", "json"))
    macro_risk = rendered["services"]["macro-risk-producer"]

    assert not macro_risk.get("profiles")
    assert macro_risk["image"] == "trading-radar-macro-risk:2026.08.02-headless.1"
    assert Path(macro_risk["build"]["context"]).resolve() == _ROOT.resolve()
    assert macro_risk["build"]["dockerfile"] == "Dockerfile.macro-risk"
    assert macro_risk["build"]["args"] == {
        "BASE_IMAGE": "docker.1ms.run/library/python:3.12-slim-bookworm"
    }
    assert macro_risk["container_name"] == "radar-macro-risk"
    assert macro_risk["restart"] == "unless-stopped"
    assert macro_risk["init"] is True
    assert macro_risk["read_only"] is True
    assert macro_risk["user"] == "1000:1000"
    assert macro_risk["depends_on"] == {
        "mihomo": {"condition": "service_healthy", "required": True}
    }
    assert macro_risk["environment"] == {
        "NO_PROXY": "127.0.0.1,localhost,mihomo,macro-risk-producer",
        "RADAR_ALL_PROXY": "http://mihomo:7890",
        "RADAR_HTTP_PROXY": "http://mihomo:7890",
        "RADAR_HTTPS_PROXY": "http://mihomo:7890",
        "RADAR_RISK_FETCH_INTERVAL_SEC": "300",
        "RADAR_RISK_HEALTH_PORT": "8093",
        "RADAR_RISK_MAX_STATE_AGE_SEC": "900",
        "RADAR_RISK_OBSERVATION_TTL_SEC": "900",
        "RADAR_RISK_OVERRIDE_FILE": "/app/data/radar_risk_override.json",
        "RADAR_RISK_STATE_FILE": "/app/data/radar_risk_state.json",
    }
    assert macro_risk["healthcheck"] == {
        "test": ["CMD", "python", "/app/run_macro_risk_producer.py", "--healthcheck"],
        "timeout": "5s",
        "interval": "30s",
        "retries": 3,
        "start_period": "1m0s",
    }
    assert macro_risk["cap_drop"] == ["ALL"]
    assert macro_risk["security_opt"] == ["no-new-privileges:true"]
    assert macro_risk["pids_limit"] == 64
    assert macro_risk["mem_limit"] == "100663296"
    assert macro_risk["cpus"] == 0.2
    assert macro_risk["stop_grace_period"] == "20s"
    assert macro_risk["tmpfs"] == ["/tmp:size=8m,mode=1777"]
    assert macro_risk["networks"] == {"radar-net": None}

    data_mounts = [volume for volume in macro_risk["volumes"] if volume["target"] == "/app/data"]
    assert len(data_mounts) == 1
    assert Path(data_mounts[0]["source"]).resolve() == (_ROOT / "data").resolve()
    assert data_mounts[0].get("read_only", False) is False
