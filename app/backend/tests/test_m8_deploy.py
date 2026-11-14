"""M8.5 deployment package: timer wiring, runbook and env keys."""
from __future__ import annotations

from pathlib import Path

from qp.jobs.registry import JOBS

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_morning_timer_is_removed_and_job_is_manual():
    assert not (DEPLOY / "systemd" / "qp-reports-morning.timer").exists()
    assert "reports.import_morning_chain" in JOBS


def test_no_report_or_morning_timer_units():
    timers = [p.name for p in (DEPLOY / "systemd").glob("*.timer")]
    assert not [name for name in timers if "report" in name or "morning" in name]
    for name in timers:
        assert "reports." not in name, name


def test_report_jobs_are_manual_only():
    for name, spec in JOBS.items():
        if name.startswith("reports."):
            assert (spec.schedule_hint or "").startswith("manual only"), (name, spec.schedule_hint)


def test_runbook_records_archive_decision():
    text = (DEPLOY / "M8-SHADOW-RUNBOOK.md").read_text(encoding="utf-8")
    assert "已归档（2026-11-14）" in text
    assert "不再恢复" in text
    assert "已作废" in text


def test_runbook_documents_acl_window_switch_and_rollback():
    text = (DEPLOY / "M8-SHADOW-RUNBOOK.md").read_text(encoding="utf-8")
    assert "setfacl" in text and "AStock_AI_System" in text
    assert "QP_MORNING_ASTOCK_ROOT" in text
    assert "5 个交易日" in text
    assert "切换" in text and "回滚" in text
    assert "A1" in text and "A6" in text
    assert "chat_id" in text
    assert "已删除" in text and "手工" in text


def test_env_example_lists_m8_keys():
    text = (DEPLOY / "qp.env.example").read_text(encoding="utf-8")
    for key in ("QP_MORNING_ASTOCK_ROOT", "QP_SIGNAL_BUS_ROOT", "QP_SIGNAL_BUS_STATE"):
        assert f"{key}=" in text, key
