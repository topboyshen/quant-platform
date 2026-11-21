"""Release DB revision check: the activation gate must see the Alembic head."""

from __future__ import annotations

import json


def test_db_check_reports_head(engine, capsys):
    from qp.cli import main

    assert main(["db", "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["current"] in payload["heads"]


def test_db_check_fails_on_unapplied_revision(engine, capsys, monkeypatch):
    from alembic.runtime.migration import MigrationContext

    from qp.cli import main

    monkeypatch.setattr(MigrationContext, "get_current_revision", lambda self: "0000")
    assert main(["db", "check"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["current"] == "0000"
    assert payload["current"] not in payload["heads"]
