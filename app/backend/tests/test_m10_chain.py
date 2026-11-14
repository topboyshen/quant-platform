"""M10 record chain schema: tables, views, append-only guards."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

TABLES = {
    'chain_policy', 'chain_universe', 'chain_universe_member', 'chain_batch', 'chain_decision',
    'chain_attempt', 'chain_state', 'chain_outcome', 'chain_eligibility', 'chain_snapshot',
    'chain_snapshot_supersede', 'chain_snapshot_item', 'chain_evidence_link',
    'chain_import_register',
}
VIEWS = {
    'chain_decision_canonical_attempt', 'chain_decision_current_state',
    'chain_decision_current_outcome', 'chain_decision_current_eligibility',
}


def _names(engine, kind: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "select table_name from information_schema.tables "
            f"where table_schema='signals' and table_type='{kind}' and table_name like 'chain_%'"))}


def test_chain_tables_and_views_exist(engine):
    assert _names(engine, 'BASE TABLE') == TABLES
    assert _names(engine, 'VIEW') == VIEWS


def test_chain_rows_are_append_only(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text(
            "insert into signals.chain_evidence_link "
            "(link_id, owner_kind, owner_id, sidecar_kind, sidecar_path, db_state, sidecar_state,"
            " checked_at, created_at) values "
            "(:id, 'decision', 'qd_probe', 'outcome_input', '/tmp/probe', 'present', 'pending',"
            " :now, :now)"), {"id": "ev_" + "0" * 32, "now": now})
        with pytest.raises(DBAPIError):
            conn.execute(text("update signals.chain_evidence_link set db_state='missing'"))
        trans.rollback()
