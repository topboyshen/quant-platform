#!/usr/bin/env python3
"""Read-only frontend inode/userns binding gate; never creates/remounts sockets."""
import os
from pathlib import Path
import re
import stat


class SocketBindingBlocked(RuntimeError):
    pass


def check_binding(*, expected, runtime, parent, source, mounted):
    """Validate pre-attested public metadata and lstat results, without mutation.

    The caller must collect the container master identity twice around the
    filesystem reads; a mismatch is a stop, not permission to refresh expected.
    """
    if (not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("container_id", "")))
            or type(expected.get("master_pid")) is not int or expected["master_pid"] <= 0
            or type(expected.get("master_start_ticks")) is not int
            or expected["master_start_ticks"] <= 0):
        raise SocketBindingBlocked("container_identity_unbound")
    if any(runtime.get(key) != expected[key] for key in
           ("container_id", "master_pid", "master_start_ticks")):
        raise SocketBindingBlocked("container_identity_changed")
    if runtime.get("userns_identity_map") is not True:
        raise SocketBindingBlocked("userns_mapping_changed")
    if runtime.get("mount_type") != "bind" or runtime.get("mount_read_only") is not True:
        raise SocketBindingBlocked("readonly_single_inode_mount_required")
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != 0 or parent.st_gid != 0):
        raise SocketBindingBlocked("root_private_parent_required")
    for item in (source, mounted):
        if (not stat.S_ISSOCK(item.st_mode) or item.st_uid != 0 or item.st_gid != 101
                or stat.S_IMODE(item.st_mode) != 0o620):
            raise SocketBindingBlocked("socket_type_owner_mode_invalid")
    if ((source.st_dev, source.st_ino) != (mounted.st_dev, mounted.st_ino)
            or source.st_dev != expected.get("source_dev")
            or source.st_ino != expected.get("source_inode")):
        raise SocketBindingBlocked("socket_inode_changed_or_mount_stale")
    return {"ok": True, "reason": "private_inode_binding_verified",
            "automatic_recreate": False, "automatic_remount": False}


def check_paths(*, expected, runtime_before, runtime_after, source_path, mounted_path):
    if runtime_before != runtime_after:
        raise SocketBindingBlocked("runtime_changed_during_probe")
    source = Path(source_path)
    mounted = Path(mounted_path)
    return check_binding(expected=expected, runtime=runtime_before,
                         parent=source.parent.lstat(), source=source.lstat(), mounted=mounted.lstat())


def proc_start_ticks(pid):
    # /proc/stat comm can contain spaces/parentheses. Fields after the final )
    # begin at field3; starttime is field22, so index19 is required.
    text = Path(f"/proc/{pid}/stat").read_text()
    suffix = text.rpartition(")")[2].split()
    return int(suffix[19])


def identity_userns(pid):
    maps = []
    for name in ("uid_map", "gid_map"):
        text = Path(f"/proc/{pid}/{name}").read_text()
        maps.append([tuple(int(value) for value in line.split()) for line in text.splitlines()])
    return maps == [[(0, 0, 4294967295)], [(0, 0, 4294967295)]]
