"""M4.7 deployment package: timer -> registered job wiring and shadow-only invariants."""
from __future__ import annotations

from pathlib import Path

from qp.jobs.registry import JOBS

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
TIMERS = {
    "qp-signals-eod.timer": ("qp-job@signals.eod.service", "15:30"),
    "qp-signals-eod-retry.timer": ("qp-job@signals.eod_retry.service", "15:48"),
    "qp-signals-watchdog.timer": ("qp-job@signals.watchdog.service", "15:40"),
    "qp-signals-repair.timer": ("qp-job@signals.total_amount_repair.service", "15:47"),
    "qp-signals-outcomes.timer": ("qp-job@signals.outcomes.service", "16:10"),
    "qp-signals-digest.timer": ("qp-job@signals.execution_digest.service", "08:35"),
}
DAILY_TIMERS = {"qp-signals-digest.timer"}
EXPORT_TIMERS = {
    "qp-signals-export.timer": ("qp-signals-export@today.service", "16:20"),
    "qp-signals-export-previous.timer": ("qp-signals-export@yesterday.service", "08:45"),
}


def _read(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text(encoding="utf-8")


def test_job_timers_target_registered_shadow_jobs():
    for name, (unit, clock) in TIMERS.items():
        text = _read(name)
        assert f"Unit={unit}" in text, name
        assert clock in text, name
        assert "Asia/Shanghai" in text and "Persistent=false" in text, name
        job = unit.split("@", 1)[1].removesuffix(".service")
        assert job in JOBS and job.startswith("signals."), job


def test_digest_timer_runs_every_day_so_friday_is_covered():
    # Legacy quant-execution-digest.timer is daily. A Mon-Fri digest at 08:35
    # would never produce Friday's digest, because that run happens on Saturday.
    assert "OnCalendar=*-*-* 08:35:00 Asia/Shanghai" in _read("qp-signals-digest.timer")
    for name in TIMERS:
        if name in DAILY_TIMERS:
            continue
        assert "OnCalendar=Mon..Fri *-*-*" in _read(name), name


def test_export_timers_use_relative_dates_and_read_only_service():
    for name, (unit, clock) in EXPORT_TIMERS.items():
        text = _read(name)
        assert f"Unit={unit}" in text, name
        assert clock in text, name
        assert "Persistent=false" in text, name
    service = _read("qp-signals-export@.service")
    assert "ExecStart=" in service and "--date %i" in service
    assert "--out-root /var/lib/qp-inbox/signals-legacy" in service
    assert "User=ubuntu" in service and "Group=qp" in service
    assert "ProtectHome=read-only" in service
    assert "ReadWritePaths=/var/lib/qp-inbox" in service
    assert "ReadWritePaths=/home" not in service


def test_no_shadow_unit_can_send_or_persist_missed_runs():
    for name in list(TIMERS) + list(EXPORT_TIMERS):
        text = _read(name)
        assert "Persistent=true" not in text, name


def test_runbook_keeps_the_m3_gate_and_rollback():
    text = (DEPLOY / "M4-SHADOW-RUNBOOK.md").read_text(encoding="utf-8")
    assert "M3 影子验收 PASS" in text
    assert "signals.eod_retry" in text and "15:48" in text
    assert "/var/lib/qp/shadow-reports" in text
    assert "回滚" in text and "delivery_attempt" in text
    # The runbook must never embed a secret value placeholder that implies one is committed.
    assert "openssl rand -hex 32" in text
