"""Operational status helpers for the Dashboard.

All probes are best-effort and must be safe in restricted environments where
systemd, /proc, or SQLite files may be unavailable. The Dashboard should show a
structured partial status instead of surfacing command errors to users.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

MONITOR_SERVICE_CANDIDATES = (
    "stock-radar.service",
    "stock-radar",
    "radar-monitor.service",
    "trading-radar.service",
)

DASHBOARD_SERVICE_CANDIDATES = (
    "stock-radar-dashboard.service",
    "radar-dashboard.service",
    "dashboard.service",
    "trading-radar-dashboard.service",
)

SWING_SERVICE_CANDIDATES = (
    "stock-radar-swing-eod.service",
    "stock-radar-swing-eod",
    "radar-swing-eod.service",
    "radar-swing-eod",
    "trading-radar-swing-eod.service",
)

SWING_TIMER_CANDIDATES = (
    "stock-radar-swing-eod.timer",
    "radar-swing-eod.timer",
    "trading-radar-swing-eod.timer",
)

PROCESS_PATTERNS = {
    "monitor": (
        re.compile(r"/(?:opt/trading_radar|home/ubuntu/trading-radar)/radar_monitor_v\d+\.py"),
        re.compile(r"/radar_monitor_v\d+\.py"),
        re.compile(r"/radar_monitor\.py"),
    ),
    "dashboard": (
        re.compile(r"/(?:opt/trading_radar|home/ubuntu/trading-radar)/dashboard/app\.py"),
        re.compile(r"/dashboard/app\.py"),
        re.compile(r"gunicorn.*dashboard\.(?:wsgi|app)"),
    ),
    "swing": (
        re.compile(r"/(?:opt/trading_radar|home/ubuntu/trading-radar)/radar_swing_eod\.py"),
        re.compile(r"/radar_swing_eod\.py"),
    ),
}


@dataclass(frozen=True)
class CommandResult:
    args: Sequence[str]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


def run_command(args: Sequence[str], timeout: float = 2.0) -> CommandResult:
    """Run a small read-only command with bounded time and structured errors."""
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(args=args, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except FileNotFoundError as exc:
        return CommandResult(args=args, returncode=None, stdout="", stderr="", error=f"not found: {exc.filename}")
    except subprocess.TimeoutExpired:
        return CommandResult(args=args, returncode=None, stdout="", stderr="", error=f"timeout after {timeout}s")
    except OSError as exc:
        return CommandResult(args=args, returncode=None, stdout="", stderr="", error=str(exc))


def _parse_systemctl_show(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _format_bytes(value: str) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "N/A"
    if number <= 0:
        return "N/A"
    units = ("B", "K", "M", "G", "T")
    size = float(number)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.1f}{unit}"


def _format_cpu_ns(value: str) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "N/A"
    if number <= 0:
        return "N/A"
    seconds = number / 1_000_000_000
    return f"{seconds:.3f}s"


def _service_from_show(unit: str, values: dict[str, str]) -> dict[str, Any]:
    active_state = values.get("ActiveState") or "unknown"
    sub_state = values.get("SubState") or "unknown"
    main_pid = values.get("MainPID") or "0"
    return {
        "available": True,
        "unit": unit,
        "status": active_state,
        "sub_state": sub_state,
        "active": active_state == "active",
        "main_pid": int(main_pid) if main_pid.isdigit() else 0,
        "memory": _format_bytes(values.get("MemoryCurrent", "")),
        "cpu": _format_cpu_ns(values.get("CPUUsageNSec", "")),
        "uptime": values.get("ActiveEnterTimestamp") or "N/A",
    }


def get_service_status(
    candidates: Iterable[str] = MONITOR_SERVICE_CANDIDATES,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Return systemd service status with robust fallback when systemd is absent."""
    if shutil.which("systemctl") is None:
        return {
            "available": False,
            "status": "unknown",
            "active": False,
            "unit": None,
            "reason": "systemctl not found",
        }

    attempted: list[dict[str, Any]] = []
    last_error = "service not found"
    for unit in candidates:
        show = run_command(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "MemoryCurrent",
                "-p",
                "CPUUsageNSec",
                "-p",
                "ActiveEnterTimestamp",
                "--no-pager",
            ],
            timeout=timeout,
        )
        if show.error:
            last_error = show.error
            attempted.append({"unit": unit, "error": show.error})
            continue
        values = _parse_systemctl_show(show.stdout)
        load_state = values.get("LoadState")
        if show.returncode == 0 and load_state not in {"not-found", ""} and values:
            status = _service_from_show(unit, values)
            status["load_state"] = load_state
            status["attempted"] = attempted
            return status
        message = (show.stderr or show.stdout or f"returncode={show.returncode}").strip()
        last_error = message or last_error
        attempted.append({"unit": unit, "returncode": show.returncode, "message": message[:200]})

    return {
        "available": False,
        "status": "unknown",
        "active": False,
        "unit": None,
        "reason": last_error,
        "attempted": attempted,
    }


def get_timer_status(
    candidates: Iterable[str] = SWING_TIMER_CANDIDATES,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Return systemd timer status, including next scheduled trigger when available."""
    if shutil.which("systemctl") is None:
        return {
            "available": False,
            "status": "unknown",
            "active": False,
            "unit": None,
            "reason": "systemctl not found",
        }

    attempted: list[dict[str, Any]] = []
    last_error = "timer not found"
    for unit in candidates:
        show = run_command(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "NextElapseUSecRealtime",
                "-p",
                "LastTriggerUSec",
                "--no-pager",
            ],
            timeout=timeout,
        )
        if show.error:
            last_error = show.error
            attempted.append({"unit": unit, "error": show.error})
            continue
        values = _parse_systemctl_show(show.stdout)
        load_state = values.get("LoadState")
        if show.returncode == 0 and load_state not in {"not-found", ""} and values:
            active_state = values.get("ActiveState") or "unknown"
            return {
                "available": True,
                "unit": unit,
                "status": active_state,
                "sub_state": values.get("SubState") or "unknown",
                "active": active_state == "active",
                "load_state": load_state,
                "next_elapse": values.get("NextElapseUSecRealtime") or "N/A",
                "last_trigger": values.get("LastTriggerUSec") or "N/A",
                "attempted": attempted,
            }
        message = (show.stderr or show.stdout or f"returncode={show.returncode}").strip()
        last_error = message or last_error
        attempted.append({"unit": unit, "returncode": show.returncode, "message": message[:200]})

    return {
        "available": False,
        "status": "unknown",
        "active": False,
        "unit": None,
        "reason": last_error,
        "attempted": attempted,
    }


def _match_process_role(command: str) -> str | None:
    if _is_non_production_process(command):
        return None
    for role, patterns in PROCESS_PATTERNS.items():
        if any(pattern.search(command) for pattern in patterns):
            return role
    return None


def _is_non_production_process(command: str) -> bool:
    return "/tmp/pytest-of-" in command or "/.pytest_cache/" in command


def parse_process_table(stdout: str) -> dict[str, Any]:
    """Parse ps output produced by get_process_stats."""
    by_role: dict[str, list[dict[str, Any]]] = {"monitor": [], "dashboard": [], "swing": []}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "grep" in line:
            continue
        parts = line.split(maxsplit=9)
        if len(parts) < 10:
            continue
        pid, ppid, pgid, user, uid, stat, weekday, month, day, rest = parts
        rest_parts = rest.split(maxsplit=2)
        if len(rest_parts) < 3:
            continue
        time_text, year, command = rest_parts
        role = _match_process_role(command)
        if role is None:
            continue
        by_role[role].append(
            {
                "pid": int(pid) if pid.isdigit() else pid,
                "ppid": int(ppid) if ppid.isdigit() else ppid,
                "pgid": int(pgid) if pgid.isdigit() else pgid,
                "user": user,
                "uid": int(uid) if uid.isdigit() else uid,
                "stat": stat,
                "started": " ".join((weekday, month, day, time_text, year)),
                "args": command,
            }
        )

    monitor_instances = by_role["monitor"]
    dashboard_instances = by_role["dashboard"]
    swing_instances = by_role["swing"]
    all_instances = monitor_instances + dashboard_instances + swing_instances
    return {
        "pid": monitor_instances[0]["pid"] if monitor_instances else None,
        "count": len(monitor_instances),
        "single_instance_ok": len(monitor_instances) <= 1,
        "instances": monitor_instances,
        "dashboard": {
            "count": len(dashboard_instances),
            "single_instance_ok": len(dashboard_instances) <= 1,
            "instances": dashboard_instances,
        },
        "swing": {
            "count": len(swing_instances),
            "single_instance_ok": len(swing_instances) <= 1,
            "instances": swing_instances,
        },
        "all_instances": all_instances,
        "multi_instance": len(monitor_instances) > 1 or len(dashboard_instances) > 1 or len(swing_instances) > 1,
    }


def get_process_stats(*, timeout: float = 2.0) -> dict[str, Any]:
    result = run_command(
        [
            "ps",
            "-C",
            "python",
            "-C",
            "python3",
            "-o",
            "pid,ppid,pgid,user,uid,stat,lstart,args",
        ],
        timeout=timeout,
    )
    if result.error or result.returncode not in {0, 1}:
        return {
            "pid": None,
            "count": 0,
            "single_instance_ok": False,
            "instances": [],
            "dashboard": {"count": 0, "single_instance_ok": False, "instances": []},
            "swing": {"count": 0, "single_instance_ok": False, "instances": []},
            "all_instances": [],
            "multi_instance": False,
            "error": result.error or (result.stderr.strip() or f"returncode={result.returncode}"),
        }
    parsed = parse_process_table(result.stdout)
    if parsed["instances"]:
        first = parsed["instances"][0]
        parsed.update(
            {
                "cpu_percent": "N/A",
                "mem_percent": "N/A",
                "rss_mb": None,
                "uptime": first.get("started", "unknown"),
            }
        )
    return parsed


def get_disk_usage(base_dir: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(base_dir)
    except OSError as exc:
        return {"available": None, "total": None, "used": None, "percent": None, "error": str(exc)}

    percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    return {
        "total": _format_bytes(str(usage.total)),
        "used": _format_bytes(str(usage.used)),
        "available": _format_bytes(str(usage.free)),
        "percent": f"{percent}%",
    }


def get_cooldown_stats(base_dir: Path) -> dict[str, Any]:
    db_path = base_dir / "radar_cache_cooldown.db"
    if not db_path.exists():
        return {
            "available": False,
            "reason": "cooldown database is not mounted in this container",
            "total_stocks": None,
            "total_alerts": None,
            "today_alerts": None,
        }
    try:
        with sqlite3.connect(str(db_path), timeout=1.0) as conn:
            cursor = conn.execute("SELECT COUNT(*), SUM(alert_count) FROM cooldown")
            row = cursor.fetchone() or (0, 0)
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            cursor = conn.execute("SELECT COUNT(*) FROM cooldown WHERE last_alert >= ?", (today_start,))
            today_alerts = cursor.fetchone()[0]
        return {
            "available": True,
            "total_stocks": row[0] or 0,
            "total_alerts": row[1] or 0,
            "today_alerts": today_alerts,
        }
    except sqlite3.Error as exc:
        return {"error": str(exc), "db_path": str(db_path)}


def get_monitor_health(url: str | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
    url = url or os.environ.get(
        "RADAR_MONITOR_HEALTH_URL", "http://127.0.0.1:8080/health"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "reachable": True,
                "http_status": resp.status,
                "body": json.loads(body) if body else {},
            }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"reachable": False, "error": str(exc)}


def collect_status_snapshot(base_dir: Path) -> dict[str, Any]:
    """Collect a full status snapshot, isolating failures per probe."""
    probes = {
        "service": lambda: get_service_status(),
        "dashboard_service": lambda: get_service_status(DASHBOARD_SERVICE_CANDIDATES),
        "swing_service": lambda: get_service_status(SWING_SERVICE_CANDIDATES),
        "swing_timer": get_timer_status,
        "process": get_process_stats,
        "disk": lambda: get_disk_usage(base_dir),
        "cooldown": lambda: get_cooldown_stats(base_dir),
        "monitor_health": get_monitor_health,
    }
    snapshot: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, probe in probes.items():
        try:
            snapshot[name] = probe()
        except Exception as exc:  # defensive boundary for dashboard availability
            snapshot[name] = {"error": str(exc)}
            errors[name] = str(exc)
    _normalize_container_snapshot(snapshot)
    snapshot["generated_at"] = datetime.now().isoformat()
    snapshot["partial_failure"] = bool(errors) or any(
        isinstance(value, dict) and value.get("error") for value in snapshot.values()
    )
    if errors:
        snapshot["errors"] = errors
    return snapshot


def _normalize_container_snapshot(snapshot: dict[str, Any]) -> None:
    """Replace known container capability gaps with health-backed status.

    Dashboard and Monitor run in separate PID namespaces.  A missing systemctl
    or ps inside Dashboard therefore means "not observable here", not stopped.
    """
    health = snapshot.get("monitor_health") or {}
    monitor_reachable = health.get("reachable") is True
    service = snapshot.get("service") or {}
    if monitor_reachable and service.get("available") is False:
        snapshot["service"] = {
            "available": True,
            "active": True,
            "status": "active",
            "sub_state": "healthy",
            "unit": "docker:radar-monitor",
            "source": "health-endpoint",
            "main_pid": None,
        }

    dashboard_service = snapshot.get("dashboard_service") or {}
    if dashboard_service.get("available") is False:
        snapshot["dashboard_service"] = {
            "available": True,
            "active": True,
            "status": "active",
            "sub_state": "running",
            "unit": "docker:radar-dashboard",
            "source": "self",
            "main_pid": os.getpid(),
        }

    process = snapshot.get("process") or {}
    if monitor_reachable and str(process.get("error", "")).startswith("not found: ps"):
        monitor_body = health.get("body") or {}
        snapshot["process"] = {
            "pid": None,
            "count": 1,
            "single_instance_ok": True,
            "instances": [{"pid": None, "args": "docker:radar-monitor", "source": "health-endpoint"}],
            "dashboard": {
                "count": 1,
                "single_instance_ok": True,
                "instances": [{"pid": os.getpid(), "args": "docker:radar-dashboard", "source": "self"}],
            },
            "swing": {"count": 0, "single_instance_ok": True, "instances": []},
            "all_instances": [],
            "multi_instance": False,
            "cpu_percent": None,
            "mem_percent": None,
            "rss_mb": None,
            "uptime": _format_uptime(monitor_body.get("uptime_seconds")),
            "source": "container-health",
        }


def _format_uptime(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "N/A"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def check_proxy_environment(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    radar_proxy = env.get("RADAR_ALL_PROXY", "")
    ambient = {
        key: env.get(key, "")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        if env.get(key)
    }
    return {
        "radar_all_proxy": radar_proxy,
        "ambient_proxy_vars": ambient,
        "has_conflict": bool(ambient),
        "trust_env": env.get("RADAR_REQUESTS_TRUST_ENV", "0"),
    }
