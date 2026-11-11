"""Prospective fixed-scope, append-only notification ownership and receipts.

No message text, recipient, credentials or exception strings are persisted.
An intent without a valid finish is UNKNOWN and continues to own its coverage.
There is no recovery sender, expiry, deletion, upgrade of UNKNOWN or retry API.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path("/var/lib/quant-notify-delivery")
OWNER_UID = 1000
TZ = ZoneInfo("Asia/Shanghai")
LOCK_TIMEOUT = 2.0
MAX_RECORD = 32768
MAX_DAY_BYTES = 4 * 1024 * 1024
MAX_INTENTS = 128
MAX_TEXT = 1024 * 1024
MAX_COVERAGE = 8
SCHEMA = "quant.crosspath-delivery.v1"
JOBS = frozenset({"ec6c86fd856c", "026a1083d9e0"})
IDENTITY_KEYS = frozenset({"accountId", "businessDate", "channel", "targetFingerprint", "scope", "subject"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SUBJECT = re.compile(r"profit:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
_META_HASHES = frozenset({"sourceEnvelopeSha256", "sourceRawSha256", "originalReceiptSha256",
                         "sourceReceiptSha256", "resultSha256", "textSha256", "sourceResultSha256"})
_META_ENUMS = {
    "source": frozenset({"original", "summary"}),
    "reportRole": frozenset({"profit", "health", "summary", "etf", "fault"}),
    "deliveryClass": frozenset({"routine", "operational_warning", "operational_fault", "daily_summary"}),
}


class LedgerError(ValueError):
    """Conservative failure: never authorizes falling back to another sender."""
    before_claim = False

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(ok, code):
    if not ok:
        raise LedgerError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _day(value):
    _need(isinstance(value, str), "business_date_invalid")
    try:
        _need(date.fromisoformat(value).isoformat() == value, "business_date_invalid")
    except ValueError:
        raise LedgerError("business_date_invalid") from None
    return value


def _subject(value):
    if not isinstance(value, str):
        return False
    match = _SUBJECT.fullmatch(value)
    return bool(match and str(uuid.UUID(match[1])) == match[1] and uuid.UUID(match[1]).int)


def _identity(value):
    _need(isinstance(value, dict) and set(value) == IDENTITY_KEYS, "identity_shape")
    _need(value["accountId"] == "1" and value["channel"] == "telegram", "identity_account_channel")
    _day(value["businessDate"])
    _need(isinstance(value["targetFingerprint"], str)
          and bool(_SHA.fullmatch(value["targetFingerprint"])), "identity_target")
    scope, subject = value["scope"], value["subject"]
    _need(scope in ("original", "summary", "fault"), "identity_scope")
    _need((scope == "original" and _subject(subject))
          or (scope == "summary" and subject == "daily_research_summary")
          or (scope == "fault" and isinstance(subject, str)
              and subject.startswith("job:") and subject[4:] in JOBS), "identity_subject")
    return dict(value)


def _coverage(value, identity=None):
    _need(isinstance(value, (list, tuple)) and len(value) <= MAX_COVERAGE,
          "coverage_shape")
    _need(all(_subject(key) for key in value) and len(set(value)) == len(value),
          "coverage_invalid")
    result = sorted(value)
    if identity is not None:
        _need(identity["scope"] != "original" or result == [identity["subject"]],
              "original_coverage_required")
        _need(identity["scope"] != "fault" or not result, "fault_coverage_refused")
    return result


def _metadata(value):
    _need(isinstance(value, dict) and len(value) <= 20, "metadata_shape")
    for key, item in value.items():
        if key in _META_HASHES:
            valid = isinstance(item, str) and bool(_SHA.fullmatch(item))
        elif key in _META_ENUMS:
            valid = isinstance(item, str) and item in _META_ENUMS[key]
        elif key == "jobId":
            valid = item in JOBS if isinstance(item, str) else False
        elif key in ("dispatchId", "invocationId"):
            valid = isinstance(item, str) and bool(_ID.fullmatch(item)) and item != "0" * 32
        elif key == "executionId":
            valid = _subject("profit:" + item) if isinstance(item, str) else False
        elif key == "summaryRevision":
            valid = isinstance(item, str) and bool(re.fullmatch(r"v[1-9][0-9]{0,5}", item))
        elif key == "reasonCode":
            valid = isinstance(item, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item))
        else:
            valid = False
        _need(valid, "metadata_field_refused")
    _need(len(_json(value)) <= 4096, "metadata_budget")
    return dict(value)


def _finite_json(value, depth=0, budget=None):
    budget = [0] if budget is None else budget
    budget[0] += 1
    _need(depth <= 8 and budget[0] <= 1024, "json_structure_budget")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        _need(math.isfinite(value), "nonfinite_json")
    elif isinstance(value, str):
        _need(len(value) <= MAX_RECORD, "json_string_budget")
    elif isinstance(value, list):
        for item in value:
            _finite_json(item, depth + 1, budget)
    elif isinstance(value, dict):
        for key, item in value.items():
            _need(isinstance(key, str), "json_key_type")
            _finite_json(key, depth + 1, budget)
            _finite_json(item, depth + 1, budget)
    else:
        raise LedgerError("non_json_value")


def _strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(LedgerError("nonfinite_json")))
        _finite_json(value)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise LedgerError("record_json_invalid") from None


def _sig(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode),
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _dir_sig(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))


def _parents(path, synthetic):
    for parent in path.parents:
        info = parent.lstat()
        _need(stat.S_ISDIR(info.st_mode), "parent_not_directory")
        if not synthetic:
            _need(info.st_uid == 0 and not info.st_mode & 0o022, "parent_boundary")


def _directory(path, owner):
    info = path.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == owner
          and stat.S_IMODE(info.st_mode) == 0o700, "private_directory_required")
    return info


def _read(path, owner):
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
          and before.st_uid == owner and stat.S_IMODE(before.st_mode) == 0o600
          and before.st_size <= MAX_RECORD, "record_boundary")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _need(_sig(before) == _sig(os.fstat(fd)), "record_changed")
        chunks, count = [], 0
        while count <= MAX_RECORD:
            chunk = os.read(fd, min(4096, MAX_RECORD + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
        raw = b"".join(chunks)
        _need(len(raw) <= MAX_RECORD and _sig(before) == _sig(os.fstat(fd))
              and _sig(before) == _sig(path.lstat()), "record_changed")
        return raw
    finally:
        os.close(fd)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _clock():
    return datetime.now(timezone.utc).isoformat()


def _valid_clock(value):
    _need(isinstance(value, str), "record_clock")
    try:
        parsed = datetime.fromisoformat(value)
        _need(parsed.tzinfo is not None and parsed.utcoffset() is not None, "record_clock")
    except (TypeError, ValueError):
        raise LedgerError("record_clock") from None


def _provider(receipt, expected):
    safe = {"ok": None, "message_id": None, "targetFingerprint": None,
            "http_status": None, "diagnosticsSha256": None, "providerReceiptSha256": None}
    reason = "unconfirmed_provider_response"
    if not isinstance(receipt, dict):
        return "unknown", safe, "invalid_receipt"
    try:
        _finite_json(receipt)
        raw = _json(receipt)
        _need(len(raw) <= MAX_RECORD, "receipt_budget")
        safe["providerReceiptSha256"] = _sha(raw)
        diagnostic = receipt.get("diagnostics")
        if diagnostic is not None:
            diagnostic_raw = _json(diagnostic)
            _need(len(diagnostic_raw) <= 8192, "diagnostic_budget")
            safe["diagnosticsSha256"] = _sha(diagnostic_raw)
    except (LedgerError, TypeError, ValueError, OverflowError, RecursionError):
        return "unknown", safe, "invalid_receipt"
    safe["ok"] = receipt.get("ok") if type(receipt.get("ok")) is bool else None
    message = receipt.get("message_id")
    if type(message) is int and 0 < message < 10 ** 24:
        safe["message_id"] = message
    elif isinstance(message, str) and re.fullmatch(r"[1-9][0-9]{0,23}", message):
        safe["message_id"] = int(message)
    target = receipt.get("targetFingerprint")
    if isinstance(target, str) and _SHA.fullmatch(target):
        safe["targetFingerprint"] = target
    for field in ("http_status", "status_code", "error_code"):
        value = receipt.get(field)
        if type(value) is int and 100 <= value <= 599:
            safe["http_status"] = value
            if value >= 400:
                return "unknown", safe, "provider_5xx" if value >= 500 else "provider_4xx"
    if target != expected:
        reason = "target_mismatch" if target is not None else "missing_target_fingerprint"
    elif safe["ok"] is True and safe["message_id"]:
        return "sent", safe, None
    elif safe["ok"] is True:
        reason = "missing_message_id"
    return "unknown", safe, reason


class Ledger:
    def __init__(self, root=ROOT, *, owner_uid=None):
        self.root = Path(root)
        self.synthetic = self.root != ROOT
        _need(not self.synthetic or owner_uid is not None, "test_root_requires_owner")
        _need(owner_uid is None or type(owner_uid) is int and owner_uid >= 0, "owner_invalid")
        _need(self.synthetic or owner_uid in (None, OWNER_UID), "production_owner_fixed")
        self.owner = OWNER_UID if owner_uid is None else owner_uid
        _need(self.root.is_absolute() and ".." not in self.root.parts, "root_invalid")

    @contextmanager
    def locked(self, business_date=None):
        business_date = _day(business_date or datetime.now(TZ).date().isoformat())
        fd = None
        try:
            _parents(self.root, self.synthetic)
            root_sig = _dir_sig(_directory(self.root, self.owner))
            lock = self.root / "delivery.lock"
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(fd)
            _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == self.owner
                  and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == 0, "lock_boundary")
            os.fsync(fd)
            _fsync_dir(self.root)
            until = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _need(time.monotonic() < until, "lock_timeout")
                    time.sleep(0.01)
            _need(_sig(info) == _sig(lock.lstat()) and root_sig == _dir_sig(_directory(self.root, self.owner)),
                  "lock_changed")
            day = self.root / business_date
            try:
                day.mkdir(mode=0o700)
                _fsync_dir(self.root)
            except FileExistsError:
                pass
            tx = _Transaction(self, day, business_date, root_sig, fd)
            tx._scan()
            yield tx
            tx._anchors()
        except LedgerError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise LedgerError("ledger_io_unavailable") from None
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def deliver(self, identity, text, sender, *, coverage=(), metadata=None):
        try:
            checked = _identity(identity)
            _need(callable(sender), "sender_invalid")
            with self.locked(checked["businessDate"]) as tx:
                claim = tx.begin(checked, text, coverage=coverage, metadata={} if metadata is None else metadata)
        except LedgerError as exc:
            return {"allowed": False, "status": "blocked", "reason": exc.code,
                    "before_claim": False, "attempted": False, "persisted": False}
        if not claim["allowed"]:
            return claim
        try:
            receipt = sender(text)
        except Exception:
            receipt = {"ok": False}
        try:
            with self.locked(checked["businessDate"]) as tx:
                return tx.finish(claim, receipt)
        except LedgerError:
            return {**claim, "allowed": False, "status": "unknown", "reason": "finish_unavailable",
                    "requires_reconciliation": True, "attempted": True, "persisted": False,
                    "before_claim": False}


class _Transaction:
    def __init__(self, ledger, path, business_date, root_sig, lock_fd):
        self.ledger, self.path, self.business_date = ledger, path, business_date
        self.root_sig, self.lock_fd = root_sig, lock_fd
        self.day_sig = _dir_sig(_directory(path, ledger.owner))
        self.intents, self.finishes = {}, {}

    def _anchors(self):
        _parents(self.ledger.root, self.ledger.synthetic)
        _need(self.root_sig == _dir_sig(_directory(self.ledger.root, self.ledger.owner))
              and self.day_sig == _dir_sig(_directory(self.path, self.ledger.owner)), "directory_changed")
        _need(_sig(os.fstat(self.lock_fd)) == _sig((self.ledger.root / "delivery.lock").lstat()),
              "lock_changed")

    def _scan(self):
        self._anchors()
        entries = []
        with os.scandir(self.path) as stream:
            for entry in stream:
                entries.append(entry.name)
                _need(len(entries) <= MAX_INTENTS * 2, "day_entry_budget")
        intents, finishes, total = {}, {}, 0
        for name in sorted(entries):
            match = re.fullmatch(r"([if])-([0-9a-f]{32})\.json", name)
            _need(match is not None, "unexpected_day_entry")
            raw = _read(self.path / name, self.ledger.owner)
            total += len(raw)
            _need(total <= MAX_DAY_BYTES, "day_byte_budget")
            value = _strict(raw)
            claim_id = match[2]
            _need(isinstance(value, dict) and value.get("schema") == SCHEMA
                  and value.get("claimId") == claim_id, "record_identity")
            _valid_clock(value.get("createdAt"))
            if match[1] == "i":
                fields = {"schema", "claimId", "identity", "identitySha256", "bodySha256", "bodyBytes",
                          "coverage", "metadata", "createdAt"}
                _need(set(value) == fields, "intent_shape")
                identity = _identity(value["identity"])
                _need(identity["businessDate"] == self.business_date
                      and value["identitySha256"] == _sha(_json(identity)), "intent_binding")
                _need(isinstance(value["bodySha256"], str) and _SHA.fullmatch(value["bodySha256"])
                      and type(value["bodyBytes"]) is int and 1 <= value["bodyBytes"] <= MAX_TEXT,
                      "intent_body")
                _need(_coverage(value["coverage"], identity) == value["coverage"], "intent_coverage")
                _metadata(value["metadata"])
                intents[claim_id] = (value, _sha(raw))
            else:
                fields = {"schema", "claimId", "intentSha256", "status", "provider", "reason", "createdAt"}
                _need(set(value) == fields and value["status"] in ("sent", "unknown")
                      and isinstance(value["intentSha256"], str) and _SHA.fullmatch(value["intentSha256"]),
                      "finish_shape")
                provider = value["provider"]
                _need(isinstance(provider, dict) and set(provider) == {
                    "ok", "message_id", "targetFingerprint", "http_status", "diagnosticsSha256",
                    "providerReceiptSha256"}, "finish_provider_shape")
                for key in ("diagnosticsSha256", "providerReceiptSha256", "targetFingerprint"):
                    _need(provider[key] is None or isinstance(provider[key], str)
                          and _SHA.fullmatch(provider[key]), "finish_provider_hash")
                _need(provider["ok"] is None or type(provider["ok"]) is bool, "finish_provider_ok")
                _need(provider["message_id"] is None or type(provider["message_id"]) is int
                      and 0 < provider["message_id"] < 10 ** 24, "finish_message_id")
                _need(provider["http_status"] is None or type(provider["http_status"]) is int
                      and 100 <= provider["http_status"] <= 599, "finish_http_status")
                _need(value["reason"] in (None, "invalid_receipt", "provider_5xx", "provider_4xx", "target_mismatch",
                    "missing_target_fingerprint", "missing_message_id", "unconfirmed_provider_response"),
                    "finish_reason")
                finishes[claim_id] = value
        _need(len(intents) <= MAX_INTENTS and set(finishes) <= set(intents), "orphan_finish_or_budget")
        keys, covered = set(), set()
        for claim_id, (intent, digest) in intents.items():
            _need(intent["identitySha256"] not in keys, "duplicate_identity_in_ledger")
            keys.add(intent["identitySha256"])
            for key in intent["coverage"]:
                namespace = self._coverage_namespace(intent["identity"], key)
                _need(namespace not in covered, "duplicate_coverage_in_ledger")
                covered.add(namespace)
            if claim_id in finishes:
                finish = finishes[claim_id]
                _need(finish["intentSha256"] == digest, "finish_intent_mismatch")
                proof = finish["provider"]
                if finish["status"] == "sent":
                    _need(proof["ok"] is True and proof["message_id"] is not None
                          and proof["targetFingerprint"] == intent["identity"]["targetFingerprint"]
                          and proof["providerReceiptSha256"] is not None and finish["reason"] is None
                          and (proof["http_status"] is None or proof["http_status"] < 400),
                          "sent_proof_invalid")
                else:
                    _need(finish["reason"] is not None, "unknown_reason_required")
        self.intents, self.finishes = intents, finishes
        self.day_bytes = total
        self._anchors()

    def _create(self, name, value):
        self._anchors()
        raw = _json(value) + b"\n"
        _need(len(raw) <= MAX_RECORD, "record_budget")
        _need(self.day_bytes + len(raw) <= MAX_DAY_BYTES, "day_byte_budget")
        fd = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            _need(info.st_uid == self.ledger.owner and stat.S_IMODE(info.st_mode) == 0o600
                  and info.st_nlink == 1 and stat.S_ISREG(info.st_mode), "new_record_boundary")
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                _need(written > 0, "record_write_failed")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(self.path)
        self._anchors()
        self.day_bytes += len(raw)
        return _sha(raw)

    def _view(self, claim_id):
        intent, digest = self.intents[claim_id]
        finish = self.finishes.get(claim_id)
        status = finish["status"] if finish else "unknown"
        return {"allowed": False, "status": status, "claim_id": claim_id,
                "business_date": self.business_date, "identity": dict(intent["identity"]),
                "identity_sha256": intent["identitySha256"], "intent_sha256": digest,
                "body_sha256": intent["bodySha256"], "textSha256": intent["bodySha256"],
                "body_bytes": intent["bodyBytes"], "coverage": list(intent["coverage"]),
                "metadata": dict(intent["metadata"]), "provider": dict(finish["provider"]) if finish else None,
                "message_id": finish["provider"]["message_id"] if finish else None,
                "reason": finish["reason"] if finish else "finish_missing",
                "requires_reconciliation": status == "unknown", "persisted": True,
                "attempted": False, "before_claim": False}

    @staticmethod
    def _coverage_namespace(identity, key):
        return tuple(identity[field] for field in ("accountId", "businessDate", "channel", "targetFingerprint")) + (key,)

    def coverage(self, keys, *, identity=None):
        wanted = _coverage(keys)
        checked = _identity(identity) if identity is not None else None
        if checked is not None:
            _need(checked["businessDate"] == self.business_date, "transaction_day_mismatch")
        self._scan()
        result = {key: {"status": "absent", "claim_id": None,
                        "requires_reconciliation": False} for key in wanted}
        for claim_id, (intent, _) in self.intents.items():
            for key in set(wanted).intersection(intent["coverage"]):
                if checked is None or self._coverage_namespace(checked, key) == self._coverage_namespace(intent["identity"], key):
                    _need(result[key]["status"] == "absent", "coverage_identity_required")
                    result[key] = self._view(claim_id)
        return result

    def begin(self, identity, text, *, coverage=(), metadata=None):
        identity = _identity(identity)
        _need(identity["businessDate"] == self.business_date, "transaction_day_mismatch")
        _need(isinstance(text, str) and bool(text.strip()), "text_invalid")
        raw = text.encode("utf-8")
        _need(1 <= len(raw) <= MAX_TEXT, "text_budget")
        covered, metadata = _coverage(coverage, identity), _metadata({} if metadata is None else metadata)
        self._scan()
        key, digest = _sha(_json(identity)), _sha(raw)
        for claim_id, (prior, _) in self.intents.items():
            if prior["identitySha256"] == key:
                view = self._view(claim_id)
                return {**view, "status": "duplicate" if prior["bodySha256"] == digest else "blocked",
                        "duplicate_status": view["status"],
                        "reason": "identity_already_claimed" if prior["bodySha256"] == digest else "identity_conflict"}
        collisions = self.coverage(covered, identity=identity)
        if any(item["status"] != "absent" for item in collisions.values()):
            return {"allowed": False, "status": "blocked", "reason": "coverage_already_claimed",
                    "coverage": collisions, "before_claim": False, "attempted": False, "persisted": True}
        _need(len(self.intents) < MAX_INTENTS, "intent_budget")
        claim_id = uuid.uuid4().hex
        intent = {"schema": SCHEMA, "claimId": claim_id, "identity": identity,
                  "identitySha256": key, "bodySha256": digest, "bodyBytes": len(raw),
                  "coverage": covered, "metadata": metadata, "createdAt": _clock()}
        intent_sha = self._create("i-" + claim_id + ".json", intent)
        self.intents[claim_id] = (intent, intent_sha)
        return {**self._view(claim_id), "allowed": True, "status": "sending", "reason": None}

    def finish(self, claim, provider_receipt):
        _need(isinstance(claim, dict) and claim.get("allowed") is True
              and isinstance(claim.get("claim_id"), str) and _ID.fullmatch(claim["claim_id"]),
              "claim_invalid")
        self._scan()
        claim_id = claim["claim_id"]
        _need(claim_id in self.intents, "claim_missing")
        intent, digest = self.intents[claim_id]
        _need(claim.get("intent_sha256") == digest
              and claim.get("identity") == intent["identity"]
              and claim.get("body_sha256") == intent["bodySha256"], "claim_binding")
        status, provider, reason = _provider(provider_receipt, intent["identity"]["targetFingerprint"])
        value = {"schema": SCHEMA, "claimId": claim_id, "intentSha256": digest,
                 "status": status, "provider": provider, "reason": reason, "createdAt": _clock()}
        if claim_id in self.finishes:
            old = self.finishes[claim_id]
            _need(all(old[key] == value[key] for key in ("status", "provider", "reason")), "finish_conflict")
            return {**self._view(claim_id), "attempted": True}
        self._create("f-" + claim_id + ".json", value)
        self.finishes[claim_id] = value
        return {**self._view(claim_id), "attempted": True}
