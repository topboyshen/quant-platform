"""Operational guardrails for systemd service templates."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_service(name: str) -> str:
    return (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")


def _read_quant_service(name: str) -> str:
    return (ROOT / "deploy" / "systemd" / "quant-server" / name).read_text(
        encoding="utf-8"
    )


def test_monitor_service_is_local_hardened_and_single_entrypoint():
    text = _read_service("stock-radar.service")
    assert "ExecStart=/opt/trading_radar/.venv/bin/python /opt/trading_radar/radar_monitor_v3.py" in text
    assert "Environment=HEALTH_HOST=127.0.0.1" in text
    assert "Environment=RADAR_ALL_PROXY=http://127.0.0.1:7890" in text
    assert "Environment=HTTP_PROXY=" in text
    assert "Environment=HTTPS_PROXY=" in text
    assert "Environment=ALL_PROXY=" in text
    assert "Environment=NO_PROXY=127.0.0.1,localhost" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "ProtectSystem=full" in text


def test_dashboard_service_uses_gunicorn_local_bind_and_hardening():
    text = _read_service("radar-dashboard.service")
    assert "ExecStart=/opt/trading_radar/.venv/bin/gunicorn" in text
    assert "dashboard.wsgi:app" in text
    assert "Environment=DASHBOARD_HOST=127.0.0.1" in text
    assert "Environment=RADAR_ALL_PROXY=http://127.0.0.1:7890" in text
    assert "Environment=HTTP_PROXY=" in text
    assert "Environment=HTTPS_PROXY=" in text
    assert "Environment=ALL_PROXY=" in text
    assert "Environment=NO_PROXY=127.0.0.1,localhost" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "ProtectSystem=full" in text


def test_ml_serve_service_defaults_to_local_bind():
    text = _read_service("radar-ml-serve.service")
    assert "Environment=ML_SERVE_HOST=127.0.0.1" in text
    assert "Environment=NO_PROXY=127.0.0.1,localhost" in text


def test_dashboard_service_candidate_names_are_covered_by_ops_status():
    from dashboard.ops_status import DASHBOARD_SERVICE_CANDIDATES, MONITOR_SERVICE_CANDIDATES

    assert "stock-radar.service" in MONITOR_SERVICE_CANDIDATES
    assert "stock-radar-dashboard.service" in DASHBOARD_SERVICE_CANDIDATES
    assert "radar-dashboard.service" in DASHBOARD_SERVICE_CANDIDATES


def test_swing_eod_service_and_timer_are_core_deployment_units():
    service = _read_service("radar-swing-eod.service")
    timer = _read_service("radar-swing-eod.timer")

    assert "WorkingDirectory=/opt/trading_radar" in service
    assert "Environment=PYTHONPATH=/opt/trading_radar" in service
    assert "ExecStart=/opt/trading_radar/.venv/bin/python /opt/trading_radar/radar_swing_eod.py --config /opt/trading_radar/config.swing.json" in service
    assert "Environment=HTTP_PROXY=" in service
    assert "Environment=HTTPS_PROXY=" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=full" in service
    assert "OnCalendar=Mon..Fri 15:30" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=0" in timer


def test_swing_watchdog_and_outcome_timers_are_persistent():
    watchdog = _read_service("radar-swing-watchdog.timer")
    outcomes = _read_service("radar-swing-outcomes.timer")

    assert "OnCalendar=Mon..Fri 15:40" in watchdog
    assert "OnCalendar=Mon..Fri 15:50" in watchdog
    assert "Persistent=true" in watchdog
    assert "OnCalendar=Mon..Fri 16:10" in outcomes
    assert "Persistent=true" in outcomes


def test_quant_server_swing_timers_use_live_paths_and_persistent_schedule():
    eod_service = _read_quant_service("stock-radar-swing-eod.service")
    eod_timer = _read_quant_service("stock-radar-swing-eod.timer")
    watchdog = _read_quant_service("stock-radar-swing-watchdog.timer")
    outcomes = _read_quant_service("stock-radar-swing-outcomes.timer")

    assert "WorkingDirectory=/home/ubuntu/trading-radar" in eod_service
    assert "ExecCondition=" in eod_service
    assert "scripts/run_swing_eod.sh" in eod_service
    assert (
        "Environment=RADAR_LOCK_DIR=/home/ubuntu/trading-radar/data/runtime-locks"
        in eod_service
    )
    assert "PrivateTmp=true" in eod_service
    assert "OnCalendar=Mon..Fri 15:30" in eod_timer
    assert "Persistent=true" in eod_timer
    assert "OnCalendar=Mon..Fri 15:40" in watchdog
    assert "OnCalendar=Mon..Fri 15:50" in watchdog
    assert "OnCalendar=Mon..Fri 16:10" in outcomes


def test_quant_total_amount_repair_is_stale_only_and_never_catches_up():
    service = _read_quant_service(
        "stock-radar-swing-total-amount-repair.service"
    )
    timer = _read_quant_service("stock-radar-swing-total-amount-repair.timer")
    wrapper = (ROOT / "scripts" / "run_swing_total_amount_repair.sh").read_text(
        encoding="utf-8"
    )

    assert "User=ubuntu" in service
    assert "ConditionPathExists=/home/ubuntu/trading-radar/data/swing_macro.db" in service
    assert "scripts/run_swing_total_amount_repair.sh" in service
    assert "ReadWritePaths=/home/ubuntu/trading-radar/data" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=full" in service
    assert "PYTHONDONTWRITEBYTECODE=1" in service
    assert (
        "Environment=RADAR_LOCK_DIR=/home/ubuntu/trading-radar/data/runtime-locks"
        in service
    )
    assert "PrivateTmp=true" in service
    assert "TimeoutStartSec=90s" in service
    assert "--kill-after=15s 75s" in wrapper
    assert "OnCalendar=Mon..Fri *-*-* 15:47:00 Asia/Shanghai" in timer
    assert "OnCalendar=Mon..Fri *-*-* 15:57:00 Asia/Shanghai" in timer
    assert "15:45:00" not in timer
    assert "15:55:00" not in timer
    assert timer.count("OnCalendar=") == 2
    assert "Persistent=false" in timer
    assert "RandomizedDelaySec=0" in timer
    assert "Unit=stock-radar-swing-total-amount-repair.service" in timer


def test_quant_swing_wrappers_share_explicit_proxy_contract():
    wrappers = [
        (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "run_swing_eod.sh",
            "run_swing_watchdog.sh",
            "run_swing_outcomes.sh",
            "run_swing_total_amount_repair.sh",
        )
    ]
    for wrapper in wrappers:
        assert 'export RADAR_ALL_PROXY="${RADAR_ALL_PROXY:-http://127.0.0.1:7890}"' in wrapper
        assert "unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy" in wrapper
        assert wrapper.index("export RADAR_ALL_PROXY=") < wrapper.index("unset HTTP_PROXY")


def test_quant_eod_and_repair_wrappers_share_host_visible_lock_directory():
    for name in ("run_swing_eod.sh", "run_swing_total_amount_repair.sh"):
        wrapper = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert (
            'export RADAR_LOCK_DIR="${RADAR_LOCK_DIR:-'
            '/home/ubuntu/trading-radar/data/runtime-locks}"'
            in wrapper
        )


def test_image_retention_timer_is_persistent_and_keeps_two_revisions():
    service = _read_service("radar-image-prune.service")
    timer = _read_service("radar-image-prune.timer")

    assert "prune_radar_images.sh --apply --retain 2" in service
    assert "OnCalendar=Sun *-*-* 03:30" in timer
    assert "Persistent=true" in timer


def test_quant_backup_policy_keeps_automation_retired():
    policy = (ROOT / "deploy" / "QUANT_BACKUP_POLICY.md").read_text(
        encoding="utf-8"
    )

    assert "NO_BACKUP" in policy
    assert "must remain absent" in policy
    for path in (
        ROOT / "deploy" / "systemd" / "quant-server" / "stock-radar-sqlite-backup.service",
        ROOT / "deploy" / "systemd" / "quant-server" / "stock-radar-sqlite-backup.timer",
        ROOT / "scripts" / "backup_sqlite_to_cos.sh",
        ROOT / "scripts" / "sqlite_cos_backup.py",
    ):
        assert not path.exists()


def test_integration_api_uses_minimal_dependency_image():
    dockerfile = (ROOT / "Dockerfile.integration").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-docker-integration.txt").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-compose.integration.override.yml").read_text(
        encoding="utf-8"
    )

    assert "COPY . ." not in dockerfile
    assert "radar/swing/validity.py" in dockerfile
    assert "Dockerfile.integration" in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "Flask" in requirements
    assert "gunicorn" in requirements
    runtime_requirements = "\n".join(
        line for line in requirements.splitlines() if line and not line.startswith("#")
    ).lower()
    for heavyweight in ("pandas", "numpy", "pyarrow", "akshare"):
        assert heavyweight not in runtime_requirements


def test_index_close_cache_timer_retries_stale_cache_without_catchup():
    service = _read_service("research-index-close-cache.service")
    timer = _read_service("research-index-close-cache.timer")
    digest = _read_service("research-digest.service")

    assert "scripts/update_index_close_cache.py --repair-if-stale" in service
    assert "PYTHONDONTWRITEBYTECODE=1" in service
    assert "NoNewPrivileges=true" in service
    assert "OnCalendar=Mon..Fri *-*-* 15:35:00 Asia/Shanghai" in timer
    assert "OnCalendar=Mon..Fri *-*-* 15:45:00 Asia/Shanghai" in timer
    assert "OnCalendar=Mon..Fri *-*-* 15:55:00 Asia/Shanghai" in timer
    assert timer.count("OnCalendar=") == 3
    assert "Persistent=false" in timer
    assert "RandomizedDelaySec=0" in timer
    assert "Unit=research-index-close-cache.service" in timer
    assert "update_index_close_cache.py" not in digest


def test_integration_smoke_overlay_uses_exact_base_and_only_adds_smoke_script():
    dockerfile = (ROOT / "Dockerfile.integration-smoke-overlay").read_text(
        encoding="utf-8"
    )
    assert (
        "FROM sha256:df09103f04b6226f3347a2bb8d6dd2323b99ab11c73b8e3e9b50847e555b7475"
        in dockerfile
    )
    assert "COPY scripts/smoke_integration.py /app/scripts/smoke_integration.py" in dockerfile
    assert "python -m py_compile /app/scripts/smoke_integration.py" in dockerfile
    assert dockerfile.count("COPY ") == 1


def test_swing_service_candidates_are_covered_by_ops_status():
    from dashboard.ops_status import SWING_SERVICE_CANDIDATES, SWING_TIMER_CANDIDATES

    assert "stock-radar-swing-eod.service" in SWING_SERVICE_CANDIDATES
    assert "stock-radar-swing-eod.timer" in SWING_TIMER_CANDIDATES
    assert "radar-swing-eod.service" in SWING_SERVICE_CANDIDATES
    assert "radar-swing-eod.timer" in SWING_TIMER_CANDIDATES
