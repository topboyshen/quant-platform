"""Failure routing and evidence-based recovery; fake senders only."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_alert_wiring as wiring
from tests import test_research_chain_alert as fixtures
from tests import test_calendar_skip_relay as calendar_cases

rca = fixtures.rca


def healthy(**over):
    values = dict(occurrence=fixtures.occurrence(), last_run=fixtures.last_run(),
                  report_exists=True, report_sha256=fixtures.REPORT_SHA,
                  seal=fixtures.seal(), delivery=fixtures.receipt(), data_date="2026-09-19",
                  telegram_state="confirmed")
    values.update(over)
    return fixtures.facts(**values)


class RelayFailureRoutingTests(unittest.TestCase):
    def test_real_wrapper_routes_all_failure_results_and_keeps_exit_code(self):
        case = wiring.WrapperIntegrationTests()
        case.setUp()
        try:
            for status in ("held", "rejected", "not_sent", "failed", "already_failed",
                           "ambiguous", "accepted", "already_accepted", "unexpected_status"):
                with self.subTest(status=status):
                    case.marker.unlink(missing_ok=True)
                    result = case.run_wrapper(FAKE_RELAY_STATUS=status, FAKE_RELAY_RC="2")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertTrue(case.marker.exists(), result.stdout + result.stderr)
                    args = json.loads(case.marker.read_text())["argv"]
                    self.assertEqual(args, ["--phase", "post", "--relay-status",
                                            "unknown" if status == "unexpected_status" else status, "--json"])
            for status in ("confirmed", "already_confirmed", "confirmed_from_query", "skipped"):
                case.marker.unlink(missing_ok=True)
                result = case.run_wrapper(FAKE_RELAY_STATUS=status, FAKE_RELAY_RC="0")
                self.assertEqual(result.returncode, 0)
                self.assertFalse(case.marker.exists())
        finally:
            case.doCleanups()

    def test_failed_receipt_and_ambiguous_results_are_classified_before_deadline(self):
        for status, code in (("failed", "relay_blocked"), ("ambiguous", "relay_unconfirmed"),
                              ("accepted", "relay_unconfirmed")):
            with self.subTest(status=status):
                f = healthy(now=fixtures.PRE.replace(minute=1),
                            delivery=fixtures.receipt(status=status, reason="content_mismatch"))
                self.assertIn(code, fixtures.codes(rca.classify(f)))
                self.assertFalse(rca._success_evidence(f)[0])

    def test_wrapper_status_hint_covers_journal_lag_and_malformed_stdout(self):
        for status, code in (("failed", "relay_blocked"), ("unknown", "relay_unconfirmed")):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(status=status):
                f = healthy(now=fixtures.PRE.replace(minute=1), delivery=None)
                output = io.StringIO()
                with patch.object(rca, "collect", return_value=f), contextlib.redirect_stdout(output):
                    result = rca.main(["--phase", "post", "--relay-status", status,
                                       "--state-dir", tmp, "--dry-run", "--json"])
                self.assertEqual(result, 0)
                self.assertIn(code, json.loads(output.getvalue())["incidents"][0]["causes"])
                self.assertEqual(list(Path(tmp).iterdir()), [])


class ReceiverEvidenceTests(unittest.TestCase):
    def test_http_only_or_mismatching_confirmed_records_never_recover(self):
        for changes in ({"receiver_storage": "not_in_acknowledgement"},
                        {"receiver_storage": "receiver_reported_not_stored", "confirmed_via": "read_only_query"},
                        {"payload_sha256": None}, {"receiver_content_sha256": "d" * 64},
                        {"receiver_content_sha256": "garbage"}, {"content_match": False}):
            with self.subTest(changes=changes):
                f = healthy(delivery=fixtures.receipt(**changes))
                self.assertNotEqual(rca.delivery_evidence(f)[0], "confirmed")
                self.assertFalse(rca._success_evidence(f)[0])
                self.assertIn("predeadline_not_ready", fixtures.codes(rca.classify(f)))

    def test_read_only_query_with_matching_stored_hash_is_success(self):
        f = healthy(delivery=fixtures.receipt(receiver_storage="receiver_reported_stored",
                                              confirmed_via="read_only_query"))
        self.assertTrue(rca._success_evidence(f)[0])
        self.assertEqual(rca.classify(f), [])

    def test_suppressed_incident_recovers_despite_old_relay_failure(self):
        sender = fixtures.SenderFake()
        old_runs = [{"status": "held", "reason": "analysis_support_missing", "started": None}]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            def process(f):
                success, evidence = rca._success_evidence(f)
                return rca.process(rca.classify(f), state, fixtures.PRE, date=fixtures.DATE,
                                   sender=sender, success=success, success_evidence=evidence)
            process(healthy(delivery=None, relay_runs=old_runs))
            self.assertEqual(process(healthy(relay_runs=old_runs))["recovered"], 1)
            self.assertEqual(process(healthy(relay_runs=old_runs, draft={"status": "failed"}))["skipped"], 1)
            self.assertEqual(process(healthy(relay_runs=old_runs))["recovered"], 0)
            saved = json.loads(rca._state_path(state, fixtures.DATE).read_text())
            self.assertIsNone(saved["incident"])
            self.assertTrue(saved["notified_once"])
            self.assertEqual(len(saved["history"]), 1)
            self.assertTrue(saved["history"][-1]["resolution"]["seal_bound"])
            self.assertEqual(len(sender.sent), 2)

    def test_success_recovery_branch_clears_unnotified_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = rca._state_path(Path(tmp), fixtures.DATE)
            rca._save_state(path, {"version": 2, "notified_once": True,
                                  "incident": {"key": fixtures.DATE + ":research_chain",
                                               "causes": [{"code": "relay_blocked", "reason": "old_failure"}],
                                               "attempts": [{"outcome": "confirmed"}]}})
            historical = rca.classify(healthy(delivery=None, relay_runs=[
                {"status": "held", "reason": "old_failure", "started": None}]))
            result = rca.process(historical, Path(tmp), fixtures.PRE, date=fixtures.DATE,
                                 sender=fixtures.SenderFake(), success=True,
                                 success_evidence={"seal_bound": True})
            self.assertEqual(result["recovered"], 0)
            self.assertIsNotNone(json.loads(path.read_text())["incident"])
            result = rca.process([], Path(tmp), fixtures.PRE, date=fixtures.DATE,
                                 sender=fixtures.SenderFake(), success=True,
                                 success_evidence=fixtures.SUCCESS_EVIDENCE)
            self.assertEqual(result["recovered"], 1)
            self.assertIsNone(json.loads(path.read_text())["incident"])


class PostWindowRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sender = fixtures.SenderFake()

    def check(self, facts, phase):
        """Run the installed timer's CLI path with fixture reads and a fake sender."""
        output = io.StringIO()
        with patch.object(rca, "collect", return_value=facts), \
                patch.object(rca, "HermesSender", return_value=self.sender), \
                contextlib.redirect_stdout(output):
            result = rca.main(["--phase", phase, "--date", fixtures.DATE,
                               "--now", facts["now"].isoformat(),
                               "--state-dir", self.temp.name, "--json"])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["phase"], phase)
        return payload

    def test_0812_not_ready_0813_success_0815_recovers_once(self):
        first = self.check(healthy(delivery=None), "predeadline")
        self.assertEqual(first["incidents"][0]["causes"], ["predeadline_not_ready"])
        self.assertEqual(first["notified"], 1)
        completed = healthy(now=fixtures.PRE.replace(minute=13))
        self.assertTrue(rca._success_evidence(completed)[0])
        self.assertEqual(rca.classify(completed), [])
        # Success does not invoke the failure wrapper; the 08:15 timer observes it.
        recheck = {**completed, "now": fixtures.PRE.replace(minute=15)}
        recovered = self.check(recheck, "post")
        repeated = self.check(recheck, "post")
        self.assertEqual((recovered["recovered"], recovered["notified"]), (1, 1))
        self.assertEqual((repeated["recovered"], repeated["notified"]), (0, 0))
        self.assertEqual(len(self.sender.sent), 2)
        self.assertIn("研究链路恢复", self.sender.sent[1])
        state = json.loads(rca._state_path(Path(self.temp.name), fixtures.DATE).read_text())
        self.assertIsNone(state["incident"])

    def test_0812_healthy_then_late_fault_0815_notifies_once(self):
        first = self.check(healthy(), "predeadline")
        self.assertEqual((first["incidents"], first["notified"]), ([], 0))
        late = healthy(now=fixtures.PRE.replace(minute=14, second=30),
                       telegram_state="mismatch", telegram_wording="附件哈希与密封报告不一致")
        self.assertEqual(fixtures.codes(rca.classify(late)), ["telegram_mismatch"])
        recheck = {**late, "now": fixtures.PRE.replace(minute=15)}
        new_fault = self.check(recheck, "post")
        repeated = self.check(recheck, "post")
        self.assertEqual(new_fault["incidents"][0]["causes"], ["telegram_mismatch"])
        self.assertEqual(new_fault["notified"], 1)
        self.assertEqual(repeated["notified"], 0)
        self.assertEqual(len(self.sender.sent), 1)

    def test_same_fault_across_predeadline_and_post_notifies_once(self):
        failed = healthy(delivery=fixtures.receipt(status="failed", reason="content_mismatch"))
        first = self.check(failed, "predeadline")
        recheck = self.check({**failed, "now": fixtures.PRE.replace(minute=15)}, "post")
        self.assertEqual(first["incidents"][0]["causes"], ["relay_blocked"])
        self.assertEqual(recheck["incidents"][0]["causes"], ["relay_blocked"])
        self.assertEqual((first["notified"], recheck["notified"]), (1, 0))
        self.assertEqual(len(self.sender.sent), 1)


class CalendarAlertTests(unittest.TestCase):
    def test_skip_confirmation_needs_date_id_storage_and_matching_content(self):
        case = calendar_cases.CalendarSkipTests()
        case.setUp()
        try:
            case.case.config()
            case.case.execute(sender=case.confirmed_sender)
            now = calendar_cases.NOW.astimezone(rca.TZ).replace(minute=12)
            original = rca.collect(case.case.root, case.case.bus, date=calendar_cases.DAY, now=now, relay_log="")
            self.assertTrue(rca._success_evidence(original)[0])
            for changes in ({"receiver_record_id": None}, {"as_of": "2026-09-08"},
                            {"receiver_storage": "receiver_reported_not_stored"},
                            {"receiver_content_sha256": "d" * 64}, {"content_match": False},
                            {"payload_kind": "research"}):
                with self.subTest(changes=changes):
                    f = {**original, "delivery": {**original["delivery"], **changes}}
                    self.assertFalse(rca._success_evidence(f)[0])
                    self.assertNotEqual(rca.delivery_evidence(f)[0], "confirmed")
        finally:
            case.doCleanups()

    def test_skipped_status_alone_does_not_silence_invalid_calendar(self):
        f = fixtures.facts(occurrence=fixtures.occurrence("skipped"))
        self.assertIn("calendar_skip_invalid", fixtures.codes(rca.classify(f)))

    def test_collect_validates_skip_proof_and_requires_receiver_confirmation(self):
        case = calendar_cases.CalendarSkipTests()
        case.setUp()
        try:
            now = calendar_cases.NOW.astimezone(rca.TZ).replace(minute=12)
            f = rca.collect(case.case.root, case.case.bus, date=calendar_cases.DAY, now=now, relay_log="")
            self.assertIsNotNone(f["calendar_skip"])
            self.assertIn("predeadline_not_ready", fixtures.codes(rca.classify(f)))
            case.case.config()
            case.case.execute(sender=case.confirmed_sender)
            f = rca.collect(case.case.root, case.case.bus, date=calendar_cases.DAY, now=now, relay_log="")
            self.assertEqual(rca.classify(f), [])
            self.assertTrue(rca._success_evidence(f)[1]["calendar_bound"])
            case.case.write("work/morning-context.json", {"tampered": True})
            f = rca.collect(case.case.root, case.case.bus, date=calendar_cases.DAY, now=now, relay_log="")
            self.assertIn("calendar_skip_invalid", fixtures.codes(rca.classify(f)))
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
