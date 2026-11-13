"""M8.4 morning-chain classification and legacy atom comparison.

qp derives the expected failure atoms from the stage artifacts and compares them
with the legacy alert state.  Atoms use the legacy ``date|code|reason`` shape so
the two sides are directly comparable.
"""
from __future__ import annotations

from typing import Any


def _atom(day: str, code: str, reason: str = "") -> str:
    return f"{day}|{code}|{reason}"


def _parse(atom: Any) -> tuple[str, str] | None:
    if not isinstance(atom, str):
        return None
    parts = atom.split("|")
    if len(parts) != 3 or not parts[1]:
        return None
    return parts[1], parts[2]


def expected_atoms(payload: dict[str, Any]) -> list[str]:
    day = str(payload.get("date") or "")
    generation = payload.get("generation") or {}
    seal = payload.get("seal") or {}
    relay = payload.get("relay") or {}
    precollect = payload.get("precollect") or {}
    atoms: list[str] = []

    generation_ok = (bool(generation) and generation.get("status") == "succeeded"
                     and generation.get("child_exit_code") in (0, None))
    if not generation_ok:
        atoms.append(_atom(day, "generation_failed"))
    elif (not seal or seal.get("state") != "SEALED"
          or seal.get("target_matches") is False
          or seal.get("report_sha_matches") is False
          or seal.get("digest_present") is not True):
        atoms.append(_atom(day, "seal_invalid"))
    # relay can be blocked by a stale digest even when generation failed
    attempt = relay.get("attempt") or {}
    delivery = relay.get("delivery") or {}
    if attempt and attempt.get("status") == "held":
        atoms.append(_atom(day, "relay_blocked", str(attempt.get("reason") or "")))
    elif not attempt and generation_ok:
        atoms.append(_atom(day, "relay_missing"))
    if delivery and (delivery.get("status") != "confirmed"
                     or delivery.get("content_match") is False):
        atoms.append(_atom(day, "relay_unconfirmed"))
    if precollect and precollect.get("payload_sha256_ok") is False:
        atoms.append(_atom(day, "precollect_invalid"))
    return sorted(set(atoms))


def legacy_atoms(payload: dict[str, Any]) -> list[str]:
    alerts = payload.get("alerts") or {}
    atoms = alerts.get("business_atoms") if isinstance(alerts.get("business_atoms"), list) else []
    out = []
    for atom in atoms:
        parsed = _parse(atom)
        if parsed is not None:
            out.append(_atom(str(payload.get("date") or ""), parsed[0], parsed[1]))
    return sorted(set(out))


def classify_chain(payload: dict[str, Any]) -> dict[str, Any]:
    expected = expected_atoms(payload)
    legacy = legacy_atoms(payload)
    expected_set, legacy_set = set(expected), set(legacy)
    return {
        "state": "healthy" if not expected else "degraded",
        "expected_atoms": expected,
        "legacy_atoms": legacy,
        "missing_in_legacy": sorted(expected_set - legacy_set),
        "extra_in_legacy": sorted(legacy_set - expected_set),
        "matches_legacy": expected_set == legacy_set,
    }
