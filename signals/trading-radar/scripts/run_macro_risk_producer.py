#!/usr/bin/env python3
"""Publish the shared Radar macro-risk state without the legacy monitor stack."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("macro-risk-producer")
ALLOWED_LEVELS = {"ok", "warning", "critical", "growth_blocked"}
DEFAULT_STATE_FILE = "/app/data/radar_risk_state.json"
DEFAULT_OVERRIDE_FILE = "/app/data/radar_risk_override.json"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MAX_STATE_AGE_SECONDS = 900


def _positive_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def state_path() -> Path:
    return Path(os.environ.get("RADAR_RISK_STATE_FILE", DEFAULT_STATE_FILE))


def load_published_state(
    path: Path | None = None,
    *,
    max_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
    now: float | None = None,
) -> tuple[dict[str, Any], float]:
    """Load and strictly validate the state consumed by health and metrics."""
    target = path or state_path()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("risk state must be an object")

    level = str(payload.get("level") or "")
    if level not in ALLOWED_LEVELS:
        raise ValueError(f"unsupported risk level: {level!r}")

    timestamp = float(payload.get("timestamp") or 0)
    current = float(now if now is not None else time.time())
    age = current - timestamp
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("risk timestamp is invalid")
    if age < -5:
        raise ValueError("risk timestamp is in the future")
    if age > max_age_seconds:
        raise ValueError(f"risk state is stale: {age:.1f}s")

    for field in ("vix", "us_10y", "crude_oil"):
        raw = payload.get(field)
        if raw is not None and _positive_number(raw) is None:
            raise ValueError(f"{field} must be a positive finite number or null")

    missing = payload.get("missing_fields") or []
    cached = payload.get("cached_fields") or []
    if not isinstance(missing, list) or not isinstance(cached, list):
        raise ValueError("risk field status must be a list")
    return payload, max(0.0, age)


def prometheus_metrics() -> str:
    try:
        payload, age = load_published_state(
            max_age_seconds=int(
                os.environ.get(
                    "RADAR_RISK_MAX_STATE_AGE_SEC",
                    str(DEFAULT_MAX_STATE_AGE_SECONDS),
                )
            )
        )
        healthy = 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        payload, age, healthy = {}, 0.0, 0

    current_level = str(payload.get("level") or "unavailable")
    lines = [
        "# HELP radar_macro_risk_producer_up Whether the published state is valid and fresh.",
        "# TYPE radar_macro_risk_producer_up gauge",
        f"radar_macro_risk_producer_up {healthy}",
        "# HELP radar_macro_risk_level Current evaluated macro-risk level.",
        "# TYPE radar_macro_risk_level gauge",
    ]
    for level in sorted(ALLOWED_LEVELS):
        lines.append(
            f'radar_macro_risk_level{{level="{level}"}} '
            f'{1 if current_level == level else 0}'
        )

    lines.extend(
        [
            "# HELP radar_macro_risk_value Latest macro-risk input value.",
            "# TYPE radar_macro_risk_value gauge",
        ]
    )
    for field in ("vix", "us_10y", "crude_oil"):
        value = _positive_number(payload.get(field))
        if value is not None:
            lines.append(f'radar_macro_risk_value{{field="{field}"}} {value}')

    timestamp = float(payload.get("timestamp") or 0)
    missing_count = len(payload.get("missing_fields") or [])
    cached_count = len(payload.get("cached_fields") or [])
    lines.extend(
        [
            "# HELP radar_macro_risk_timestamp_seconds Publication timestamp.",
            "# TYPE radar_macro_risk_timestamp_seconds gauge",
            f"radar_macro_risk_timestamp_seconds {timestamp}",
            "# HELP radar_macro_risk_age_seconds Age of the published state.",
            "# TYPE radar_macro_risk_age_seconds gauge",
            f"radar_macro_risk_age_seconds {age}",
            "# HELP radar_macro_risk_missing_fields Number of unavailable inputs.",
            "# TYPE radar_macro_risk_missing_fields gauge",
            f"radar_macro_risk_missing_fields {missing_count}",
            "# HELP radar_macro_risk_cached_fields Number of cached inputs.",
            "# TYPE radar_macro_risk_cached_fields gauge",
            f"radar_macro_risk_cached_fields {cached_count}",
        ]
    )
    return "\n".join(lines) + "\n"


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "RadarMacroRisk/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/live":
            self._send(HTTPStatus.OK, b'{"status":"alive"}\n', "application/json")
            return
        if self.path == "/metrics":
            self._send(
                HTTPStatus.OK,
                prometheus_metrics().encode("utf-8"),
                "text/plain; version=0.0.4; charset=utf-8",
            )
            return
        if self.path not in {"/health", "/ready"}:
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"not_found"}\n', "application/json")
            return
        try:
            payload, age = load_published_state(
                max_age_seconds=int(
                    os.environ.get(
                        "RADAR_RISK_MAX_STATE_AGE_SEC",
                        str(DEFAULT_MAX_STATE_AGE_SECONDS),
                    )
                )
            )
            body = json.dumps(
                {
                    "status": "ok",
                    "level": payload["level"],
                    "age_seconds": round(age, 3),
                    "missing_fields": payload.get("missing_fields") or [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            body = json.dumps(
                {"status": "unhealthy", "reason": str(exc)[:160]},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                body,
                "application/json; charset=utf-8",
            )

    def log_message(self, format_: str, *args: Any) -> None:
        LOGGER.debug("http " + format_, *args)


def publish_once(controller: Any | None = None) -> dict[str, Any]:
    # Import after CLI/env configuration because the risk class resolves its
    # file paths at import time.  The long-running producer reuses one instance
    # so persistent risk states are not mistaken for new warning events.
    if controller is None:
        from radar.risk.macro_risk_control import MacroRiskControl

        controller = MacroRiskControl(LOGGER)
    state = controller.check_with_fetch()
    controller.save_state(state)
    payload = state.to_dict()
    LOGGER.info(
        "published level=%s vix=%s us_10y=%s crude_oil=%s cached=%s missing=%s",
        payload["level"],
        payload.get("vix"),
        payload.get("us_10y"),
        payload.get("crude_oil"),
        payload.get("cached_fields"),
        payload.get("missing_fields"),
    )
    return payload


def run_forever(interval_seconds: int, health_port: int) -> int:
    stop = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s; stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    server = ThreadingHTTPServer(("0.0.0.0", health_port), HealthHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    LOGGER.info("health server listening on 0.0.0.0:%s", health_port)

    from radar.risk.macro_risk_control import MacroRiskControl

    controller = MacroRiskControl(LOGGER)
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                publish_once(controller=controller)
            except Exception:
                LOGGER.exception("macro-risk publication failed")
            remaining = max(1.0, interval_seconds - (time.monotonic() - started))
            stop.wait(remaining)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless Radar macro-risk producer")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="publish once and exit")
    mode.add_argument(
        "--healthcheck", action="store_true", help="validate the current state and exit"
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("RADAR_RISK_STATE_FILE", DEFAULT_STATE_FILE),
    )
    parser.add_argument(
        "--override-file",
        default=os.environ.get("RADAR_RISK_OVERRIDE_FILE", DEFAULT_OVERRIDE_FILE),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(
            os.environ.get("RADAR_RISK_FETCH_INTERVAL_SEC", DEFAULT_INTERVAL_SECONDS)
        ),
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=int(os.environ.get("RADAR_RISK_HEALTH_PORT", "8093")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 60 <= args.interval <= 3600:
        raise SystemExit("--interval must be between 60 and 3600 seconds")
    if not 1 <= args.health_port <= 65535:
        raise SystemExit("--health-port must be between 1 and 65535")

    # The integration API consumes this non-secret market-state file through a
    # read-only bind mount under a remapped container identity.  Preserve the
    # established 0644 cross-container contract.
    os.umask(0o022)
    os.environ["RADAR_RISK_STATE_FILE"] = str(Path(args.state_file))
    os.environ["RADAR_RISK_OVERRIDE_FILE"] = str(Path(args.override_file))
    os.environ["RADAR_RISK_FETCH_INTERVAL_SEC"] = str(args.interval)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.healthcheck:
        try:
            payload, age = load_published_state()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.error("healthcheck failed: %s", exc)
            return 1
        LOGGER.info("healthy level=%s age_seconds=%.3f", payload["level"], age)
        return 0
    if args.once:
        publish_once()
        return 0
    return run_forever(args.interval, args.health_port)


if __name__ == "__main__":
    raise SystemExit(main())
