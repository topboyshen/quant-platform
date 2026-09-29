#!/usr/bin/env python3
"""No-network, no-state-write worker group reaper for supervisor death.

The supervisor starts this process as the worker's process-group leader, passes
the worker PID, waits for READY, and only then releases the worker's start gate.
The leader remains unreaped until all group signals/checks finish, pinning the
numeric PGID. The control pipe is held exclusively by the supervisor; an EOF
without explicit DISARM means it exited or crashed.
"""
import os
from pathlib import Path
import signal
import sys
import time


def _readline(fd, maximum=32):
    data = bytearray()
    while len(data) < maximum:
        part = os.read(fd, 1)
        if not part:
            return None
        if part == b"\n":
            return bytes(data)
        data.extend(part)
    return None


def live_group_members(pgid, *, exclude=()):
    """Linux-only live membership check; None means identity is unprovable."""
    if not sys.platform.startswith("linux"):
        return None
    members = []
    excluded = set(exclude)
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            fields = raw[raw.rfind(")") + 2:].split()
            state, process_group = fields[0], int(fields[2])
        except (ValueError, IndexError):
            return None
        if process_group == pgid and state != "Z" and int(entry.name) not in excluded:
            members.append(int(entry.name))
    return members


def _stop_group(pgid):
    # The watchdog itself is the process-group leader; its numeric ID cannot
    # be reused while this process is alive. This final signal kills it too.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            time.sleep(0.5)


def run_watchdog(control_fd, ack_fd):
    line = _readline(control_fd)
    try:
        pgid = int(line) if line is not None else None
    except ValueError:
        pgid = None
    if pgid is None or pgid <= 1 or pgid == os.getpid():
        return 64
    if sys.platform.startswith("linux"):
        try:
            if os.getpgid(pgid) != os.getpgrp():
                return 64
        except ProcessLookupError:
            return 64
    os.write(ack_fd, b"READY\n")
    command = members = None
    try:
        command = _readline(control_fd, maximum=16)
        if command == b"DISARM":
            members = live_group_members(os.getpgrp(), exclude=(os.getpid(),))
            if members is None and not sys.platform.startswith("linux"):
                # Local Darwin tests exercise protocol flow only. Deployment
                # requires the Linux /proc proof above.
                members = []
            if members == []:
                os.write(ack_fd, b"DISARMED\n")
                return 0
            os.write(ack_fd, b"STRAGGLER\n")
    finally:
        if command != b"DISARM" or members != []:
            _stop_group(os.getpgrp())
        os.close(control_fd)
        os.close(ack_fd)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(64)
    raise SystemExit(run_watchdog(int(sys.argv[1]), int(sys.argv[2])))
