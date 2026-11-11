#!/usr/bin/env python3
"""Root-only fixed-role log-health rollup; never reads raw nginx logs or secrets.

The installation profile contains only root-owned, pre-attested public inode and
container identity metadata. This program never updates that expected identity.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import mount_snapshot_guard

import rollup_log_health_monitor as monitor
from redacted_log_collector import health_check
from socket_binding import SocketBindingBlocked, check_binding, identity_userns, proc_start_ticks

SOURCE = Path("/run/qd-frontend-log-private/syslog.sock")
TARGET = "/run/qd-frontend-log/syslog.sock"
ROLES = {
    "host": ("/run/qd-redacted-log/syslog.sock", "/var/lib/qd-redacted-log"),
    "frontend": (str(SOURCE), "/var/lib/qd-redacted-log-frontend"),
}
STATE = Path("/var/lib/qd-log-health-root")
INCIDENT = STATE / "incidents"
METRICS = Path("/var/lib/qd-log-health-metrics/qd_redacted_log.prom")
ROLE_METRICS = STATE / "qd_redacted_log_roles.prom"
NOTIFY = "/run/qd-approved-alert/notify.sock"
UNIT = "qd-redacted-log-health"
BINDING = Path("/run/qd-log-health-binding/frontend.sock")


def read_profile(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise ValueError("profile_not_root_private")
        raw = os.read(descriptor, 4097)
        value = json.loads(raw)
    finally:
        os.close(descriptor)
    if (not isinstance(value, dict) or set(value) != {"schema", "phase", "enabled", "expected"}
            or value["schema"] != 1 or value["phase"] != "P-installed"
            or value["enabled"] is not True or not isinstance(value["expected"], dict)
            or set(value["expected"]) != {"container_id", "master_pid", "master_start_ticks",
                                          "source_dev", "source_inode"}):
        raise ValueError("profile_not_phase_bound")
    return value["expected"]


def runtime_for(container_id):
    # Fixed read-only Docker operation. No env/config dump, exec, start or mutation.
    result = subprocess.run([
        "/usr/bin/docker", "inspect", "--format",
        '{"id":{{json .Id}},"pid":{{.State.Pid}},"mounts":{{json .Mounts}}}', container_id],
        check=True, capture_output=True, text=True, timeout=2)
    value = json.loads(result.stdout)
    pid = value["pid"]
    # Current production frontend master is host UID 0. Identity drift must
    # close this gate, not automatically widen ptrace or rebuild the container.
    if Path(f"/proc/{pid}").stat().st_uid != 0:
        raise SocketBindingBlocked("frontend_master_uid_changed")
    chosen = [item for item in value["mounts"] if item["Destination"] == TARGET]
    if len(chosen) != 1 or chosen[0].get("Source") != str(SOURCE):
        raise SocketBindingBlocked("readonly_single_inode_mount_required")
    return {"container_id": value["id"], "master_pid": pid,
            "master_start_ticks": proc_start_ticks(pid),
            "userns_identity_map": identity_userns(pid),
            "mount_type": chosen[0]["Type"], "mount_read_only": not chosen[0]["RW"]}


def readonly_self_mount(raw, target=str(BINDING)):
    """Require one exact file mount, readonly in this service's own namespace."""
    chosen = []
    for line in raw.splitlines():
        left, separator, _right = line.partition(" - ")
        fields = left.split()
        if separator and len(fields) >= 6 and fields[4] == target:
            chosen.append(fields[5].split(","))
    return len(chosen) == 1 and "ro" in chosen[0]


def attest(expected, runtime_reader=runtime_for):
    # Reject unknown identity before handing it to Docker or constructing /proc path.
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("container_id", ""))):
        raise SocketBindingBlocked("container_identity_unbound")
    runtime = runtime_reader(expected["container_id"])
    pid = runtime["master_pid"]
    # Root installation proves the host source equals the container kernel
    # socket. A private companion freezes mount metadata, not Docker declarations
    # alone. No SYS_PTRACE and no foreign process root traversal by this helper.
    mount_snapshot_guard.attest(expected, runtime_reader)
    mounted = BINDING
    parent = BINDING.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_gid != 0
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not readonly_self_mount(Path("/proc/self/mountinfo").read_text())):
        raise SocketBindingBlocked("private_readonly_attestation_mount_required")
    result = check_binding(expected=expected, runtime=runtime, parent=SOURCE.parent.lstat(),
                           source=SOURCE.lstat(), mounted=mounted.lstat())
    mount_snapshot_guard.attest(expected, runtime_reader)
    return result


def probe_roles(expected, *, binding_probe=attest, canary_probe=health_check):
    roles = {role: False for role in ROLES}
    binding_ok = False
    try:
        binding_ok = binding_probe(expected).get("ok") is True
    except Exception:
        pass
    # A source-path canary can still prove host health when frontend's mount is stale.
    for role, (socket_path, state_path) in ROLES.items():
        if role == "frontend" and not binding_ok:
            continue
        try:
            roles[role] = canary_probe(socket_path, state_path, timeout=2, max_age=3).get("ok") is True
        except Exception:
            roles[role] = False
    # Bind identity must stay valid across both probes; otherwise frontend is closed.
    if binding_ok:
        try:
            binding_ok = binding_probe(expected).get("ok") is True
        except Exception:
            binding_ok = False
    roles["frontend"] = roles["frontend"] and binding_ok
    return {"ok": all(roles.values()),
            "reason": "collector_canary_acknowledged" if all(roles.values()) else "probe_failed",
            "roles": roles, "frontend_binding_ok": binding_ok}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="/etc/qd-redacted-log/frontend-binding.json")
    parser.add_argument("--forced", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print('{"ok":false,"reason":"root_probe_required"}')
        return 2
    roles = {"host": False, "frontend": False}
    binding_ok = False
    try:
        expected = read_profile(args.profile)
        health = ({"ok": False, "reason": "unit_failed"} if args.forced else probe_roles(expected))
        roles = health.get("roles", roles)
        binding_ok = health.get("frontend_binding_ok", False)
    except Exception:
        health = {"ok": False, "reason": "probe_failed"}
    try:
        monitor.safe_state(STATE)
        monitor.safe_state(METRICS.parent)
        # Same incident identity and ledger for the timer and all collector OnFailure units.
        result = monitor.observe(state_dir=STATE, incident_dir=INCIDENT, metrics_file=METRICS,
                                 collector_socket=ROLES["host"][0], collector_state=ROLES["host"][1],
                                 notify_socket=NOTIFY, notify_uid=0, unit=UNIT,
                                 forced=False, health_result=health)
        numeric = {"qd_redacted_log_host_health_ok": int(roles["host"]),
                   "qd_redacted_log_frontend_health_ok": int(roles["frontend"]),
                   "qd_redacted_log_frontend_binding_ok": int(binding_ok)}
        monitor.atomic(ROLE_METRICS, "".join(f"{key} {value}\n" for key, value in numeric.items()).encode(), 0o644)
        print(json.dumps({**result, "roles": roles, "frontend_binding_ok": binding_ok}, sort_keys=True))
        return 0 if result["ok"] else 2
    except Exception:
        print('{"ok":false,"reason":"monitor_failed","metrics_written":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
