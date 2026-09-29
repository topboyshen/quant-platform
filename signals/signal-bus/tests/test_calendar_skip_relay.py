"""Signed current-day calendar proof: no generation, real POST or production data."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import test_research_relay as relay_cases
from tests.test_research_relay import DAY, NOW, relay


class CalendarSkipTests(unittest.TestCase):
    def setUp(self):
        self.case = relay_cases.RelayTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.run.update(status="skipped", phase="collect", tradingStatus="non_trading_day",
                             publicationGate="not_applicable", contextFile="work/morning-context.json")
        self.case.occurrence.update(status="skipped", startedAt=self.case.run["frozenCutoffInstant"],
                                    invocationId="2f454f14-e89f-477c-b0f3-5c0b66f55932")
        self.calendar = {"targetDate": DAY, "status": "non_trading_day",
                         "previousTradingDate": "2026-09-08", "nextTradingDate": "2026-09-10",
                         "dataAsOf": self.case.run["frozenCutoffInstant"],
                         "sourceIds": [relay.CALENDAR_SOURCE_ID]}
        self.write()

    def write(self):
        self.context = {"schemaVersion": "1.0", "data": {"tradingCalendar": self.calendar}}
        body = relay.encode_payload(self.context)
        self.case.write("work/morning-context.json", body)
        self.case.run["contextSha256"] = relay.sha256(body)
        self.case.write("work/last-run.json", self.case.run)
        self.case.write(f"work/orchestrator/states/morning-report-{DAY}.json", self.case.occurrence)

    def prepare(self):
        return relay.prepare_research(self.case.root, NOW)

    def confirmed_sender(self, payload, config, now):
        digest = relay.sha256(relay.encode_payload(payload))
        return {"status": "confirmed", "reason": "accepted", "ack_scope": "http_accepted",
                "receiver_storage": "receiver_reported_stored", "content_match": True,
                "payload_sha256": digest, "receiver_content_sha256": digest,
                "receiver_record_id": "fixture-calendar"}

    def test_valid_skip_is_an_independent_proof_without_fake_research(self):
        (self.case.root / f"reports/daily/{DAY}.md").unlink()
        prepared = self.prepare()
        self.assertEqual(prepared["kind"], "skip")
        payload = prepared["payload"]
        self.assertEqual(payload["as_of"], DAY)
        self.assertNotIn("research_contract", payload)
        self.assertNotIn("report_sha256", prepared["proof"])
        skip = payload["research_skip"]
        self.assertEqual(skip["status"], "not_scheduled")
        self.assertEqual(skip["market"], "XSHG")
        self.assertEqual(skip["proof"]["calendar_sha256"], relay.sha256(relay.encode_payload(skip["calendar"])))
        self.assertEqual(prepared["proof"]["skip_sha256"], relay.sha256(relay.encode_payload(skip)))

    def test_skip_dry_run_is_read_only_and_keeps_the_window(self):
        before = {str(p): p.read_bytes() for p in self.case.root.rglob("*") if p.is_file()}
        with patch.object(relay, "read_post_config", side_effect=AssertionError("config read")):
            outcome = self.case.execute(dry_run=True)
            self.assertEqual(outcome["status"], "ready")
            self.assertEqual(outcome["data_status"], "not_scheduled")
            self.assertFalse(outcome["would_send"])
            outside = relay.run_relay(self.case.root, self.case.bus, now=NOW.replace(minute=14))
            self.assertEqual(outside["reason"], "outside_send_window")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.case.root.rglob("*") if p.is_file()})
        self.assertFalse((self.case.bus / "out").exists())

    def test_skip_uses_hmac_transport_once_and_confirms_real_saved_content(self):
        self.case.config()
        sender = Mock(side_effect=self.confirmed_sender)
        first = self.case.execute(sender=sender)
        second = self.case.execute(sender=sender)
        self.assertEqual((first["status"], second["status"]), ("confirmed", "already_confirmed"))
        self.assertEqual(sender.call_count, 1)
        state = json.loads((self.case.bus / f"out/relay-delivery/{DAY}.json").read_bytes())
        self.assertEqual(state["payload_kind"], "skip")
        self.assertNotIn("report_sha256", state)
        self.assertEqual(state["receiver_content_sha256"], state["payload_sha256"])
        payload = self.prepare()["payload"]
        import hashlib, hmac
        response = Mock(status=200)
        response.read.return_value = relay.encode_payload({
            "status": "accepted", "source": "nng8", "day": DAY, "target_date": DAY,
            "record_id": "fixture-calendar", "stored": True,
            "content_sha256": relay.sha256(relay.encode_payload(payload))})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(relay.urllib.request, "build_opener", return_value=opener):
            outcome = relay.post_payload(payload, {"url": "https://example.org/ingest", "secret": "fixture"}, NOW)
        self.assertEqual(outcome["status"], "confirmed")
        request = opener.open.call_args.args[0]
        timestamp = str(int(NOW.timestamp()))
        expected = hmac.new(b"fixture", timestamp.encode() + b"." + request.data, hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-signature"), "sha256=" + expected)

    def test_unproven_skip_holds_instead_of_suppressing_missing_research(self):
        for field, value, reason in (
                ("status", "trading_day", "calendar_skip_proof_invalid"),
                ("sourceIds", ["unknown-calendar"], "calendar_skip_proof_invalid"),
                ("targetDate", "2026-09-08", "calendar_skip_proof_invalid"),
                ("previousTradingDate", DAY, "calendar_skip_coverage_invalid"),
                ("nextTradingDate", "2026-09-08", "calendar_skip_coverage_invalid"),
                ("dataAsOf", "2026-09-09T09:00:00+08:00", "calendar_skip_time_invalid")):
            original = self.calendar[field]
            self.calendar[field] = value
            self.write()
            with self.subTest(field=field):
                self.assertEqual(self.case.execute(dry_run=True)["reason"], reason)
            self.calendar[field] = original
        self.write()
        self.case.write("work/morning-context.json", b"{}")
        self.assertEqual(self.case.execute(dry_run=True)["reason"], "calendar_skip_context_invalid")

    def test_skip_cannot_replace_existing_research_or_mutate_after_confirmation(self):
        self.case.config()
        self.case.execute(sender=self.confirmed_sender)
        state_path = self.case.bus / f"out/relay-delivery/{DAY}.json"
        before = state_path.read_bytes()
        self.calendar["nextTradingDate"] = "2026-09-11"
        self.write()
        self.assertEqual(self.case.execute(sender=Mock())["reason"], "same_day_report_changed")
        self.assertEqual(state_path.read_bytes(), before)

    def test_skip_cannot_borrow_an_unclaimed_or_mismatched_run(self):
        for mapping, field, value in ((self.case.occurrence, "invocationId", "operator-recovery"),
                                       (self.case.occurrence, "childExitCode", 1),
                                       (self.case.occurrence, "status", "running"),
                                       (self.case.run, "frozenCutoffInstant", "2026-09-09T00:00:01Z"),
                                       (self.case.run, "publicationGate", "passed")):
            previous = mapping[field]
            mapping[field] = value
            self.write()
            with self.subTest(field=field):
                self.assertEqual(self.case.execute(dry_run=True)["status"], "held")
            mapping[field] = previous

    def test_skip_cannot_replace_a_confirmed_research_contract(self):
        self.case.config()
        self.case.execute(sender=self.confirmed_sender)
        state_path = self.case.bus / f"out/relay-delivery/{DAY}.json"
        state = json.loads(state_path.read_bytes())
        state.pop("skip_sha256")
        state.update(payload_kind="research", report_sha256="a" * 64)
        state_path.write_bytes(relay.encode_payload(state))
        before = state_path.read_bytes()
        sender = Mock(side_effect=AssertionError("attempted replacement send"))
        self.assertEqual(self.case.execute(sender=sender)["reason"], "same_day_contract_kind_changed")
        sender.assert_not_called()
        self.assertEqual(state_path.read_bytes(), before)

    def test_calendar_proof_passes_the_actual_sibling_renderer(self):
        gate = Path(os.environ.get("SIGNAL_GATE_TEST_ROOT", str(Path(__file__).resolve().parents[2] / "signal-gate")))
        source = gate / "app/digest_content.py"
        if not source.exists():
            self.skipTest("sibling integration source absent")
        spec = importlib.util.spec_from_file_location("calendar_consumer", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        inbox = self.case.bus / "inbox/nng8"
        inbox.mkdir(parents=True)
        raw = relay.encode_payload(self.prepare()["payload"])
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz or dt.timezone.utc)
        with patch.object(module.research_receipts, "datetime", Clock):
            module.research_receipts.capture(self.case.bus.resolve(), raw, now=NOW)
        (inbox / "digest-latest.json").write_bytes(raw)
        renderer = module.Renderer(self.case.bus.resolve(), self.case.bus / "absent.db", NOW.astimezone(module.TZ))
        text = "\n".join(renderer.highlights("nng8"))
        self.assertEqual(renderer.issues, [])
        self.assertTrue("非交易日" in text or "休市" in text or "无计划研究" in text)


if __name__ == "__main__":
    unittest.main()
