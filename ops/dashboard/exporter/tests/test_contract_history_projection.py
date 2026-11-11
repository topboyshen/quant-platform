"""Read-only bridge from a real audit fixture; no databases or business runs.

Fixture originates from data/tests/test_essential_core.py IntervalAuditTests,
using data_health.audit against fixed Qlib/SQLite. It is copied byte-for-byte
from data/evidence/core-to-full-audit-fixture.json, not hand-authored success.
"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("history_exporter", ROOT / "export_dashboard.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
FIXTURE = json.loads((ROOT.parent / "fixtures/core-to-full-audit-fixture.json").read_text())


class ContractHistoryProjectionTests(unittest.TestCase):
    def test_full_resume_preserves_core_history_without_current_full_endorsement(self):
        errors = []
        before = copy.deepcopy(FIXTURE)
        result = exporter.contract_evidence(FIXTURE, errors)
        self.assertEqual(errors, [])
        self.assertEqual(result["activeContract"], exporter.FULL_CONTRACT)
        self.assertEqual(result["activeRunDecision"]["verification"]["status"], "pass")
        self.assertEqual(result["contractIntervals"], FIXTURE["contractIntervals"])
        self.assertIsNone(result["legacyFullSnapshot"]["scheduledBefore"])
        self.assertEqual(result["legacyFullSnapshot"]["intervals"], FIXTURE["legacyFullSnapshot"]["intervals"])
        core = result["coreMarket"]
        self.assertTrue(core["historicalOnly"])
        self.assertEqual(core["status"], "insufficient")
        self.assertIsNone(core["quality"])
        self.assertEqual(core["lastRun"]["status"], "unknown")
        self.assertNotEqual(core["lastRun"], result["activeLastRun"])
        self.assertEqual(core["ledger"][0]["date"], "2026-10-08")
        self.assertEqual(core["ledger"][0]["payloadSha256"], FIXTURE["coreMarket"]["rowHashes"]["2026-10-08"])
        self.assertFalse(core["ledger"][0]["fullSnapshot"])
        self.assertEqual(FIXTURE, before)
        projected = json.loads((ROOT.parent / "fixtures/core-to-full-projection.json").read_text())
        self.assertEqual(projected, {**result, "sourceTime": FIXTURE["generatedAt"]})
        # Core evidence cannot backfill the separate full archive.
        full = next(row for row in FIXTURE["ledger"] if row["tradeDate"] == "2026-10-08")
        self.assertEqual(full["snapshotState"], "missing")
        self.assertIs(full["expectedUnderContract"], False)

    def test_full_failure_and_core_invalid_row_remain_independent(self):
        health = copy.deepcopy(FIXTURE)
        health["activeRunDecision"]["verification"].update(status="fail", reasons=["snapshot_missing"])
        health["coreMarket"]["ledger"][0]["coreState"] = "invalid"
        result = exporter.contract_evidence(health, [])
        self.assertEqual(result["activeRunDecision"]["verification"]["status"], "fail")
        self.assertEqual(result["coreMarket"]["ledger"][0]["state"], "invalid")
        self.assertEqual(result["coreMarket"]["validDates"], 0)
        self.assertNotEqual(result["coreMarket"]["status"], "pass")

    def test_malformed_interval_is_explicitly_unavailable_not_raw_passthrough(self):
        for replacement in ({}, [{"activeContract": []}], [{"activeContract": exporter.CORE_CONTRACT,
                "fromInclusive": "2026-10-08"}], [{"activeContract": exporter.CORE_CONTRACT,
                "fromInclusive": "invalid", "untilExclusive": None}]):
            with self.subTest(intervals=replacement):
                health, errors = copy.deepcopy(FIXTURE), []
                health["contractIntervals"] = replacement
                result = exporter.contract_evidence(health, errors)
                self.assertIsNone(result["contractIntervals"])
                self.assertIn("contract_intervals_projection_invalid", errors)
                self.assertTrue(result["coreMarket"]["historicalOnly"])

    def test_full_only_old_audit_does_not_invent_core_history_or_intervals(self):
        health = copy.deepcopy(FIXTURE)
        health.pop("contractIntervals")
        health["effectiveTradeDate"] = None
        result = exporter.contract_evidence(health, [])
        self.assertIsNone(result["coreMarket"])
        self.assertNotIn("contractIntervals", result)


if __name__ == "__main__":
    unittest.main()
