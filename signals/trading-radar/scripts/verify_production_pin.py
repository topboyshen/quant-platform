#!/usr/bin/env python3
"""Read-only application image pin gate. Image IDs are NOT registry manifest digests.

Preflight checks resolved Compose images and local image IDs without requiring
running containers (so a stopped service can be restored). Runtime verification
requires matching IDs/tags AND running, healthy containers. Never rebuild/pull.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PIN = ROOT / "deploy/production_pin.json"
REQUIRED = {"radar-quantdinger-backend", "radar-quantdinger-frontend", "radar-integration-api"}
ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}\Z")


def validate_pin(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("pin schema_version must be 1")
    services = data.get("services")
    if not isinstance(services, dict) or not REQUIRED.issubset(services):
        raise ValueError("pin is missing a required production service")
    for name, expected in services.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not name.startswith("radar-"):
            raise ValueError("invalid pinned container name")
        if not isinstance(expected, dict):
            raise ValueError("pin service object required")
        if not isinstance(expected.get("image_id"), str) or not ID.fullmatch(expected["image_id"]):
            raise ValueError("full sha256 image_id required: " + name)
        if not isinstance(expected.get("image"), str) or not IMAGE.fullmatch(expected["image"]):
            raise ValueError("image reference required: " + name)
    return data


def read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("oversize JSON")
    return json.loads(raw)


def load_pin(path: Path) -> dict[str, Any]:
    return validate_pin(read_json(path))


def _docker(args: list[str]) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=10, stderr=subprocess.DEVNULL)


def normalize_inspect(name: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("container inspect object required")
    config, state = payload.get("Config"), payload.get("State")
    if not isinstance(config, dict) or not isinstance(state, dict):
        raise ValueError("container config/state required")
    health = state.get("Health")
    return {"name": name, "image_id": payload.get("Image"), "image": config.get("Image"),
            "running": state.get("Running"),
            "health": health.get("Status") if isinstance(health, dict) else None}


def inspect_running(name: str) -> dict[str, Any]:
    # Do not read .Config.Env or other credentials into the diagnostic process.
    template = ('{"Image":{{json .Image}},"Config":{"Image":{{json .Config.Image}}},'
                '"State":{"Running":{{json .State.Running}},'
                '"Health":{{if .State.Health}}{"Status":{{json .State.Health.Status}}}{{else}}null{{end}}}}')
    return normalize_inspect(name, json.loads(_docker(["inspect", "--type", "container", "--format", template, name])))


def compare_service(name: str, expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    errors = []
    for key in ("image_id", "image"):
        if not expected.get(key) or actual.get(key) != expected[key]:
            errors.append(f"{name} {key} mismatch")
    if actual.get("running") is not True:
        errors.append(f"{name} is not running")
    if actual.get("health") != "healthy":
        errors.append(f"{name} is not healthy")
    return errors


def verify(pin: Mapping[str, Any], running: Mapping[str, Mapping[str, Any]]) -> list[str]:
    validate_pin(pin)
    errors = []
    for name, expected in pin["services"].items():
        if name not in running:
            errors.append(f"{name} is missing")
        else:
            errors.extend(compare_service(name, expected, running[name]))
    return errors


def preflight(pin: dict, compose: Any) -> list[str]:
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise ValueError("resolved Compose services required")
    errors = []
    for name, expected in pin["services"].items():
        service = compose["services"].get(name.removeprefix("radar-"))
        if not isinstance(service, dict) or service.get("image") != expected["image"]:
            errors.append(name + " resolved Compose image mismatch")
            continue
        actual_id = _docker(["image", "inspect", "--format", "{{.Id}}", expected["image"]]).strip()
        if actual_id != expected["image_id"]:
            errors.append(name + " local image ID mismatch")
    return errors


def runtime_errors(pin: dict, fixture: Any = None) -> list[str]:
    if fixture is not None:
        if not isinstance(fixture, dict):
            raise ValueError("inspect fixture mapping required")
        running = {n: normalize_inspect(n, p) for n, p in fixture.items()}
    else:
        running = {}
        for name in pin["services"]:
            try:
                running[name] = inspect_running(name)
            except subprocess.CalledProcessError:
                pass  # Missing containers fail verify(), never count as a healthy match.
    return verify(pin, running)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN)
    parser.add_argument("--inspect-json", type=Path, help="Offline Docker inspect fixture mapping")
    parser.add_argument("--compose-json", help="Resolved Compose JSON path; '-' reads stdin without logging secrets")
    parser.add_argument("--phase", choices=("runtime", "preflight"), default="runtime")
    parser.add_argument("--wait-seconds", type=int, default=0, help="Bounded post-deploy health wait (0..120)")
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 120:
        parser.error("wait-seconds must be 0..120")
    try:
        pin = load_pin(args.pin)
        if args.phase == "preflight":
            if not args.compose_json:
                raise ValueError("preflight requires resolved Compose JSON")
            if args.compose_json == "-":
                raw = sys.stdin.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("oversize Compose JSON")
                compose = json.loads(raw)
            else:
                compose = read_json(Path(args.compose_json))
            errors = preflight(pin, compose)
        else:
            fixture = read_json(args.inspect_json) if args.inspect_json else None
            if args.inspect_json and not isinstance(fixture, dict):
                raise ValueError("inspect fixture mapping required")
            deadline = time.monotonic() + args.wait_seconds
            while True:
                errors = runtime_errors(pin, fixture)
                if not errors or fixture is not None or time.monotonic() >= deadline:
                    break
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError) as exc:
        print("production pin verification failed: " + type(exc).__name__, file=sys.stderr)
        return 2
    print("production pin " + ("preflight passed" if args.phase == "preflight" else "matches running healthy images"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
