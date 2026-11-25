"""Return-role classification does not treat an unlabeled fill as executable."""

from qp.signals.return_roles import (
    EXECUTABLE,
    UNKNOWN,
    executable_net_return,
    resolve_role,
    role_for_decision,
)
from qp.signals.rotation_shadow import _cap_increases


def test_old_filled_row_without_basis_is_not_executable():
    role = resolve_role(details={"fill_reason": "next_session_open"})
    assert role == UNKNOWN
    assert executable_net_return(role, "filled", 0.08) is None


def test_decision_state_is_a_basis_without_rewriting_the_row():
    assert role_for_decision("allowed", "allowed") == EXECUTABLE
    assert role_for_decision("no_trigger", "not_applicable") == "research_counterfactual"
    assert role_for_decision("data_failed", "not_applicable") == "research_counterfactual"
    assert role_for_decision("blocked", "blocked") == "counterfactual_ungated"
    assert role_for_decision(None, None) == UNKNOWN


def test_rule_shadow_table_is_counterfactual_even_without_a_stamp():
    role = resolve_role(details={}, source="rule_shadow")
    assert role == "counterfactual_ungated"
    assert executable_net_return(role, "filled", 0.041) is None


def test_cap_blocks_new_and_added_shares_but_keeps_reductions():
    desired = {"held": 200, "new": 100, "reduce": 50}
    shares = {"held": 100, "reduce": 80}
    capped = _cap_increases(desired, shares, {"held", "new", "reduce"})
    assert capped["held"] == 100
    assert "new" not in capped
    assert capped["reduce"] == 50
