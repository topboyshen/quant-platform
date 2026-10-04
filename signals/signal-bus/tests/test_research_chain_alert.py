#!/usr/bin/env python3
"""Offline acceptance v2 for research-chain alerting.

Fixes the v1 gaps: ack-based confirmation, crash/concurrency safety, real
production inputs (journal relay runs, hash-bound seal), one incident per date,
and a dry-run that leaves the whole tree untouched.
"""
from __future__ import annotations

import hashlib
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
from unittest.mock import patch

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
SUCCESS_EVIDENCE = {"seal_bound": True, "delivery_state": "confirmed", "telegram_state": "confirmed",
                    "stages": {"generation": True, "seal": True, "relay": True, "telegram": True}}


def facts(**over):
    base = {"date": DATE, "now": PRE, "occurrence": None, "last_run": None, "report_exists": False,
            "report_sha256": None, "seal": None, "seal_mtime": None,
            "delivery": None, "relay_runs": [], "telegram_state": None, "telegram_wording": "",
            "telegram_evidence": {}}
    base.update(over)
    return base


def seal(report_sha=REPORT_SHA, **over):
    value = {"schemaVersion": "astock.research-seal.v1", "kind": "research_seal", "state": "SEALED",
             "targetDate": over.pop("targetDate", DATE),
             "source": {"reportSha256": report_sha, "publicationGate": "passed", "evidenceReview": "passed"}}
    value.update(over)
    return value


def receipt(status="confirmed", **over):
    value = {"status": status, "reason": "accepted", "target_date": DATE, "as_of": "2026-09-19",
             "report_sha256": REPORT_SHA,
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
            real_run, real_popen = rca.subprocess.run, rca.subprocess.Popen
            spawn = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("spawned"))
            rca.subprocess.run = rca.subprocess.Popen = spawn
            outcome = rca.HermesSender().send("message")
            rca.subprocess.run, rca.subprocess.Popen = real_run, real_popen
            # Nothing was spawned, so this is provably not sent (not "uncertain").
            self.assertEqual(outcome[0], "not_sent")
            self.assertEqual(outcome[1], "not_started:expected_chat_not_configured")
            self.assertEqual(calls, [])
        finally:
            rca.EXPECTED_CHAT = original

    def test_delivery_evidence_binds_date_and_report_hash(self):
        self.assertEqual(rca.delivery_evidence(facts())[0], "unconfirmed")
        self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(), report_sha256=REPORT_SHA,
                                                    data_date="2026-09-19"))[0], "confirmed")
        self.assertNotEqual(rca.delivery_evidence(facts(delivery=receipt(), report_sha256=REPORT_SHA))[0],
                            "confirmed")
        self.assertNotEqual(rca.delivery_evidence(facts(
            delivery=receipt(as_of="2026-01-01"), report_sha256=REPORT_SHA,
            data_date="2026-09-19"))[0], "confirmed")
        self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(), report_sha256="b" * 64))[0], "uncertain")
        self.assertEqual(rca.delivery_evidence(facts(delivery=receipt(target_date="2026-09-18"),
                                                    report_sha256=REPORT_SHA))[0], "uncertain")
        # Without a usable report hash nothing may be confirmed.
        unknown_hash = rca.delivery_evidence(facts(delivery=receipt(), report_sha256=None))
        self.assertEqual(unknown_hash[0], "unverified")
        self.assertNotIn("已确认", unknown_hash[1])
        # A05: stored=false / 缺 ID / 哈希冲突 不得确认。
        self.assertNotEqual(rca.delivery_evidence(facts(
            delivery=receipt(receiver_storage="not_in_acknowledgement"), report_sha256=REPORT_SHA))[0], "confirmed")
        self.assertNotEqual(rca.delivery_evidence(facts(
            delivery=receipt(receiver_record_id=""), report_sha256=REPORT_SHA))[0], "confirmed")
        self.assertNotEqual(rca.delivery_evidence(facts(
            delivery=receipt(receiver_content_sha256="d" * 64, content_match=True),
            report_sha256=REPORT_SHA))[0], "confirmed")
        self.assertNotEqual(rca.delivery_evidence(facts(
            delivery=receipt(as_of=DATE), report_sha256=REPORT_SHA))[0], "confirmed")
        self.assertNotEqual(rca.delivery_evidence(facts(
            delivery=receipt(as_of="2026-09-18"), report_sha256=REPORT_SHA,
            data_date="2026-09-19"))[0], "confirmed")

    def test_invalid_relay_evidence_is_not_success(self):
        """A05: stored=false、哈希冲突、缺 ID 不得输出 success。"""
        seal_ok = seal()
        for delivery in (
                receipt(receiver_storage="not_in_acknowledgement"),
                receipt(receiver_content_sha256="d" * 64),
                receipt(receiver_record_id=""),
                receipt(content_match=False),
                receipt(status="confirmed", payload_sha256="not-hex"),
        ):
            with self.subTest(delivery=delivery):
                ok, evidence = rca._success_evidence(facts(
                    occurrence=occurrence(), last_run=last_run(), report_exists=True,
                    report_sha256=REPORT_SHA, seal=seal_ok, delivery=delivery,
                    telegram_state="confirmed"))
                self.assertFalse(ok)
                self.assertNotEqual(evidence["delivery_state"], "confirmed")

    def test_telegram_receipt_is_required_for_research_success(self):
        """A06: 仅密封+relay 不能判成功；缺 Telegram 回执保持不可确认。"""
        base = dict(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                    report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(), now=POST,
                    data_date="2026-09-19")
        missing = rca.classify(facts(**base, telegram_state="missing",
                                     telegram_wording="尚未确认（无 Telegram 发送记录）"))
        self.assertIn("telegram_missing", codes(missing))
        ok, evidence = rca._success_evidence(facts(**base, telegram_state="missing"))
        self.assertFalse(ok)
        self.assertEqual(evidence["telegram_state"], "missing")
        confirmed = rca.classify(facts(**base, telegram_state="confirmed"))
        self.assertEqual(confirmed, [])
        ok, _ = rca._success_evidence(facts(**base, telegram_state="confirmed"))
        self.assertTrue(ok)
        skipped = rca.classify(facts(occurrence={"status": "skipped"}, now=POST,
                                     telegram_state="missing"))
        self.assertIn("calendar_skip_invalid", codes(skipped))
        self.assertNotIn("telegram_missing", codes(skipped))

    def _write_telegram(self, root: Path, *, payload=b"payload-bytes", receipt_over=None, sent_over=None):
        event_id = f"astock-{DATE}-success-telegram-v1"
        payload_rel = f"payloads/{event_id}.md"
        (root / "work/notifications/payloads").mkdir(parents=True)
        (root / "work/notifications/sent").mkdir(parents=True)
        (root / "work/notifications/receipts/send").mkdir(parents=True)
        (root / "work/notifications" / payload_rel).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        sent = {"schemaVersion": "1.0", "eventId": event_id, "source": "AStock_AI_System",
                "route": "telegram", "targetDate": DATE, "outcome": "success",
                "report": {"payloadFile": payload_rel, "sha256": digest, "bytes": len(payload),
                            "sourceSha256": REPORT_SHA, "sourceBytes": 10}}
        if sent_over:
            if "report" in sent_over:
                sent["report"].update(sent_over.pop("report"))
            sent.update(sent_over)
        receipt = {"schemaVersion": "1.0", "eventId": event_id, "targetDate": DATE,
                   "route": "telegram", "payloadSha256": digest, "payloadBytes": len(payload),
                   "platformMessageId": "4274", "ackKind": "hermes_cli_json_message_id",
                   "confirmedAt": "2026-09-22T00:00:32.728Z", "sourceSha256": REPORT_SHA}
        if receipt_over:
            receipt.update(receipt_over)
        (root / "work/notifications/sent" / f"{event_id}.json").write_text(json.dumps(sent))
        (root / "work/notifications/receipts/send" / f"{event_id}.json").write_text(json.dumps(receipt))
        return digest

    def test_telegram_attachment_hash_bytes_and_time_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root)
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "confirmed")
            (root / "work/notifications/payloads" / f"astock-{DATE}-success-telegram-v1.md").unlink()
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "unconfirmed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root, receipt_over={"payloadSha256": "d" * 64})
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root, receipt_over={"payloadBytes": 1})
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root, receipt_over={"confirmedAt": "not-a-timestamp"})
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "unconfirmed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root, receipt_over={"confirmedAt": "2026-09-22"})
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "unconfirmed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root, receipt_over={"confirmedAt": "2026-09-22T00:00:32"})
            self.assertEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "unconfirmed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_telegram(root, payload=b"x", receipt_over={"payloadBytes": True},
                                 sent_over={"report": {"bytes": True}})
            self.assertNotEqual(rca.telegram_delivery_evidence(root, DATE, REPORT_SHA)[0], "confirmed")

    def test_collect_passes_expected_data_date_from_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = Path(tmp) / "bus"
            (root / "reports/daily").mkdir(parents=True)
            (root / "work/orchestrator/states").mkdir(parents=True)
            (root / f"reports/daily/{DATE}.md").write_text(
                f"# x\n## 2. 上一交易日 A 股行情（2026-09-19）\n", encoding="utf-8")
            facts = rca.collect(root=root, bus=bus, date=DATE, now=POST, relay_log="")
            self.assertEqual(facts["data_date"], "2026-09-19")

    def test_collect_does_not_read_on_demand_draft_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, bus = Path(tmp), Path(tmp) / "bus"
            draft_file = root / "work/xhs/last-run.json"
            draft_file.parent.mkdir(parents=True)
            draft_file.write_text(json.dumps({"targetDate": DATE, "status": "failed"}))
            read_json = rca._read_json

            def read_core_only(path, *args, **kwargs):
                self.assertNotEqual(path, draft_file, "automatic core collection read the on-demand result")
                return read_json(path, *args, **kwargs)

            with patch.object(rca, "_read_json", side_effect=read_core_only):
                collected = rca.collect(root=root, bus=bus, date=DATE, now=POST, relay_log="")
            self.assertNotIn("draft", collected)
            self.assertEqual(json.loads(draft_file.read_text())["status"], "failed")

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
                                      report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(),
                                      telegram_state="confirmed", data_date="2026-09-19"))
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
            summary = rca.process(later, state, PRE, date=DATE, sender=sender)
            self.assertEqual(len(sender.sent), 2)
            self.assertEqual(summary["notified"], 1)

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
                        success_evidence=SUCCESS_EVIDENCE)
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
                                 "seal": None, "seal_mtime": None, "delivery": None,
                                 "relay_runs": []})
        try:
            module.process(items, Path(state_dir), PRE, date=DATE, sender=WorkerSender(), dry_run=False)
        except Exception as exc:  # surface child failures in the result file
            with open(results, "a", encoding="utf-8") as handle:
                handle.write(f"error:{type(exc).__name__}:{exc}\n")

    def test_two_real_processes_send_exactly_once(self):
        results = Path(self.tmp.name) / "sends.txt"
        context = multiprocessing.get_context("fork")
        workers = [context.Process(target=self.worker, args=(str(self.state), str(results)))
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

    def test_on_demand_draft_failure_does_not_pollute_core_success_or_notify(self):
        state = Path(self.tmp.name) / "state2"
        core = facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                     report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(), now=POST,
                     data_date="2026-09-19", telegram_state="confirmed",
                     draft={"targetDate": DATE, "status": "held",
                            "reasonCodes": ["REPORT_CONTRACT_MISMATCH"]})
        mixed = rca.classify(core)
        self.assertEqual(mixed, [])
        success, evidence = rca._success_evidence(core)
        self.assertTrue(success)
        self.assertNotIn("draft", evidence["stages"])
        sender = SenderFake()
        result = rca.process(mixed, state, POST, date=DATE, sender=sender,
                             success=success, success_evidence=evidence)
        saved = json.loads((state / f"research-chain-alert-{DATE}.json").read_text())
        self.assertIsNone(saved["incident"])
        self.assertEqual(saved["businessAtoms"], [])
        self.assertEqual(result["notified"], 0)
        self.assertEqual(sender.sent, [])

    def test_on_demand_draft_failure_does_not_mask_core_failures(self):
        base = dict(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                    report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(), now=POST,
                    data_date="2026-09-19", telegram_state="confirmed",
                    draft={"targetDate": DATE, "status": "failed"})
        cases = (("generation_failed", {"occurrence": occurrence("failed", 1)}),
                 ("seal_failed", {"seal": seal(state="REJECTED")}),
                 ("telegram_missing", {"telegram_state": "missing"}),
                 ("relay_blocked", {"delivery": receipt(status="held", reason="digest_hash_mismatch")}))
        for expected, change in cases:
            with self.subTest(cause=expected):
                result = rca.classify(facts(**(base | change)))
                self.assertIn(expected, codes(result))
                self.assertNotIn("draft_failed", codes(result))

    def test_legacy_draft_atom_is_preserved_without_claiming_recovery(self):
        state = Path(self.tmp.name) / "legacy_draft"
        state.mkdir()
        atom = f"{DATE}|draft_failed|report_contract_mismatch"
        target = state / f"research-chain-alert-{DATE}.json"
        target.write_text(json.dumps({
            "version": 2, "businessAtoms": [atom],
            "incident": {"key": f"{DATE}:research_chain", "date": DATE,
                         "causes": [{"code": "draft_failed", "reason": "report_contract_mismatch",
                                     "detail": "existing on-demand draft failure"}],
                         "attempts": [{"outcome": "confirmed", "at": PRE.isoformat()}]}}))
        sender = SenderFake()
        result = rca.process([], state, POST, date=DATE, sender=sender, success=True,
                             success_evidence=SUCCESS_EVIDENCE)
        self.assertEqual(json.loads(target.read_text())["businessAtoms"], [atom])
        self.assertEqual(result["recovered"], 0)
        self.assertEqual(sender.sent, [])

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

    def test_recovery_then_same_day_new_cause_notifies_distinct_changes(self):
        state = Path(self.tmp.name) / "state_daily"
        sender = SenderFake()
        generation = rca.classify(facts(occurrence=occurrence("failed", 1)))
        rca.process(generation, state, PRE, date=DATE, sender=sender)
        rca.process([], state, PRE, date=DATE, sender=sender, success=True,
                    success_evidence=SUCCESS_EVIDENCE)
        telegram = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                      report_sha256=REPORT_SHA, seal=seal(), delivery=receipt(),
                                      now=POST, data_date="2026-09-19", telegram_state="missing"))
        self.assertEqual(codes(telegram), ["telegram_missing"])
        summary = rca.process(telegram, state, POST, date=DATE, sender=sender, success=False)
        self.assertEqual(len(sender.sent), 3, "fault, proven recovery, new fault are distinct transitions")
        self.assertEqual(summary["notified"], 1)
        self.assertIn("恢复", sender.sent[1])

    def test_new_cause_is_recorded_notified_and_proven_recovery_clears_it(self):
        state = Path(self.tmp.name) / "state_trace"
        sender = SenderFake()
        generation = rca.classify(facts(occurrence=occurrence("failed", 1)))
        rca.process(generation, state, PRE, date=DATE, sender=sender)
        relay = rca.classify(facts(occurrence=occurrence(), last_run=last_run(), report_exists=True,
                                   report_sha256=REPORT_SHA, seal=seal(),
                                   delivery=receipt(status="held", reason="digest_hash_mismatch"),
                                   now=POST, data_date="2026-09-19", telegram_state="confirmed"))
        self.assertEqual(codes(relay), ["relay_blocked"])
        rca.process(relay, state, POST, date=DATE, sender=sender)
        path = state / f"research-chain-alert-{DATE}.json"
        saved = json.loads(path.read_text())
        codes_now = [cause["code"] for cause in saved["incident"]["causes"]]
        self.assertIn("relay_blocked", codes_now, "new cause must be recorded even when suppressed")
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("relay_blocked", " ".join(saved["businessAtoms"]))
        rca.process([], state, POST, date=DATE, sender=sender, success=True,
                    success_evidence=SUCCESS_EVIDENCE)
        saved = json.loads(path.read_text())
        self.assertIsNone(saved["incident"], "recovery must not leave a stale incident")
        self.assertEqual(saved["history"][-1]["resolved_at"][:10], DATE)
        log_lines = (state / "research-chain-alert.log").read_text().splitlines()
        events = [json.loads(line)["event"] for line in log_lines]
        self.assertEqual(events.count("send"), 3)
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
        self.assertEqual(rca.main(["--phase", "drill", "--date", DATE]), 3)
        self.assertEqual(len(self.sent), 1, "uncertain drill must not be resent")

    def test_drill_dry_run_writes_nothing_and_sends_nothing(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(rca.main(["--phase", "drill", "--date", DATE, "--dry-run"]), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)

if __name__ == "__main__":
    unittest.main()
