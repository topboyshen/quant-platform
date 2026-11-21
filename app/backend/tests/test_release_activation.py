"""Deploy health gate: dry-run and static safety checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.assets

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "app" / "deploy" / "bin" / "qp-release-activate"


def _fake_release(path: Path) -> None:
    (path / "venv" / "bin").mkdir(parents=True)
    qp = path / "venv" / "bin" / "qp"
    qp.write_text("#!/usr/bin/env bash\nexit 0\n")
    qp.chmod(0o755)


def test_dry_run_preserves_current_symlink(tmp_path):
    previous = tmp_path / "prev"
    release = tmp_path / "next"
    _fake_release(previous)
    _fake_release(release)
    current = tmp_path / "current"
    current.symlink_to(previous)
    result = subprocess.run(
        [str(SCRIPT), "--release", str(release), "--previous", str(previous), "--current", str(current), "--dry-run"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY_RUN OK" in result.stdout
    assert current.resolve() == previous.resolve()


def test_script_has_atomic_switch_health_and_rollback():
    text = SCRIPT.read_text(encoding="utf-8")
    assert os.access(SCRIPT, os.X_OK)
    assert "mv -T" in text and "$CURRENT.tmp" in text
    assert "health_ok" in text and "ROLLBACK_OK" in text
    assert "systemctl restart" in text
    assert "qp-release-activate" in text
    assert "DB_CHECK_FAILED" in text and "DB_CHECK_OK" in text
    assert text.index("DB_CHECK_FAILED") < text.index('echo "ACTIVATE_START')
