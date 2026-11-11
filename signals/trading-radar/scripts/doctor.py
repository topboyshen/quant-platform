#!/usr/bin/env python3
"""Trading Radar startup doctor.

Read-only checks for config, model artifacts, runtime ports, secrets posture, and
common production risks. Designed to be safe to run before restarts.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _mask(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _fetch_json(url: str, timeout: float = 1.0) -> tuple[bool, dict[str, Any] | str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, json.loads(body) if body else {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)


def check_config(config: dict[str, Any]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    from radar.config_schema import ConfigSchemaError, audit_optional_fields, validate_monitor_config_dict

    try:
        validate_monitor_config_dict(config)
        findings.append(("OK", "config schema validation passed"))
    except ConfigSchemaError as exc:
        findings.append(("FAIL", f"config schema validation failed: {exc}"))

    for issue in audit_optional_fields(config, raw_data=config):
        findings.append(("WARN", f"config audit: {issue}"))

    alerts = config.get("alerts", {}) if isinstance(config.get("alerts"), dict) else {}
    rl_mode = config.get("rl_mode", "confirm")
    if rl_mode not in {"off", "confirm", "independent"}:
        findings.append(("FAIL", f"unsupported rl_mode={rl_mode!r}"))

    ml_enabled = alerts.get("ml_prediction", {}).get("enabled", True)
    rl_enabled = alerts.get("rl_strategy", {}).get("enabled", True)
    findings.append(("INFO", f"global ml_enabled={ml_enabled}, rl_enabled={rl_enabled}, rl_mode={rl_mode}"))
    return findings


def check_models(config: dict[str, Any]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    alerts = config.get("alerts", {}) if isinstance(config.get("alerts"), dict) else {}
    global_ml = alerts.get("ml_prediction", {}).get("enabled", True)
    global_rl = alerts.get("rl_strategy", {}).get("enabled", True)
    ml_dir = ROOT / str(config.get("ml_model_dir", "models"))
    rl_dir = ROOT / str(config.get("rl_model_dir", "rl_models"))

    for target in config.get("targets", []):
        code = str(target.get("code", "")).zfill(6)
        direction = str(target.get("direction", "below"))
        name = target.get("name", code)
        if global_ml and target.get("ml_enabled", True):
            model = ml_dir / f"{code}_{direction}_model.pkl"
            scaler = ml_dir / f"{code}_{direction}_scaler.pkl"
            features = ml_dir / f"{code}_{direction}_features.json"
            missing = [p.name for p in (model, scaler, features) if not p.exists()]
            if missing:
                findings.append(("WARN", f"ML artifacts missing for {name}({code}) {direction}: {', '.join(missing)}"))
        if global_rl and target.get("rl_enabled", global_rl):
            universal = rl_dir / f"ppo_universal_{direction}.zip"
            if not universal.exists():
                findings.append(("WARN", f"RL universal model missing for {name}({code}) {direction}: {universal.name}"))

    return findings


def check_secrets(config: dict[str, Any]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    tg = config.get("telegram", {}) if isinstance(config.get("telegram"), dict) else {}
    token = os.getenv("TELEGRAM_BOT_TOKEN", str(tg.get("bot_token", ""))).strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", str(tg.get("chat_id", ""))).strip()
    disabled = {"", "DISABLED", "REPLACE", "REPLACE_ME", "NONE"}
    if token.upper() in disabled or chat.upper() in disabled:
        findings.append(("OK", "Telegram is disabled or incomplete"))
    else:
        findings.append(("WARN", f"Telegram appears enabled (token={_mask(token)}, chat_id={_mask(chat)})"))

    env_path = ROOT / ".env"
    if env_path.exists():
        findings.append(("WARN", ".env exists in project directory; ensure chmod 600 and never commit/back up with logs"))
    return findings


def check_runtime(config: dict[str, Any]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    health = config.get("health", {}) if isinstance(config.get("health"), dict) else {}
    health_host = os.getenv("HEALTH_HOST", str(health.get("host", "127.0.0.1")))
    health_port = int(os.getenv("HEALTH_PORT", str(health.get("port", 8080))))
    dashboard_host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "8081"))

    for label, host, port in (
        ("monitor health", "127.0.0.1", health_port),
        ("dashboard", "127.0.0.1", dashboard_port),
        ("ml serve", "127.0.0.1", int(os.getenv("ML_SERVE_PORT", "8090"))),
    ):
        findings.append(("INFO", f"{label} port {port} listening={_port_listening(host, port)}"))

    if health_host == "0.0.0.0":
        findings.append(("WARN", "monitor health host is 0.0.0.0; prefer 127.0.0.1 behind reverse proxy/firewall"))
    if dashboard_host == "0.0.0.0":
        findings.append(("WARN", "dashboard host is 0.0.0.0; prefer 127.0.0.1 behind reverse proxy/auth"))
    return findings


def check_ops_status(config: dict[str, Any]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    from dashboard.ops_status import (
        DASHBOARD_SERVICE_CANDIDATES,
        MONITOR_SERVICE_CANDIDATES,
        check_proxy_environment,
        get_process_stats,
        get_service_status,
    )

    monitor_service = get_service_status(MONITOR_SERVICE_CANDIDATES)
    if monitor_service.get("available"):
        findings.append(("OK", f"monitor unit detected: {monitor_service.get('unit')} status={monitor_service.get('status')}"))
    else:
        findings.append(("WARN", f"monitor systemd unit unavailable: {monitor_service.get('reason', 'unknown')}"))

    dashboard_service = get_service_status(DASHBOARD_SERVICE_CANDIDATES)
    if dashboard_service.get("available"):
        findings.append(("OK", f"dashboard unit detected: {dashboard_service.get('unit')} status={dashboard_service.get('status')}"))
    else:
        findings.append(("WARN", f"dashboard systemd unit unavailable: {dashboard_service.get('reason', 'unknown')}"))

    process = get_process_stats()
    monitor_count = process.get("count", 0)
    dashboard_count = process.get("dashboard", {}).get("count", 0)
    if process.get("multi_instance"):
        findings.append(("FAIL", f"multiple service processes detected: monitor={monitor_count}, dashboard={dashboard_count}"))
    else:
        findings.append(("OK", f"process instances: monitor={monitor_count}, dashboard={dashboard_count}"))

    proxy = check_proxy_environment()
    if proxy.get("ambient_proxy_vars"):
        findings.append(("WARN", f"ambient proxy vars present: {sorted(proxy['ambient_proxy_vars'])}; prefer RADAR_ALL_PROXY only"))
    else:
        findings.append(("OK", "ambient proxy vars are clear"))
    if proxy.get("radar_all_proxy"):
        findings.append(("INFO", f"RADAR_ALL_PROXY={proxy['radar_all_proxy']}"))
    else:
        findings.append(("INFO", "RADAR_ALL_PROXY is not set"))

    health = config.get("health", {}) if isinstance(config.get("health"), dict) else {}
    health_port = int(os.getenv("HEALTH_PORT", str(health.get("port", 8080))))
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "8081"))
    for label, url in (
        ("monitor /health", f"http://127.0.0.1:{health_port}/health"),
        ("dashboard /api/status", f"http://127.0.0.1:{dashboard_port}/api/status"),
    ):
        ok, payload = _fetch_json(url)
        if ok and isinstance(payload, dict):
            findings.append(("OK", f"{label} reachable keys={sorted(payload)[:8]}"))
        else:
            findings.append(("WARN", f"{label} unreachable or invalid: {payload}"))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run trading-radar read-only startup checks")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    args = parser.parse_args()

    config = _load_json(Path(args.config))
    checks = []
    checks.extend(check_config(config))
    checks.extend(check_models(config))
    checks.extend(check_secrets(config))
    checks.extend(check_runtime(config))
    checks.extend(check_ops_status(config))

    exit_code = 0
    for level, message in checks:
        print(f"[{level}] {message}")
        if level == "FAIL":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
