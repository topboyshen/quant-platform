"""Macro-risk gate for bullish swing entries; fail-closed on unknown/missing state."""
from __future__ import annotations

GROWTH_RISK_SECTORS = {"成长", "硬科技", "港股科技"}
GROWTH_RISK_KEYWORDS = ("半导体", "芯片", "科技", "创业", "科创")
BLOCK_ALL_LEVELS = {"critical", "warning", "unavailable"}


def is_growth_risk_target(sector: str | None, name: str | None) -> bool:
    sector = str(sector or "")
    name = str(name or "")
    return sector in GROWTH_RISK_SECTORS or any(keyword in name for keyword in GROWTH_RISK_KEYWORDS)


def gate_action(level: str | None, sector: str | None, name: str | None) -> str:
    """Return 'allowed' or 'blocked' for one triggered entry signal."""
    normalized = str(level or "unavailable").lower()
    if normalized == "ok":
        return "allowed"
    if normalized == "growth_blocked":
        return "blocked" if is_growth_risk_target(sector, name) else "allowed"
    if normalized in BLOCK_ALL_LEVELS:
        return "blocked"
    return "blocked"
