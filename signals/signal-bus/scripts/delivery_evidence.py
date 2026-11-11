"""Canonical delivery proof contract v1. Pure values in/out; no I/O or sending.

This verifies evidence, NOT permission to retry. A legacy sent/confirmed record
with incomplete proof must still suppress a resend in its owner's state machine.
"""
import datetime as dt
import re

CONTRACT_VERSION = 1
HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
SEND_RECEIPT_KEYS = frozenset(("schemaVersion", "eventId", "targetDate", "route", "payloadSha256",
    "payloadBytes", "sourceSha256", "platformMessageId", "ackKind", "confirmedAt"))
RECEIPT_KEYS = ("messageId", "message_id", "platformMessageId", "platform_message_id")


def valid_hash(value):
    return isinstance(value, str) and bool(HASH_RE.fullmatch(value))


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def valid_instant(value):
    if not isinstance(value, str) or not INSTANT_RE.fullmatch(value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() is not None
    except ValueError:
        return False


def exact_bytes(value):
    return type(value) is int and value >= 0


def verdict(status, reason, text, evidence=None, state=None):
    return {"status": status, "reason": reason, "limitations": [text],
            "state": state or {"pass": "confirmed", "fail": "mismatch", "insufficient": "unconfirmed"}[status],
            "evidence": evidence or {}}


def relay_verdict(record, *, report_date, report_sha256=None, data_date=None,
                  payload_kind="research", skip_sha256=None):
    """Require independent dates, source identity, stored receiver id and equal body hashes."""
    r = record if isinstance(record, dict) else {}
    evidence = {key: r.get(key) for key in ("status", "reason", "target_date", "as_of",
        "report_sha256", "skip_sha256", "receiver_storage", "receiver_content_sha256",
        "payload_sha256", "content_match", "http_status")}
    evidence.update(payload_kind=r.get("payload_kind", "research"), record_id=r.get("receiver_record_id"))

    def result(status, reason, text, state=None):
        return verdict(status, reason, text, evidence, state)

    identity_key = "skip_sha256" if payload_kind == "skip" else "report_sha256"
    identity = skip_sha256 if payload_kind == "skip" else report_sha256
    if not valid_date(report_date) or not valid_hash(identity) or payload_kind not in ("research", "skip"):
        return result("insufficient", "source_identity_unverified", "缺少合法且已绑定的来源报告/休市证明哈希", "unverified")
    if not isinstance(record, dict):
        return result("insufficient", "record_missing", "尚无当日 relay 记录")
    if r.get("status") in ("rejected", "failed", "error", "not_sent"):
        return result("fail", "delivery_failed", f"relay 终态为 {r['status']}", "failed")
    if r.get("target_date") not in (None, "") and r["target_date"] != report_date:
        return result("fail", "target_date_mismatch", f"relay 记录日期 {r['target_date']} 与本次 {report_date} 不一致", "uncertain")
    if r.get("payload_kind", "research") != payload_kind:
        return result("fail", "payload_kind_mismatch", "relay 合同种类与独立来源不一致", "uncertain")
    if r.get("status") == "accepted":
        return result("insufficient", "storage_unconfirmed", "HTTP 已接受，但接收端未确认存储：不能据此判通过")
    # The reason describes HOW proof was acquired, not a weaker kind of proof.
    if r.get("status") != "confirmed" or r.get("reason") not in ("accepted", "confirmed_via_read_only_query"):
        state = "uncertain" if r.get("status") in ("sending", "ambiguous") else "unconfirmed"
        return result("insufficient", "not_confirmed", "relay 终态未确认为 accepted 或只读查询补证", state)
    if r.get("reason") == "confirmed_via_read_only_query" and r.get("confirmed_via") != "read_only_query":
        return result("insufficient", "query_binding_missing", "只读查询补证缺少 confirmed_via 绑定")
    if r.get("receiver_storage") != "receiver_reported_stored":
        return result("insufficient", "storage_unconfirmed", "接收端未自报已存储")
    received, payload = r.get("receiver_content_sha256"), r.get("payload_sha256")
    if not valid_hash(received):
        return result("insufficient", "receiver_hash_missing", "接收端未返回内容哈希；本地 report_sha256 不能自证送达")
    if not valid_hash(payload):
        return result("insufficient", "payload_hash_missing", "接收端返回了内容哈希，但本地没有保存发送字节哈希：无法与同一对象比较")
    if received != payload:
        return result("fail", "payload_hash_mismatch", "接收端内容哈希与本次发送字节不一致（同一对象比较）", "uncertain")
    if r.get("target_date") in (None, ""):
        return result("insufficient", "target_date_missing", "relay 记录缺少 target_date")
    if not valid_date(r.get("as_of")):
        return result("insufficient", "data_date_missing", "relay 的 as_of 缺失或不是有效日期")
    if not isinstance(r.get("receiver_record_id"), str) or not r["receiver_record_id"].strip():
        return result("insufficient", "receiver_id_missing", "接收端记录 ID 缺失")
    if not valid_hash(r.get(identity_key)):
        return result("insufficient", "source_hash_missing", "relay 缺少来源报告哈希")
    if r[identity_key] != identity:
        return result("fail", "source_hash_mismatch", "relay 来源报告哈希与本次密封报告不一致", "uncertain")
    if r.get("content_match") is False:
        return result("fail", "content_match_conflict", "relay 明确记录内容不一致", "uncertain")
    if r.get("content_match") is not True:
        return result("insufficient", "content_match_missing", "relay 未记录内容一致确认", "uncertain")
    if not valid_date(data_date):
        return result("insufficient", "expected_date_missing", "缺少独立且已绑定的期望数据日", "unverified")
    if r["as_of"] > report_date:
        return result("fail", "data_date_future", "relay 数据日晚于报告日期，违反协议顺序", "uncertain")
    if r["as_of"] != data_date:
        return result("fail", "data_date_mismatch", f"relay 数据日 {r['as_of']} 与独立封存的期望数据日 {data_date} 不一致", "uncertain")
    return result("pass", "confirmed", "接收记录 ID、报告日/数据日和存储证据完整，内容哈希与实际发送字节一致；一致性仅针对研究摘要，不代表完整 Markdown 附件送达")


def local_delivery_is_confirmed(record, **expected):
    result = relay_verdict(record, **expected)
    return result["status"] == "pass", result["state"], result["evidence"]


# Statuses that prove a POST may have reached (or did reach) the receiver.
_SUPPRESSING = frozenset(("sending", "ambiguous", "confirmed", "accepted", "failed", "rejected"))


def relay_delivery_decision(record, **expected):
    """The single relay decision shared by every entry point.

    Two independent answers:
    - ``doNotResend``: idempotency hold. Any record that may have reached the
      receiver suppresses a new POST, whatever its proof quality.
    - ``evidenceConfirmed``: only a full ``relay_verdict`` pass (receiver id,
      stored, matching hashes, bound source and dates).
    A record that merely *says* confirmed is never reported as confirmed.
    Ambiguous/sending states never auto-resend; they need manual review.
    """
    proof = relay_verdict(record, **expected)
    status = record.get("status") if isinstance(record, dict) else None
    return {"doNotResend": status in _SUPPRESSING,
            "manualReviewRequired": status in ("sending", "ambiguous"),
            "recordedStatus": status,
            "evidenceConfirmed": proof["status"] == "pass",
            "proof": proof}


def validate_send_receipt(record, date):
    if not isinstance(record, dict):
        return None, "receipt_not_object"
    if set(record) - SEND_RECEIPT_KEYS:
        return None, "receipt_unknown_fields"
    if record.get("schemaVersion") != "1.0" or record.get("eventId") != f"astock-{date}-success-telegram-v1":
        return None, "receipt_event_mismatch"
    if record.get("targetDate") != date or record.get("route") != "telegram":
        return None, "receipt_target_mismatch"
    if record.get("ackKind") != "hermes_cli_json_message_id":
        return None, "receipt_ack_kind_unknown"
    if not isinstance(record.get("platformMessageId"), str) or not re.fullmatch(r"[1-9][0-9]*", record["platformMessageId"]):
        return None, "receipt_message_id_invalid"
    if not valid_hash(record.get("payloadSha256")):
        return None, "receipt_payload_hash_invalid"
    if not exact_bytes(record.get("payloadBytes")):
        return None, "receipt_payload_bytes_invalid"
    if record.get("sourceSha256") is not None and not valid_hash(record["sourceSha256"]):
        return None, "receipt_source_hash_invalid"
    if not valid_instant(record.get("confirmedAt")):
        return None, "receipt_confirmed_at_invalid"
    return record, None


def platform_receipt(record):
    """Legacy embedded receipt is display-only, but explicit failure still blocks."""
    if not isinstance(record, dict):
        return None, "no_record", False
    containers = [record, record.get("receipt"), record.get("platformReceipt")]
    saw_id = False
    receipt = None
    for item in containers:
        if not isinstance(item, dict):
            continue
        if item.get("success") is False or item.get("confirmed") is False or item.get("status") in ("failed", "rejected", "ambiguous"):
            return None, "receipt_success_false", True
        if item.get("skipped") is True:
            return None, "receipt_skipped", False
        value = next((item.get(key) for key in RECEIPT_KEYS if not isinstance(item.get(key), bool)
                      and isinstance(item.get(key), (str, int)) and str(item[key]).strip()), None)
        if value is not None:
            saw_id = True
            if item.get("success") is True:
                receipt = {"messageId": str(value)[:128], "skipped": False, "success": True,
                           "target": item.get("target") if isinstance(item.get("target"), str) else None}
    return (receipt, None, False) if receipt else (None, "receipt_success_missing" if saw_id else "receipt_missing_message_id", False)


def telegram_verdict(sent, receipt, *, date, report_sha256, payload_sha256,
                     payload_bytes, paths_safe=True, receipt_issue=None):
    """Same event/receipt/actual-attachment proof for alert and dashboard.

    Legacy raw-original delivery is accepted only with explicit outcome=success,
    no presentation fields, and payload identity equal to the sealed report.
    Missing outcome never grants confirmation and never authorizes a resend.
    """
    event_id = f"astock-{date}-success-telegram-v1"
    s = sent if isinstance(sent, dict) else {}
    report = s.get("report") if isinstance(s.get("report"), dict) else {}
    legacy, embedded_error, embedded_failed = platform_receipt(s)
    checked, issue = validate_send_receipt(receipt, date)
    issue = receipt_issue or issue
    r = checked or {}
    payload, source = report.get("sha256"), report.get("sourceSha256")
    event_bytes = report.get("bytes") if exact_bytes(report.get("bytes")) else None
    payload_checks = ({"receipt_vs_event": r.get("payloadSha256") == payload,
        "event_vs_file": payload == payload_sha256, "receipt_vs_file": r.get("payloadSha256") == payload_sha256}
        if checked and isinstance(payload, str) else None)
    bytes_checks = ({"receipt_vs_event": r.get("payloadBytes") == event_bytes,
        "event_vs_file": event_bytes == payload_bytes, "receipt_vs_file": r.get("payloadBytes") == payload_bytes}
        if checked and event_bytes is not None else None)
    source_checks = ({"receipt_vs_event": r.get("sourceSha256") == source,
        "event_vs_sealed": source == report_sha256, "receipt_vs_sealed": r.get("sourceSha256") == report_sha256}
        if checked and isinstance(source, str) and isinstance(r.get("sourceSha256"), str) else None)
    evidence = {"outcome": s.get("outcome"), "route": s.get("route"),
        "messageId": r.get("platformMessageId") or (legacy or {}).get("messageId"),
        "receiptSource": "send_receipt" if checked else "embedded" if legacy else None,
        "receiptReason": issue, "platformConfirmedAt": r.get("confirmedAt"),
        "platformPayloadSha256": r.get("payloadSha256"), "platformSourceSha256": r.get("sourceSha256"),
        "eventPayloadSha256": payload, "eventPayloadBytes": event_bytes, "eventSourceSha256": source,
        "recomputedPayloadSha256": payload_sha256, "recomputedPayloadBytes": payload_bytes,
        "sealedReportSha256": report_sha256, "payloadChecks": payload_checks,
        "bytesChecks": bytes_checks, "sourceChecks": source_checks,
        "receiptSuccess": (legacy or {}).get("success"),
        "receiptError": embedded_error if not checked or embedded_failed else None}

    def result(status, reason, text, state=None):
        return verdict(status, reason, text, evidence, state)

    if not valid_date(date) or not valid_hash(report_sha256):
        return result("insufficient", "source_identity_unverified", "密封报告日期或哈希缺失/非法，无法核验送达")
    if not isinstance(sent, dict):
        return result("insufficient", "sent_missing", "尚无当日发送记录；事件名不等价送达", "missing")
    if not paths_safe or s.get("schemaVersion") != "1.0" or s.get("source") != "AStock_AI_System":
        return result("insufficient", "event_invalid", "发送记录类型或证据路径不合法，不能确认送达")
    if s.get("eventId") != event_id or s.get("targetDate") != date or s.get("route") != "telegram":
        return result("fail", "event_identity_mismatch", "发送记录日期或路由与当日研究不一致")
    if s.get("outcome") is None:
        return result("insufficient", "outcome_missing", "发送记录缺少明确 outcome=success，不能确认送达")
    if s.get("outcome") != "success":
        return result("fail", "send_failed", f"本地发送结果为 {s.get('outcome')}")
    if embedded_failed:
        return result("fail", "embedded_failure", f"平台回执明确失败（{embedded_error}）")
    if embedded_error == "receipt_skipped":
        return result("insufficient", "receipt_skipped", "平台记录为 skipped，不能确认实际送达")
    if isinstance(payload, str) and not valid_hash(payload):
        return result("fail", "payload_hash_invalid", "发送事件里的附件哈希不是完整 64 位十六进制")
    if report.get("payloadFile") != f"payloads/{event_id}.md":
        return result("insufficient", "payload_path_invalid", "发送事件的附件路径缺失或不匹配当日附件，不能确认送达")
    if isinstance(receipt, dict):
        receipt_event = receipt.get("eventId")
        event_match = (re.fullmatch(r"astock-(\d{4}-\d{2}-\d{2})-success-telegram-v1", receipt_event)
                       if isinstance(receipt_event, str) else None)
        identity_conflict = (
            event_match is not None and valid_date(event_match.group(1)) and receipt_event != event_id
            or valid_date(receipt.get("targetDate")) and receipt["targetDate"] != date
            or isinstance(receipt.get("route"), str) and bool(receipt["route"].strip()) and receipt["route"] != "telegram")
        if identity_conflict:
            return result("fail", "receipt_identity_mismatch", "平台回执明确指向不同发送事件、日期或路由")
    if not checked or issue:
        return result("insufficient", issue or "receipt_missing", f"发送记录存在、回执缺失（{issue}）：无法证明实际送达")
    if not valid_hash(payload_sha256) or not exact_bytes(payload_bytes):
        return result("insufficient", "attachment_missing", "附件文件缺失，无法重算发送内容哈希")
    if payload_checks is None:
        return result("insufficient", "event_hash_missing", "发送事件缺少附件哈希，无法与回执/文件三方核验")
    if not all(payload_checks.values()):
        return result("fail", "payload_hash_mismatch", "发送附件哈希与回执/事件/文件不一致")
    if bytes_checks is None:
        return result("insufficient", "event_bytes_missing", "发送事件缺少附件字节数，无法与回执/文件三方核验")
    if not all(bytes_checks.values()):
        return result("fail", "payload_bytes_mismatch", "发送附件字节数与回执/事件/文件不一致")
    # A malformed/new presentation may never silently take the raw-original path.
    presented = "presentation" in report or "presentation" in s
    if presented and not isinstance(source, str):
        return result("insufficient", "source_hash_missing", "结果版事件缺少来源报告哈希，无法核验来源绑定")
    if isinstance(source, str) and source_checks is None:
        return result("insufficient", "receipt_source_missing", "结果版事件缺少回执 sourceSha256：无法核验来源绑定")
    if isinstance(source, str) and not all(source_checks.values()):
        return result("fail", "source_hash_mismatch", "来源报告与回执/事件/密封件不一致")
    if source is not None and not isinstance(source, str):
        return result("insufficient", "source_hash_invalid", "来源报告哈希类型非法")
    if source is None and payload != report_sha256:
        return result("fail", "raw_original_mismatch", "完整报告事件的附件哈希与密封报告重算值不一致")
    if source is None and "sourceSha256" in r and r["sourceSha256"] != report_sha256:
        return result("fail", "source_hash_mismatch", "完整报告回执所声明的来源哈希与密封报告不一致")
    return result("pass", "confirmed", "平台回执含消息 ID，发送附件的哈希与字节数（及结果版来源报告）均三方一致；平台回执不代表用户已读")
