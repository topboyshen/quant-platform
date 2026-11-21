"""P5 split guard: the historical ``qp.signals.chain`` import surface stays intact."""

from __future__ import annotations

import qp.signals.chain as chain


def test_facade_exports_every_declared_name():
    missing = [name for name in chain.__all__ if not hasattr(chain, name)]
    assert missing == []


def test_facade_reexports_from_split_modules():
    assert chain.register_run.__module__ == "qp.signals.chain_writer"
    assert chain.append_outcome_revision.__module__ == "qp.signals.chain_writer"
    assert chain.build_snapshot.__module__ == "qp.signals.chain_snapshot"
    assert chain.decision_uid.__module__ == "qp.signals.chain_base"
    assert chain._ensure_policies.__module__ == "qp.signals.chain_base"
    assert chain._uid.__module__ == "qp.signals.chain_base"
