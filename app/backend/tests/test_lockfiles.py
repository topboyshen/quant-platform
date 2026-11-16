"""Keep the CI/test lock and the release runtime lock on the same pins."""
from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parse_lock(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"non-exact pin in {path.name}: {line}"
        name, version = line.split("==", 1)
        pins[name.lower().replace("_", "-")] = version.strip()
    assert pins, f"empty lock: {path}"
    return pins


def test_runtime_pins_are_a_subset_of_the_test_lock():
    runtime = parse_lock(repo_root() / "app/backend/requirements-runtime.lock")
    full = parse_lock(repo_root() / "app/backend/requirements.lock")
    missing = {name: version for name, version in runtime.items()
               if full.get(name) != version}
    assert not missing, missing
    for test_only in ("pytest", "httpx", "pgserver"):
        assert test_only not in runtime, test_only


def test_release_script_uses_the_runtime_lock():
    script = repo_root() / "app/deploy/bin/qp-release-venv"
    text = script.read_text(encoding="utf-8")
    assert "requirements-runtime.lock" in text
    assert "pip" in text and "--no-deps" in text
    assert "grep -q '^qp @'" in text
