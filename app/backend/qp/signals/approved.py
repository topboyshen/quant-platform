"""M4.5 approved-signal intake + execution digest (SHADOW).

Inbound mirrors the legacy stdlib service ``deploy/approved-signals/app.py``:

* ``X-Source: dedirock``, ``X-Timestamp`` (epoch seconds), ``X-Signature:
  sha256=<hmac(secret, "<ts>." + raw body)>``, skew <= 300 s;
* required payload fields and the approve/confidence/expiry/side risk gate;
* idempotency by ``payload["idempotency_key"]``; duplicates are not overwritten.

Outbound mirrors ``scripts/export_execution_digest.py``: previous calendar day's
rows (legacy uses the UTC date prefix of ``received_at``), noise filtering, at most
20 items, JSON/Markdown files, and a deterministic body hash.  Shadow mode writes
files and the ``signals.execution_digest`` row with ``push_state='shadowed'`` and
never performs an HTTP POST.  HMAC signing is exposed for the compare harness and
future switch-over, but the job does not send.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.models.base import utcnow
from qp.models.signals import ApprovedIntake, ExecutionDigest

CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc
MAX_SKEW_SEC = 300
REQUIRED_FIELDS = ("idempotency_key", "signal_type", "symbol", "confidence", "expires_at",
                   "provenance", "decision")
ALLOWED_SIDES = (None, "long", "short", "flat")
DIGEST_SCHEMA_VERSION = "0.2"
MAX_DIGEST_ITEMS = 20
NOISE_RE = re.compile(r"(selftest|test:|probe|deploy-test|tls-test|443-test|WireTest)", re.I)


# --------------------------------------------------------------------------- inbound
def parse_expires(raw: Any) -> dt.datetime | None:
    if not raw:
        return None
    try:
        value = str(raw)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def risk_check(payload: dict[str, Any],
               *, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Legacy risk gate; the result and detail string are C8-compared verbatim."""
    missing = [key for key in REQUIRED_FIELDS if key not in payload]
    if missing:
        return False, "missing_fields:" + ",".join(missing)
    if payload.get("decision") != "approve":
        return False, "decision_not_approve"
    if payload.get("review_status") == "pending":
        return False, "still_pending"
    try:
        confidence = float(payload.get("confidence"))
    except Exception:
        return False, "bad_confidence"
    if confidence < 0.5:
        return False, "confidence_below_floor"
    expires = parse_expires(payload.get("expires_at"))
    if expires is None:
        return False, "bad_expires_at"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < (now or dt.datetime.now(UTC)):
        return False, "expired"
    if payload.get("side") not in ALLOWED_SIDES:
        return False, "bad_side"
    return True, "ok"


def load_hmac_secret(path: str | os.PathLike[str]) -> bytes | None:
    """Return the stripped secret, or None when the file is absent/empty."""
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value.encode("utf-8") or None


def expected_signature(secret: bytes, timestamp: str, raw: bytes) -> str:
    digest = hmac.new(secret, f"{timestamp}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def signature_valid(secret: bytes, timestamp: str, raw: bytes, provided: str) -> bool:
    return hmac.compare_digest(expected_signature(secret, timestamp, raw), provided or "")


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    """Deterministic hash shared with the legacy exporter for C8 comparison.

    Legacy SQLite stores the parsed payload, not the wire bytes; hashing the canonical
    JSON is the only representation both sides can reproduce.  The hash still covers
    the full payload (including any token fields) but does not reveal them.
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def payload_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def record_intake(session: Session, *, payload: dict[str, Any], raw: bytes, source: str,
                  received_at: dt.datetime | None = None,
                  now: dt.datetime | None = None) -> dict[str, Any]:
    """Insert once by idempotency key; never overwrite an existing decision."""
    key = str(payload["idempotency_key"])
    passed, detail = risk_check(payload, now=now)
    status = "accepted" if passed else "rejected"
    stmt = (pg_insert(ApprovedIntake)
            .values(idempotency_key=key, source=source, received_at=received_at or utcnow(),
                    status=status, detail=detail,
                    risk_check={"passed": passed, "detail": detail},
                    payload_sha256=canonical_payload_sha256(payload), payload=payload)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(ApprovedIntake.id))
    inserted = session.execute(stmt).first()
    if inserted is None:
        existing = session.execute(select(ApprovedIntake)
                                   .where(ApprovedIntake.idempotency_key == key)).scalar_one()
        return {"duplicate": True, "status": existing.status, "detail": existing.detail,
                "idempotency_key": key}
    return {"duplicate": False, "status": status, "detail": detail, "idempotency_key": key}


def verify_signed_request(*, secret: bytes | None, source: str, timestamp: str,
                          signature: str, raw: bytes,
                          now_epoch: int | None = None,
                          max_skew: int = MAX_SKEW_SEC) -> tuple[int, str]:
    """Return ``(http_status, error_code)``; ``(200, "")`` means the request is valid."""
    if source != "dedirock":
        return 401, "bad_source"
    try:
        ts = int(timestamp)
    except Exception:
        return 401, "bad_timestamp"
    if abs(int(now_epoch if now_epoch is not None else time.time()) - ts) > max_skew:
        return 401, "timestamp_skew"
    if secret is None:
        return 503, "not_configured"
    if not signature_valid(secret, timestamp, raw, signature):
        return 401, "bad_signature"
    return 200, ""


# --------------------------------------------------------------------------- outbound
def _utc_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC)
    return start, start + dt.timedelta(days=1)


def is_noise(key: str, payload: dict[str, Any]) -> bool:
    provenance = payload.get("provenance")
    source = ""
    if isinstance(provenance, dict):
        source = str(provenance.get("source") or "")
    blob = " ".join([key or "", str(provenance or ""), source, str(payload.get("reviewer") or "")])
    return bool(NOISE_RE.search(blob))


def _digest_item(row: ApprovedIntake) -> dict[str, Any]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    received = row.received_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "idempotency_key": row.idempotency_key,
        "received_at": received,
        "status": row.status,
        "detail": row.detail,
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "signal_type": payload.get("signal_type"),
        "confidence": payload.get("confidence"),
    }


def digest_rows(session: Session, digest_date: dt.date) -> list[dict[str, Any]]:
    """Rows for the legacy UTC calendar day, filtered for test noise, in insert order."""
    start, end = _utc_day_bounds(digest_date)
    rows = session.execute(
        select(ApprovedIntake)
        .where(ApprovedIntake.received_at >= start, ApprovedIntake.received_at < end)
        .order_by(ApprovedIntake.id)).scalars()
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if is_noise(row.idempotency_key, payload):
            continue
        items.append(_digest_item(row))
    return items


def _fmt_item(row: dict[str, Any]) -> str:
    side = row.get("side") or "—"
    confidence = row.get("confidence")
    conf = f"{float(confidence):.0%}" if isinstance(confidence, (int, float)) else "—"
    return f"{row.get('symbol') or '未知'} · {side} · {row.get('signal_type') or '—'} · 置信{conf}"


def digest_markdown(day: dt.date, rows: Sequence[dict[str, Any]]) -> str:
    accepted = [row for row in rows if row["status"] == "accepted"]
    rejected = [row for row in rows if row["status"] == "rejected"]
    lines = [
        f"【执行摘要】{day.isoformat()}",
        f"受理 {len(accepted)} 条（风控通过/订单占位 queued_stub，未实盘下单） · "
        f"拒绝 {len(rejected)} 条 · 成交 — · 盈亏 未启用",
    ]
    if accepted:
        lines.append("受理明细（最多5条）：")
        for row in accepted[:5]:
            lines.append(f"- {_fmt_item(row)}")
    if rejected:
        lines.append("拒绝明细（最多5条）：")
        for row in rejected[:5]:
            reason = row.get("detail") or "风控拒绝"
            lines.append(f"- {_fmt_item(row)} （{reason}）")
    if not rows:
        lines.append("昨日无业务受理/拒绝（已过滤测试探针）。")
    return "\n".join(lines) + "\n"


def build_digest_body(rows: Sequence[dict[str, Any]], *, digest_date: dt.date,
                      generated_at: dt.datetime) -> dict[str, Any]:
    accepted = [row for row in rows if row["status"] == "accepted"]
    rejected = [row for row in rows if row["status"] == "rejected"]
    markdown = digest_markdown(digest_date, rows)
    generated = generated_at.astimezone(CST)
    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "report_type": "execution_digest",
        "as_of_day": digest_date.isoformat(),
        "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "source": "quant",
        "summary": {
            "accepted": len(accepted),
            "rejected": len(rejected),
            "fills": 0,
            "pnl": None,
            "pnl_status": "not_enabled",
            "note": "accepted=queued_stub占位；无实盘下单；成交/盈亏未启用",
        },
        "text": markdown,
        "markdown": markdown,
        "items": list(rows[:MAX_DIGEST_ITEMS]),
    }


def canonical_body(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def body_sha256(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_body(body)).hexdigest()


def sign_digest(body: dict[str, Any], secret: bytes, timestamp: str) -> str:
    return expected_signature(secret, timestamp, canonical_body(body))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_digest_files(body: dict[str, Any], output_dir: str | os.PathLike[str]) -> list[str]:
    root = Path(output_dir)
    rendered = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    day = str(body["as_of_day"])
    targets = {
        f"execution_digest_{day}.json": rendered,
        "latest.json": rendered,
        "latest.md": str(body["markdown"]),
    }
    for name, text in targets.items():
        _atomic_write(root / name, text)
    return sorted(targets)


def persist_digest(session: Session, body: dict[str, Any],
                   *, push_state: str = "shadowed") -> None:
    values = {
        "digest_date": dt.date.fromisoformat(str(body["as_of_day"])),
        "item_count": len(body["items"]),
        "accepted": int(body["summary"]["accepted"]),
        "rejected": int(body["summary"]["rejected"]),
        "body": body,
        "sha256": body_sha256(body),
        "push_state": push_state,
    }
    stmt = pg_insert(ExecutionDigest).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["digest_date"],
        set_={key: value for key, value in values.items() if key != "digest_date"})
    session.execute(stmt)


def build_previous_day_digest(session: Session, *, now: dt.datetime | None = None,
                              output_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Generate the previous calendar day's shadow digest; never POSTs."""
    now = now or dt.datetime.now(CST)
    digest_date = now.astimezone(CST).date() - dt.timedelta(days=1)
    rows = digest_rows(session, digest_date)
    body = build_digest_body(rows, digest_date=digest_date, generated_at=now)
    persist_digest(session, body)
    settings = get_settings()
    target = output_dir if output_dir is not None else settings.execution_digest_output_dir
    files = write_digest_files(body, target)
    return {
        "ok": True,
        "digest_date": digest_date.isoformat(),
        "item_count": len(body["items"]),
        "row_count": len(rows),
        "accepted": body["summary"]["accepted"],
        "rejected": body["summary"]["rejected"],
        "sha256": body_sha256(body),
        "push_state": "shadowed",
        "would_post": False,
        "files": files,
        "output_dir": str(target),
    }
