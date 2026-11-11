"""Restricted SSH pull and conservative, atomic generation publication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import ipaddress
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid

from .common import (Busy, Deadline, ERRORS, MAX_BUNDLE, MAX_HEALTH, MAX_STDERR, OPERATION, PublicationUncertain,
                     SyncError, VERSION, alarm, atomic_write, canonical, instant, lock,
                     open_directory, read_at, read_file, sha, source_times, strict_json,
                     utcnow, validate_bundle)

CONFIG = Path("/etc/quant-nng8-sync/config.json")
STAGING = Path("/var/lib/quant-nng8-sync-staging")
CONTROL = Path("/var/lib/quant-nng8-sync")
IDENTITY = Path("/etc/quant-nng8-sync/identity")
KNOWN_HOSTS = Path("/etc/quant-nng8-sync/known_hosts")
HEALTH_FIELDS = frozenset(("schema", "writer_version", "attempt_id", "status", "started_at", "finished_at",
                          "last_success_at", "bundle_sha256", "captured_at", "consecutive_failures", "error_code"))


@dataclass(frozen=True)
class Outcome:
    status: str
    error_code: str | None = None
    published: bool = False
    health_written: bool = False
    data_committed: bool = False  # Both current and private receipt completed fsync.
    public_success_visible: bool = False
    durability_unknown: bool = False


def restricted_ssh_argv(config):
    """Config can select only a literal address/port/known user, never SSH options."""
    if not isinstance(config, dict) or set(config) != {"schema", "host", "port", "user"}:
        raise SyncError("transport_failed")
    if config["schema"] != "nng8.ssh-config.v1" or config["user"] != "nng8-qd-export":
        raise SyncError("transport_failed")
    try:
        host = str(ipaddress.ip_address(config["host"]))
    except (TypeError, ValueError):
        raise SyncError("transport_failed") from None
    port = config["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise SyncError("transport_failed")
    options = ("BatchMode=yes", "ConnectTimeout=10", "ConnectionAttempts=1",
               "StrictHostKeyChecking=yes", "UserKnownHostsFile=" + str(KNOWN_HOSTS),
               "GlobalKnownHostsFile=/dev/null", "IdentityAgent=none", "IdentitiesOnly=yes",
               "IdentityFile=" + str(IDENTITY), "CertificateFile=none", "ForwardAgent=no",
               "ClearAllForwardings=yes", "ForwardX11=no", "RequestTTY=no", "PermitLocalCommand=no",
               "ProxyCommand=none", "ProxyJump=none", "ControlMaster=no", "ControlPath=none",
               "ControlPersist=no", "CanonicalizeHostname=no", "UpdateHostKeys=no",
               "VerifyHostKeyDNS=no", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
               "PreferredAuthentications=publickey", "NumberOfPasswordPrompts=0", "LogLevel=ERROR",
               "ServerAliveInterval=5", "ServerAliveCountMax=2", "EscapeChar=none")
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-n"]
    for option in options:
        argv.extend(("-o", option))
    argv.extend(("-p", str(port), "-l", "nng8-qd-export", host, OPERATION))
    return tuple(argv)


def _admin_file(path, *, uid, mode, limit):
    """Use the same pinned inode for permission checks and the bounded read."""
    directory = open_directory(path.parent, strict_parents=True, owner=0)
    try:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1:
            raise SyncError("transport_failed")
        # Config and host keys must be admin-owned, inaccessible to writer edits.
        raw = read_at(directory, path.name, limit)
        after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
            raise SyncError("transport_failed")
        return raw
    finally:
        os.close(directory)


def load_production_config():
    uid = os.geteuid()
    if uid == 0:
        raise SyncError("transport_failed")
    config = strict_json(_admin_file(CONFIG, uid=0, mode=0o640, limit=4096), 4096)
    _admin_file(KNOWN_HOSTS, uid=0, mode=0o640, limit=65_536)
    # Identity is writer-owned 0600 in the root-owned fixed config directory.
    # It is never printed, decoded, returned, or copied to staging.
    _admin_file(IDENTITY, uid=uid, mode=0o600, limit=16_384)
    return restricted_ssh_argv(config)


def terminate_group(process):
    """Target only this call's new session; descendants holding pipes are included."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # A fixed short grace leaves budget for bounded failure publication.
    until = time.monotonic() + 0.2
    while time.monotonic() < until and process.poll() is None:
        time.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass


def receive(argv, deadline):
    """Actually bound bytes as pipes arrive; no communicate()/unbounded buffer."""
    deadline.check()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/",
                               close_fds=True, start_new_session=True)
    selector = None
    completed = False
    try:
        selector = selectors.DefaultSelector()
        buffers, counts = {"stdout": []}, {"stdout": 0, "stderr": 0}
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline.remaining()
            for key, _ in selector.select(min(0.1, remaining)):
                name = key.data
                part = os.read(key.fileobj.fileno(), 65_536)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                counts[name] += len(part)
                if counts[name] > (MAX_BUNDLE if name == "stdout" else MAX_STDERR) or sum(counts.values()) > 1_100_000:
                    raise SyncError("size_limit")
                if name == "stdout":
                    buffers[name].append(part)
        try:
            status = process.wait(timeout=deadline.remaining())
        except subprocess.TimeoutExpired:
            raise SyncError("timeout") from None
        deadline.check()
        if status != 0:
            raise SyncError("transport_failed")
        completed = True
        return b"".join(buffers["stdout"])
    finally:
        if not completed:
            terminate_group(process)
        if selector is not None:
            selector.close()
        process.stdout.close()
        process.stderr.close()


def _previous_health(directory, old, old_raw, now, deadline):
    """Untrusted/corrupt sidecars cannot manufacture inherited success or dates."""
    try:
        health = strict_json(read_at(directory, "sync_health.json", MAX_HEALTH, deadline), MAX_HEALTH)
        if set(health) != HEALTH_FIELDS or health["schema"] != "nng8.sync-health.v1" or health["writer_version"] != VERSION:
            return None
        if not re.fullmatch(r"[0-9a-f]{32}", health["attempt_id"]):
            return None
        if health["status"] not in {"running", "success", "failed", "unknown"}:
            return None
        count = health["consecutive_failures"]
        if type(count) is not int or not 0 <= count <= 10**9:
            return None
        if health["error_code"] is not None and health["error_code"] not in ERRORS:
            return None
        dates = {key: instant(health[key]) if health[key] is not None else None
                 for key in ("started_at", "finished_at", "last_success_at")}
        if dates["started_at"] is None or any(value and value > now + timedelta(seconds=5) for value in dates.values()):
            return None
        if dates["finished_at"] and dates["finished_at"] < dates["started_at"]:
            return None
        if health["status"] == "success" and (count != 0 or health["error_code"] is not None or
                dates["finished_at"] is None or dates["last_success_at"] != dates["finished_at"]):
            return None
        if health["status"] == "running" and (dates["finished_at"] is not None or health["error_code"] is not None):
            return None
        if health["status"] in {"failed", "unknown"} and (dates["finished_at"] is None or health["error_code"] is None):
            return None
        if health["status"] != "success" and dates["last_success_at"] and dates["last_success_at"] > dates["started_at"]:
            return None
        expected = (sha(old_raw), old["captured_at"]) if old is not None else (None, None)
        if (health["bundle_sha256"], health["captured_at"]) != expected:
            return None
        return health
    except (OSError, SyncError, TypeError, KeyError):
        return None


def _health(attempt, status, started, finished, last_success, failures, bundle, raw, error=None):
    return {"schema": "nng8.sync-health.v1", "writer_version": VERSION, "attempt_id": attempt,
            "status": status, "started_at": started, "finished_at": finished,
            "last_success_at": last_success, "bundle_sha256": sha(raw) if raw is not None else None,
            "captured_at": bundle["captured_at"] if bundle is not None else None,
            "consecutive_failures": failures, "error_code": error}


def _publish_health(directory, health, deadline):
    raw = canonical(health) + b"\n"
    if len(raw) > MAX_HEALTH:
        raise SyncError("publication_failed")
    atomic_write(directory, "sync_health.json", raw, deadline)


def _check_generation(old, old_raw, new, raw):
    if old is None:
        return
    before, after = instant(old["captured_at"]), instant(new["captured_at"])
    if after < before:
        raise SyncError("replay")
    if after == before and raw != old_raw:
        raise SyncError("capture_conflict")
    old_times, new_times = source_times(old), source_times(new)
    if any(new_times[path] < value for path, value in old_times.items() if path in new_times):
        raise SyncError("source_time_regressed")


def run_once(staging, control, transport, *, timeout=45, now=utcnow, strict_directories=False):
    """Injection is a Python test API only; the production entrypoint accepts no args."""
    deadline = Deadline(timeout)
    stagefd = controlfd = None
    try:
        stagefd = open_directory(staging, protected=True, owner=os.geteuid(), strict_parents=strict_directories)
        controlfd = open_directory(control, protected=True, owner=os.geteuid(), strict_parents=strict_directories)
        if os.fstat(stagefd).st_ino == os.fstat(controlfd).st_ino and os.fstat(stagefd).st_dev == os.fstat(controlfd).st_dev:
            raise SyncError("publication_failed")
        with lock(controlfd, "writer.lock"):
            return _locked_run(stagefd, controlfd, transport, deadline, now, timeout)
    except Busy:
        return Outcome("skipped_busy")
    except SyncError as exc:
        return Outcome("unknown", exc.code)
    except OSError:
        return Outcome("unknown", "publication_failed")
    finally:
        for fd in (stagefd, controlfd):
            if fd is not None:
                os.close(fd)


def _locked_run(stagefd, controlfd, transport, deadline, now, timeout):
    old = old_raw = None
    initial_error = None
    try:
        old_raw = read_at(stagefd, "current.json", MAX_BUNDLE, deadline)
        old = validate_bundle(old_raw, now=now())
    except FileNotFoundError:
        pass
    except (SyncError, OSError):
        old = old_raw = None
        initial_error = SyncError("invalid_bundle")
    started = now().isoformat()
    prior = _previous_health(stagefd, old, old_raw, now(), deadline)
    last_success = prior["last_success_at"] if prior else None
    failures = prior["consecutive_failures"] if prior else 0
    if prior and prior["status"] == "running":
        failures = min(10**9, failures + 1)  # Prior crashed attempt is never inherited as success.
    attempt = uuid.uuid4().hex
    published = data_committed = False
    candidate = candidate_raw = None
    phase = "publication_failed"
    try:
        _publish_health(stagefd, _health(attempt, "running", started, None, last_success,
                                        failures, old, old_raw), deadline)
        if initial_error:
            raise initial_error
        phase = "transport_failed"
        reserve = min(2.5, timeout / 4)
        candidate_raw = transport(Deadline(end=deadline.end - reserve))
        deadline.check()
        if not isinstance(candidate_raw, bytes):
            raise SyncError("invalid_bundle")
        candidate = validate_bundle(candidate_raw, now=now())
        deadline.check()
        _check_generation(old, old_raw, candidate, candidate_raw)
        phase = "publication_failed"
        # Same timestamp and exact same bytes is idempotent; never restamp capture.
        if old_raw != candidate_raw:
            atomic_write(stagefd, "current.json", candidate_raw, deadline)
            published = True
        else:
            # A prior interrupted attempt may have renamed before its directory fsync.
            # Idempotent confirmation therefore also confirms existing-file durability.
            fd = os.open("current.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=stagefd)
            try:
                deadline.check()
                os.fsync(fd)
                deadline.check()
                os.fsync(stagefd)
            finally:
                os.close(fd)
        phase = "receipt_failed"
        finished = now().isoformat()
        success = _health(attempt, "success", started, finished, finished, 0, candidate, candidate_raw)
        # A single bounded receipt replaces the previous one; no content history.
        atomic_write(controlfd, "receipt.json", canonical(success) + b"\n", deadline, mode=0o600)
        data_committed = True
        phase = "publication_failed"
        try:
            _publish_health(stagefd, success, deadline)
        except PublicationUncertain as exc:
            # Success visibility follows durable data + receipt. Its own directory
            # fsync failure cannot reliably revoke that already-visible fact.
            # Do not promise a compensation write or falsely label receipt_failed.
            return Outcome("unknown", exc.code, published, False, True, True, True)
        return Outcome("success", published=published, health_written=True,
                       data_committed=True, public_success_visible=True)
    except Exception as exc:
        code = (exc.code if isinstance(exc, SyncError) and not isinstance(exc, PublicationUncertain)
                else "timeout" if isinstance(exc, PublicationUncertain) and exc.code == "timeout"
                else phase if isinstance(exc, (OSError, PublicationUncertain)) else "unknown")
        # replace may have committed before fsync failed. Reconcile actual bytes.
        try:
            current_raw = read_at(stagefd, "current.json", MAX_BUNDLE, deadline)
            if candidate is not None and current_raw == candidate_raw:
                published = old_raw != candidate_raw
                linked, linked_raw = candidate, candidate_raw
            else:
                linked, linked_raw = old, old_raw
        except (OSError, SyncError):
            linked, linked_raw = old, old_raw
        status = "unknown" if published or data_committed or phase == "receipt_failed" else "failed"
        health = _health(attempt, status, started, now().isoformat(), last_success,
                         min(10**9, failures + 1), linked, linked_raw, code)
        try:
            _publish_health(stagefd, health, deadline)
            return Outcome(status, code, published, True, data_committed,
                           durability_unknown=isinstance(exc, PublicationUncertain))
        except (OSError, SyncError):
            # No post-deadline writes. Prior running/stale/mismatched health is conservative.
            return Outcome("unknown", code, published, False, data_committed,
                           durability_unknown=isinstance(exc, PublicationUncertain))


def main():
    if sys.argv[1:] or os.geteuid() == 0:
        return 1
    os.umask(0o027)
    deadline = Deadline(45)
    try:
        with alarm(deadline):
            argv = load_production_config()
            outcome = run_once(STAGING, CONTROL, lambda bound: receive(argv, bound),
                               timeout=deadline.remaining(), strict_directories=True)
        # Bounded enums/booleans only. Never emit raw SSH stderr, config, or content.
        print(canonical({"status": outcome.status, "error_code": outcome.error_code,
                         "data_committed": outcome.data_committed,
                         "public_success_visible": outcome.public_success_visible,
                         "durability_unknown": outcome.durability_unknown}).decode("ascii"))
        return 0 if outcome.status == "success" else 75 if outcome.status == "skipped_busy" else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
