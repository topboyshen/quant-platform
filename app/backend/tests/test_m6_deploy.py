"""M6 report module: timers removed on 2026-11-13, jobs stay manual-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from qp.jobs.registry import JOBS

pytestmark = pytest.mark.assets

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
HISTORICAL_TIMERS = {
    "qp-reports-daily.timer": "reports.import_daily",
    "qp-reports-research-digest.timer": "reports.import_research_digest",
    "qp-reports-execution-digest.timer": "reports.import_execution_digest",
    "qp-reports-profit.timer": "reports.import_profit_summary",
    "qp-reports-health.timer": "reports.import_health_summary",
    "qp-reports-shadow-deliver.timer": "reports.shadow_deliver",
    "qp-reports-receipts.timer": "reports.import_receipts",
    "qp-reports-compare.timer": "reports.compare",
}


def test_report_timers_are_removed_from_repo():
    for name in HISTORICAL_TIMERS:
        assert not (DEPLOY / "systemd" / name).exists(), name
    assert not list((DEPLOY / "systemd").glob("qp-reports-*.timer"))


def test_report_jobs_remain_registered_for_manual_runs():
    for job in HISTORICAL_TIMERS.values():
        assert job in JOBS and job.startswith("reports."), job


def test_runbook_documents_removed_timers_and_manual_operation():
    text = (DEPLOY / "M6-SHADOW-RUNBOOK.md").read_text(encoding="utf-8")
    assert "setfacl" in text and "daily-research-summary" in text
    assert "S1" in text and "S8" in text
    assert "回滚" in text and "qp reports compare" in text
    assert "真实 Telegram" in text
    assert "QP_LEGACY_DAILY_DIR" in text
    assert "已删除" in text and "手工" in text


def test_env_example_lists_m6_keys():
    text = (DEPLOY / "qp.env.example").read_text(encoding="utf-8")
    for key in (
        "QP_LEGACY_DAILY_DIR",
        "QP_RESEARCH_DIGEST_JSON",
        "QP_HERMES_ORIGINALS_DIR",
        "QP_HERMES_DELIVERIES_DB",
        "QP_HERMES_EXECUTIONS_DB",
        "QP_REPORTS_REPORT_DIR",
    ):
        assert f"{key}=" in text, key
