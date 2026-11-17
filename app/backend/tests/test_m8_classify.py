"""M8.4 chain classification: expected atoms vs legacy alert atoms."""

from __future__ import annotations

from qp.morning.classify import classify_chain, expected_atoms

DAY = "2026-11-13"


def healthy_payload() -> dict:
    return {
        "date": DAY,
        "precollect": {"payload_sha256_ok": True},
        "generation": {"status": "succeeded", "child_exit_code": 0},
        "seal": {"state": "SEALED", "target_matches": True, "report_sha_matches": True, "digest_present": True},
        "relay": {
            "attempt": {"status": "sent", "reason": ""},
            "delivery": {"status": "confirmed", "content_match": True},
        },
        "alerts": {"business_atoms": []},
    }


def failed_payload() -> dict:
    payload = healthy_payload()
    payload["generation"] = {"status": "failed", "child_exit_code": 1}
    payload["relay"] = {"attempt": {"status": "held", "reason": "timestamp_invalid"}, "delivery": None}
    payload["alerts"] = {"business_atoms": [f"{DAY}|generation_failed|", f"{DAY}|relay_blocked|timestamp_invalid"]}
    return payload


def test_healthy_chain_has_no_atoms():
    payload = healthy_payload()
    assert expected_atoms(payload) == []
    result = classify_chain(payload)
    assert result["state"] == "healthy"
    assert result["matches_legacy"] is True


def test_failed_chain_matches_legacy_atoms():
    payload = failed_payload()
    result = classify_chain(payload)
    assert result["state"] == "degraded"
    assert result["expected_atoms"] == [f"{DAY}|generation_failed|", f"{DAY}|relay_blocked|timestamp_invalid"]
    assert result["legacy_atoms"] == result["expected_atoms"]
    assert result["matches_legacy"] is True


def test_missing_legacy_atom_is_reported():
    payload = failed_payload()
    payload["alerts"] = {"business_atoms": [f"{DAY}|generation_failed|"]}
    result = classify_chain(payload)
    assert result["matches_legacy"] is False
    assert f"{DAY}|relay_blocked|timestamp_invalid" in result["missing_in_legacy"]


def test_seal_and_relay_checks_only_when_generation_ok():
    payload = healthy_payload()
    payload["seal"] = {"state": "MISSING", "digest_present": False}
    assert expected_atoms(payload) == [f"{DAY}|seal_invalid|"]

    payload = healthy_payload()
    payload["relay"] = {"attempt": None, "delivery": None}
    assert expected_atoms(payload) == [f"{DAY}|relay_missing|"]

    payload = healthy_payload()
    payload["relay"] = {"attempt": {"status": "sent"}, "delivery": {"status": "pending", "content_match": None}}
    assert expected_atoms(payload) == [f"{DAY}|relay_unconfirmed|"]
