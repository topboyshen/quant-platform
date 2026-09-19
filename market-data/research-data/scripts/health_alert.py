"""Bounded status/message/receipt handling for the research-data health alerter.

The shell wrapper must not treat a zero exit code as proof of delivery: the
hermes send command returns a JSON receipt, and only a reported success with a
message id and no skip counts as delivered. Keeping this parsing, the message
formatting and the receipt sanitizing here makes them testable without
subprocesses, and keeps every value the shell interpolates allowlisted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

STATUS_VALUES = ("ok", "warning", "critical", "missing", "unknown")
ALERT_STATUSES = frozenset({"critical", "missing", "unknown"})
UNIT_STATES = ("active", "inactive", "failed", "unknown")
CODE_RE = re.compile(r"[a-z0-9_]{1,64}\Z")
REASON_RE = re.compile(r"[a-z0-9_]{1,64}\Z")
INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
MAX_RECEIPT_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 1200
CST = dt.timezone(dt.timedelta(hours=8))
RECEIPT_SCHEMA_VERSION = 1


def _instant(value):
    if not isinstance(value, str) or not INSTANT_RE.fullmatch(value):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _codes(data):
    codes = []
    if isinstance(data, dict):
        for issue in data.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            code = issue.get("code")
            if isinstance(code, str) and CODE_RE.fullmatch(code) and code not in codes:
                codes.append(code)
            if len(codes) >= 8:
                break
    return codes


def describe(path, now=None, max_age_hours=6.0):
    """Classify the audit artifact without ever raising on bad input."""
    now = now or dt.datetime.now(CST)
    result = {"status": "unknown", "codes": [], "generatedAt": None,
              "stale": True, "ageHours": None, "reason": None}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        result.update(status="missing", reason="health_json_missing")
        return result
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError):
        result.update(status="unknown", reason="health_json_invalid")
        return result
    if not isinstance(data, dict):
        result.update(status="unknown", reason="health_json_not_object")
        return result
    status = data.get("status")
    if not isinstance(status, str) or status not in STATUS_VALUES:
        result.update(status="unknown", reason="health_status_invalid")
    else:
        result["status"] = status
    result["codes"] = _codes(data)
    generated = _instant(data.get("generatedAt"))
    if generated is None:
        result.update(stale=True, reason=result["reason"] or "health_generated_at_invalid")
        return result
    result["generatedAt"] = data["generatedAt"]
    age = (now.astimezone(CST) - generated.astimezone(CST)).total_seconds() / 3600
    result["ageHours"] = round(age, 3)
    # A stale artifact means this alert is not describing the run that just
    # finished; say so instead of presenting old numbers as current.
    result["stale"] = age > float(max_age_hours) or age < -0.5
    if result["stale"] and result["reason"] is None:
        result["reason"] = "health_json_stale"
    return result


def message(info, hostname="unknown", unit_state="unknown", now=None, entry_reason="scheduled_audit"):
    now = now or dt.datetime.now(CST)
    status = info.get("status") if info.get("status") in STATUS_VALUES else "unknown"
    state = unit_state if unit_state in UNIT_STATES else "unknown"
    reason = info.get("reason") if isinstance(info.get("reason"), str) and REASON_RE.fullmatch(info["reason"]) else "none"
    codes = ",".join(info.get("codes") or []) or "none"
    generated = info.get("generatedAt") if _instant(info.get("generatedAt")) else "unknown"
    entry = entry_reason if isinstance(entry_reason, str) and REASON_RE.fullmatch(entry_reason) else "scheduled_audit"
    safe_host = hostname if isinstance(hostname, str) and re.fullmatch(r"[\w.-]{1,64}", hostname) else "unknown"
    text = "\n".join([
        f"research-data 健康告警｜{now.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"状态：{status}",
        f"审计单元：{state}",
        f"触发：{entry}",
        f"问题：{codes}",
        f"审计时间：{generated}",
        f"数据新鲜度：{'陈旧' if info.get('stale') else '当次'}（{reason}）",
        f"主机：{safe_host}",
        "说明：只读审计，不自动恢复；本消息由发送回执确认。",
    ])
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS - 1] + "…"
    return text


def should_alert(info, unit_state):
    """Stale or unreadable audit output alerts regardless of its stored status.

    An old `ok`/`warning` artifact means the audit that should have run did not
    leave a current result; staying silent there is how a broken schedule hides.
    """
    if unit_state == "failed":
        return True
    if info.get("status") in ALERT_STATUSES:
        return True
    return bool(info.get("stale"))


def parse_receipt(raw, exit_code):
    """Return {'delivered': bool, 'reason': code, 'receipt': allowlisted}."""
    receipt = {"schemaVersion": RECEIPT_SCHEMA_VERSION, "hermesExitCode": int(exit_code)}
    if not isinstance(raw, (bytes, bytearray)):
        return {"delivered": False, "reason": "receipt_unreadable", "receipt": receipt}
    if len(raw) > MAX_RECEIPT_BYTES:
        return {"delivered": False, "reason": "receipt_too_large", "receipt": receipt}
    receipt["receiptSha256"] = hashlib.sha256(bytes(raw)).hexdigest()
    if int(exit_code) != 0:
        return {"delivered": False, "reason": "hermes_exit_nonzero", "receipt": receipt}
    try:
        payload = json.loads(bytes(raw).decode("utf-8"),
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError):
        return {"delivered": False, "reason": "receipt_invalid_json", "receipt": receipt}
    if not isinstance(payload, dict):
        return {"delivered": False, "reason": "receipt_not_object", "receipt": receipt}
    receipt["success"] = payload.get("success") is True
    receipt["skipped"] = payload.get("skipped") is True
    if payload.get("success") is not True:
        return {"delivered": False, "reason": "receipt_not_success", "receipt": receipt}
    if payload.get("skipped") is True:
        # A skip is not a delivery.
        return {"delivered": False, "reason": "receipt_skipped", "receipt": receipt}
    message_id = payload.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)) or not str(message_id).strip():
        return {"delivered": False, "reason": "receipt_missing_message_id", "receipt": receipt}
    text = str(message_id)
    receipt["messageId"] = text[:128]
    target = payload.get("target")
    if isinstance(target, str) and 0 < len(target) <= 128 and re.fullmatch(r"[\w:.#@+-]{1,128}", target):
        receipt["target"] = target
    return {"delivered": True, "reason": "delivered", "receipt": receipt}


def write_receipt(path, receipt):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("receipt_path_not_regular_file")
    body = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _shell_lines(info, unit_state):
    status = info.get("status") if info.get("status") in STATUS_VALUES else "unknown"
    codes = ",".join(code for code in (info.get("codes") or []) if CODE_RE.fullmatch(code))
    generated = info.get("generatedAt") if _instant(info.get("generatedAt")) else "unknown"
    reason = info.get("reason") if isinstance(info.get("reason"), str) and REASON_RE.fullmatch(info["reason"]) else "none"
    state = unit_state if unit_state in UNIT_STATES else "unknown"
    lines = [f"STATUS={status}", f"CODES={codes}", f"GENERATED_AT={generated}",
             f"STALE={'yes' if info.get('stale') else 'no'}", f"REASON={reason}",
             f"UNIT_STATE={state}",
             f"ALERT={'yes' if should_alert(info, unit_state) else 'no'}"]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    describe_parser = sub.add_parser("describe")
    describe_parser.add_argument("--json", dest="path", required=True)
    describe_parser.add_argument("--max-age-hours", type=float, default=6.0)
    describe_parser.add_argument("--unit-state", default="unknown")
    message_parser = sub.add_parser("message")
    message_parser.add_argument("--json", dest="path", required=True)
    message_parser.add_argument("--max-age-hours", type=float, default=6.0)
    message_parser.add_argument("--unit-state", default="unknown")
    message_parser.add_argument("--host", default="unknown")
    message_parser.add_argument("--entry-reason", default="scheduled_audit")
    receipt_parser = sub.add_parser("receipt")
    receipt_parser.add_argument("--json-file", required=True)
    receipt_parser.add_argument("--exit-code", type=int, required=True)
    receipt_parser.add_argument("--state-file", required=True)
    args = parser.parse_args(argv)

    if args.command == "describe":
        info = describe(args.path, max_age_hours=args.max_age_hours)
        print("\n".join(_shell_lines(info, args.unit_state)))
        return 0
    if args.command == "message":
        info = describe(args.path, max_age_hours=args.max_age_hours)
        print(message(info, hostname=args.host, unit_state=args.unit_state,
                      entry_reason=args.entry_reason))
        return 0
    try:
        raw = Path(args.json_file).read_bytes()
    except OSError:
        raw = b""
    result = parse_receipt(raw, args.exit_code)
    receipt = {**result["receipt"], "delivered": result["delivered"], "reason": result["reason"],
               "checkedAt": dt.datetime.now(CST).isoformat()}
    try:
        write_receipt(args.state_file, receipt)
        stored = "yes"
    except (OSError, ValueError):
        stored = "no"
    print(f"DELIVERED={'yes' if result['delivered'] else 'no'}")
    print(f"RECEIPT_REASON={result['reason']}")
    print(f"RECEIPT_STORED={stored}")
    return 0 if result["delivered"] and stored == "yes" else 1


if __name__ == "__main__":
    raise SystemExit(main())
