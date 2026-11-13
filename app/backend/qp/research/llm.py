"""M5 LLM abstraction: one entry point, replay-only during the shadow window.

Live provider calls are intentionally **not implemented** in M5.  ``complete``
either replays an already-imported AI task result (``mode=replay``) or refuses;
every attempt is written to ``research.llm_call`` so a shadow run leaves an
audit trail without spending tokens.

The user decision for M5 is: shadow period replays only, no real Kimi calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from qp.models.research import AiTask, LlmCall

DEFAULT_MODEL = "kimi-k3"
DEFAULT_PROVIDER = "kimi"


class LLMError(RuntimeError):
    """Base error for the M5 LLM entry point."""


class LLMShadowError(LLMError):
    """Shadow replay was requested but no imported result is available."""


class LLMLiveDisabledError(LLMError):
    """A live provider call was attempted; M5 only ships replay."""


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    system: str = ""
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    max_tokens: int = 1200
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    mode: str
    status: str
    request_sha256: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_sha256(req: LLMRequest) -> str:
    """Stable digest of the semantic request (routing metadata excluded)."""
    payload = {
        "prompt": req.prompt,
        "system": req.system,
        "provider": req.provider,
        "model": req.model,
        "max_tokens": req.max_tokens,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(raw)


def shadow_enabled() -> bool:
    return os.getenv("QP_LLM_SHADOW", "1").strip() != "0"


def complete(session: Session, req: LLMRequest, *, shadow: bool | None = None) -> LLMResponse:
    """Return a replay response or refuse; never performs network I/O."""
    if shadow is None:
        shadow = shadow_enabled()
    digest = request_sha256(req)
    started = time.monotonic()

    if not shadow:
        err = LLMLiveDisabledError("live LLM calls are not implemented in M5; replay only")
        session.add(LlmCall(request_sha256=digest, provider=req.provider, model=req.model,
                            mode="live", shadow=False, status="refused", error=str(err)))
        session.flush()
        raise err

    task_id = req.metadata.get("replay_task_id")
    task = session.get(AiTask, task_id) if task_id is not None else None
    if task is None or not task.result_json:
        err = LLMShadowError(
            "shadow replay requires metadata.replay_task_id of an imported ai_task "
            "with result_json")
        session.add(LlmCall(request_sha256=digest, provider=req.provider, model=req.model,
                            mode="replay", shadow=True, status="missing_replay", error=str(err)))
        session.flush()
        raise err

    text = json.dumps(task.result_json, ensure_ascii=False, sort_keys=True)
    response = LLMResponse(text=text, provider=task.model or req.provider,
                           model=task.model or req.model, mode="replay", status="replayed",
                           request_sha256=digest)
    session.add(LlmCall(request_sha256=digest, provider=response.provider,
                        model=response.model, mode="replay", shadow=True, status="replayed",
                        response_sha256=_sha256(text), usage={},
                        latency_ms=int((time.monotonic() - started) * 1000)))
    session.flush()
    return response
