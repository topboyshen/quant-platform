"""M5.3 AI record import and replay-only LLM entry point."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.research import (
    AiMemory,
    AiMessage,
    AiSession,
    AiTask,
    ImportRun,
    LlmCall,
)
from qp.research.importer import import_ai
from qp.research.llm import (
    LLMLiveDisabledError,
    LLMRequest,
    LLMShadowError,
    complete,
    request_sha256,
)


class FakeReader:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def fetch(self, name: str) -> list[dict]:
        return list(self.tables.get(name, []))


def ai_tables() -> dict[str, list[dict]]:
    now = dt.datetime(2026, 8, 20, 10, 0)
    return {
        "qd_analysis_tasks": [
            {
                "id": 1,
                "user_id": 1,
                "market": "cn",
                "symbol": "510300",
                "model": "kimi-k3",
                "language": "zh",
                "status": "completed",
                "result_json": '{"decision": "buy", "confidence": 72}',
                "error_message": None,
                "created_at": now,
                "completed_at": now,
            }
        ],
        "qd_analysis_memory": [
            {
                "id": 1,
                "user_id": 1,
                "market": "cn",
                "symbol": "510300",
                "decision": "buy",
                "confidence": 72,
                "price_at_analysis": 4.1,
                "summary": "trend",
                "reasons": '["ma20", "volume"]',
                "scores": '{"trend": 8}',
                "indicators_snapshot": '{"ma20": 4.0}',
                "raw_result": '{"ok": true}',
                "created_at": now,
                "updated_at": now,
            }
        ],
        "ai_chat_sessions": [
            {
                "id": "sess-1",
                "user_id": 1,
                "title": "chat",
                "market": "cn",
                "symbol": "510300",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "ai_chat_messages": [
            {
                "id": 1,
                "session_id": "sess-1",
                "role": "user",
                "content": "hi",
                "created_at": now,
            }
        ],
    }


def cleanup(engine) -> None:
    with Session(engine) as session:
        for model in (AiTask, AiMemory, AiSession, AiMessage, LlmCall, ImportRun):
            session.execute(delete(model))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_ai_import_is_idempotent_and_parses_json(engine):
    cleanup(engine)
    try:
        first = import_ai(engine, FakeReader(ai_tables()))
        assert first == {"ai_task": 1, "ai_memory": 1, "ai_session": 1, "ai_message": 1}
        second = import_ai(engine, FakeReader(ai_tables()))
        assert second == first
        assert count(engine, AiTask) == 1 and count(engine, AiMessage) == 1
        with Session(engine) as session:
            task = session.get(AiTask, 1)
            assert task.result_json == {"decision": "buy", "confidence": 72}
            memory = session.get(AiMemory, 1)
            assert memory.reasons == ["ma20", "volume"]
            assert memory.scores == {"trend": 8}
    finally:
        cleanup(engine)


def test_replay_completion_uses_imported_task_and_audits(engine):
    cleanup(engine)
    try:
        import_ai(engine, FakeReader(ai_tables()))
        with Session(engine) as session:
            response = complete(session, LLMRequest(prompt="analyze 510300", metadata={"replay_task_id": 1}))
            session.commit()
            assert response.mode == "replay" and response.status == "replayed"
            assert response.model == "kimi-k3"
            assert '"decision": "buy"' in response.text
            call = session.scalar(select(LlmCall).order_by(LlmCall.id.desc()).limit(1))
            assert call.mode == "replay" and call.shadow is True
            assert call.status == "replayed"
            assert call.request_sha256 == response.request_sha256
    finally:
        cleanup(engine)


def test_missing_replay_result_is_refused_and_audited(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            with pytest.raises(LLMShadowError):
                complete(session, LLMRequest(prompt="x", metadata={"replay_task_id": 999}))
            session.commit()
        assert count(engine, LlmCall) == 1
        with Session(engine) as session:
            call = session.scalar(select(LlmCall))
            assert call.status == "missing_replay" and call.mode == "replay"
    finally:
        cleanup(engine)


def test_live_call_is_refused_and_audited(engine):
    cleanup(engine)
    try:
        with Session(engine) as session:
            with pytest.raises(LLMLiveDisabledError):
                complete(session, LLMRequest(prompt="x"), shadow=False)
            session.commit()
        with Session(engine) as session:
            call = session.scalar(select(LlmCall))
            assert call.mode == "live" and call.shadow is False and call.status == "refused"
    finally:
        cleanup(engine)


def test_request_digest_ignores_routing_metadata():
    a = LLMRequest(prompt="p", system="s", metadata={"replay_task_id": 1})
    b = LLMRequest(prompt="p", system="s", metadata={"replay_task_id": 2, "x": "y"})
    assert request_sha256(a) == request_sha256(b)
    assert request_sha256(a) != request_sha256(LLMRequest(prompt="p2", system="s"))
