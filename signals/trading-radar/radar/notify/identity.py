"""Stable business identity for outgoing notifications.

The idempotency key of the v2 dispatch queue is derived from *what business event* a
message announces (namespace + channel + operation + business id), never from the
message body: the body hash is stored only to detect a changed text for the same event.

Nothing in here is secret: the target conversation is represented by a keyed,
irreversible digest (never the bot token or the plain chat id).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Dict, Iterable, Mapping, Sequence

IDENTITY_VERSION = "v2"


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def first_date(text: Any) -> str:
    """First ``YYYY-MM-DD`` inside ``text`` ('' if none): the business day an identity names."""
    match = _DATE_RE.search(str(text if text is not None else ""))
    return match.group(0) if match else ""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_sha256(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json({"args": list(args), "kwargs": dict(kwargs)}))


def _text(value: Any, limit: int = 160) -> str:
    return str(value if value is not None else "")[:limit]


def business_id(operation: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    """Return the business descriptor (not the body) of one notification operation."""
    args = list(args)
    kwargs = dict(kwargs or {})
    if operation == "send_system_alert":
        # The title already carries the date/plan, e.g. "EOD 日报 2026-10-09".
        return "title=" + _text(args[0] if args else kwargs.get("title"))
    if operation == "send_trend_alert":
        names = ("name", "code", "signal", "period")
        values = [args[i] if i < len(args) else kwargs.get(names[i]) for i in range(4)]
        detail = kwargs.get("detail")
        data_date = detail.get("data_date") if isinstance(detail, dict) else None
        return "trend=" + "|".join(_text(v, 64) for v in values) + "|" + _text(data_date, 32)
    if operation == "send_macro_alert":
        detail = kwargs.get("detail")
        factor = detail.get("factor") if isinstance(detail, dict) else None
        return "macro=" + _text(kwargs.get("level", "info"), 32) + "|" + _text(factor, 64)
    if operation == "send_datasource_alert":
        source = args[0] if args else kwargs.get("source_name")
        return "datasource=" + _text(source, 96) + "|" + _text(kwargs.get("status", "abnormal"), 32)
    # Unknown operation: fall back to a digest of all arguments (exactly-once per content).
    return "args=" + payload_sha256(args, kwargs)


def business_key(namespace: str, channel: str, operation: str, business: str) -> str:
    material = canonical_json(
        {
            "v": IDENTITY_VERSION,
            "namespace": namespace,
            "channel": channel,
            "operation": operation,
            "business": business,
        }
    )
    return f"{IDENTITY_VERSION}:{sha256_hex(material)}"


def target_digest(channel: str, token: str, chat_id: str) -> str:
    """Keyed irreversible digest of the target conversation.

    HMAC key is derived from the bot token, which is never stored; the plain chat id is
    low-entropy so an unkeyed hash would be brute-forceable.  Token rotation changes the
    digest, which fails closed (the dispatch stops for review instead of re-routing).
    """
    key = hashlib.sha256(("quant-notify-target-v1|" + str(token)).encode("utf-8")).digest()
    message = f"{channel}|{chat_id}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def legacy_payload(args: Sequence[Any], kwargs: Mapping[str, Any]) -> Dict[str, Any]:
    return {"args": list(args), "kwargs": dict(kwargs)}


def legacy_key_candidates(
    notification_key: Any, operation: str, scope: str, args: Sequence[Any], kwargs: Mapping[str, Any]
) -> Iterable[str]:
    """Key the pre-v2 DurableOutboxNotifier would have produced for the same call."""
    payload = legacy_payload(args, kwargs)
    key_payload = {"scope": scope, **payload} if scope else payload
    yield notification_key(operation, key_payload)
