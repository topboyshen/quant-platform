"""Current receiver's pinned pure validator, dated fixtures, fake POST only."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

import test_research_relay_digest as digest_cases
import test_calendar_skip_relay as calendar_cases

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE = FIXTURES / "research_contract_3fafc74e.py"
IDENTITY = json.loads((FIXTURES / "RECEIVER_IDENTITY.json").read_text())
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != IDENTITY["sha256"]:
    raise RuntimeError("pinned receiver validator identity changed")
SPEC = importlib.util.spec_from_file_location("receiver_3fafc74e_QA", SOURCE)
receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver)


class ReceiverCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.case = digest_cases.DigestConsumerTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.now = digest_cases.datetime(2026, 9, 24, 8, 5, tzinfo=digest_cases.SHANGHAI)

    def send_through_validator(self):
        sent = []
        fake = self.case.sender(sent)
        def sender(payload, config, now):
            receiver.validate(payload, now)
            return fake(payload, config, now)
        outcome = digest_cases.relay.run_relay(self.case.project, self.case.bus,
                                               now=self.now, sender=sender)
        self.assertEqual(outcome["status"], "confirmed", outcome)
        self.assertEqual(len(sent), 1)
        return self.case.last_payload

    def test_real_payload_inference_with_no_counter_is_accepted(self):
        payload = self.send_through_validator()
        self.assertEqual(payload["research_summary"]["analysis_contract"]["counter_evidence_status"],
                         "explicitly_missing")
        self.assertEqual(payload["research_summary"]["counter_evidence"], [])

    def test_real_payload_facts_with_zero_items_is_accepted(self):
        self.case.write_digest(digest_cases.digest_body(
            report_sha=self.case.report_sha, input_sha=self.case.input_sha, items=[]))
        payload = self.send_through_validator()
        self.assertNotIn("analysis_contract", payload["research_summary"])
        self.assertIn("[事实]", payload["research_summary"]["conclusion"])

    def test_resealed_label_change_remains_compatible(self):
        self.case.report = self.case.report.replace("支持证据（成交额）：", "佐证（成交额）：")
        self.case.report_sha = hashlib.sha256(self.case.report.encode()).hexdigest()
        (self.case.project / f"reports/daily/{digest_cases.DAY}.md").write_text(self.case.report)
        self.case.write_digest()
        self.send_through_validator()

    def test_receiver_rejects_wrong_roles_period_dates_and_missing_metadata(self):
        payload = digest_cases.relay.prepare_research(self.case.project, self.now)["payload"]
        for change in ("role", "period", "same_day", "future_day", "missing_metadata"):
            with self.subTest(change=change), self.assertRaises(ValueError):
                altered = copy.deepcopy(payload)
                analysis = altered["research_summary"]["analysis_contract"]
                if change == "role":
                    analysis["evidence_roles"][0]["role"] = "未知角色"
                elif change == "period":
                    analysis["period_note"] = "利润同比报告期 2026-06-30"
                elif change in ("same_day", "future_day"):
                    day = digest_cases.DAY if change == "same_day" else "2026-09-25"
                    altered["as_of"] = altered["research_contract"]["as_of"] = day
                else:
                    altered["research_summary"].pop("analysis_contract")
                receiver.validate(altered, self.now)

    def test_signed_calendar_skip_same_day_is_a_separate_accepted_contract(self):
        case = calendar_cases.CalendarSkipTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        prepared = case.prepare()
        accepted = receiver.validate(prepared["payload"], calendar_cases.NOW)
        self.assertEqual(accepted["kind"], "skip")
        self.assertEqual(accepted["as_of"], calendar_cases.DAY)


if __name__ == "__main__":
    unittest.main()
