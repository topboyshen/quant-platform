"""Top-level documentation guard: the map and the retirement plan stay linked."""
from __future__ import annotations

from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_architecture_and_roadmap_are_linked():
    readme = (root() / "README.md").read_text(encoding="utf-8")
    assert "ARCHITECTURE.md" in readme
    assert "DECOMMISSION-ROADMAP.md" in readme
    assert (root() / "ARCHITECTURE.md").is_file()
    assert (root() / "app/deploy/DECOMMISSION-ROADMAP.md").is_file()
    assert (root() / "archive/README.md").is_file()


def test_architecture_keeps_the_daily_chain():
    architecture = (root() / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for marker in ("15:30", "15:55", "16:45", "16:55", "17:40"):
        assert marker in architecture, marker


def test_roadmap_keeps_the_four_phases():
    roadmap = (root() / "app/deploy/DECOMMISSION-ROADMAP.md").read_text(encoding="utf-8")
    for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert phase in roadmap, phase
