#!/usr/bin/env python3
"""Root helper: a sealed argv whitelist, independently checked before action."""

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys


SYSTEMCTL = "/usr/bin/systemctl"
HASHES = {
    "nng8-dashboard-refresh.timer": "2b186ab9c0de507b2ae63faf1b7e0614893bba28b596d07bdd6c49b9cbb8491b",
    "nng8-dashboard-refresh.service": "e195b5431c475ad3e464d3fdf4735fb6b0e2e5c4c7b82d74358755061023cf12",
    "research-data-health.timer": "b9316a906e0e473a385a75fe467b65e2d3ac763a955fc63572148ef9df176043",
    "research-data-health-alert.timer": "ea9bcc78573d3b528016bd9514c947a1df6d63f681c9c2dbcf0f3e6fa6edc0e0",
}
TIMERS = tuple(name for name in HASHES if name.endswith(".timer"))
ALLOWED = {tuple((verb, "--now", name)) for verb in ("enable", "disable") for name in TIMERS}
ALLOWED.add(("start", "nng8-dashboard-refresh.service"))


def command(args, capture=False, timeout=5):
    process = subprocess.Popen([SYSTEMCTL] + list(args), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    try:
        output = process.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError("timeout") from None
    if process.returncode or output is not None and len(output) > 8192:
        raise RuntimeError("command_failed")
    return output.decode("utf-8") if output is not None else ""


def validate(args):
    args = tuple(args)
    if args not in ALLOWED:
        raise RuntimeError("unregistered_argv")
    unit = args[-1]
    expected = Path("/etc/systemd/system") / unit
    if not expected.is_file() or expected.is_symlink():
        raise RuntimeError("invalid_unit_file")
    if hashlib.sha256(expected.read_bytes()).hexdigest() != HASHES[unit]:
        raise RuntimeError("unit_config_changed")
    raw = command(("show", "--no-pager", unit, "-p", "Id", "-p", "LoadState", "-p", "FragmentPath",
                   "-p", "DropInPaths", "-p", "Persistent", "-p", "OnSuccess", "-p", "OnFailure"), capture=True)
    properties = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if properties.get("Id") != unit or properties.get("LoadState") != "loaded" \
            or properties.get("FragmentPath") != str(expected) or properties.get("DropInPaths"):
        raise RuntimeError("unit_identity_changed")
    if args[0] == "enable" and properties.get("Persistent") != "no":
        raise RuntimeError("persistent_resume_forbidden")
    if args[0] == "start" and (properties.get("OnSuccess") or properties.get("OnFailure")):
        raise RuntimeError("chained_execution_forbidden")
    return args


def main(argv):
    if os.geteuid() != 0:
        return 2
    try:
        args = validate(argv)
        command(args, timeout=20)
    except (RuntimeError, OSError, UnicodeError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
