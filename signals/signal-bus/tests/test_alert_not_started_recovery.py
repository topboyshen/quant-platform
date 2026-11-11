"""A send that provably never started must not freeze the day's alerting (offline)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_research_chain_alert as base

rca = base.rca
DATE, PRE = base.DATE, base.PRE


class NotStartedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.items = rca.classify(base.facts(occurrence=base.occurrence("failed", 1)))

    def saved(self):
        return json.loads((self.state / f"research-chain-alert-{DATE}.json").read_text())

    def test_missing_chat_config_is_not_sent_without_spawning(self):
        with patch.object(rca, "EXPECTED_CHAT", ""), \
             patch.object(rca.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            outcome, detail, _ = rca.HermesSender().send("x")
        self.assertEqual(outcome, "not_sent")
        self.assertEqual(detail, "not_started:expected_chat_not_configured")

    def test_unexecutable_sender_is_not_sent(self):
        with patch.object(rca, "EXPECTED_CHAT", "123"):
            outcome, detail, _ = rca.HermesSender(python=Path("/nonexistent/python")).send("x")
        self.assertEqual(outcome, "not_sent")
        self.assertTrue(detail.startswith("not_started:spawn_failed:"))

    def test_timeout_after_start_stays_uncertain(self):
        script = self.state / "slow-python"
        script.write_text("#!/bin/sh\nexec sleep 5\n")
        script.chmod(0o700)
        with patch.object(rca, "EXPECTED_CHAT", "123"):
            outcome, detail, _ = rca.HermesSender(python=script, timeout=1).send("x")
        self.assertEqual((outcome, detail), ("uncertain", "send_timeout"))

    def test_config_error_then_fix_sends_same_day(self):
        broken = base.SenderFake(("not_sent", "not_started:expected_chat_not_configured", {}))
        first = rca.process(self.items, self.state, PRE, date=DATE, sender=broken)
        self.assertEqual(first["paused"], 0)
        self.assertFalse(self.saved().get("uncertainSend", False))
        fixed = base.SenderFake()
        second = rca.process(self.items, self.state, PRE, date=DATE, sender=fixed)
        self.assertEqual(second["notified"], 1)
        self.assertEqual(len(fixed.sent), 1)
        attempts = self.saved()["transition"]["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["failed", "confirmed"])
        self.assertTrue(attempts[0]["notStarted"])

    def test_not_started_retries_are_bounded(self):
        broken = base.SenderFake(("not_sent", "not_started:expected_chat_not_configured", {}))
        for _ in range(rca.MAX_NOT_STARTED_ATTEMPTS + 3):
            rca.process(self.items, self.state, PRE, date=DATE, sender=broken)
        self.assertEqual(len(broken.sent), rca.MAX_NOT_STARTED_ATTEMPTS)

    def test_not_started_does_not_consume_definite_failure_budget(self):
        broken = base.SenderFake(("not_sent", "not_started:expected_chat_not_configured", {}))
        rca.process(self.items, self.state, PRE, date=DATE, sender=broken)
        rca.process(self.items, self.state, PRE, date=DATE, sender=broken)
        failing = base.SenderFake(("failed", "rejected_http_401", {}))
        for _ in range(3):
            rca.process(self.items, self.state, PRE, date=DATE, sender=failing)
        self.assertEqual(len(failing.sent), rca.MAX_ATTEMPTS)

    def write_legacy(self, detail, evidence):
        rca.process(self.items, self.state, PRE, date=DATE, sender=base.SenderFake(("uncertain", detail, evidence)))
        self.assertTrue(self.saved()["uncertainSend"])
        state = self.saved()
        state.pop("uncertainLedger", None)  # state written before this fix
        (self.state / f"research-chain-alert-{DATE}.json").write_text(json.dumps(state))

    def test_legacy_pre_send_config_uncertain_is_reclassified_and_unpaused(self):
        self.write_legacy("expected_chat_not_configured", {})
        fixed = base.SenderFake()
        summary = rca.process(self.items, self.state, PRE, date=DATE, sender=fixed)
        self.assertEqual(summary["paused"], 0)
        self.assertEqual(len(fixed.sent), 1)
        self.assertIn("not_started_reclassified", summary["problems"])

    def test_post_ack_config_mismatch_stays_paused(self):
        # The CLI ran and returned an ack: the message may have been delivered.
        self.write_legacy("expected_chat_not_configured", {"chat_id": "9", "exit": 0})
        fixed = base.SenderFake()
        summary = rca.process(self.items, self.state, PRE, date=DATE, sender=fixed)
        self.assertEqual(summary["paused"], 1)
        self.assertEqual(fixed.sent, [])

    def test_genuine_uncertain_still_pauses(self):
        rca.process(self.items, self.state, PRE, date=DATE, sender=base.SenderFake(("uncertain", "no_ack_exit_0", {})))
        fixed = base.SenderFake()
        summary = rca.process(self.items, self.state, PRE, date=DATE, sender=fixed)
        self.assertEqual(summary["paused"], 1)
        self.assertEqual(fixed.sent, [])

    def test_state_written_by_new_code_is_readable_by_old_schema(self):
        broken = base.SenderFake(("not_sent", "not_started:expected_chat_not_configured", {}))
        rca.process(self.items, self.state, PRE, date=DATE, sender=broken)
        for attempt in self.saved()["transition"]["attempts"]:
            self.assertIn(attempt["outcome"], ("sending", "confirmed", "failed", "uncertain"))


if __name__ == "__main__":
    unittest.main()
