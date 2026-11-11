"""Preserve original cron output; silence only verified routine completions.

This wrapper launches one byte-pinned existing command. It never generates a
report itself, updates a handoff, writes a business table, retries a command,
or edits Hermes' scheduler. The evidence loader is bounded and read-only.

Raw stdout is archived separately with exclusive creation before a SILENT
suffix may be emitted. Unknown evidence keeps the original return code and
stdout. stderr is passed through for Hermes' existing redaction/failure path;
it is never archived or included in the wrapper's diagnostics.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

from summary_availability import MAINTENANCE_SUFFIX, checked_probe, probe


TZ = ZoneInfo("Asia/Shanghai")
ARCHIVE_ROOT = Path("/home/ubuntu/trading-radar/data/daily-research-summary/1/originals")
HERMES_LEDGER = Path("/home/ubuntu/.hermes/cron/executions.db")
CONTROL = Path("/home/ubuntu/releases/daily-research-summary-20261012.1/control.json")
SILENT_SUFFIX = b"\n[SILENT]\n"
STANDARD_DISCLAIMER = "⚠️ 仅研究和模拟，不下单；AI评分不是上涨概率。"
MAX_PROOF_BYTES = 2 * 1024 * 1024
SPECS = {
    "profit": {
        "job_id": "ec6c86fd856c",
        "script": "/home/ubuntu/.hermes/scripts/quantdinger_profit_research.sh",
        "sha256": "bee100680fc8d1c220b3662bec79f6865a1598babf0497919d6480df2fe909a3",
        "interpreter": "/bin/bash", "timeout": 960,
    },
    "health": {
        "job_id": "026a1083d9e0",
        "script": "/home/ubuntu/.hermes/scripts/profit_radar_health.py",
        "sha256": "f627ec515d44b2b68a8d16bde7696f53630680ba8228d795886daecde29ccdd9",
        # Hermes invokes this wrapper with its Python; keep the same runtime.
        "interpreter": sys.executable, "timeout": 90,
    },
}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MARKER = re.compile(r"<!--\s*profit-execution-evidence:(.*?)\s*-->", re.S)
_FAILURE = re.compile(
    r"Traceback|\b(?:ERROR|CRITICAL|UNKNOWN|NOT_COMMITTED|BUSY|LOCK_BUSY)\b|"
    r"未能核验|身份不一致|身份冲突|证据核验未通过|运行异常|正在运行|失败|紧急|⚠|"
    r"(?:错误|待核)\s*[1-9]\d*", re.I)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _instant(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("naive_timestamp")
    return moment


def transform(stdout, rc, evidence):
    """Return (original bytes plus optional suffix, whether suffix was added).

    Evidence comes from collect.collect_profit_evidence, augmented with mode
    and businessDate by run(). Profit additionally requires sourceReport and
    sourceReportSha256 from the immutable launch receipt. No report archive
    for the current dispatch is required: stdout is already in hand.
    """
    if not isinstance(stdout, bytes):
        raise TypeError("stdout must be bytes")
    try:
        if (type(rc) is not int or rc != 0 or not isinstance(evidence, dict)
                or evidence.get("verified") is not True or evidence.get("routine") is not True
                or evidence.get("state") != "COMMITTED"
                or type(evidence.get("accountId")) is not int or evidence["accountId"] != 1
                or evidence.get("mode") not in SPECS or len(stdout) > MAX_PROOF_BYTES):
            return stdout, False
        day = evidence["businessDate"]
        if date.fromisoformat(day).isoformat() != day or evidence["marketDate"] != day:
            return stdout, False
        execution = evidence["executionId"]
        if str(uuid.UUID(execution)) != execution:
            return stdout, False
        generated = _instant(evidence["generatedAt"])
        if generated.astimezone(TZ).date().isoformat() != day:
            return stdout, False
        for key in ("releaseSha256", "resultSha256"):
            if not isinstance(evidence.get(key), str) or not _SHA.fullmatch(evidence[key]):
                return stdout, False
        status = evidence["status"]
        if status not in ("PAPER_CANDIDATES", "CASH", "DATA_BLOCKED"):
            return stdout, False
        if (type(evidence.get("candidateCount")) is not int or evidence["candidateCount"] < 0
                or status == "DATA_BLOCKED" and (evidence.get("dataReady") is not False
                                                or evidence["candidateCount"] != 0)):
            return stdout, False
        text = stdout.decode("utf-8")
        # This exact fixed disclaimer is part of the original routine report,
        # not an operational warning. Remove it ONLY from warning inspection;
        # output bytes, receipt equality and archival hashes remain untouched.
        warning_text = "\n".join(line for line in text.splitlines()
                                 if line.strip() != STANDARD_DISCLAIMER)
        if not text.strip() or _FAILURE.search(warning_text):
            return stdout, False
        if any(line.strip().upper() in ("[SILENT]", "SILENT", "NO_REPLY", "NO REPLY")
               for line in text.splitlines()):
            return stdout, False
        if evidence["mode"] == "profit":
            original = evidence.get("sourceReport")
            if (not isinstance(original, str) or not original
                    or _sha(original.encode("utf-8")) != evidence.get("sourceReportSha256")
                    or text.strip() != original.strip()
                    or execution not in text or evidence["generatedAt"] not in text
                    or not any(phrase in text for phrase in (
                        "QuantDinger 盈利研究", "盈利研究执行证据"))):
                return stdout, False
            # The verified attempt carries the machine business status. The
            # pinned buy-condition renderer displays human candidate/holding
            # states and need not print PAPER_CANDIDATES or CASH. Its exact
            # receipt body/hash, execution and timestamp still bind stdout to
            # that attempt; warning inspection above remains fail-closed.
        else:
            matches = _MARKER.findall(text)
            if len(matches) != 1 or re.findall(r"^状态：(\S+)\s*$", text, re.M) != [status]:
                return stdout, False
            marker = json.loads(matches[0])
            if (marker.get("contract_version") != "profit.execution-evidence.v1"
                    or marker.get("state") != "COMMITTED"
                    or marker.get("execution_id") != execution
                    or marker.get("market_date") != day
                    or marker.get("release_sha256") != evidence["releaseSha256"]
                    or marker.get("result_sha256") != evidence["resultSha256"]
                    or _instant(marker["generated_at"]) != generated):
                return stdout, False
            if not any(title in text for title in ("盈利研究执行证据", "Profit Radar")):
                return stdout, False
        return stdout + SILENT_SUFFIX, True
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return stdout, False


def _read_regular(path, *, limit=MAX_PROOF_BYTES, private=False):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlink_refused")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("regular_file_required")
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("private_file_required")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file_limit")
    return raw


def _private_dir(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlink_refused")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private_directory_required")


def _exclusive_bytes(path, raw):
    """Never replaces an existing raw file, envelope or receipt."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read_regular(path, limit=max(len(raw), 1), private=True) != raw:
            raise ValueError("immutable_archive_conflict")
        # A concurrent creator may have written matching bytes but not yet
        # fsynced them. Make the reused inode and its name durable ourselves.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def archive_output(root, *, mode, day, dispatch_id, stdout, envelope, rc, silent,
                   availability=None, notice_kind=None):
    """Independent archive only; no references to Hermes' historical files."""
    if mode not in SPECS or date.fromisoformat(day).isoformat() != day:
        raise ValueError("archive_identity_invalid")
    suffix = SILENT_SUFFIX if silent else b""
    if notice_kind == "summary_maintenance_warning":
        if silent or not isinstance(availability, dict) or availability.get("available") is not False:
            raise ValueError("maintenance_notice_without_blocked_handoff")
        suffix = MAINTENANCE_SUFFIX
    elif notice_kind is not None:
        raise ValueError("unknown_notice_kind")
    if envelope != stdout + suffix:
        raise ValueError("envelope_not_original_plus_suffix")
    if dispatch_id:
        dispatch_id = uuid.UUID(dispatch_id).hex
        stem = dispatch_id
    else:
        stem = day + "-" + _sha(stdout)
    root = Path(root)
    _private_dir(root)
    directory = root / SPECS[mode]["job_id"]
    _private_dir(directory)
    raw_path = directory / (stem + ".raw")
    envelope_path = directory / (stem + ".envelope.raw")
    receipt_path = directory / (stem + ".json")
    receipt = {"schema": "daily-summary-original-envelope.v1", "mode": mode,
               "jobId": SPECS[mode]["job_id"], "businessDate": day,
               "dispatchId": dispatch_id or None, "returnCode": rc,
               "bodySha256": _sha(stdout), "bodyBytes": len(stdout),
               "envelopeSha256": _sha(envelope), "envelopeBytes": len(envelope),
               "silent": silent, "originalFile": raw_path.name,
               "envelopeFile": envelope_path.name}
    if notice_kind:
        receipt.update(schema="daily-summary-original-envelope.v2", noticeKind=notice_kind,
                       handoffStatus="unavailable", availability=availability,
                       deliveryClass="operational_warning", deliveryStatus="unconfirmed",
                       automaticResend=False)
    _exclusive_bytes(raw_path, stdout)
    _exclusive_bytes(envelope_path, envelope)
    _exclusive_bytes(receipt_path, _encoded(receipt))
    # Read back all three artifacts; an incomplete/conflicting publication
    # cannot authorize suppression. O_EXCL makes a crash leave evidence.
    if (_sha(_read_regular(raw_path, limit=max(1, len(stdout)), private=True)) != receipt["bodySha256"]
            or _sha(_read_regular(envelope_path, limit=max(1, len(envelope)), private=True)) != receipt["envelopeSha256"]
            or json.loads(_read_regular(receipt_path, private=True)) != receipt):
        raise ValueError("archive_readback_failed")
    return receipt


def archive_redecision(root, prior, stdout, availability):
    """Append one immutable late-maintenance decision; keep all prior artifacts."""
    if not prior.get("silent") or availability.get("available") is not False:
        raise ValueError("redecision_requires_silent_prior_and_unavailable_consumer")
    dispatch = uuid.UUID(prior["dispatchId"]).hex
    directory = Path(root) / prior["jobId"]
    prior_name = dispatch + ".json"
    prior_raw = _read_regular(directory / prior_name, private=True)
    if json.loads(prior_raw) != prior or prior["bodySha256"] != _sha(stdout):
        raise ValueError("redecision_prior_identity_conflict")
    original = _read_regular(directory / prior["originalFile"], private=True)
    old_envelope = _read_regular(directory / prior["envelopeFile"], private=True)
    if original != stdout or old_envelope != stdout + SILENT_SUFFIX:
        raise ValueError("redecision_prior_bytes_conflict")
    stem = prior["businessDate"] + "-" + dispatch
    envelope_name = stem + ".final.envelope.raw"
    receipt_name = stem + ".final-decision.json"
    final_envelope = stdout + MAINTENANCE_SUFFIX
    receipt = {**prior, "schema": "daily-summary-original-redecision.v1", "silent": False,
               "priorReceiptFile": prior_name, "priorReceiptSha256": _sha(prior_raw),
               "priorEnvelopeSha256": _sha(old_envelope), "envelopeFile": envelope_name,
               "envelopeSha256": _sha(final_envelope), "envelopeBytes": len(final_envelope),
               "noticeKind": "summary_maintenance_warning", "handoffStatus": "unavailable",
               "availability": availability, "deliveryClass": "operational_warning",
               "deliveryStatus": "unconfirmed", "automaticResend": False}
    _exclusive_bytes(directory / envelope_name, final_envelope)
    _exclusive_bytes(directory / receipt_name, _encoded(receipt))
    if (_read_regular(directory / envelope_name, private=True) != final_envelope
            or json.loads(_read_regular(directory / receipt_name, private=True)) != receipt):
        raise ValueError("redecision_readback_failed")
    return receipt


def _owned_dispatch(job_id, *, ledger=HERMES_LEDGER, proc_root=Path("/proc")):
    """Match an existing running Hermes dispatch against the parent chain.

    This mirrors the original dispatch resolver's 64-level ancestry bound.
    Starting a child process/session does not change its parent PID chain.
    No dispatch is inferred from the latest row or created by this wrapper.
    """
    pid = os.getpid()
    ancestors = {}
    for _ in range(64):
        if pid <= 0:
            break
        if pid in ancestors:
            raise ValueError("ancestor_cycle")
        folder = proc_root / str(pid)
        raw = (folder / "stat").read_text()
        fields = raw[raw.rfind(")") + 1:].split()
        ancestors[pid] = (int(fields[19]), folder.stat().st_uid)
        pid = int(fields[1])
    else:
        raise ValueError("ancestor_depth_limit")
    ledger = Path(ledger)
    if any(part.is_symlink() for part in (ledger, *ledger.parents)):
        raise ValueError("linked_ledger")
    info = ledger.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unowned_ledger")
    conn = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("SELECT id,pid,process_started_at,source FROM executions "
                            "WHERE job_id=? AND status='running' LIMIT 33", (job_id,)).fetchall()
        if len(rows) >= 33:
            raise ValueError("dispatch_limit")
        matches = [uuid.UUID(identity).hex for identity, owner, started, source in rows
                   if source == "builtin" and type(started) is int
                   and ancestors.get(owner) == (started, os.getuid())]
        if len(matches) != 1:
            raise ValueError("dispatch_identity_unavailable")
        return matches[0]
    finally:
        conn.close()


def _kill_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _execute(spec):
    """One child launch; a session allows bounded cleanup on wrapper timeout."""
    proc = subprocess.Popen([spec["interpreter"], spec["script"]],
                            cwd=str(Path(spec["script"]).parent),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    completed_normally = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=spec["timeout"])
            completed_normally = True
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_group(proc, signal.SIGKILL)
                stdout, stderr = proc.communicate(timeout=3)
            return 124, stdout, stderr + b"\ntransport_wrapper_timeout\n"
    finally:
        # On timeout/cancellation, a reaped leader and closed pipes do NOT
        # prove that its process group is gone: a grandchild can ignore TERM
        # and redirect its own output. Always send the final KILL to our group
        # on these paths, even when communicate() already reaped the leader.
        # The pinned originals must not daemonize outside this process group.
        if not completed_normally:
            _kill_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=3)
            finally:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()


def _load_evidence(day, now):
    from collect import collect_profit_evidence
    return collect_profit_evidence(account_id=1, business_date=day, as_of=now)


def _load_control():
    return json.loads(_read_regular(CONTROL))


def _control_enabled(loader, day):
    # Pure shared schema validation; this import does not construct a sender
    # or read sender credentials. A rollback takes effect before suppression.
    from run_summary import validate_control
    try:
        validate_control(loader(), day)
        return True
    except Exception:
        return False


def run(mode, *, archive_root=ARCHIVE_ROOT, evidence_loader=None, now=None,
        dispatch_resolver=None, control_loader=None, availability_loader=None):
    """Run a fixed original command once; return bytes for the caller to emit."""
    if mode not in SPECS:
        raise ValueError("unsupported_mode")
    spec = SPECS[mode]
    start = now or datetime.now(timezone.utc)
    if start.tzinfo is None:
        raise ValueError("aware_time_required")
    day = start.astimezone(TZ).date().isoformat()
    result = {"returncode": 78, "stdout": b"", "stderr": b"", "silent": False,
              "archive": None, "reason": None}
    try:
        if _sha(_read_regular(spec["script"])) != spec["sha256"]:
            raise ValueError("original_script_drift")
    except Exception:
        result.update(stderr=b"transport_wrapper_original_identity_refused\n",
                      reason="original_identity_refused")
        return result
    dispatch_id = None
    try:
        dispatch_id = (dispatch_resolver or _owned_dispatch)(spec["job_id"])
    except Exception:
        pass  # Original command still performs its own authoritative guard.
    try:
        rc, stdout, stderr = _execute(spec)
    except Exception:
        result.update(returncode=70, stderr=b"transport_wrapper_execution_error\n",
                      reason="execution_error")
        return result
    envelope, silent = stdout, False
    reason = "original_not_routine"
    availability = None
    handoff_observation = {}
    research_binding = None
    notice_kind = None
    control_loader = control_loader or _load_control
    enabled = _control_enabled(control_loader, day)
    if not enabled:
        reason = "control_not_effective"
    if enabled and rc == 0 and not stderr.strip() and dispatch_id:
        try:
            availability_loader = availability_loader or probe
            before = checked_probe(availability_loader, business_date=day,
                                   now=now or datetime.now(timezone.utc))
            handoff_observation["before"] = before
            evidence = dict((evidence_loader or _load_evidence)(day, now or datetime.now(timezone.utc)))
            evidence.update(mode=mode, businessDate=day)
            # Profit's current dispatch must be the handoff producer. Health
            # has its own dispatch but reads the same producer execution.
            if mode == "profit" and uuid.UUID(evidence["dispatchId"]).hex != dispatch_id:
                raise ValueError("producer_dispatch_mismatch")
            if _sha(_read_regular(spec["script"])) != spec["sha256"]:
                raise ValueError("original_script_drift")
            envelope, silent = transform(stdout, rc, evidence)
            if silent:
                research_binding = {"verified": True,
                                    "executionId": evidence["executionId"],
                                    "resultSha256": evidence["resultSha256"],
                                    "status": evidence["status"]}
            reason = "verified_routine" if silent else "routine_validation_failed"
            if silent:
                after = checked_probe(availability_loader, business_date=day,
                                      now=now or datetime.now(timezone.utc))
                handoff_observation["after"] = after
                if not before["available"] or not after["available"]:
                    unavailable = before if not before["available"] else after
                    availability = {**unavailable, "before": before, "after": after}
                    envelope, silent = stdout + MAINTENANCE_SUFFIX, False
                    notice_kind = "summary_maintenance_warning"
                    reason = "summary_consumer_unavailable"
            # A read-only evidence lookup can take seconds. Recheck the
            # toggle after it, so disabling while it runs keeps output visible.
            if not _control_enabled(control_loader, day):
                envelope, silent, reason = stdout, False, "control_not_effective"
                notice_kind, availability = None, None
        except Exception:
            reason = "readonly_evidence_unavailable"
    try:
        receipt = archive_output(archive_root, mode=mode, day=day,
                                 dispatch_id=dispatch_id, stdout=stdout, envelope=envelope,
                                 rc=rc, silent=silent, availability=availability,
                                 notice_kind=notice_kind)
    except Exception:
        # Archival failure cannot silently discard the only human-facing copy.
        envelope = stdout + (MAINTENANCE_SUFFIX if notice_kind else b"")
        silent, receipt, reason = False, None, "archive_unavailable"
    if silent and receipt is not None:
        final = checked_probe(availability_loader, business_date=day,
                              now=now or datetime.now(timezone.utc))
        handoff_observation["final"] = final
        if not final["available"]:
            # Never overwrite the durable SILENT proposal. The appended final
            # decision records the warning actually returned to Hermes.
            envelope, silent = stdout + MAINTENANCE_SUFFIX, False
            notice_kind, availability = "summary_maintenance_warning", final
            try:
                receipt = archive_redecision(archive_root, receipt, stdout, final)
                reason = "summary_consumer_changed_after_archive"
            except Exception:
                receipt, reason = None, "late_maintenance_archive_unavailable"
    if receipt is not None and handoff_observation:
        # A later scheduled Invocation can replace systemd's last-attempt
        # fields. Preserve this dispatch's read-only decision snapshots in a
        # NEW sidecar; never retrofit or overwrite old envelopes/receipts.
        observation = {"schema": "quant.original-handoff-observation.v1",
                       "jobId": spec["job_id"], "businessDate": day,
                       "dispatchId": dispatch_id,
                       "originalReceiptSha256": _sha(_encoded(receipt)),
                       "bodySha256": receipt["bodySha256"],
                       "envelopeSha256": receipt["envelopeSha256"],
                       "silent": silent, "observations": handoff_observation,
                       "platformDeliveryVerified": False, "unknownPreserved": True}
        path = Path(archive_root) / spec["job_id"] / (dispatch_id + ".handoff-observation.json")
        try:
            raw_observation = _encoded(observation)
            _exclusive_bytes(path, raw_observation)
            if _read_regular(path, private=True) != raw_observation:
                raise ValueError("handoff_observation_readback_failed")
        except Exception:
            result.update(returncode=78, stdout=b"", stderr=stderr + b"handoff_observation_archive_refused\n",
                          reason="handoff_observation_archive_refused", silent=False,
                          archive=receipt, archiveVerified=True)
            return result
    result.update(returncode=rc, stdout=envelope, stderr=stderr, silent=silent,
                  archive=receipt, reason=reason, noticeKind=notice_kind,
                  availability=availability, researchBinding=research_binding,
                  handoffObservation=handoff_observation,
                  businessDate=day, dispatchId=dispatch_id,
                  archiveVerified=receipt is not None,
                  originalReceiptSha256=_sha(_encoded(receipt)) if receipt is not None else None)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=tuple(SPECS))
    args = parser.parse_args(argv)
    result = run(args.mode)
    # Fresh subprocess ownership: managed successful originals never fall
    # through to the long-lived gateway's retry/fallback sender. Non-zero
    # research failures keep their existing, separate fault-alert route.
    if result["returncode"] == 0 and not result["silent"]:
        from original_delivery import finalize_original
        from crosspath_delivery import Ledger
        from telegram_once import configured_sender
        result = finalize_original(
            result, args.mode, result.get("businessDate"), result.get("dispatchId"),
            _load_control(), Ledger(), configured_sender,
            lambda: datetime.now(timezone.utc))
    sys.stdout.buffer.write(result["stdout"])
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(result["stderr"])
    sys.stderr.buffer.flush()
    code = result["returncode"]
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    os.umask(0o077)
    # Let _execute's finally terminate its child group if Hermes cancels us.
    def _cancel(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        print("transport_wrapper_unexpected_error", file=sys.stderr)
        raise SystemExit(70)
