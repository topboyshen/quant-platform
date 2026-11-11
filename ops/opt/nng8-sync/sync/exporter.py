"""Fixed six-source forced command. No production URL/path/command arguments."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import os
from pathlib import Path
import sys
import time

from .common import (Busy, Deadline, DOCUMENTS, MAX_BUNDLE, MAX_SOURCE, MODES, OPERATION,
                     SyncError, alarm, canonical, entry, lock, open_directory, read_file,
                     strict_json, utcnow, validate_bundle)

FILES = tuple((name, Path("/var/lib/nng8-dashboard") / (name + ".json"))
              for name in ("today", "progress", "delivery", "projects"))
ENDPOINTS = (("ledger", 18769, "/v1/calibration/status"),
             ("phase2", 18768, "/api/dashboard"))
MODE_FILE = Path("/etc/research-ledger/runtime-health.mode")
CONTROL = Path("/var/lib/nng8-qd-export")
GET_SECONDS = 8


@dataclass(frozen=True)
class Sources:
    """Only Python tests can inject isolated fixtures; production CLI has no flags."""
    files: tuple = FILES
    endpoints: tuple = ENDPOINTS
    mode_file: Path = MODE_FILE


def http_get(port, path, deadline):
    bound = Deadline(end=min(deadline.end, time.monotonic() + GET_SECONDS))
    with alarm(bound):
        return _http_get(port, path, bound)


def _http_get(port, path, deadline):
    # HTTPConnection never consults proxy environment and never follows redirects.
    connection = http.client.HTTPConnection("127.0.0.1", port,
                                            timeout=min(GET_SECONDS, deadline.remaining()))
    try:
        connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise SyncError("transport_failed")
        chunks, size = [], 0
        while True:
            deadline.check()
            chunk = response.read1(min(65_536, MAX_SOURCE + 1 - size))
            size += len(chunk)
            if size > MAX_SOURCE:
                raise SyncError("size_limit")
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except TimeoutError:
        raise SyncError("timeout") from None
    except (OSError, http.client.HTTPException):
        raise SyncError("transport_failed") from None
    finally:
        connection.close()


def capture(sources=Sources(), *, deadline=None, get=http_get, now=utcnow):
    deadline = deadline or Deadline()
    documents = {}
    for name, path in sources.files:
        deadline.check()
        documents[name] = entry(strict_json(read_file(path, MAX_SOURCE, deadline), MAX_SOURCE))
    for name, port, path in sources.endpoints:
        deadline.check()
        documents[name] = entry(strict_json(get(port, path, deadline), MAX_SOURCE))
    if set(documents) != DOCUMENTS:
        raise SyncError("invalid_bundle")
    try:
        mode_raw = read_file(sources.mode_file, 64, deadline)
        mode = mode_raw.decode("ascii").strip()
        if mode not in MODES:
            raise SyncError("invalid_bundle")
    except FileNotFoundError:
        mode = "UNKNOWN"
    except UnicodeError:
        raise SyncError("invalid_bundle") from None
    phase2 = documents["phase2"]["payload"].get("runtime_mode", "UNKNOWN")
    if not isinstance(phase2, str) or phase2 not in MODES:
        phase2 = "UNKNOWN"
    stamp = now()
    captured = stamp.isoformat()
    bundle = {"schema": "nng8.readonly-bundle.v1", "captured_at": captured,
              "documents": documents,
              "runtime_modes": {name: entry({"mode": value, "observed_at": captured})
                                for name, value in (("ledger", mode), ("phase2", phase2))}}
    raw = canonical(bundle) + b"\n"
    if len(raw) > MAX_BUNDLE:
        raise SyncError("size_limit")
    validate_bundle(raw, now=stamp)
    deadline.check()
    return raw


def authorize(argv, original_command, uid):
    if argv or original_command != OPERATION or uid == 0:
        raise SyncError("transport_failed")


def main():
    deadline = Deadline(45)
    directory = None
    try:
        authorize(sys.argv[1:], os.environ.get("SSH_ORIGINAL_COMMAND"), os.geteuid())
        # No caller-controlled env influences modules, paths, URLs, or transport.
        os.environ.clear()
        os.environ.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        os.umask(0o027)
        with alarm(deadline):
            directory = open_directory(CONTROL, protected=True, owner=os.geteuid(), strict_parents=True)
            with lock(directory, "export.lock"):
                raw = capture(deadline=deadline)
                view = memoryview(raw)
                while view:
                    deadline.check()
                    length = os.write(1, view[:65_536])
                    if length <= 0:
                        raise SyncError("transport_failed")
                    view = view[length:]
        return 0
    except Busy:
        return 75
    except Exception:
        # Exit status only: no raw source error, filename, URL, or stderr body.
        return 1
    finally:
        if directory is not None:
            os.close(directory)


if __name__ == "__main__":
    raise SystemExit(main())
