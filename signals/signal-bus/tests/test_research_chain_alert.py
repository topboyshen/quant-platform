#!/usr/bin/env python3
"""Offline acceptance v2 for research-chain alerting.

Fixes the v1 gaps: ack-based confirmation, crash/concurrency safety, real
production inputs (journal relay runs, hash-bound seal), one incident per date,
and a dry-run that leaves the whole tree untouched.
"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "research_chain_alert_v2",
    Path(__file__).resolve().parents[1] / "scripts" / "research_chain_alert.py")
assert SPEC and SPEC.loader
rca = importlib.util.module_from_spec(SPEC)
sys.modules["research_chain_alert_v2"] = rca
SPEC.loader.exec_module(rca)

DATE = "2026-09-22"
PRE = datetime(2026, 9, 22, 8, 12, tzinfo=rca.TZ)
POST = datetime(2026, 9, 22, 8, 20, tzinfo=rca.TZ)
REPORT_SHA = "a" * 64


def facts(**over):
    base = {"date": DATE, "now": PRE, "occurrence": None, "last_run": None, "report_exists": False,
            "report_sha256": None, "seal": None, "seal_mtime": None, "draft": None,
            "delivery": None, "relay_runs": []}
    base.update(over)
    return base


def seal(report_sha=REPORT_SHA, **over):
    value = {"schemaVersion": "astock.research-seal.v1", "kind": "research_seal", "state": "SEALED",
             "targetDate": over.pop("targetDate", DATE),
             "source": {"reportSha256": report_sha, "publicationGate": "passed", "evidenceReview": "passed"}}
    value.update(over)
    return value


def receipt(status="confirmed", **over):
    value = {"status": status, "reason": "accepted", "target_date": DATE, "report_sha256": REPORT_SHA,
             "receiver_storage": "receiver_reported_stored", "receiver_content_sha256": "c" * 64,
             "payload_sha256": "c" * 64, "content_match": True,
             "receiver_record_id": "rr_test"}
    value.update(over)
    return value


def occurrence(status="success", exit_code=0):
    return {"targetDate": DATE, "status": status, "childExitCode": exit_code, "childSignal": None,
            "timedOut": False, "interrupted": False}


def last_run(status="success"):
    return {"targetDate": DATE, "status": status, "phase": "publish", "publicationGate": "passed",
            "evidenceReview": "passed", "dataStatus": "partial"}


def codes(items):
    return [cause["code"] for cause in items[0]["causes"]] if items else []


class SenderFake(rca.Sender):
    def __init__(self, outcome=("confirmed", "ack", {"message_id": "1"})):
        self.outcome = outcome
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return self.outcome


class ConfirmationTests(unittest.TestCase):
    def test_ack_is_required_for_confirmation(self):
        pretty = json.dumps({"success": True, "platform": "telegram", "chat_id": "12345",
                             "message_id": "99"}, indent=2)
        self.assertIn("\n", pretty)                      # installed CLI prints formatted JSON
        for output, returncode, expected in (
                (pretty, 0, "confirmed"),
                (json.dumps({"success": True, "platform": "telegram", "chat_id": "12345",
                             "message_id": 99}), 0, "confirmed"),
                ("", 0, "uncertain"),
                ("boom", 0, "uncertain"),
                ('{"success": true, "platform": "slack", "chat_id": "1", "message_id": "9"}', 0, "uncertain"),
                ('{"success": true, "platform": "telegram", "message_id": "9"}', 0, "uncertain"),
                ('{"success": false, "error_code": 500}', 1, "uncertain"),
                ('{"success": false, "error_code": 400}', 1, "uncertain")):
            with self.subTest(output=output[:32], code=returncode):
                outcome, detail, extra = rca.classify_send(returncode, output, "12345")
                self.assertEqual(outcome, expected)
                if expected == "confirmed":
                    self.assertEqual(extra.get("message_id"), "99")
        self.assertEqual(rca.classify_send(0, pretty, "999")[0], "uncertain")
        # No configured target: nothing may be confirmed, and the sender is not called.
        self.assertEqual(rca.classify_send(0, pretty, "")[0], "uncertain")
        original = rca.EXPECTED_CHAT
        try:
            rca.EXPECTED_CHAT = ""
            calls = []
            real_run = rca.subprocess.run
            rca.subprocess.run = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("spawned"))
            outcome = rca.HermesSender().send("message")
            rca.subprocess.run = real_run
            self.assertEqual(outcome[0], "uncertain")
            self.assertEqual(outcome[1], "expected_chat_not_configured")
            self.assertEqual(calls, [])
        finally:
            rca.EXPECTED_CHAT = original

    def test_delivery_evidence_binds_date_and_report_hash(self):
        self.assertEqual(rca.delivery_evidence(facts())[0], "unconfirmed")
        self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(), report_sha256=REPORT_SHA))[0], "confirmed")
        self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(), report_sha256="b" * 64))[0], "uncertain")
        self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(target_date="2026-09-18"),
                                                    report_sha256=REPORT_SHA))[0], "uncertain")
        # Without a usable report hash nothing may be confirmed.
        unknown_hash = rca.delivery_evidence(facts(delivery=receipt(), report_sha256=None))
        self.assertEqual(unknown_hash[0], "unverified")
        self.assertNotIn("已确认", unknown_hash[1])

    def test_uncertain_alert_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            sender = SenderFake(("uncertain", "no_ack_exit_0", {}))
            items = rca.classify(facts(occurrence=occurrence("failed", 1)))
            rca.process(items, state, PRE, date=DATE, sender=sender)
            rca.process(items, state, PRE, date=DATE, sender=SenderFake())
            self.assertEqual(len(sender.sent), 1)
            saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
            self.assertEqual(saved["incident"]["attempts"][-1]["outcome"], "uncertain")


class ProductionInputTests(unittest.TestCase):
    def test_relay_reason_is_read_from_the_unit_journal_when_no_state_file_exists(self):
        journal = (
            "2026-09-21T08:05:01+08:00 VM systemd[3725]: Starting nng8-research-digest.service\n"
            '2026-09-21T08:05:02+08:00 VM runner: {"reason": "timestamp_invalid", "sent": 0, "status": "held"}\n'
            "2026-09-21T08:10:01+08:00 VM systemd[3725]: Starting nng8-research-digest.service\n"
            '2026-09-21T08:10:02+08:00 VM runner: {"reason": "research_seal_missing", "sent": 0, "status": "held"}\n')
        runs = rca.parse_relay_runs(journal)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["started"].hour, 8)
        self.assertEqual([run["reason"] for run in runs], ["timestamp_invalid", "research_seal_missing"])
        items = rca.classify(facts(occurrence=occurrence(), relay_runs=runs))
        self.assertIn("relay_blocked", codes(items))

    def test_new_run_in_flight_is_not_masked_by_the_previous_finished_run(self):
        journal = (
            "2026-09-22T08:05:01+08:00 VM systemd[3725]: Starting nng8-research-digest.service\n"
            '2026-09-22T08:05:02+08:00 VM runner: {"reason": "timestamp_invalid", "sent": 0, "status": "held"}\n'
            "2026-09-22T08:11:30+08:00 VM systemd[3725]: Starting nng8-research-digest.service\n")
        runs = rca.parse_relay_runs(journal)
        self.assertIsNone(runs[-1]["status"])
        items = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                   report_sha256=REPORT_SHA, seal=seal(), relay_runs=runs,
                                   now=PRE.replace(minute=12)))
        self.assertEqual(items, [])

    def test_stalled_run_beyond_five_minutes_is_no_longer_in_flight(self):
        journal = ("2026-09-22T08:02:00+08:00 VM systemd[3725]: Starting nng8-research-digest.service\n")
        items = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                   report_sha256=REPORT_SHA, seal=seal(),
                                   relay_runs=rca.parse_relay_runs(journal), now=PRE.replace(minute=12)))
        self.assertEqual(codes(items), ["predeadline_not_ready"])

    def test_seal_must_be_bound_to_the_current_report_hash(self):
        ok_items = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                      report_sha256=REPORT_SHA, seal=seal(), delivery=receipt()))
        self.assertEqual(ok_items, [])
        for bad in (seal(report_sha="b" * 64), seal(state="DRAFT"), seal(targetDate="2026-09-18"),
                    {"kind": "research_seal"},
                    {"schemaVersion": "astock.research-seal.v1", "kind": "research_seal", "state": "SEALED",
                     "targetDate": DATE, "source": {"reportSha256": REPORT_SHA, "publicationGate": "blocked"}}):
            with self.subTest(seal=bad):
                items = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                           report_sha256=REPORT_SHA, seal=bad))
                self.assertIn("seal_failed", codes(items))

    def test_bind_helper_rejects_invalid_and_mismatched_seals(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / DATE
            folder.mkdir(parents=True)
            (folder / f"{REPORT_SHA}.json").write_text(json.dumps(seal()), encoding="utf-8")
            self.assertIsNotNone(rca.seal_for_report(folder, REPORT_SHA)[0])
            self.assertIsNone(rca.seal_for_report(folder, "b" * 64)[0])
            (folder / f"{REPORT_SHA}.json").write_text("{broken", encoding="utf-8")
            self.assertIsNone(rca.seal_for_report(folder, REPORT_SHA)[0])


class IncidentTests(unittest.TestCase):
    def test_generation_failure_and_blocked_compensations_are_one_incident(self):
        first = rca.classify(facts(occurrence=occurrence("failed", 1)))
        later = rca.classify(facts(occurrence=occurrence("failed", 1),
                                   relay_runs=rca.parse_relay_runs(
                                       '{"status": "held", "reason": "timestamp_invalid"}')))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(later), 1)
        self.assertEqual(first[0]["key"], later[0]["key"])
        self.assertIn("generation_failed", codes(later))
        self.assertIn("relay_blocked", codes(later))
        with tempfile.TemporaryDirectory() as tmp:
            state, sender = Path(tmp), SenderFake()
            rca.process(first, state, PRE, date=DATE, sender=sender)
            summary = rca.process(later, state, PRE, date=DATE, sender=SenderFake())
            self.assertEqual(len(sender.sent), 1)
            self.assertEqual(summary["notified"], 0)

    def test_recovery_requires_seal_and_matching_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            items = rca.classify(facts(occurrence=occurrence("failed", 1)))
            rca.process(items, state, PRE, date=DATE, sender=SenderFake())
            # Error code disappearing with no proof: no recovery.
            rca.process([], state, POST, date=DATE, sender=SenderFake(), success=False)
            saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
            self.assertIsNone(saved["incident"].get("resolved_at"))
            # Real evidence: hash-bound seal + matching confirmed receipt.
            rca.process([], state, POST, date=DATE, sender=SenderFake(), success=True,
                        success_evidence={"seal_bound": True, "delivery_state": "confirmed"})
            saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
            self.assertIsNone(saved["incident"])
            self.assertTrue(saved["history"][0]["resolved_at"])


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def worker(self, state_dir: str, results: str) -> None:
        spec = importlib.util.spec_from_file_location("rca_worker", rca.__file__)
        module = importlib.util.module_from_spec(spec)
        sys.modules["rca_worker"] = module
        spec.loader.exec_module(module)

        class WorkerSender(module.Sender):
            def send(self, message):
                with open(results, "a", encoding="utf-8") as handle:
                    handle.write("sent\n")
                time.sleep(0.05)
                return "confirmed", "ack", {"message_id": "1"}

        items = module.classify({"date": DATE, "now": PRE, "occurrence": {"status": "failed", "childExitCode": 1},
                                 "last_run": None, "report_exists": False, "report_sha256": None,
                                 "seal": None, "seal_mtime": None, "draft": None, "delivery": None,
                                 "relay_runs": []})
        try:
            module.process(items, Path(state_dir), PRE, date=DATE, sender=WorkerSender(), dry_run=False)
        except Exception as exc:  # surface child failures in the result file
            with open(results, "a", encoding="utf-8") as handle:
                handle.write(f"error:{type(exc).__name__}:{exc}\n")

    def test_two_real_processes_send_exactly_once(self):
        results = Path(self.tmp.name) / "sends.txt"
        workers = [multiprocessing.Process(target=self.worker, args=(str(self.state), str(results)))
                   for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        sends = results.read_text().count("sent") if results.exists() else 0
        self.assertEqual(sends, 1)

    def test_crash_between_send_and_result_does_not_resend(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"research-chain-alert-{DATE}.json").write_text(json.dumps({
            "version": 2, "incident": {"key": f"{DATE}:research_chain", "date": DATE, "notified_at": None,
                                       "causes": [{"code": "generation_failed", "detail": "x"}],
                                       "attempts": [{"at": PRE.isoformat(), "outcome": "sending"}]}}),
            encoding="utf-8")
        items = rca.classify(facts(occurrence=occurrence("failed", 1)))
        sender = SenderFake()
        summary = rca.process(items, self.state, PRE, date=DATE, sender=sender)
        self.assertIn("crash_recovered_as_uncertain", summary["problems"])
        self.assertEqual(sender.sent, [])
        saved = json.loads((self.state / f"research-chain-alert-{DATE}.json").read_text())
        self.assertEqual(saved["incident"]["attempts"][0]["outcome"], "uncertain")

    def test_corrupt_state_pauses_instead_of_notifying(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"research-chain-alert-{DATE}.json").write_text("{not-json", encoding="utf-8")
        sender = SenderFake()
        summary = rca.process(rca.classify(facts(occurrence=occurrence("failed", 1))), self.state, PRE,
                              date=DATE, sender=sender)
        self.assertEqual(summary["paused"], 1)
        self.assertEqual(summary["problems"], ["state_corrupt_paused"])
        self.assertEqual(sender.sent, [])

    def test_dry_run_leaves_the_whole_tree_unchanged(self):
        root = Path(self.tmp.name) / "dry"
        root.mkdir()

        def snapshot(base: Path):
            return sorted((str(path.relative_to(base)), path.is_dir(), path.stat().st_size if path.is_file() else 0)
                          for path in base.rglob("*"))

        before = snapshot(root)
        items = rca.classify(facts(occurrence=occurrence("failed", 1)))
        summary = rca.process(items, root / "state", PRE, date=DATE, sender=SenderFake(), dry_run=True)
        self.assertEqual(snapshot(root), before)
        self.assertEqual(summary["problems"], [])
        self.assertFalse((root / "state").exists())

    def test_draft_failure_survives_research_success_and_keeps_one_notification_a_day(self):
        state = Path(self.tmp.name) / "state2"
        mixed = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                   report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(),
                                   draft={"targetDate": DATE, "status": "held",
                                          "reasonCodes": ["REPORT_CONTRACT_MISMATCH"]}))
        self.assertIn("draft_failed", codes(mixed))
        sender = SenderFake()
        rca.process(mixed, state, POST, date=DATE, sender=sender, success=True,
                    success_evidence={"seal_bound": True, "delivery_state": "confirmed"})
        saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
        self.assertIsNotNone(saved["incident"], "research success must not clear a draft failure")
        self.assertIsNone(saved["incident"].get("resolved_at"))
        self.assertEqual(len(sender.sent), 1)

    def test_state_with_null_attempt_is_corrupt_and_pauses(self):
        state = Path(self.tmp.name) / "state3"
        state.mkdir(parents=True, exist_ok=True)
        (state / f"research-chain-alert-{DATE}.json").write_text(json.dumps({
            "version": 2, "incident": {"key": f"{DATE}:research_chain", "attempts": [None]}}), encoding="utf-8")
        sender = SenderFake()
        summary = rca.process(rca.classify(facts(occurrence=occurrence("failed", 1))), state, PRE,
                              date=DATE, sender=sender)
        self.assertEqual(summary["paused"], 1)
        self.assertEqual(sender.sent, [])

    def test_recovery_then_same_day_new_cause_notifies_only_once(self):
        state = Path(self.tmp.name) / "state_daily"
        sender = SenderFake()
        generation = rca.classify(facts(occurrence=occurrence("failed", 1)))
        rca.process(generation, state, PRE, date=DATE, sender=sender)
        rca.process([], state, PRE, date=DATE, sender=sender, success=True,
                    success_evidence={"seal_bound": True, "delivery_state": "confirmed"})
        draft = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                   report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(),
                                   draft={"targetDate": DATE, "status": "held", "reasonCodes": ["X"]}))
        summary = rca.process(draft, state, PRE, date=DATE, sender=sender, success=False)
        self.assertEqual(len(sender.sent), 1, "same-day second incident must not notify again")
        self.assertEqual(summary["skipped"], 1)

    def test_suppressed_new_cause_is_recorded_and_recovery_clears_it(self):
        state = Path(self.tmp.name) / "state_trace"
        sender = SenderFake()
        generation = rca.classify(facts(occurrence=occurrence("failed", 1)))
        rca.process(generation, state, PRE, date=DATE, sender=sender)
        draft = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                   report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(),
                                   draft={"targetDate": DATE, "status": "held", "reasonCodes": ["X"]}))
        rca.process(draft, state, PRE, date=DATE, sender=sender)
        path = state / f"research-chain-alert-{DATE}.json"
        saved = json.loads(path.read_text())
        codes_now = [cause["code"] for cause in saved["incident"]["causes"]]
        self.assertIn("draft_failed", codes_now, "new cause must be recorded even when suppressed")
        self.assertTrue(saved["incident"]["suppressed_updates"])
        rca.process([], state, POST, date=DATE, sender=sender, success=True,
                    success_evidence={"seal_bound": True, "delivery_state": "confirmed"})
        saved = json.loads(path.read_text())
        self.assertIsNone(saved["incident"], "recovery must not leave a stale incident")
        self.assertEqual(saved["history"][-1]["resolved_at"][:10], DATE)
        log_lines = (state / "research-chain-alert.log").read_text().splitlines()
        events = [json.loads(line)["event"] for line in log_lines]
        self.assertIn("same_day_suppressed", events)
        self.assertIn("recovered", events)

    def test_missing_attempts_or_null_causes_pause_instead_of_raising(self):
        for broken in ({"incident": {"key": f"{DATE}:research_chain"}},
                       {"incident": {"key": f"{DATE}:research_chain", "attempts": [], "causes": None}},
                       {"incident": {"key": f"{DATE}:research_chain", "attempts": [None], "causes": []}}):
            with self.subTest(broken=broken):
                state = Path(self.tmp.name) / f"s_{abs(hash(str(broken)))}"
                state.mkdir(parents=True, exist_ok=True)
                (state / f"research-chain-alert-{DATE}.json").write_text(
                    json.dumps({"version": 2, **broken}), encoding="utf-8")
                sender = SenderFake()
                summary = rca.process(rca.classify(facts(occurrence=occurrence("failed", 1))), state, PRE,
                                      date=DATE, sender=sender)
                self.assertEqual(summary["paused"], 1)
                self.assertEqual(sender.sent, [])

    def test_bounded_definite_failure_retry(self):
        calls = []

        class FailingSender(rca.Sender):
            def send(self, message):
                calls.append(1)
                return "failed", "rejected_http_401", {}

        items = rca.classify(facts(occurrence=occurrence("failed", 1)))
        for _ in range(3):
            rca.process(items, self.state, PRE, date=DATE, sender=FailingSender())
        self.assertEqual(len(calls), rca.MAX_ATTEMPTS)


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.production_state = self.root / "state"
        self.production_state.mkdir()
        (self.production_state / f"research-chain-alert-{DATE}.json").write_text(
            json.dumps({"version": 2, "incident": None}), encoding="utf-8")
        self.sent = []

        class FakeSender:
            def send(inner, message):
                self.sent.append(message)
                return ("confirmed", "ack", {"message_id": "7"}) if inner.outcome == "confirmed" else ("uncertain", "no_ack_exit_0", {})

        FakeSender.outcome = "confirmed"
        self.fake = FakeSender
        self.original = rca.HermesSender
        rca.HermesSender = self.fake
        rca.STATE_DIR = self.production_state

    def tearDown(self):
        rca.HermesSender = self.original

    def test_drill_sends_a_marked_message_through_the_new_state_machine(self):
        code = rca.main(["--phase", "drill", "--date", DATE, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("演练", self.sent[0])
        self.assertIn("非真实故障", self.sent[0])
        self.assertNotIn("研究生成失败", self.sent[0])
        drill_state = self.production_state / f"drill-{DATE}"
        self.assertTrue((drill_state / f"research-chain-alert-drill-{DATE}.json").is_file())
        self.assertEqual((self.production_state / f"research-chain-alert-{DATE}.json").read_text(),
                         json.dumps({"version": 2, "incident": None}), "production state must be untouched")

    def test_drill_uncertain_is_nonzero_and_never_retried(self):
        self.fake.outcome = "uncertain"
        self.assertEqual(rca.main(["--phase", "drill", "--date", DATE]), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(rca.main(["--phase", "drill", "--date", DATE]), 1)
        self.assertEqual(len(self.sent), 1, "uncertain drill must not be resent")

    def test_drill_dry_run_writes_nothing_and_sends_nothing(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(rca.main(["--phase", "drill", "--date", DATE, "--dry-run"]), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)

if __name__ == "__main__":
    unittest.main()
