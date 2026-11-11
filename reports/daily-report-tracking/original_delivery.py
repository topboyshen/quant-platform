"""Prospective ownership of successful original notifications only.

The caller has already archived the immutable business output. This module
never launches research or Hermes, and never falls back to a second sender.
The fixed ledger owns the provider attempt before the lazy sender is created.
Nonzero business results keep their existing, explicitly out-of-scope route.
"""
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from zoneinfo import ZoneInfo


ARCHIVE_ROOT = Path("/home/ubuntu/trading-radar/data/daily-research-summary/1/originals")
JOBS = {"profit": "ec6c86fd856c", "health": "026a1083d9e0"}
SCHEDULED_SILENCE = b"[SILENT]\n"
SCHEMA = "quant.original-delivery.v1"
MAX_BYTES = 2 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")
SHA = re.compile(r"[0-9a-f]{64}\Z")
_STAT_FIELDS = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                "st_size", "st_mtime_ns", "st_ctime_ns")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _identity(info):
    return tuple(getattr(info, key) for key in _STAT_FIELDS)


def _strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate_json_key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def _directory(job_id):
    path = ARCHIVE_ROOT / job_id
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("archive_directory_identity")
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("archive_directory_private")
    return path


def _read(path, limit=MAX_BYTES):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.getuid() or before.st_mode & 0o077
            or before.st_size > limit):
        raise ValueError("archive_file_identity")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if _identity(os.fstat(fd)) != _identity(before):
            raise ValueError("archive_open_race")
        chunks, count = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > limit:
                raise ValueError("archive_size_limit")
        if (_identity(os.fstat(fd)) != _identity(before)
                or _identity(path.lstat()) != _identity(before)):
            raise ValueError("archive_read_race")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _exclusive(path, raw):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read(path, max(len(raw), 1)) != raw:
            raise ValueError("managed_archive_conflict")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_size == 0:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("managed_archive_write")
                view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if _read(path, max(len(raw), 1)) != raw:
        raise ValueError("managed_archive_readback")


def _validated(result, mode, day, dispatch_id, control, clock):
    if mode not in JOBS:
        raise ValueError("unsupported_mode")
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("business_day_invalid")
    dispatch = uuid.UUID(dispatch_id).hex
    if dispatch != dispatch_id:
        raise ValueError("dispatch_not_canonical")
    now = clock()
    if (not isinstance(now, datetime) or now.tzinfo is None
            or now.astimezone(TZ).date().isoformat() != day):
        raise ValueError("business_clock_invalid")
    if (not isinstance(control, dict) or str(control.get("accountId")) != "1"
            or not isinstance(control.get("targetFingerprint"), str)
            or not SHA.fullmatch(control["targetFingerprint"])):
        raise ValueError("notification_target_binding_invalid")
    raw = result.get("stdout")
    receipt = result.get("archive")
    if (type(raw) is not bytes or len(raw) > MAX_BYTES or not raw.strip()
            or result.get("archiveVerified") is not True or not isinstance(receipt, dict)
            or receipt.get("mode") != mode or receipt.get("jobId") != JOBS[mode]
            or receipt.get("businessDate") != day or receipt.get("dispatchId") != dispatch
            or type(receipt.get("returnCode")) is not int or receipt["returnCode"] != 0
            or receipt.get("silent") is not False
            or receipt.get("envelopeSha256") != _sha(raw)
            or receipt.get("envelopeBytes") != len(raw)):
        raise ValueError("original_archive_binding_invalid")
    text = raw.decode("utf-8")
    if len(text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("single_provider_message_limit")
    if re.search(r"(?:^|\s)MEDIA:", text):
        raise ValueError("media_not_in_managed_contract")
    directory = _directory(JOBS[mode])
    if receipt.get("schema") == "daily-summary-original-redecision.v1":
        receipt_name = day + "-" + dispatch + ".final-decision.json"
        envelope_name = day + "-" + dispatch + ".final.envelope.raw"
    elif receipt.get("schema") in {"daily-summary-original-envelope.v1", "daily-summary-original-envelope.v2"}:
        receipt_name = dispatch + ".json"
        envelope_name = dispatch + ".envelope.raw"
    else:
        raise ValueError("original_archive_schema_invalid")
    if (receipt.get("originalFile") != dispatch + ".raw"
            or receipt.get("envelopeFile") != envelope_name):
        raise ValueError("original_archive_path_invalid")
    receipt_raw = _read(directory / receipt_name)
    original = _read(directory / (dispatch + ".raw"))
    if (_strict_json(receipt_raw) != receipt
            or _sha(receipt_raw) != result.get("originalReceiptSha256")
            or receipt.get("bodySha256") != _sha(original)
            or receipt.get("bodyBytes") != len(original)
            or _read(directory / envelope_name) != raw):
        raise ValueError("original_archive_readback_invalid")
    return directory, text, now.isoformat(), dispatch, control["targetFingerprint"]


def _binding(result, mode, dispatch, target, day):
    binding = result.get("researchBinding")
    verified = isinstance(binding, dict) and binding.get("verified") is True
    execution = None
    if verified:
        try:
            execution = str(uuid.UUID(binding["executionId"]))
            verified = (execution == binding["executionId"]
                        and isinstance(binding.get("resultSha256"), str)
                        and SHA.fullmatch(binding["resultSha256"]) is not None
                        and binding.get("status") in {"PAPER_CANDIDATES", "CASH", "DATA_BLOCKED"})
        except (KeyError, ValueError, TypeError, AttributeError):
            verified = False
    subject = "profit:" + execution if verified else "job:" + JOBS[mode]
    identity = {"accountId": "1", "businessDate": day, "channel": "telegram",
                "targetFingerprint": target, "scope": "original" if verified else "fault",
                "subject": subject}
    metadata = {"jobId": JOBS[mode], "dispatchId": dispatch, "reportRole": mode,
                "source": "original", "sourceEnvelopeSha256": _sha(result["stdout"]),
                "sourceRawSha256": result["archive"]["bodySha256"],
                "originalReceiptSha256": result["originalReceiptSha256"]}
    if verified:
        metadata.update(executionId=execution, resultSha256=binding["resultSha256"])
    return identity, [subject] if verified else [], metadata


def _refused(result, gate=False):
    answer = dict(result)
    prior_stderr = result.get("stderr", b"")
    if type(prior_stderr) is not bytes:
        prior_stderr = b""
    reason = (b"original_delivery_pre_send_gate_refused\n" if gate
              else b"original_delivery_pre_send_identity_refused\n")
    answer.update(returncode=78, silent=False,
                  stderr=prior_stderr + reason,
                  managedDelivery={"schema": SCHEMA, "status": "blocked",
                                   "providerAttempted": False, "receiptPersisted": False})
    return answer


def _claim(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None


def _duplicate_status(value):
    return value if value in {"sent", "unknown"} else None


def _saved_proof_valid(value):
    if value.get("claimId") != _claim(value.get("claimId")):
        return False
    if value.get("duplicateStatus") != _duplicate_status(value.get("duplicateStatus")):
        return False
    generated = value.get("generatedAt")
    try:
        if not isinstance(generated, str) or datetime.fromisoformat(generated).tzinfo is None:
            return False
    except ValueError:
        return False
    if "diagnosticsSha256" in value and not (isinstance(value["diagnosticsSha256"], str)
                                             and SHA.fullmatch(value["diagnosticsSha256"])):
        return False
    if value.get("status") == "sent":
        return (_claim(value.get("claimId")) is not None
                and type(value.get("messageId")) is int and value["messageId"] > 0)
    return "messageId" not in value


def _gate_refusal(result, common, directory, generated):
    """Surface a real gate failure, never route the original body to a fallback.

    Missing/unreadable ledger state does not prove that an earlier claim is
    absent. This refusal authorizes only Hermes' existing generic failure
    alert; it does not authorize another provider call for the business text.
    """
    answer = _refused(result, gate=True)
    receipt = {key: value for key, value in common.items()
               if key not in {"scheduledOutputSha256", "scheduledOutputFile", "ownership"}}
    receipt.update(generatedAt=generated, status="blocked",
                   decision="generic_gate_fault_only_no_original_retry",
                   ledgerState="unavailable_not_proven_absent", providerBoundaryEntered=False)
    persisted = False
    try:
        _exclusive(directory / (common["dispatchId"] + ".managed-gate-refusal.json"), _encoded(receipt))
        persisted = True
    except (OSError, ValueError, TypeError):
        pass
    answer["managedDelivery"] = {**receipt, "receiptPersisted": persisted}
    return answer


def finalize_original(result, mode, day, dispatch_id, control, ledger, sender_factory, clock):
    """Keep business output immutable; suppress legacy only after taking ownership.

    ``sender_factory`` is lazy: even configuration lookup occurs only inside the
    ledger's durable claim. A ledger/finish exception is uncertain and must not
    escape into Hermes' independent failure-message sender.
    """
    if (not isinstance(result, dict) or type(result.get("returncode")) is not int):
        raise ValueError("original_result_invalid")
    if result["returncode"] != 0 or result.get("silent") is True:
        return result
    try:
        directory, text, generated, dispatch, target = _validated(
            result, mode, day, dispatch_id, control, clock)
        identity, coverage, metadata = _binding(result, mode, dispatch, target, day)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        return _refused(result)
    managed_path = directory / (dispatch + ".managed-delivery.json")
    schedule_path = directory / (dispatch + ".managed.stdout.raw")
    common = {"schema": SCHEMA, "businessDate": day, "jobId": JOBS[mode],
              "dispatchId": dispatch, "reportRole": mode,
              "sourceEnvelopeSha256": metadata["sourceEnvelopeSha256"],
              "sourceRawSha256": metadata["sourceRawSha256"],
              "originalReceiptSha256": metadata["originalReceiptSha256"],
              "textSha256": _sha(text.encode("utf-8")),
              "textBytes": len(text.encode("utf-8")),
              "targetFingerprint": target, "coverage": coverage,
              "scheduledOutputSha256": _sha(SCHEDULED_SILENCE),
              "scheduledOutputFile": schedule_path.name,
              "ownership": "managed_original_sender_not_Hermes_delivery"}
    answer = dict(result)
    answer.update(stdout=SCHEDULED_SILENCE, silent=True)
    provider_boundary_entered = False
    existing_managed = managed_path.exists() or managed_path.is_symlink()
    ownership_persisted = existing_managed
    try:
        if existing_managed:
            old = _strict_json(_read(managed_path, 32768))
            if (not isinstance(old, dict) or any(old.get(k) != v for k, v in common.items())
                    or old.get("status") not in {"sent", "unknown", "blocked", "duplicate"}
                    or set(old) - (set(common) | {"generatedAt", "status", "claimId", "duplicateStatus", "messageId", "diagnosticsSha256"})
                    or not _saved_proof_valid(old)
                    or _read(schedule_path, 64) != SCHEDULED_SILENCE):
                raise ValueError("managed_archive_conflict")
            answer["managedDelivery"] = {**old, "receiptPersisted": True}
            return answer
        def one_attempt(body):
            nonlocal provider_boundary_entered
            provider_boundary_entered = True
            return sender_factory(target)(body)
        outcome = ledger.deliver(identity, text, one_attempt,
                                 coverage=coverage, metadata=metadata)
        if not isinstance(outcome, dict):
            raise ValueError("ledger_result_invalid")
        ownership_persisted = outcome.get("persisted") is True
        status = outcome.get("status")
        if (status == "blocked" and outcome.get("attempted") is False
                and not ownership_persisted and not provider_boundary_entered):
            return _gate_refusal(result, common, directory, generated)
        if status not in {"sent", "unknown", "blocked", "duplicate"}:
            status = "unknown"
        receipt = {**common, "generatedAt": generated, "status": status,
                   "claimId": _claim(outcome.get("claim_id")),
                   "duplicateStatus": _duplicate_status(outcome.get("duplicate_status"))}
        message = outcome.get("message_id")
        if status == "sent":
            if type(message) is not int or message <= 0 or receipt["claimId"] is None:
                receipt["status"] = "unknown"
            else:
                receipt["messageId"] = message
        # The ledger owns safe detailed transport diagnostics. This receipt
        # stores only a digest, not arbitrary nested values from a transport.
        if isinstance(outcome.get("diagnostics"), dict):
            receipt["diagnosticsSha256"] = _sha(_encoded(outcome["diagnostics"]))
        _exclusive(schedule_path, SCHEDULED_SILENCE)
        _exclusive(managed_path, _encoded(receipt))
        answer["managedDelivery"] = {**receipt, "receiptPersisted": True}
    except BaseException:
        # Any boundary uncertainty may include an already-issued provider
        # request. Retain the ledger intent and never trigger legacy fallback.
        if not provider_boundary_entered and not ownership_persisted:
            return _gate_refusal(result, common, directory, generated)
        answer["managedDelivery"] = {**common, "status": "unknown",
                                     "receiptPersisted": False,
                                     "reason": "managed_receipt_or_ledger_unavailable"}
    return answer
