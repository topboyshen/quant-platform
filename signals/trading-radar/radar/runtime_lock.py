"""Process-level single-instance locks for long-running services."""

from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path
from typing import TextIO


class SingleInstanceLockError(RuntimeError):
    """Raised when another service instance already holds the lock."""


def _default_lock_dir() -> Path:
    root = os.getenv("RADAR_LOCK_DIR")
    if root:
        return Path(root)
    return Path(tempfile.gettempdir()) / "trading-radar"


class SingleInstanceLock:
    def __init__(self, name: str, lock_dir: Path | None = None):
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in name)
        self.path = (lock_dir or _default_lock_dir()) / f"{safe_name}.lock"
        self._handle: TextIO | None = None

    def acquire(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip() or "unknown"
            handle.close()
            raise SingleInstanceLockError(
                f"service already running: lock={self.path} holder={holder}"
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
