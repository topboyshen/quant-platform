"""Resend suppression and evidence confirmation are separate answers (offline)."""
import json
import unittest
from unittest.mock import Mock

import test_research_relay as base

relay = base.relay
DAY = base.DAY
HASH = "c" * 64


def full_ack(*_args):
    return {"status": "confirmed", "reason": "accepted", "receiver_storage": "receiver_reported_stored",
            "payload_sha256": HASH, "receiver_content_sha256": HASH, "receiver_record_id": "rec-1",
            "content_match": True}


class ConfirmationSplitTests(unittest.TestCase):
    def setUp(self):
        self.h = base.RelayTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.config()
        self.state = self.h.bus / f"out/relay-delivery/{DAY}.json"

    def send_once(self, sender):
        return self.h.execute(sender=sender)

    def rewrite(self, **changes):
        record = json.loads(self.state.read_text())
        record.update(changes)
        for key, value in list(changes.items()):
            if value is None:
                record.pop(key)
        self.state.write_text(json.dumps(record))
        return self.state.read_bytes()

    def again(self):
        sender = Mock(side_effect=AssertionError("must never resend"))
        result = self.h.execute(sender=sender)
        self.assertEqual(sender.call_count, 0)
        self.assertEqual(result["sent"], 0)
        return result

    def test_full_evidence_record_is_already_confirmed(self):
        self.assertEqual(self.send_once(Mock(side_effect=full_ack))["status"], "confirmed")
        result = self.again()
        self.assertEqual(result["status"], "already_confirmed")
        self.assertTrue(result["confirmed"])

    def test_confirmed_record_with_hash_conflict_is_not_confirmed(self):
        self.send_once(Mock(side_effect=full_ack))
        before = self.rewrite(receiver_content_sha256="d" * 64)
        result = self.again()
        self.assertEqual(result["status"], "already_recorded_unverified")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "payload_hash_mismatch")
        self.assertEqual(self.state.read_bytes(), before, "evidence file must not be rewritten")

    def test_confirmed_record_missing_receipt_id_is_not_confirmed(self):
        self.send_once(Mock(side_effect=full_ack))
        self.rewrite(receiver_record_id=None)
        result = self.again()
        self.assertEqual(result["status"], "already_recorded_unverified")
        self.assertEqual(result["reason"], "receiver_id_missing")

    def test_confirmed_record_missing_receiver_hash_is_not_confirmed(self):
        self.send_once(Mock(side_effect=full_ack))
        self.rewrite(receiver_content_sha256=None, receiver_storage=None)
        result = self.again()
        self.assertEqual(result["status"], "already_recorded_unverified")
        self.assertFalse(result["confirmed"])

    def test_new_send_without_proof_reports_confirmed_unverified_and_never_resends(self):
        first = self.send_once(Mock(return_value={"status": "confirmed", "reason": "accepted"}))
        self.assertEqual(first["status"], "confirmed_unverified")
        self.assertFalse(first["confirmed"])
        self.assertEqual(self.again()["status"], "already_recorded_unverified")

    def test_ambiguous_record_never_auto_resends(self):
        self.send_once(Mock(side_effect=full_ack))
        self.rewrite(status="ambiguous", reason="transport_result_unknown")
        result = self.again()
        self.assertEqual((result["status"], result["reason"]), ("held", "delivery_requires_manual_review"))

    def test_query_answer_with_other_hash_does_not_upgrade(self):
        self.send_once(Mock(return_value={"status": "accepted", "reason": "http_accepted_storage_unconfirmed",
                                          "ack_scope": "http_accepted", "payload_sha256": HASH}))
        before = self.state.read_bytes()

        def query(config, target, now, expected, data_date=None):
            return {"status": "confirmed", "receiver_content_sha256": "d" * 64, "receiver_record_id": "rec-x",
                    "receiver_storage": "receiver_reported_stored", "content_match": True}
        sender = Mock(side_effect=AssertionError("must never resend"))
        result = self.h.execute(sender=sender, query=query)
        self.assertEqual(result["status"], "already_accepted")
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.state.read_bytes(), before)


class SharedDecisionTests(unittest.TestCase):
    expected = {"report_date": DAY, "report_sha256": "a" * 64, "data_date": "2026-09-08"}

    def record(self, **kw):
        value = {"version": 1, "target_date": DAY, "as_of": "2026-09-08", "report_sha256": "a" * 64,
                 "status": "confirmed", "reason": "accepted", "receiver_storage": "receiver_reported_stored",
                 "payload_sha256": HASH, "receiver_content_sha256": HASH, "receiver_record_id": "r",
                 "content_match": True}
        value.update(kw)
        return value

    def decide(self, record):
        return relay._DELIVERY.relay_delivery_decision(record, **self.expected)

    def test_matrix(self):
        cases = [
            (None, False, False, False),
            (self.record(), True, True, False),
            (self.record(receiver_content_sha256="d" * 64), True, False, False),
            (self.record(report_sha256="b" * 64), True, False, False),
            (self.record(status="ambiguous"), True, False, True),
            (self.record(status="sending"), True, False, True),
            (self.record(status="accepted"), True, False, False),
            (self.record(status="failed"), True, False, False),
            (self.record(status="not_sent"), False, False, False),
        ]
        for record, hold, confirmed, manual in cases:
            with self.subTest(status=(record or {}).get("status")):
                decision = self.decide(record)
                self.assertEqual(decision["doNotResend"], hold)
                self.assertEqual(decision["evidenceConfirmed"], confirmed)
                self.assertEqual(decision["manualReviewRequired"], manual)

    def test_chain_alert_entry_uses_same_proof(self):
        # The chain alert and run_relay must agree on the same record.
        bad = self.record(receiver_content_sha256="d" * 64)
        ok, _, _ = relay.local_delivery_is_confirmed(bad, **self.expected)
        self.assertEqual(ok, self.decide(bad)["evidenceConfirmed"])


if __name__ == "__main__":
    unittest.main()
