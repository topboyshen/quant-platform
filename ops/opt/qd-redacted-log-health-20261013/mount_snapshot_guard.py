"""Phase-frozen kernel mount identity: no secrets, ptrace or auto-rebinding.

The explicit root installation step must prove the container socket inode via
the kernel. Runtime DAC-only health reads only public mount/status metadata and
its own manager-bound socket. A changed/inaccessible snapshot always closes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

SOURCE = Path("/run/qd-frontend-log-private/syslog.sock")
TARGET = "/run/qd-frontend-log/syslog.sock"
COMPANION = Path("/etc/qd-redacted-log/frontend-kernel-mount.json")
KEYS = {"container_id", "master_pid", "master_start_ticks", "source_dev", "source_inode"}
CREDENTIALS = {"CapPrm": 0xa00425fb, "CapEff": 0xa00425fb,
               "CapBnd": 0xa00425fb, "NoNewPrivs": 0}
MAX_MOUNTINFO = 1024 * 1024


class MountSnapshotBlocked(ValueError):
    pass


def validate_expected(expected):
    if not isinstance(expected, dict) or set(expected) != KEYS:
        raise MountSnapshotBlocked("kernel_profile_shape")
    if not re.fullmatch(r"[a-f0-9]{64}", str(expected["container_id"])):
        raise MountSnapshotBlocked("kernel_container_identity")
    for key in KEYS - {"container_id"}:
        if type(expected[key]) is not int or expected[key] < 1:
            raise MountSnapshotBlocked("kernel_numeric_identity")
    if expected["master_pid"] >= 2 ** 31:
        raise MountSnapshotBlocked("kernel_numeric_pid")


def bounded_read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        output = bytearray()
        while len(output) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
        if not output or len(output) > maximum:
            raise MountSnapshotBlocked("kernel_metadata_empty_or_oversize")
        return bytes(output)
    finally:
        os.close(fd)


def mount_digest(pid):
    if type(pid) is not int or not 1 <= pid < 2 ** 31:
        raise MountSnapshotBlocked("kernel_numeric_pid")
    return hashlib.sha256(bounded_read(f"/proc/{pid}/mountinfo", MAX_MOUNTINFO)).hexdigest()


def target_credentials(pid):
    raw = bounded_read(f"/proc/{pid}/status", 65536).decode("ascii")
    values = {}
    for line in raw.splitlines():
        key, separator, text = line.partition(":")
        if separator and key in CREDENTIALS:
            values[key] = int(text.strip(), 16 if key.startswith("Cap") else 10)
        if separator and key in ("Uid", "Gid"):
            if [int(value) for value in text.split()] != [0, 0, 0, 0]:
                raise MountSnapshotBlocked("kernel_master_credentials_drift")
            values[key] = True
    if set(values) != set(CREDENTIALS) | {"Uid", "Gid"}:
        raise MountSnapshotBlocked("kernel_master_credentials_missing")
    selected = {key: values[key] for key in CREDENTIALS}
    if selected != CREDENTIALS:
        raise MountSnapshotBlocked("kernel_master_credentials_drift")
    return selected


def runtime_identity(expected, reader):
    value = reader(expected["container_id"])
    for key in ("container_id", "master_pid", "master_start_ticks"):
        if value.get(key) != expected[key] or type(value.get(key)) is not type(expected[key]):
            raise MountSnapshotBlocked("kernel_runtime_identity_changed")
    if (value.get("userns_identity_map") is not True or value.get("mount_type") != "bind"
            or value.get("mount_read_only") is not True):
        raise MountSnapshotBlocked("kernel_runtime_binding_changed")
    return value


def capture(expected, runtime_reader):
    """Explicit root P only: fresh kernel evidence, returned not auto-written."""
    if os.geteuid() != 0:
        raise MountSnapshotBlocked("root_installation_proof_required")
    validate_expected(expected)
    runtime_identity(expected, runtime_reader)
    pid = expected["master_pid"]
    digest = mount_digest(pid)
    credentials = target_credentials(pid)
    source = SOURCE.lstat()
    actual = Path(f"/proc/{pid}/root{TARGET}").stat()
    wanted = (expected["source_dev"], expected["source_inode"])
    if (not stat.S_ISSOCK(source.st_mode) or not stat.S_ISSOCK(actual.st_mode)
            or (source.st_dev, source.st_ino) != wanted or (actual.st_dev, actual.st_ino) != wanted):
        raise MountSnapshotBlocked("initial_kernel_socket_proof_failed")
    runtime_identity(expected, runtime_reader)
    if mount_digest(pid) != digest or target_credentials(pid) != credentials:
        raise MountSnapshotBlocked("initial_kernel_snapshot_changed")
    return {"schema": 1, "phase": "P-installed", "enabled": True,
            "expected": {**expected, "mountinfo_sha256": digest},
            "kernel_socket": {"dev": actual.st_dev, "inode": actual.st_ino},
            "credentials": credentials}


def validate_companion(value, expected):
    validate_expected(expected)
    if (not isinstance(value, dict) or set(value) != {"schema", "phase", "enabled", "expected", "kernel_socket", "credentials"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["phase"] != "P-installed" or value["enabled"] is not True
            or not isinstance(value["expected"], dict)
            or set(value["expected"]) != KEYS | {"mountinfo_sha256"}):
        raise MountSnapshotBlocked("kernel_companion_shape")
    if ({key: value["expected"][key] for key in KEYS} != expected
            or any(type(value["expected"][key]) is not type(expected[key]) for key in KEYS)):
        raise MountSnapshotBlocked("kernel_companion_profile_mismatch")
    digest = value["expected"]["mountinfo_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise MountSnapshotBlocked("kernel_companion_digest")
    if (not isinstance(value["kernel_socket"], dict)
            or value["kernel_socket"] != {"dev": expected["source_dev"], "inode": expected["source_inode"]}
            or any(type(item) is not int for item in value["kernel_socket"].values())
            or not isinstance(value["credentials"], dict) or value["credentials"] != CREDENTIALS
            or any(type(item) is not int for item in value["credentials"].values())):
        raise MountSnapshotBlocked("kernel_companion_proof_mismatch")
    return digest


def read_companion(expected, path=COMPANION):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise MountSnapshotBlocked("kernel_companion_not_private")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise MountSnapshotBlocked("kernel_companion_oversize")
        return validate_companion(json.loads(raw), expected)
    finally:
        os.close(fd)


def attest(expected, runtime_reader, *, companion_reader=read_companion,
           digest_reader=mount_digest, credentials_reader=target_credentials):
    validate_expected(expected)
    wanted = companion_reader(expected)
    runtime_identity(expected, runtime_reader)
    pid = expected["master_pid"]
    if digest_reader(pid) != wanted or credentials_reader(pid) != CREDENTIALS:
        raise MountSnapshotBlocked("kernel_mount_or_credentials_changed")
    runtime_identity(expected, runtime_reader)
    if digest_reader(pid) != wanted:
        raise MountSnapshotBlocked("kernel_mount_changed_during_probe")
    return {"ok": True, "mountinfo_sha256": wanted}
