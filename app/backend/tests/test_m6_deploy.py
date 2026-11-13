"""M6.6 deployment package: timer wiring, runbook and env keys."""
from __future__ import annotations

from pathlib import Path

from qp.jobs.registry import JOBS

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
TIMERS = {
    "qp-reports-daily.timer": ("qp-job@reports.import_daily.service", "16:40"),
    "qp-reports-research-digest.timer": ("qp-job@reports.import_research_digest.service", "16:50"),
    "qp-reports-execution-digest.timer": ("qp-job@reports.import_execution_digest.service", "09:00"),
    "qp-reports-profit.timer": ("qp-job@reports.import_profit_summary.service", "15:20"),
    "qp-reports-health.timer": ("qp-job@reports.import_health_summary.service", "15:45"),
    "qp-reports-shadow-deliver.timer": ("qp-job@reports.shadow_deliver.service", "18:30"),
    "qp-reports-receipts.timer": ("qp-job@reports.import_receipts.service", "18:40"),
    "qp-reports-compare.timer": ("qp-job@reports.compare.service", "18:50"),
}


def _read(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text(encoding="utf-8")


def test_report_timers_target_registered_jobs():
    for name, (unit, clock) in TIMERS.items():
        text = _read(name)
        assert f"Unit={unit}" in text, name
        assert clock in text, name
        assert "Asia/Shanghai" in text and "Persistent=false" in text, name
        job = unit.split("@", 1)[1].removesuffix(".service")
        assert job in JOBS and job.startswith("reports."), job


def test_no_report_timer_can_backfill_missed_runs():
    for name in TIMERS:
        assert "Persistent=true" not in _read(name), name


def test_runbook_documents_read_only_sources_and_rollback():
    text = (DEPLOY / "M6-SHADOW-RUNBOOK.md").read_text(encoding="utf-8")
    assert "setfacl" in text and "daily-research-summary" in text
    assert "S1" in text and "S8" in text
    assert "回滚" in text and "reports.compare" in text
    assert "真实 Telegram" in text
    assert "QP_LEGACY_DAILY_DIR" in text


def test_env_example_lists_m6_keys():
    text = (DEPLOY / "qp.env.example").read_text(encoding="utf-8")
    for key in ("QP_LEGACY_DAILY_DIR", "QP_RESEARCH_DIGEST_JSON", "QP_HERMES_ORIGINALS_DIR",
                "QP_HERMES_DELIVERIES_DB", "QP_HERMES_EXECUTIONS_DB", "QP_REPORTS_REPORT_DIR"):
        assert f"{key}=" in text, key
