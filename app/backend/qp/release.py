"""Release identity read from the immutable release directory.

``RELEASE_NAME``/``RELEASE_SHA``/``RELEASE_BASE`` are written by the deploy script
next to ``current``.  In a source checkout or test process they are absent, so the
package version is the fallback.
"""
from __future__ import annotations

import os
from pathlib import Path

from qp import __version__


def _read(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def release_info() -> dict[str, str]:
    app_dir = os.getenv("QP_APP_DIR", "").strip()
    root = Path(app_dir).parent if app_dir else Path("/opt/qp/current")
    return {
        "version": _read(root / "RELEASE_NAME") or __version__,
        "sha": _read(root / "RELEASE_SHA") or "",
        "base": _read(root / "RELEASE_BASE") or "",
    }
