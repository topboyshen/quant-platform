"""A core contract scheduled for the future is not history (offline, fixture-based)."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("not_started_exporter", ROOT / "export_dashboard.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
FIXTURE = json.loads((ROOT.parent / "fixtures/core-to-full-audit-fixture.json").read_text())


def before_core(generated="2026-10-03T22:45:00+08:00"):
    health = copy.deepcopy(FIXTURE)
    health["generatedAt"] = generated
    health["contractIntervals"] = [{"activeContract": exporter.CORE_CONTRACT,
                                    "fromInclusive": "2026-10-08", "untilExclusive": None}]
    health["legacyFullSnapshot"]["intervals"] = [{"fromInclusive": None, "untilExclusive": "2026-10-08"}]
    return health


class CoreNotStartedTests(unittest.TestCase):
    def test_future_core_contract_is_not_labelled_historical(self):
        result = exporter.contract_evidence(before_core(), [])
        self.assertIsNone(result["coreMarket"])
        self.assertEqual(result["coreContractSchedule"],
                         {"contract": exporter.CORE_CONTRACT, "fromInclusive": "2026-10-08", "status": "not_started"})

    def test_effective_date_fallback_when_intervals_absent(self):
        health = before_core()
        health.pop("contractIntervals")
        result = exporter.contract_evidence(health, [])
        self.assertIsNone(result["coreMarket"])
        self.assertEqual(result["coreContractSchedule"]["status"], "not_started")

    def test_after_core_ran_it_is_history(self):
        result = exporter.contract_evidence(copy.deepcopy(FIXTURE), [])
        self.assertTrue(result["coreMarket"]["historicalOnly"])
        self.assertIsNone(result.get("coreContractSchedule"))

    def test_start_day_itself_counts_as_started(self):
        result = exporter.contract_evidence(before_core("2026-10-08T23:00:00+08:00"), [])
        self.assertTrue(result["coreMarket"]["historicalOnly"])


if __name__ == "__main__":
    unittest.main()
