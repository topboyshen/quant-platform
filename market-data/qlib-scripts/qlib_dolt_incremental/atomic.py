"""Qlib-only locked staging and atomic promotion; no retained data backups.

The fully validated, embedded generation manifest is the commit identity.
RENAME_EXCHANGE is the single commit point. The canonical release tag is not
changed or forged by an incremental update.
"""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

MANIFEST = ".qlib-dolt-incremental.json"
MANIFEST_LIMIT = 65536
PREFIX = ".qlib-incremental."


def regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not_regular_file:{path.name}")
    return info


def read_small(path: Path, limit=65536) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"unsafe_file:{path.name}")
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError("file_limit")
        return value


def sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value, *, limit=None):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if limit is not None and len(raw) > limit:
        raise ValueError("manifest_size_limit")
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    sync_dir(path.parent)


def exchange(left: Path, right: Path):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise RuntimeError("Linux_RENAME_EXCHANGE_required")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _tree_inventory(root: Path):
    """Read sizes and source identities together, without following links.

    Each file's one no-follow stat supplies both products. Directory descriptors
    keep traversal anchored if a directory name is replaced while walking.
    The initial baseline is retained for comparison, not as an admission cache:
    every prepare, including the independent commit, obtains fresh inventories.
    """
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("root_not_real_directory")
    files, identities = {}, {}

    def visit(directory_fd, prefix):
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                name = prefix + entry.name
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise ValueError("unsafe_directory")
                        visit(child_fd, name + "/")
                    finally:
                        os.close(child_fd)
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(f"not_regular_file:{entry.name}")
                    if info.st_nlink != 1:
                        raise ValueError("linked_data_file")
                    files[name] = info.st_size
                    identities[name] = (info.st_ino, info.st_mtime_ns, info.st_size)

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(root_fd, "")
    finally:
        os.close(root_fd)
    return files, identities


def scan_tree(root: Path):
    return _tree_inventory(root)[0]


def prefix_digest(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        remaining = size
        while remaining:
            raw = stream.read(min(remaining, 1024 * 1024))
            if not raw:
                raise ValueError("short_historical_prefix")
            digest.update(raw)
            remaining -= len(raw)
    return digest.hexdigest()


def historical_prefixes(old: Path, new: Path, sizes: dict[str, int]):
    """Prove every old feature byte and historical calendar byte unchanged."""
    digest = hashlib.sha256()
    count = total = 0
    for name, size in sorted(sizes.items()):
        if name.startswith("features/") or name == "calendars/day.txt":
            if regular(new / name).st_size < size:
                raise ValueError(f"historical_truncation:{name}")
            before = prefix_digest(old / name, size)
            if before != prefix_digest(new / name, size):
                raise ValueError(f"historical_rewrite:{name}")
            digest.update(f"{name}\0{size}\0{before}\n".encode())
            count += 1
            total += size
        elif not name.startswith("instruments/") and name not in (MANIFEST, "calendars/day_future.txt"):
            if regular(new / name).st_size != size or prefix_digest(old / name, size) != prefix_digest(new / name, size):
                raise ValueError(f"unexpected_file_change:{name}")
    return {"files": count, "bytes": total, "sha256": digest.hexdigest(), "unchanged": True}


class LockBusy(BlockingIOError):
    """Another updater holds the lock; this process has no publication right."""


class Transaction:
    def __init__(self, data_dir, exchange_fn=exchange):
        self.data = Path(data_dir).absolute()
        self.active = self.data / "cn_data"
        self.exchange_fn = exchange_fn
        self.workspace = None
        self.stage = None
        self.lock = None
        self.locked = False
        self.on_lock_busy = None
        self.committed = False
        self.identity = uuid.uuid4().hex
        self.progress = None

    def _manifest_phase(self, phase, operation):
        """Report fixed-name read/write subphases; incomplete work has no completion."""
        started = time.monotonic()
        if self.progress is not None:
            self.progress(phase, state="start", elapsedMs=0)
        result = operation()
        if self.progress is not None:
            self.progress(phase, state="completed", elapsedMs=round((time.monotonic() - started) * 1000))
        return result

    def __enter__(self):
        if self.data == Path("/") or self.data.resolve() != self.data or not self.data.is_dir():
            raise ValueError("unsafe_data_directory")
        if self.active.is_symlink() or not self.active.is_dir():
            raise ValueError("unsafe_active_directory")
        self.lock = os.open(self.data / ".update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            if not stat.S_ISREG(os.fstat(self.lock).st_mode):
                raise ValueError("unsafe_lock")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                # A busy lock means the running updater owns the shared status
                # file. This process must not publish over it; the caller gets
                # a hook for an independent attempt record instead.
                hook = getattr(self, "on_lock_busy", None)
                if callable(hook):
                    try:
                        hook()
                    except Exception:
                        pass
                raise LockBusy(*exc.args) from None
            self.locked = True
            if (self.data / ".cn_data.rollback").exists() or (self.data / ".cn_data.rollback").is_symlink():
                raise ValueError("canonical_release_recovery_required")
            self.recover()
            self.origin_inode = self.active.stat().st_ino
            self.origin_tag = read_small(self.data / ".tag", 128)
            if self.origin_tag.strip() != read_small(self.active / ".qlib-release", 128).strip():
                raise ValueError("release_identity_disagrees")
            self.sizes, self.original_stats = _tree_inventory(self.active)
            self.workspace = self.data / (PREFIX + self.identity)
            self.workspace.mkdir(mode=0o700)
            self.stage = self.workspace / "cn_data"
            write_json(self.workspace / "transaction.json", {
                "id": self.identity, "origin_inode": self.origin_inode, "candidate_inode": None,
            })
            return self
        except BaseException as exc:
            # Failed initialization after taking the lock is still this run's
            # terminal state: publish it before releasing the lock. A busy
            # lock never reaches here (self.locked stays False), so a waiting
            # process can never overwrite the running updater's status.
            callback = getattr(self, "before_unlock", None)
            if self.locked and callable(callback):
                try:
                    callback(type(exc), exc)
                except Exception:
                    pass
            os.close(self.lock)
            self.lock = None
            raise

    def copy(self):
        # A temporary build generation, not a retained backup. No hard links:
        # appending a candidate file must never modify the active inode.
        required = sum(self.sizes.values()) + 128 * 1024 * 1024
        if shutil.disk_usage(self.data).free < required * 2:
            raise ValueError("insufficient_staging_space")
        shutil.copytree(self.active, self.stage, copy_function=shutil.copy2)
        self.candidate_inode = self.stage.stat().st_ino
        write_json(self.workspace / "transaction.json", {
            "id": self.identity, "origin_inode": self.origin_inode, "candidate_inode": self.candidate_inode,
        })
        return self.stage

    def prepare_manifest(self, manifest):
        """Write the bounded candidate identity before read-only consumer checks.

        This does not promote the candidate. The workspace tag is only the
        exact release identity needed by readers of the staged generation;
        the canonical tag remains untouched. Repeating preparation is safe.
        """
        def check_generation():
            if (self.workspace.is_symlink() or not self.workspace.is_dir()
                    or self.workspace.resolve() != self.workspace
                    or self.stage.is_symlink() or not self.stage.is_dir()
                    or self.stage.stat().st_ino != self.candidate_inode):
                raise ValueError("candidate_generation_changed")

        self._manifest_phase("manifest_generation_check", check_generation)
        self._manifest_phase("manifest_candidate_inventory_initial", lambda: scan_tree(self.stage))

        def check_production():
            if self.active.stat().st_ino != self.origin_inode or read_small(self.data / ".tag", 128) != self.origin_tag:
                raise ValueError("production_generation_changed")
            sizes, identities = _tree_inventory(self.active)
            if sizes != self.sizes:
                raise ValueError("production_files_changed")
            if identities != self.original_stats:
                raise ValueError("production_file_mutated")

        self._manifest_phase("manifest_production_inventory", check_production)
        proof = self._manifest_phase("manifest_historical_prefixes",
                                     lambda: historical_prefixes(self.active, self.stage, self.sizes))
        manifest = dict(manifest, generationId=self.identity, historicalPrefixes=proof)
        self._manifest_phase("manifest_write",
                             lambda: write_json(self.stage / MANIFEST, manifest, limit=MANIFEST_LIMIT))

        def write_candidate_tag():
            candidate_tag = self.workspace / ".tag"
            try:
                fd = os.open(candidate_tag, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            except FileExistsError:
                if read_small(candidate_tag, 128) != self.origin_tag:
                    raise ValueError("candidate_release_identity_changed")
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.origin_tag)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_dir(self.workspace)

        self._manifest_phase("manifest_candidate_tag", write_candidate_tag)
        self._manifest_phase("manifest_candidate_inventory_final", lambda: scan_tree(self.stage))
        return manifest

    def commit(self, manifest):
        # All callers, including ones that omit a separate consumer precheck,
        # must satisfy the same size, prefix and generation guards.
        manifest = self.prepare_manifest(manifest)
        # Durability barrier before exposing the candidate tree.
        for directory, _, names in os.walk(self.stage):
            for name in names:
                fd = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            sync_dir(Path(directory))
        if (self.workspace.is_symlink() or self.stage.is_symlink()
                or self.workspace.resolve() != self.workspace
                or self.stage.stat().st_ino != self.candidate_inode
                or self.stage.stat().st_dev != self.active.stat().st_dev
                or self.active.stat().st_ino != self.origin_inode):
            raise ValueError("candidate_or_active_changed_before_exchange")
        self.exchange_fn(self.active, self.stage)
        self.committed = True
        sync_dir(self.data)
        sync_dir(self.workspace)
        return manifest

    def recover(self):
        """Remove only identified non-active generations while holding lock."""
        for path in sorted(self.data.glob(PREFIX + "*")):
            if not re.fullmatch(re.escape(PREFIX) + r"[0-9a-f]{32}", path.name):
                raise ValueError("unknown_staging_path")
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
                raise ValueError("unsafe_staging_path")
            marker_path = path / "transaction.json"
            if not marker_path.exists():
                # The only safe marker-less recovery is removing an empty
                # directory left between unlink(marker) and rmdir(workspace).
                path.rmdir()
                sync_dir(self.data)
                continue
            marker = json.loads(read_small(marker_path))
            if marker.get("id") != path.name[len(PREFIX):]:
                raise ValueError("staging_identity_mismatch")
            current_inode = self.active.stat().st_ino
            stage = path / "cn_data"
            if stage.is_symlink():
                raise ValueError("unsafe_staging_candidate")
            stage_inode = stage.stat().st_ino if stage.exists() else None
            before = current_inode == marker.get("origin_inode")
            after = (current_inode == marker.get("candidate_inode") and
                     stage_inode in (marker.get("origin_inode"), None))
            if after:
                active_manifest = json.loads(read_small(self.active / MANIFEST))
                if active_manifest.get("generationId") != marker["id"]:
                    raise ValueError("committed_identity_mismatch")
            if not before and not after:
                raise ValueError("ambiguous_staging_recovery")
            scan_tree(path)
            # Keep the proof until all data is gone. Any interruption resumes
            # using the same marker, including after the old tree was removed.
            for child in path.iterdir():
                if child == marker_path:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            sync_dir(path)
            marker_path.unlink()
            sync_dir(path)
            path.rmdir()
            sync_dir(self.data)

    def __exit__(self, kind, value, traceback):
        try:
            callback = getattr(self, "before_unlock", None)
            if self.lock is not None and callable(callback):
                try:
                    callback(kind, value)
                except Exception:
                    if kind is None:
                        raise
            if self.workspace is not None and self.workspace.exists():
                # recover() validates the exact marker and inode pair before
                # deleting either failed staging or the obsolete old generation.
                self.recover()
        finally:
            if self.lock is not None:
                os.close(self.lock)
                self.lock = None
