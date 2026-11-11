"""Pure fixed-file/stub regressions. Never calls a production sender or endpoint."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import contextlib
import io
import test_research_chain_alert as fixtures

from test_research_chain_alert import (DATE, POST, PRE, REPORT_SHA, SUCCESS_EVIDENCE,
                                      SenderFake, facts, occurrence, rca, receipt, seal)


def incident(code="relay_blocked", reason="research_seal_missing", detail="fixed"):
    return [{"key": f"{DATE}:research_chain", "date": DATE,
             "causes": [{"code": code, "reason": reason, "detail": detail}],
             "delivery_wording": "未确认", "delivery_state": "unconfirmed"}]


class TransitionRepairTests(unittest.TestCase):
    def test_legacy_definite_failures_do_not_reset_retry_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, sender = Path(tmp), SenderFake()
            old = {"version": 2, "incident": {"key": f"{DATE}:research_chain", "date": DATE,
                   "causes": incident()[0]["causes"],
                   "attempts": [{"outcome": "failed", "at": PRE.isoformat()} for _ in range(2)]}}
            (state / f"research-chain-alert-{DATE}.json").write_text(json.dumps(old))
            result = rca.process(incident(), state, POST, date=DATE, sender=sender)
            self.assertEqual(result["notified"], 0)
            self.assertEqual(sender.sent, [])

    def test_same_atoms_ignore_time_runid_and_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, sender = Path(tmp), SenderFake()
            first = rca.process(incident(), state, PRE, date=DATE, sender=sender)
            second = rca.process(incident(detail="different runId and audit time"), state, POST, date=DATE, sender=sender)
            self.assertEqual(first["notified"], 1)
            self.assertEqual(second["notified"], 0)
            self.assertEqual(len(sender.sent), 1)

    def test_new_same_stage_reason_and_generation_failure_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, sender = Path(tmp), SenderFake()
            for item in (incident(), incident(reason="digest_hash_mismatch"),
                         incident("generation_failed", "", "new generation failure")):
                self.assertEqual(rca.process(item, state, POST, date=DATE, sender=sender)["notified"], 1)
            self.assertEqual(len(sender.sent), 3)

    def test_old_state_blindness_preserves_atoms_then_only_proven_recovery_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, sender = Path(tmp), SenderFake()
            target = state / f"research-chain-alert-{DATE}.json"
            old = {"version": 2, "notified_once": True,
                   "incident": {"key": f"{DATE}:research_chain", "date": DATE,
                                "causes": incident()[0]["causes"],
                                "attempts": [{"outcome": "confirmed", "at": PRE.isoformat()}]}}
            target.write_text(json.dumps(old))
            rca.process([], state, POST, date=DATE, sender=sender, success=False)
            during = json.loads(target.read_text())
            self.assertTrue(during["businessAtoms"])
            self.assertIsNotNone(during["incident"])
            rca.process(incident(), state, POST, date=DATE, sender=sender)
            self.assertEqual(sender.sent, [])
            # An incomplete success hint is not delivery evidence.
            rca.process([], state, POST, date=DATE, sender=sender, success=True,
                        success_evidence={"seal_bound": True, "delivery_state": "confirmed"})
            self.assertTrue(json.loads(target.read_text())["businessAtoms"])
            self.assertEqual(sender.sent, [])
            recovered = rca.process([], state, POST, date=DATE, sender=sender, success=True,
                                    success_evidence=SUCCESS_EVIDENCE)
            self.assertEqual(recovered["recovered"], 1)
            self.assertEqual(recovered["notified"], 1)
            self.assertIn("恢复", sender.sent[0])
            self.assertEqual(json.loads(target.read_text())["businessAtoms"], [])
            rca.process([], state, POST, date=DATE, sender=sender, success=True,
                        success_evidence=SUCCESS_EVIDENCE)
            self.assertEqual(len(sender.sent), 1)

    def test_partial_recovery_needs_positive_stage_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, sender = Path(tmp), SenderFake()
            combined = incident()
            combined[0]["causes"] += incident("generation_failed", "")[0]["causes"]
            rca.process(combined, state, PRE, date=DATE, sender=sender)
            rca.process([], state, POST, date=DATE, sender=sender,
                        success_evidence={"stages": {"relay": True}})
            saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
            self.assertEqual(saved["businessAtoms"], [f"{DATE}|generation_failed|"])
            self.assertEqual(len(sender.sent), 2)

    def test_uncertain_previous_alert_pauses_even_new_change_without_resending(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            uncertain = SenderFake(("uncertain", "no_ack", {}))
            rca.process(incident(), state, PRE, date=DATE, sender=uncertain)
            sender = SenderFake()
            result = rca.process(incident("generation_failed", ""), state, POST, date=DATE, sender=sender)
            self.assertEqual(result["paused"], 1)
            self.assertEqual(sender.sent, [])
            saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
            self.assertIn(f"{DATE}|generation_failed|", saved["businessAtoms"])


class DigestBindingRepairTests(unittest.TestCase):
    def write_bound(self, root, *, digest_over=None, source_over=None):
        folder = root / "work/research-seal" / DATE
        folder.mkdir(parents=True)
        (root / "reports/daily").mkdir(parents=True)
        # A usable legacy heading must not rescue a contradictory declared digest.
        (root / f"reports/daily/{DATE}.md").write_text("## 上一交易日 A 股行情（2026-09-19）\n")
        digest = {"schemaVersion": "astock.research-digest.v1", "kind": "research_digest",
                  "reportDate": DATE, "reportSha256": REPORT_SHA, "dataDate": "2026-09-19"}
        digest.update(digest_over or {})
        body = json.dumps(digest).encode()
        (folder / f"{REPORT_SHA}.digest.json").write_bytes(body)
        manifest = seal()
        manifest["source"].update(digestFile=f"work/research-seal/{DATE}/{REPORT_SHA}.digest.json",
                                   digestSha256=hashlib.sha256(body).hexdigest())
        manifest["source"].update(source_over or {})
        (folder / f"{REPORT_SHA}.json").write_text(json.dumps(manifest))
        return folder

    def test_bound_digest_date_accepts_protocol_same_day(self):
        for day in ("2026-09-19", DATE):
            with self.subTest(day=day), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_bound(root, digest_over={"dataDate": day})
                self.assertEqual(rca.expected_data_date(root, DATE, REPORT_SHA), day)
                self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(as_of=day),
                                  report_sha256=REPORT_SHA, data_date=day))[0], "confirmed")

    def test_digest_conflicts_never_fall_back_to_legacy_heading(self):
        for digest, source in (({}, {"digestSha256": "0" * 64}),
                               ({"reportSha256": None}, {}), ({"reportSha256": "b" * 64}, {}),
                               ({"reportDate": "2026-09-21"}, {}), ({"dataDate": "2026-02-31"}, {}),
                               ({"dataDate": "2026-09-23"}, {}),
                               ({}, {"digestFile": "../../wrong.json"})):
            with self.subTest(digest=digest, source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_bound(root, digest_over=digest, source_over=source)
                expected = rca.expected_data_date(root, DATE, REPORT_SHA)
                self.assertIsNone(expected)
                self.assertNotEqual(rca.delivery_evidence(facts(delivery=receipt(),
                                    report_sha256=REPORT_SHA, data_date=expected))[0], "confirmed")

    def test_missing_or_changed_declared_digest_remains_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = self.write_bound(root)
            target = folder / f"{REPORT_SHA}.digest.json"
            target.write_text("{}")
            self.assertIsNone(rca.expected_data_date(root, DATE, REPORT_SHA))
            target.unlink()
            self.assertIsNone(rca.expected_data_date(root, DATE, REPORT_SHA))

    def test_same_day_with_different_independent_expected_date_is_rejected(self):
        self.assertNotEqual(rca.delivery_evidence(facts(delivery=receipt(as_of=DATE),
                            report_sha256=REPORT_SHA, data_date="2026-09-19"))[0], "confirmed")


class ConfirmationBoundaryTests(unittest.TestCase):
    def test_invalid_dates_and_report_hashes_cannot_confirm(self):
        for date in ("2026-00-15", "2026-02-31", "2026-13-01", "garbage"):
            value = receipt(as_of=date)
            self.assertFalse(rca._RELAY.local_delivery_is_confirmed(
                value, report_date=DATE, report_sha256=REPORT_SHA, data_date=date)[0])
            value["target_date"] = date
            self.assertFalse(rca._RELAY.local_delivery_is_confirmed(
                value, report_date=date, report_sha256=REPORT_SHA, data_date=date)[0])
        for value in (None, "x", "A" * 64, True):
            self.assertFalse(rca._RELAY.local_delivery_is_confirmed(
                receipt(report_sha256=value), report_date=DATE, report_sha256=value,
                data_date="2026-09-19")[0])

    def test_telegram_receipt_shape_positive_id_and_explicit_failure(self):
        cases = [({"schemaVersion": "2"}, {}), ({"ackKind": "hermes_cli_exit_0"}, {}),
                 ({"extra": "not allowed"}, {}), ({"platformMessageId": "0"}, {}),
                 ({"platformMessageId": 0}, {}), ({"platformMessageId": 4274}, {}),
                 ({}, {"outcome": "failed"}), ({}, {"receipt": {"confirmed": False}}),
                 ({}, {"receipt": {"status": "failed"}}),
                 ({}, {"report": {"payloadFile": "payloads/another.md"}})]
        for receipt_over, sent_over in cases:
            with self.subTest(receipt=receipt_over, event=sent_over), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixtures.ConfirmationTests()._write_telegram(root, receipt_over=receipt_over,
                                                             sent_over=sent_over.copy())
                self.assertNotEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "confirmed")

    def test_original_markdown_compatibility_and_result_source_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = fixtures.ConfirmationTests()._write_telegram(root)
            folder = root / "work/notifications"
            event = folder / "sent" / f"astock-{DATE}-success-telegram-v1.json"
            ack = folder / "receipts/send" / event.name
            sent, receipt_value = json.loads(event.read_text()), json.loads(ack.read_text())
            del sent["report"]["sourceSha256"]
            del receipt_value["sourceSha256"]
            event.write_text(json.dumps(sent))
            ack.write_text(json.dumps(receipt_value))
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, digest)[0], "confirmed")
            sent["presentation"] = "research_brief_v3"
            event.write_text(json.dumps(sent))
            self.assertNotEqual(rca.telegram_delivery_evidence(root, DATE, digest)[0], "confirmed")
            del sent["presentation"]
            sent["report"]["presentation"] = "research_brief_v3"
            event.write_text(json.dumps(sent))
            self.assertNotEqual(rca.telegram_delivery_evidence(root, DATE, digest)[0], "confirmed")

    def test_symlinked_payload_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures.ConfirmationTests()._write_telegram(root)
            folder = root / "work/notifications/payloads"
            moved = folder.with_name("elsewhere")
            folder.rename(moved)
            folder.symlink_to(moved, target_is_directory=True)
            self.assertNotEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "confirmed")


class RelayObservationTests(unittest.TestCase):
    def test_observation_is_independent_safe_and_not_delivery_state(self):
        relay = rca._RELAY
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            relay.record_attempt({"status": "held", "reason": "SECRET=unsafe", "sent": False,
                                  "payload": "private"}, target_date=DATE, observed_at=POST, bus_root=root)
            value = json.loads((root / f"out/relay-attempts/{DATE}.json").read_text())
            self.assertEqual(value["schemaVersion"], "signal-bus.relay-attempt.v1")
            self.assertEqual(value["reason"], "other_held")
            self.assertEqual(value["sent"], 0)
            self.assertNotIn("SECRET", json.dumps(value))
            self.assertFalse((root / "out/relay-delivery").exists())

    def test_cli_dry_run_no_observation_and_write_failure_no_delivery_change(self):
        relay = rca._RELAY
        result = {"status": "held", "reason": "structured_digest_missing", "sent": 0}
        with patch.object(relay, "run_relay", return_value=result) as run, \
                patch.object(relay, "record_attempt", side_effect=OSError("SECRET")) as record, \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(relay.main(["--dry-run"]), 0)
            record.assert_not_called()
            self.assertEqual(relay.main([]), 0)
            self.assertEqual(run.call_count, 2)
            self.assertNotIn("now", run.call_args.kwargs, "CLI must preserve a fresh actual-send clock")
            self.assertEqual(record.call_count, 1)
            self.assertEqual(err.getvalue().strip(), "relay_attempt_observation_unavailable")
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1]), result)


if __name__ == "__main__":
    unittest.main()
