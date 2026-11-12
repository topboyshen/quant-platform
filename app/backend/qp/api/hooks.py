"""Inbound approved-signal webhook (M4.5, shadow)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from qp.api.main import get_db
from qp.config import get_settings
from qp.signals.approved import load_hmac_secret, record_intake, verify_signed_request

router = APIRouter()
HOOK_PATH = "/hooks/v1/approved-signals"


def _error(status_code: int, error: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"ok": False, "error": error, **extra})


@router.post(HOOK_PATH)
async def approved_signals(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    raw = await request.body()
    settings = get_settings()
    status, error = verify_signed_request(
        secret=load_hmac_secret(settings.approved_signals_hmac_file),
        source=request.headers.get("x-source", ""),
        timestamp=request.headers.get("x-timestamp", ""),
        signature=request.headers.get("x-signature", ""),
        raw=raw,
        max_skew=settings.approved_signals_max_skew_sec,
    )
    if status != 200:
        return _error(status, error)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return _error(400, "invalid_json")
    if not isinstance(payload, dict):
        return _error(400, "payload_not_object")
    key = payload.get("idempotency_key")
    if not key or not isinstance(key, str):
        return _error(400, "missing_idempotency_key")
    result = record_intake(db, payload=payload, raw=raw, source="dedirock")
    if result["duplicate"]:
        return JSONResponse(status_code=409, content={"ok": True, "duplicate": True, **result})
    if result["status"] != "accepted":
        return JSONResponse(status_code=422, content={
            "ok": False, "status": result["status"], "detail": result["detail"],
            "idempotency_key": result["idempotency_key"]})
    return JSONResponse(status_code=200, content={
        "ok": True, "status": "accepted", "detail": result["detail"],
        "idempotency_key": result["idempotency_key"], "order": "queued_stub"})
