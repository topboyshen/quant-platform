"""M5.7 deployment package: timer -> registered job wiring, read-only prerequisites, runbook."""
from __future__ import annotations

from pathlib import Path

from qp.jobs.registry import JOBS

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
TIMERS = {
    "qp-research-ledger.timer": ("qp-job@research.import_ledger.service", "09:40"),
    "qp-research-phase2.timer": ("qp-job@research.import_phase2.service", "16:45"),
    "qp-research-backtests.timer": ("qp-job@research.import_backtests.service", "17:30"),
    "qp-research-ai.timer": ("qp-job@research.import_ai.service", "17:40"),
    "qp-research-profit.timer": ("qp-job@research.import_profit.service", "17:45"),
    "qp-research-projects.timer": ("qp-job@research.import_projects.service", "17:50"),
    "qp-research-compare.timer": ("qp-job@research.compare.service", "18:10"),
}


def _read(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text(encoding="utf-8")


def test_research_timers_target_registered_jobs():
    for name, (unit, clock) in TIMERS.items():
        text = _read(name)
        assert f"Unit={unit}" in text, name
        assert clock in text, name
        assert "Asia/Shanghai" in text and "Persistent=false" in text, name
        job = unit.split("@", 1)[1].removesuffix(".service")
        assert job in JOBS and job.startswith("research."), job


def test_no_research_timer_can_backfill_missed_runs():
    for name in TIMERS:
        assert "Persistent=true" not in _read(name), name


def test_runbook_documents_read_only_prerequisites_and_rollback():
    text = (DEPLOY / "M5-SHADOW-RUNBOOK.md").read_text(encoding="utf-8")
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA ledger TO qp" in text
    assert "setfacl -m u:qp:r" in text
    assert "QP_LLM_SHADOW=1" in text
    assert "SET TRANSACTION READ ONLY" in text
    assert "回滚" in text and "research.compare" in text
    assert "M4" in text and "2026-11-20" in text
    # A secret value must never be embedded in the runbook.
    assert "__PASSWORD__" in text


def test_env_example_lists_m5_keys():
    text = (DEPLOY / "qp.env.example").read_text(encoding="utf-8")
    for key in ("QP_LEDGER_DSN", "QP_PHASE2_DB", "QP_QD_DSN", "QP_LLM_SHADOW",
                "QP_RESEARCH_REPORT_DIR"):
        assert f"{key}=" in text, key
