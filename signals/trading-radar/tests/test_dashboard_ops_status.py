from __future__ import annotations

import sqlite3
from pathlib import Path

from dashboard import ops_status


def test_service_status_handles_missing_systemctl(monkeypatch):
    monkeypatch.setattr(ops_status.shutil, "which", lambda name: None)

    status = ops_status.get_service_status()

    assert status["available"] is False
    assert status["status"] == "unknown"
    assert "systemctl" in status["reason"]


def test_service_status_uses_first_loaded_candidate(monkeypatch):
    calls: list[str] = []

    def fake_run_command(args, timeout=2.0):
        unit = args[2]
        calls.append(unit)
        if unit == "missing.service":
            return ops_status.CommandResult(args=args, returncode=1, stdout="LoadState=not-found\n", stderr="")
        return ops_status.CommandResult(
            args=args,
            returncode=0,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "MainPID=123\n"
                "MemoryCurrent=1048576\n"
                "CPUUsageNSec=2500000000\n"
                "ActiveEnterTimestamp=Fri 2026-05-29 07:35:51 CST\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(ops_status.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(ops_status, "run_command", fake_run_command)

    status = ops_status.get_service_status(("missing.service", "stock-radar.service"))

    assert calls == ["missing.service", "stock-radar.service"]
    assert status["available"] is True
    assert status["unit"] == "stock-radar.service"
    assert status["status"] == "active"
    assert status["main_pid"] == 123
    assert status["memory"] == "1.0M"


def test_parse_process_table_detects_multi_instances():
    sample = """
3038751 1 3038751 ubuntu 1000 Ssl Fri May 29 07:35:51 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/radar_monitor_v3.py --config /home/ubuntu/trading-radar/config.json
3038752 1 3038752 ubuntu 1000 Ssl Fri May 29 07:36:51 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/radar_monitor_v3.py --config /home/ubuntu/trading-radar/config.json
3038756 1 3038756 ubuntu 1000 Ssl Fri May 29 07:35:51 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/dashboard/app.py
"""

    parsed = ops_status.parse_process_table(sample)

    assert parsed["count"] == 2
    assert parsed["single_instance_ok"] is False
    assert parsed["dashboard"]["count"] == 1
    assert parsed["multi_instance"] is True


def test_parse_process_table_ignores_pytest_monitor_processes():
    sample = """
3155708 1 3133086 ubuntu 1000 Sl Fri May 29 10:33:18 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/radar_monitor_v3.py --config /tmp/pytest-of-ubuntu/pytest-31/test_v3_boot_health_and_target0/config.json
3155943 1 3133086 ubuntu 1000 Sl Fri May 29 10:33:34 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/radar_monitor_v3.py --config /tmp/pytest-of-ubuntu/pytest-32/test_v3_boot_health_and_target0/config.json
3840776 1 3840776 ubuntu 1000 Ssl Thu Jun 4 12:57:03 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/radar_monitor_v3.py --config /home/ubuntu/trading-radar/config.json
3723074 1 3723074 ubuntu 1000 Ssl Thu Jun 4 09:51:24 2026 /home/ubuntu/trading-radar/venv/bin/python /home/ubuntu/trading-radar/dashboard/app.py
"""

    parsed = ops_status.parse_process_table(sample)

    assert parsed["count"] == 1
    assert parsed["single_instance_ok"] is True
    assert parsed["multi_instance"] is False
    assert parsed["instances"][0]["pid"] == 3840776


def test_cooldown_stats_handles_missing_table(tmp_path: Path):
    db_path = tmp_path / "radar_cache_cooldown.db"
    sqlite3.connect(db_path).close()

    stats = ops_status.get_cooldown_stats(tmp_path)

    assert stats is not None
    assert "error" in stats


def test_cooldown_stats_reports_unmounted_database(tmp_path: Path):
    stats = ops_status.get_cooldown_stats(tmp_path)

    assert stats["available"] is False
    assert stats["today_alerts"] is None


def test_container_snapshot_uses_health_when_systemd_and_ps_missing(monkeypatch, tmp_path: Path):
    unavailable = {"available": False, "status": "unknown"}
    monkeypatch.setattr(ops_status, "get_service_status", lambda *a, **k: unavailable.copy())
    monkeypatch.setattr(
        ops_status,
        "get_process_stats",
        lambda: {"error": "not found: ps", "count": 0},
    )
    monkeypatch.setattr(ops_status, "get_disk_usage", lambda base: {"percent": "50%"})
    monkeypatch.setattr(ops_status, "get_cooldown_stats", lambda base: {"available": False})
    monkeypatch.setattr(
        ops_status,
        "get_monitor_health",
        lambda: {"reachable": True, "body": {"uptime_seconds": 3660}},
    )

    snapshot = ops_status.collect_status_snapshot(tmp_path)

    assert snapshot["service"]["unit"] == "docker:radar-monitor"
    assert snapshot["service"]["active"] is True
    assert snapshot["process"]["count"] == 1
    assert snapshot["process"]["single_instance_ok"] is True
    assert snapshot["process"]["uptime"] == "1h 1m"


def test_collect_status_snapshot_isolates_probe_failures(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ops_status, "get_service_status", lambda *a, **k: {"status": "active"})
    monkeypatch.setattr(ops_status, "get_process_stats", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ops_status, "get_disk_usage", lambda base: {"percent": "50%"})
    monkeypatch.setattr(ops_status, "get_cooldown_stats", lambda base: None)
    monkeypatch.setattr(ops_status, "get_monitor_health", lambda: {"reachable": True})

    snapshot = ops_status.collect_status_snapshot(tmp_path)

    assert snapshot["service"]["status"] == "active"
    assert snapshot["process"]["error"] == "boom"
    assert snapshot["partial_failure"] is True
    assert snapshot["errors"]["process"] == "boom"


def test_proxy_environment_reports_ambient_conflict():
    env = {
        "RADAR_ALL_PROXY": "http://127.0.0.1:7890",
        "HTTP_PROXY": "http://127.0.0.1:39817",
    }

    proxy = ops_status.check_proxy_environment(env)

    assert proxy["radar_all_proxy"] == "http://127.0.0.1:7890"
    assert proxy["has_conflict"] is True
    assert proxy["ambient_proxy_vars"] == {"HTTP_PROXY": "http://127.0.0.1:39817"}
