"""Inventory alert evaluation: malformed container rows and recovery."""

from __future__ import annotations

from qp.jobs.inventory import evaluate, recovery_evidence


def test_nameless_container_row_is_ignored_and_old_alert_can_resolve():
    snap = {"containers": [{"name": None, "state": None, "health": None}]}
    firing = evaluate(snap, [], 0)
    assert not [item for item in firing if item[0].startswith("container:")]
    assert "container:None" in recovery_evidence(snap, [], 0, {"container:None"})


def test_real_container_row_still_alerts():
    snap = {"containers": [{"name": "radar-quantdinger-postgres", "state": "exited", "health": None}]}
    firing = evaluate(snap, [], 0)
    assert any(item[0] == "container:radar-quantdinger-postgres" for item in firing)
