"""M4.6 fixed legacy fixtures: manifest integrity and branch coverage metadata."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from sqlalchemy.orm import Session

from qp.shadow.compare import compare_day
from qp.signals.eod import Signal, TargetEvaluation, apply_risk_gate, target_decisions
from qp.signals.universe import TARGETS

FIXTURES = Path(__file__).parent / "fixtures/m4"
DATES = ("2026-08-21", "2026-08-27", "2026-09-24", "2026-10-23", "2026-10-27",
         "2026-11-03", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12")
SENSITIVE_KEYS = ("token", "chat_id", "secret", "password", "authorization")


def _load(day: str) -> tuple[dict, dict]:
    target = FIXTURES / day
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    files = {name: json.loads((target / name).read_text(encoding="utf-8"))
             for name in manifest["files"]}
    return manifest, files


def test_all_contract_dates_present_with_valid_manifests():
    assert all((FIXTURES / day / "manifest.json").is_file() for day in DATES)
    for day in DATES:
        manifest, files = _load(day)
        assert manifest["ok"] is True and manifest["decision_date"] == day
        assert set(manifest["files"]) == {
            "eod_run.json", "target_decisions.json", "risk_snapshot.json",
            "trend_signals.json", "outcomes.json", "macro_slice.json",
            "notifications.json", "risk_state.json", "intake.json", "digest.json"}
        for name, meta in manifest["files"].items():
            raw = (FIXTURES / day / name).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == meta["sha256"], (day, name)
            assert len(raw) == meta["bytes"], (day, name)
        assert files["eod_run.json"]


def test_branch_metadata_matches_legacy_history():
    emitted = json.loads((FIXTURES / "2026-08-21/fixture.json").read_text())
    assert emitted["latest_run"]["decision_outcome"] == "emitted"
    breakout = json.loads((FIXTURES / "2026-08-27/fixture.json").read_text())
    assert breakout["latest_run"]["status"] == "partial"
    assert json.loads((FIXTURES / "2026-08-27/macro_slice.json").read_text())[
        "total_amount"] is None
    retry = json.loads((FIXTURES / "2026-09-24/fixture.json").read_text())
    assert retry["attempts"] == 2
    assert retry["first_run"]["status"] == "failed"
    assert retry["latest_run"]["status"] == "success"
    partial = json.loads((FIXTURES / "2026-10-23/fixture.json").read_text())
    assert (partial["latest_run"]["status"], partial["latest_run"]["decision_outcome"]) == (
        "partial", "data_unavailable")
    assert partial["notifications"] == 2
    three = json.loads((FIXTURES / "2026-10-27/fixture.json").read_text())
    assert three["attempts"] == 3
    calm = json.loads((FIXTURES / "2026-11-03/fixture.json").read_text())
    assert (calm["latest_run"]["status"], calm["latest_run"]["decision_outcome"]) == (
        "success", "no_signal")
    assert calm["target_decisions"] == 40


def test_blocked_days_have_critical_level_and_two_blocked_signals():
    for day in ("2026-11-09", "2026-11-10", "2026-11-11"):
        _, files = _load(day)
        decisions = files["target_decisions.json"]
        assert len(decisions) == 40
        triggered = [row for row in decisions if row["raw_signals"]]
        assert {row["decision_state"] for row in triggered} == {"blocked"}
        assert {row["risk_level"] for row in triggered} == {"critical"}
        assert len(files["trend_signals.json"]) == 2


def test_legacy_decisions_replay_through_qp_eod():
    """The exported legacy decisions are the expectation; qp must reproduce them."""
    by_code = {target.code: target for target in TARGETS}
    seen = set()
    for day in DATES:
        _, files = _load(day)
        latest = files["eod_run.json"][-1]
        snapshot = next(row for row in files["risk_snapshot.json"]
                        if row["run_id"] == latest["run_id"])
        level = snapshot["level"]
        evaluations = []
        for decision in files["target_decisions.json"]:
            signals = [
                Signal(decision["code"], decision["name"], row["signal_type"],
                       row["period"], row["title"], row["detail"], row["confidence"])
                for row in decision["raw_signals"]
            ]
            evaluations.append(TargetEvaluation(
                by_code[decision["code"]], decision["data_status"] == "ok",
                dt.date.fromisoformat(decision["data_date"]) if decision["data_date"] else None,
                decision["reference_close"], "", "", signals))
        blocked = apply_risk_gate(evaluations, level)
        replay = {row["code"]: row for row in target_decisions(evaluations, blocked, level)}
        for expected in files["target_decisions.json"]:
            actual = replay[expected["code"]]
            for field in ("data_status", "decision_state", "risk_action", "reason_code",
                          "risk_level", "raw_trigger_count"):
                assert actual[field] == expected[field], (day, expected["code"], field)
            assert {row["signal_type"] for row in actual["raw_signals"]} == {
                row["signal_type"] for row in expected["raw_signals"]}
            if expected["raw_signals"]:
                seen.add(expected["decision_state"])
    assert seen == {"allowed", "blocked"}


def test_compare_accepts_real_fixture_manifest(engine):
    """A real exported fixture must reach the C1+C checks instead of EXPORT BLOCKED."""
    with Session(engine) as session:
        result = compare_day(session, dt.date(2026, 8, 21), FIXTURES)
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["EXPORT"]["status"] == "PASS"
    assert checks["C1"]["status"] == "BLOCKED"  # no qp run for this historical date
    assert result["overall"] == "BLOCKED"


def test_fixtures_have_no_secret_values():
    for day in DATES:
        for name in ("notifications.json", "intake.json"):
            text = (FIXTURES / day / name).read_text(encoding="utf-8")
            blob = json.loads(text)

            def walk(value, _day=day, _name=name):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if any(part in str(key).lower() for part in SENSITIVE_KEYS):
                            assert item in ("[redacted]", None, ""), (_day, _name, key)
                        walk(item)
                elif isinstance(value, list):
                    for item in value:
                        walk(item)

            walk(blob)
