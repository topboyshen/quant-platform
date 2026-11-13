"""M9 decommission package: CLI wiring and runbook content."""
from __future__ import annotations

import json
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_cli_inventory_writes_manifest(tmp_path: Path):
    from qp.cli import main
    out = tmp_path / "inventory.json"
    assert main(["decommission", "inventory", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert "sha256" in data and "totals" in data


def test_runbook_documents_stop_archive_rollback_and_consent():
    text = (DEPLOY / "M9-DECOMMISSION-RUNBOOK.md").read_text(encoding="utf-8")
    assert "只停不删" in text
    assert "qp decommission inventory" in text
    assert "docker stop" in text and "只 stop" in text
    assert "2 周" in text
    assert "回滚" in text
    assert "再一次单独授权" in text
    assert "观察" in text
