"""M5.5 research project/branch import: structure, revisions, JSON payloads."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.research import (
    Admission,
    AdmissionAudit,
    Branch,
    Draft,
    ImportRun,
    Project,
)
from qp.research.importer import import_projects

NOW = dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.UTC)


class FakeReader:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def fetch(self, name: str) -> list[dict]:
        return list(self.tables.get(name, []))


def project_tables(revision: int = 3, writes: bool = False) -> dict[str, list[dict]]:
    return {
        "qd_research_projects": [
            {
                "id": 1,
                "public_id": "proj-1",
                "user_id": 1,
                "name": "etf-trend",
                "subject": '{"market": "cn", "symbol": "510300"}',
                "goal": "improve",
                "default_branch_id": 1,
                "status": "active",
                "revision": revision,
                "last_opened_branch_id": 1,
                "last_opened_module": "backtest",
                "client_request_id": "req-1",
                "created_at": NOW,
                "updated_at": NOW,
            }
        ],
        "qd_research_branches": [
            {
                "id": 1,
                "public_id": "branch-1",
                "project_id": 1,
                "user_id": 1,
                "name": "main",
                "parent_branch_id": None,
                "head_strategy_id": 3,
                "revision": 2,
                "status": "active",
                "created_at": NOW,
                "updated_at": NOW,
            }
        ],
        "qd_research_admission": [
            {
                "id": 1,
                "writes_enabled": writes,
                "runs_enabled": False,
                "revision": 4,
                "updated_at": NOW,
                "updated_by": 1,
            }
        ],
        "qd_research_admission_audit": [
            {
                "id": 7,
                "changed_at": NOW,
                "actor_user_id": 1,
                "before": '{"writes_enabled": true}',
                "after": '{"writes_enabled": false}',
                "env_ceiling": "shadow",
                "reason": "freeze",
            }
        ],
        "qd_research_drafts": [
            {
                "id": 1,
                "branch_id": 1,
                "user_id": 1,
                "module": "backtest",
                "payload": '{"params": {"ma": 60}}',
                "draft_revision": 2,
                "saved_at": NOW,
                "saved_from": "ui",
            }
        ],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (Project, Branch, Admission, AdmissionAudit, Draft, ImportRun):
            session.execute(delete(model))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_project_import_is_idempotent_and_mirrors_revision(engine):
    cleanup(engine)
    try:
        first = import_projects(engine, FakeReader(project_tables()))
        assert first == {"project": 1, "branch": 1, "admission": 1, "admission_audit": 1, "draft": 1}
        second = import_projects(engine, FakeReader(project_tables(revision=4, writes=True)))
        assert second == first
        assert count(engine, Project) == 1 and count(engine, Branch) == 1
        with Session(engine) as session:
            project = session.get(Project, 1)
            assert project.revision == 4
            assert project.subject == {"market": "cn", "symbol": "510300"}
            assert project.default_branch_id == 1
            branch = session.get(Branch, 1)
            assert branch.project_id == 1 and branch.head_strategy_id == 3
            admission = session.get(Admission, 1)
            assert admission.writes_enabled is True
            audit = session.get(AdmissionAudit, 7)
            assert audit.after == {"writes_enabled": False}
            assert audit.env_ceiling == "shadow"
            draft = session.get(Draft, 1)
            assert draft.payload == {"params": {"ma": 60}}
    finally:
        cleanup(engine)


def test_project_import_records_run(engine):
    cleanup(engine)
    try:
        import_projects(engine, FakeReader(project_tables()))
        with Session(engine) as session:
            run = session.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
            assert run.source == "quantdinger_projects"
            assert run.status == "succeeded"
            assert run.counts["project"] == 1
    finally:
        cleanup(engine)
