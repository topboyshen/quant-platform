"""Separate executable strategy returns from research returns.

Classification never rewrites stored rows. A new write may stamp
``details.return_role``; an old row is classified at read time from a concrete
basis (decision state, risk action, or the table that owns it). A filled number
with no basis stays ``unknown`` and is not an executable return.
"""

from __future__ import annotations

from typing import Any

EXECUTABLE = "executable"
COUNTERFACTUAL_UNGATED = "counterfactual_ungated"
RESEARCH_COUNTERFACTUAL = "research_counterfactual"
UNKNOWN = "unknown"

ROLES = frozenset({EXECUTABLE, COUNTERFACTUAL_UNGATED, RESEARCH_COUNTERFACTUAL, UNKNOWN})
ROLE_LABELS = {
    EXECUTABLE: "可执行",
    COUNTERFACTUAL_UNGATED: "未过闸反事实",
    RESEARCH_COUNTERFACTUAL: "非信号研究",
    UNKNOWN: "无依据",
}


def role_for_decision(decision_state: str | None, risk_action: str | None = None) -> str:
    """Classify a target-decision outcome. Missing state is not executable."""
    state = str(decision_state or "")
    action = str(risk_action or "")
    if state == "blocked" or action == "blocked":
        return COUNTERFACTUAL_UNGATED
    if state in {"no_trigger", "data_failed"}:
        return RESEARCH_COUNTERFACTUAL
    if state == "allowed" and action == "allowed":
        return EXECUTABLE
    return UNKNOWN


def role_for_chain_row(row: dict[str, Any]) -> str:
    """Read-time role for an attribution row. Does not infer a missing state."""
    risk_state = row.get("risk_state")
    if risk_state == "blocked":
        return COUNTERFACTUAL_UNGATED
    reason = str(row.get("reason_code") or "")
    if row.get("signal_state") == "not_triggered" or reason in {"no_trigger", "data_incomplete"}:
        return RESEARCH_COUNTERFACTUAL
    if (
        row.get("verdict") == "eligible"
        and row.get("signal_state") == "triggered"
        and risk_state in {"allowed", "not_applicable"}
    ):
        return EXECUTABLE
    return UNKNOWN


def resolve_role(
    *,
    details: dict[str, Any] | None = None,
    decision_state: str | None = None,
    risk_action: str | None = None,
    source: str | None = None,
) -> str:
    stamped = (details or {}).get("return_role")
    if stamped in ROLES and stamped != UNKNOWN:
        return str(stamped)
    if source in {"rule_shadow", "shadow"}:
        return COUNTERFACTUAL_UNGATED
    if decision_state is not None or risk_action is not None:
        return role_for_decision(decision_state, risk_action)
    return UNKNOWN


def stamp(details: dict[str, Any] | None, role: str) -> dict[str, Any]:
    if role not in ROLES or role == UNKNOWN:
        raise ValueError(f"cannot stamp return role {role!r}")
    out = dict(details or {})
    out["return_role"] = role
    out["executable"] = role == EXECUTABLE
    return out


def is_executable(role: str | None) -> bool:
    return role == EXECUTABLE


def executable_net_return(role: str | None, fill_status: str | None, net_return: float | None) -> float | None:
    if is_executable(role) and fill_status == "filled" and net_return is not None:
        return float(net_return)
    return None
