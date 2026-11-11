"""
AI Chat / Copilot API routes.

Implements the full session-based copilot interface expected by the frontend:
  GET  /api/ai/chat/sessions          — list sessions
  POST /api/ai/chat/sessions          — create session
  DELETE /api/ai/chat/sessions/<id>   — delete session
  GET  /api/ai/chat/sessions/<id>/messages — session history
  POST /api/ai/chat/message/stream    — streaming chat (SSE)
  POST /api/ai/chat/message           — non-streaming chat (fallback)
  GET  /api/ai/agent/preflight        — preflight check
  POST /api/ai/agent/intent           — intent classification
  GET  /api/ai/skills                 — available AI skills
  GET  /api/ai/memory                 — user memory items
  POST /api/ai/memory                 — save memory item
  DELETE /api/ai/memory/<id>          — delete memory item
  GET  /api/ai/chat/history           — legacy compat
  POST /api/ai/chat/history/save      — legacy compat
"""

import json
import time
import uuid
from datetime import datetime

from flask import Response, g, jsonify, request, stream_with_context

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.utils.auth import login_required
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

ai_chat_blp = Blueprint("ai_chat", __name__)

# ---------------------------------------------------------------------------
# DB helpers — tables are created lazily on first use
# ---------------------------------------------------------------------------

_SCHEMA_CREATED = False


class SessionAccessError(Exception):
    """Raised when a caller tries to use a session owned by another user."""


def _ensure_schema():
    global _SCHEMA_CREATED
    if _SCHEMA_CREATED:
        return
    ddl = """
    CREATE TABLE IF NOT EXISTS ai_chat_sessions (
        id          TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        title       TEXT NOT NULL DEFAULT 'New Chat',
        market      TEXT,
        symbol      TEXT,
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS ai_chat_messages (
        id          SERIAL PRIMARY KEY,
        session_id  TEXT NOT NULL REFERENCES ai_chat_sessions(id) ON DELETE CASCADE,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS ai_user_memory (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        content     TEXT NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_user ON ai_chat_sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_session ON ai_chat_messages(session_id);
    CREATE INDEX IF NOT EXISTS idx_ai_user_memory_user ON ai_user_memory(user_id);
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(ddl)
            conn.commit()
        _SCHEMA_CREATED = True
    except Exception as e:
        logger.warning(f"ai_chat schema init warning: {e}")


def _row_to_dict(cur, row):
    if hasattr(row, "keys") or isinstance(row, dict):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/chat/sessions", methods=["GET"])
@login_required
def list_sessions():
    _ensure_schema()
    user_id = g.user_id
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, market, symbol, created_at, updated_at "
                "FROM ai_chat_sessions WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100",
                (user_id,),
            )
            rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
        return jsonify({"code": 1, "msg": "success", "data": rows})
    except Exception as e:
        logger.error(f"list_sessions error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to list chat sessions", "data": []}), 500


@ai_chat_blp.route("/chat/sessions", methods=["POST"])
@login_required
def create_session():
    _ensure_schema()
    user_id = g.user_id
    data = request.get_json() or {}
    session_id = str(uuid.uuid4())
    title = (data.get("title") or "New Chat")[:200]
    market = data.get("market") or ""
    symbol = data.get("symbol") or ""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ai_chat_sessions(id,user_id,title,market,symbol) VALUES(%s,%s,%s,%s,%s)",
                (session_id, user_id, title, market, symbol),
            )
            conn.commit()
        return jsonify({"code": 1, "msg": "success", "data": {"id": session_id, "title": title}})
    except Exception as e:
        logger.error(f"create_session error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to create chat session", "data": None}), 500


@ai_chat_blp.route("/chat/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_session(session_id):
    _ensure_schema()
    user_id = g.user_id
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM ai_chat_sessions WHERE id=%s AND user_id=%s",
                (session_id, user_id),
            )
            conn.commit()
        return jsonify({"code": 1, "msg": "success", "data": None})
    except Exception as e:
        logger.error(f"delete_session error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to delete chat session", "data": None}), 500


@ai_chat_blp.route("/chat/sessions/<session_id>/messages", methods=["GET"])
@login_required
def get_session_messages(session_id):
    _ensure_schema()
    user_id = g.user_id
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # verify ownership
            cur.execute(
                "SELECT id FROM ai_chat_sessions WHERE id=%s AND user_id=%s",
                (session_id, user_id),
            )
            if not cur.fetchone():
                return jsonify({"code": 0, "msg": "Session not found", "data": []}), 404
            cur.execute(
                "SELECT id, role, content, created_at FROM ai_chat_messages "
                "WHERE session_id=%s ORDER BY id ASC",
                (session_id,),
            )
            rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
        return jsonify({"code": 1, "msg": "success", "data": rows})
    except Exception as e:
        logger.error(f"get_session_messages error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to load chat messages", "data": []}), 500


# ---------------------------------------------------------------------------
# Helpers: session upsert + message persist
# ---------------------------------------------------------------------------

def _upsert_session(session_id, user_id, title, market, symbol):
    """Create or update a session while atomically enforcing ownership."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ai_chat_sessions(id,user_id,title,market,symbol) "
            "VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET "
            "title=EXCLUDED.title, market=EXCLUDED.market, symbol=EXCLUDED.symbol, "
            "updated_at=NOW() "
            "WHERE ai_chat_sessions.user_id=EXCLUDED.user_id "
            "RETURNING id",
            (session_id, user_id, title[:200], market, symbol),
        )
        if cur.fetchone() is None:
            raise SessionAccessError("Session not found")
        conn.commit()


def _persist_message(session_id, user_id, role, content):
    """Persist a message only when the session belongs to the caller."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ai_chat_messages(session_id,role,content) "
            "SELECT id,%s,%s FROM ai_chat_sessions WHERE id=%s AND user_id=%s",
            (role, content, session_id, user_id),
        )
        if cur.rowcount != 1:
            raise SessionAccessError("Session not found")
        conn.commit()


def _build_title(message: str) -> str:
    """Derive a short session title from the first user message."""
    text = message.strip().replace("\n", " ")
    return text[:60] + ("…" if len(text) > 60 else "")


def _build_llm_messages(history, system_prompt, new_message):
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    for h in history[-20:]:  # keep last 20 turns to stay within context
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": new_message})
    return msgs


def _get_history(session_id, user_id):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT m.role, m.content FROM ai_chat_messages AS m "
                "JOIN ai_chat_sessions AS s ON s.id=m.session_id "
                "WHERE m.session_id=%s AND s.user_id=%s ORDER BY m.id ASC",
                (session_id, user_id),
            )
            rows = cur.fetchall()
            if not rows:
                return []
            first = rows[0]
            if hasattr(first, "keys") or isinstance(first, dict):
                return [{"role": r["role"], "content": r["content"]} for r in rows]
            return [{"role": r[0], "content": r[1]} for r in rows]
    except Exception:
        return []


def _system_prompt(context):
    market = (context or {}).get("market", "")
    symbol = (context or {}).get("symbol", "")
    base = (
        "You are QuantDinger AI Copilot, an expert quantitative analyst and trading assistant. "
        "You help users analyze markets, build strategies, interpret indicators, and understand risk. "
        "Always be concise, data-driven, and remind users that your output is for research only, "
        "not investment advice."
    )
    if symbol:
        base += f"\n\nCurrent analysis target: {market}:{symbol}."
    return base


# ---------------------------------------------------------------------------
# Streaming chat endpoint
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/chat/message/stream", methods=["POST"])
@login_required
def chat_message_stream():
    _ensure_schema()
    user_id = g.user_id
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    context = data.get("context") or {}
    market = context.get("market") or ""
    symbol = context.get("symbol") or ""

    if not message:
        return jsonify({"code": 0, "msg": "Missing message"}), 400

    title = _build_title(message)
    try:
        _upsert_session(session_id, user_id, title, market, symbol)
        _persist_message(session_id, user_id, "user", message)
        history = _get_history(session_id, user_id)
    except SessionAccessError:
        return jsonify({"code": 0, "msg": "Session not found"}), 404
    except Exception as e:
        logger.error(f"chat session setup error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to prepare chat session"}), 500

    # Remove the message we just persisted (it's already in history now)
    llm_msgs = _build_llm_messages(history[:-1], _system_prompt(context), message)

    def generate():
        try:
            from app.services.llm import LLMService
            svc = LLMService()

            # Emit session_id first so frontend can bind the session
            yield f"event: meta\ndata: {json.dumps({'session_id': session_id, 'intent': 'chat', 'actions': [], 'agent_usage': None})}\n\n"

            # Call LLM (non-streaming; wrap as single SSE chunk)
            reply = svc.call_llm_api(messages=llm_msgs, temperature=0.7, use_json_mode=False)

            # Stream reply word by word for a natural feel
            words = reply.split(" ")
            chunk_size = 4
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i:i + chunk_size])
                if i + chunk_size < len(words):
                    chunk += " "
                yield f"event: delta\ndata: {json.dumps({'text': chunk})}\n\n"
                time.sleep(0.02)

            # Persist assistant reply
            _persist_message(session_id, user_id, "assistant", reply)

            # Update session title if it was the first message
            try:
                with get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE ai_chat_sessions SET updated_at=NOW() "
                        "WHERE id=%s AND user_id=%s",
                        (session_id, user_id),
                    )
                    conn.commit()
            except Exception:
                pass

            yield f"event: done\ndata: {json.dumps({'session_id': session_id, 'message_id': None, 'intent': 'chat', 'confidence': 90, 'actions': [], 'agent_usage': None, 'memory': []})}\n\n"

        except Exception as e:
            logger.error(f"chat_message_stream error: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'message': 'Chat request failed'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Non-streaming chat (fallback + legacy)
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/chat/message", methods=["POST"])
@login_required
def chat_message():
    _ensure_schema()
    user_id = g.user_id
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    context = data.get("context") or {}
    market = context.get("market") or ""
    symbol = context.get("symbol") or ""

    if not message:
        return jsonify({"code": 0, "msg": "Missing message", "data": None}), 400

    title = _build_title(message)
    try:
        _upsert_session(session_id, user_id, title, market, symbol)
        _persist_message(session_id, user_id, "user", message)
        history = _get_history(session_id, user_id)
    except SessionAccessError:
        return jsonify({"code": 0, "msg": "Session not found", "data": None}), 404
    except Exception as e:
        logger.error(f"chat session setup error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to prepare chat session", "data": None}), 500

    llm_msgs = _build_llm_messages(history[:-1], _system_prompt(context), message)

    try:
        from app.services.llm import LLMService
        svc = LLMService()
        reply = svc.call_llm_api(messages=llm_msgs, temperature=0.7, use_json_mode=False)
        _persist_message(session_id, user_id, "assistant", reply)
        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {"reply": reply, "session_id": session_id},
        })
    except Exception as e:
        logger.error(f"chat_message error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Chat request failed", "data": None}), 500


# ---------------------------------------------------------------------------
# Agent preflight
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/agent/preflight", methods=["GET"])
@login_required
def agent_preflight():
    """Return preflight status — no blockers in personal mode."""
    return jsonify({
        "code": 1,
        "msg": "success",
        "data": {
            "blockers": [],
            "warnings": [],
            "ready": True,
        },
    })


# ---------------------------------------------------------------------------
# Agent intent
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/agent/intent", methods=["POST"])
@login_required
def agent_intent():
    """Classify user intent — return generic chat intent."""
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    return jsonify({
        "code": 1,
        "msg": "success",
        "data": {
            "intent": "chat",
            "message": message,
            "params": {},
        },
    })


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/skills", methods=["GET"])
@login_required
def list_skills():
    """Return built-in AI skill list."""
    skills = [
        {
            "id": "market_analysis",
            "name": "Market Analysis",
            "description": "Analyze price action, indicators, support/resistance, and trend.",
            "icon": "line-chart",
        },
        {
            "id": "strategy_review",
            "name": "Strategy Review",
            "description": "Review and improve trading strategy logic and parameters.",
            "icon": "code",
        },
        {
            "id": "risk_assessment",
            "name": "Risk Assessment",
            "description": "Evaluate position sizing, drawdown risk, and portfolio exposure.",
            "icon": "warning",
        },
        {
            "id": "indicator_explain",
            "name": "Indicator Explanation",
            "description": "Explain technical indicators and their signals.",
            "icon": "bar-chart",
        },
    ]
    return jsonify({"code": 1, "msg": "success", "data": skills})


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/memory", methods=["GET"])
@login_required
def list_memory():
    _ensure_schema()
    user_id = g.user_id
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, content, created_at FROM ai_user_memory "
                "WHERE user_id=%s ORDER BY id DESC LIMIT 50",
                (user_id,),
            )
            rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
        return jsonify({"code": 1, "msg": "success", "data": rows})
    except Exception as e:
        logger.error(f"list_memory error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to list memory", "data": []}), 500


@ai_chat_blp.route("/memory", methods=["POST"])
@login_required
def save_memory():
    _ensure_schema()
    user_id = g.user_id
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"code": 0, "msg": "Missing content"}), 400
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ai_user_memory(user_id, content) VALUES(%s,%s) RETURNING id",
                (user_id, content),
            )
            row = cur.fetchone()
            conn.commit()
        return jsonify({"code": 1, "msg": "success", "data": {"id": row[0]}})
    except Exception as e:
        logger.error(f"save_memory error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to save memory", "data": None}), 500


@ai_chat_blp.route("/memory/<int:memory_id>", methods=["DELETE"])
@login_required
def delete_memory(memory_id):
    _ensure_schema()
    user_id = g.user_id
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM ai_user_memory WHERE id=%s AND user_id=%s",
                (memory_id, user_id),
            )
            conn.commit()
        return jsonify({"code": 1, "msg": "success", "data": None})
    except Exception as e:
        logger.error(f"delete_memory error: {e}", exc_info=True)
        return jsonify({"code": 0, "msg": "Unable to delete memory", "data": None}), 500


# ---------------------------------------------------------------------------
# Legacy compat stubs
# ---------------------------------------------------------------------------

@ai_chat_blp.route("/chat/history", methods=["GET"])
@login_required
def get_chat_history():
    return jsonify({"code": 1, "msg": "success", "data": []})


@ai_chat_blp.route("/chat/history/save", methods=["POST"])
@login_required
def save_chat_history():
    return jsonify({"code": 1, "msg": "success", "data": None})


# openapi-compat: legacy import name
ai_chat_bp = ai_chat_blp
