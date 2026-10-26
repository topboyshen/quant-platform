"""One fixture matrix through BOTH consumers. Temporary files; zero real sends."""
import ast
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SIGNAL = Path(__file__).resolve().parents[1]
CONTRACT = SIGNAL / "scripts/delivery_evidence.py"
_DASHBOARD_CANDIDATES = (SIGNAL.parent / "ops" / "dashboard", SIGNAL.parent / "dashboard")
DASHBOARD = Path(os.environ.get(
    "DASHBOARD_TEST_ROOT",
    next((str(path) for path in _DASHBOARD_CANDIDATES if path.is_dir()), str(_DASHBOARD_CANDIDATES[0])),
))
sys.path.insert(0, str(DASHBOARD / "exporter"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Required isolated fixture code missing: " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Missing sibling fixture is an error, never a skipped negative matrix.
fixture = load("shared_delivery_dashboard_fixture", DASHBOARD / "exporter/tests/test_export_dashboard.py")
exporter = fixture.exporter
exporter.DELIVERY_CONTRACT_PATH = CONTRACT
alert = load("shared_delivery_alert", SIGNAL / "scripts/research_chain_alert.py")
relay = alert._RELAY
contract = relay._DELIVERY
DATE, DAY = fixture.DATE, fixture.PREVIOUS


class SharedDeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.meta = fixture.build_fixture(self.root)
        self.project = self.root / "AStock_AI_System"
        self.report_sha = self.meta["record"]["reportSha256"]
        self.event = f"astock-{DATE}-success-telegram-v1"
        self.sent_path = self.project / f"work/notifications/sent/{self.event}.json"
        self.receipt_path = self.project / f"work/notifications/receipts/send/{self.event}.json"
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        self.relay_path = self.root / f"signal-bus/out/relay-delivery/{DATE}.json"
        self.sent = json.loads(self.sent_path.read_text())
        payload = self.meta["report"].read_bytes()
        self.receipt = {"schemaVersion": "1.0", "eventId": self.event, "targetDate": DATE,
            "route": "telegram", "payloadSha256": self.report_sha, "payloadBytes": len(payload),
            "platformMessageId": "4242", "ackKind": "hermes_cli_json_message_id",
            "confirmedAt": f"{DATE}T00:02:00.000Z"}
        self.delivery = {"status": "confirmed", "reason": "accepted", "target_date": DATE,
            "as_of": DAY, "payload_kind": "research", "report_sha256": self.report_sha,
            "payload_sha256": "c" * 64, "receiver_content_sha256": "c" * 64,
            "receiver_storage": "receiver_reported_stored", "receiver_record_id": "fixed-record",
            "content_match": True}
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("network forbidden")).start()
        exporter.DELIVERY_CONTRACT_PATH = CONTRACT

    def layer(self, name):
        return exporter.build_delivery(self.root, fixture.NOW, {})["layers"][name]

    def test_relay_same_positive_and_negative_matrix(self):
        cases = (
            ("POST", {}, "pass"),
            ("GET", {"reason": "confirmed_via_read_only_query", "confirmed_via": "read_only_query"}, "pass"),
            ("GET missing binding", {"reason": "confirmed_via_read_only_query"}, "insufficient"),
            ("accepted only", {"status": "accepted"}, "insufficient"),
            ("wrong report date", {"target_date": DAY}, "fail"),
            ("wrong older as_of", {"as_of": "2026-09-17"}, "fail"),
            ("wrong current as_of", {"as_of": DATE}, "fail"),
            ("missing as_of", {"as_of": None}, "insufficient"),
            ("missing id", {"receiver_record_id": ""}, "insufficient"),
            ("not stored", {"receiver_storage": "not_in_acknowledgement"}, "insufficient"),
            ("wrong hash", {"receiver_content_sha256": "d" * 64}, "fail"),
            ("missing hash", {"receiver_content_sha256": None}, "insufficient"),
            ("wrong source", {"report_sha256": "d" * 64}, "fail"),
            ("content conflict", {"content_match": False}, "fail"),
            ("content not proven", {"content_match": None}, "insufficient"),
        )
        for label, changes, expected in cases:
            with self.subTest(label=label):
                record = {**self.delivery, **changes}
                self.relay_path.write_text(json.dumps(record))
                shared = contract.relay_verdict(record, report_date=DATE,
                    report_sha256=self.report_sha, data_date=DAY)
                state, _, evidence = alert.delivery_evidence({"date": DATE,
                    "delivery": record, "report_sha256": self.report_sha, "data_date": DAY})
                dashboard = self.layer("relay")
                self.assertEqual(shared["status"], expected)
                self.assertEqual(dashboard["status"], expected)
                self.assertEqual(state, shared["state"])
                self.assertEqual(dashboard["contractReason"], shared["reason"])
                self.assertEqual(state == "confirmed", expected == "pass")

    def test_independent_expected_date_cannot_be_supplied_by_receipt(self):
        self.relay_path.write_text(json.dumps(self.delivery))
        (self.meta["archive"] / "input.json").write_text("{}")
        self.assertEqual(self.layer("relay")["status"], "insufficient")
        self.assertFalse(relay.local_delivery_is_confirmed(self.delivery,
            report_date=DATE, report_sha256=self.report_sha, data_date=None)[0])

    def test_current_data_day_requires_independent_sealed_binding_too(self):
        frozen_path = self.meta["archive"] / "input.json"
        frozen = json.loads(frozen_path.read_text())
        frozen["compilerContext"]["data"]["previousAShareSession"]["sessionDate"] = DATE
        frozen_path.write_text(json.dumps(frozen))
        published_path = self.meta["archive"] / "published.json"
        published = json.loads(published_path.read_text())
        published["inputSha256"] = fixture.sha(frozen_path.read_bytes())
        published_path.write_text(json.dumps(published))
        record = {**self.delivery, "as_of": DATE}
        self.relay_path.write_text(json.dumps(record))
        self.assertEqual(self.layer("relay")["status"], "pass")
        self.assertEqual(alert.delivery_evidence({"date": DATE, "delivery": record,
            "report_sha256": self.report_sha, "data_date": DATE})[0], "confirmed")

    def test_telegram_same_positive_and_negative_matrix(self):
        cases = (
            ("raw original", {}, {}, "pass"),
            ("zoned offset", {}, {"confirmedAt": f"{DATE}T08:02:00+08:00"}, "pass"),
            ("missing outcome", {"outcome": None}, {}, "insufficient"),
            ("missing id", {}, {"platformMessageId": ""}, "insufficient"),
            ("bool id", {}, {"platformMessageId": True}, "insufficient"),
            ("wrong route", {"route": "other"}, {}, "fail"),
            ("wrong receipt event", {}, {"eventId": f"astock-{DAY}-success-telegram-v1"}, "fail"),
            ("wrong receipt date", {}, {"targetDate": DAY}, "fail"),
            ("wrong receipt route", {}, {"route": "other"}, "fail"),
            ("missing receipt event", {}, {"eventId": None}, "insufficient"),
            ("invalid receipt event", {}, {"eventId": "garbage"}, "insufficient"),
            ("missing receipt date", {}, {"targetDate": None}, "insufficient"),
            ("invalid receipt date", {}, {"targetDate": "invalid"}, "insufficient"),
            ("missing receipt route", {}, {"route": None}, "insufficient"),
            ("invalid receipt route", {}, {"route": []}, "insufficient"),
            ("hash conflict", {}, {"payloadSha256": "d" * 64}, "fail"),
            ("bytes conflict", {}, {"payloadBytes": 1}, "fail"),
            ("bool receipt bytes", {}, {"payloadBytes": True}, "insufficient"),
            ("bool event bytes", {"report": {"bytes": True}}, {}, "insufficient"),
            ("timezone absent", {}, {"confirmedAt": f"{DATE}T00:02:00"}, "insufficient"),
            ("time invalid", {}, {"confirmedAt": f"{DATE}T25:02:00Z"}, "insufficient"),
            ("unknown receipt field", {}, {"inferredSuccess": True}, "insufficient"),
            ("embedded failure", {"receipt": {"success": False}}, {}, "fail"),
            ("embedded failed status", {"receipt": {"status": "failed"}}, {}, "fail"),
            ("presentation cannot downgrade", {"presentation": None}, {}, "insufficient"),
        )
        for label, sent_change, receipt_change, expected in cases:
            with self.subTest(label=label):
                sent = copy.deepcopy(self.sent)
                update = copy.deepcopy(sent_change)
                if "report" in update:
                    sent["report"].update(update.pop("report"))
                sent.update(update)
                self.sent_path.write_text(json.dumps(sent))
                self.receipt_path.write_text(json.dumps({**self.receipt, **receipt_change}))
                state, _, evidence = alert.telegram_delivery_evidence(self.project, DATE, self.report_sha)
                dashboard = self.layer("telegram")
                self.assertEqual(evidence["contract_status"], expected)
                self.assertEqual(dashboard["status"], expected)
                self.assertEqual(dashboard["contractReason"], evidence["contract_reason"])
                self.assertEqual(state == "confirmed", expected == "pass")

    def test_result_payload_requires_source_hash_binding(self):
        sent = copy.deepcopy(self.sent)
        sent["report"].update(sourceSha256=self.report_sha, presentation="result-v1")
        receipt = {**self.receipt, "sourceSha256": self.report_sha}
        for value, expected in ((self.report_sha, "pass"), ("d" * 64, "fail"), (None, "insufficient")):
            with self.subTest(source=value):
                self.sent_path.write_text(json.dumps(sent))
                self.receipt_path.write_text(json.dumps({**receipt, "sourceSha256": value}))
                _, _, evidence = alert.telegram_delivery_evidence(self.project, DATE, self.report_sha)
                self.assertEqual(evidence["contract_status"], expected)
                self.assertEqual(self.layer("telegram")["status"], expected)

    def test_old_confirmed_is_not_resent_or_silently_repaired(self):
        old = {"version": 1, "target_date": DATE, "as_of": DAY,
               "report_sha256": self.report_sha, "status": "confirmed", "reason": "accepted"}
        self.relay_path.write_text(json.dumps(old))
        before = self.relay_path.read_bytes()
        prepared = {"target_date": DATE, "as_of": DAY, "kind": "research",
            "proof": {"report_sha256": self.report_sha, "last_run_sha256": "e" * 64},
            "payload": {"research_contract": {"data_status": "live"}},
            "completed_at": dt.datetime(2026, 9, 21, 8, 0, tzinfo=relay.SHANGHAI)}
        with patch.object(relay, "prepare_research", return_value=prepared), \
             patch.object(relay, "read_post_config", return_value={}), \
             patch.object(relay, "post_payload", side_effect=AssertionError("real send forbidden")):
            result = relay.run_relay(project_root=self.project, bus_root=self.root / "signal-bus",
                now=dt.datetime(2026, 9, 21, 8, 5, tzinfo=relay.SHANGHAI),
                sender=lambda *a: self.fail("old confirmed record must suppress sending"))
        # Suppressed (no resend) but NOT reported as confirmed: proof is incomplete.
        self.assertEqual(result["status"], "already_recorded_unverified", result)
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.relay_path.read_bytes(), before)
        with patch.object(relay, "_DELIVERY", None), \
             patch.object(relay, "prepare_research", return_value=prepared), \
             patch.object(relay, "read_post_config", return_value={}):
            result = relay.run_relay(project_root=self.project, bus_root=self.root / "signal-bus",
                now=dt.datetime(2026, 9, 21, 8, 5, tzinfo=relay.SHANGHAI),
                sender=lambda *a: self.fail("missing proof code must not authorize a resend"))
        self.assertEqual(result["status"], "already_recorded_unverified", result)
        self.assertEqual(result["reason"], "delivery_contract_unavailable")
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.relay_path.read_bytes(), before)
        self.assertEqual(self.layer("relay")["status"], "insufficient")
        old_sent = self.sent_path.read_bytes()
        self.assertNotEqual(alert.telegram_delivery_evidence(self.project, DATE, self.report_sha)[0], "confirmed")
        self.assertEqual(self.layer("telegram")["status"], "insufficient")
        self.assertEqual(self.sent_path.read_bytes(), old_sent)

    def test_missing_or_bad_module_degrades_proof_and_recovers_after_install(self):
        self.relay_path.write_text(json.dumps(self.delivery))
        self.receipt_path.write_text(json.dumps(self.receipt))
        missing = self.root / "trusted-code/delivery_evidence.py"
        for path in (missing, self.root / "bad.py"):
            if path.name == "bad.py":
                path.write_text("invalid python syntax !")
            with patch.object(exporter, "DELIVERY_CONTRACT_PATH", path):
                panel = exporter.build_delivery(self.root, fixture.NOW, {})
                self.assertIn("delivery_contract_unavailable", panel["readErrors"])
                self.assertEqual(panel["layers"]["seal"]["status"], "pass")
                for name in ("telegram", "relay"):
                    self.assertEqual(panel["layers"][name]["status"], "insufficient")
        # A receipt/source tree may not provide executable replacement code.
        malicious = self.root / "signal-bus/scripts/delivery_evidence.py"
        malicious.parent.mkdir(parents=True)
        malicious.write_text("raise AssertionError('evidence-root code executed')")
        self.assertEqual(self.layer("relay")["status"], "pass")
        self.assertEqual(self.layer("telegram")["status"], "pass")

    def test_contract_is_pure_and_default_path_is_not_source_root(self):
        tree = ast.parse(CONTRACT.read_text())
        imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
        self.assertEqual(imports, {"datetime", "re"})
        forbidden = {"open", "exec", "eval", "__import__", "compile"}
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id in forbidden for node in ast.walk(tree)))
        self.assertEqual(exporter.load_delivery_contract(CONTRACT).CONTRACT_VERSION, 1)
        self.assertEqual(exporter.build_parser().parse_args([]).delivery_contract_module, None)

    def test_existing_alert_units_are_observed_not_scheduled(self):
        collector = load("shared_delivery_collector", DASHBOARD / "exporter/collect_services.py")
        alert_units = {"research-chain-alert.timer", "research-chain-alert.service",
            "research-chain-alert-post.service", "research-chain-alert-generation.service"}
        self.assertTrue(alert_units <= set(exporter.KNOWN_UNITS))
        self.assertTrue(alert_units <= {unit for unit, scope, _ in collector.UNITS if scope == "user"})

    def test_signal_partial_installation_is_insufficient_not_an_import_crash(self):
        code = self.root / "trusted-code"
        code.mkdir()
        for name in ("research_relay.py", "research_chain_alert.py"):
            (code / name).write_bytes((SIGNAL / "scripts" / name).read_bytes())
        for label, body in (("missing", None), ("syntax", "syntax invalid !"),
                            ("wrong API", "CONTRACT_VERSION = 1")):
            with self.subTest(label=label):
                if body is not None:
                    (code / "delivery_evidence.py").write_text(body)
                partial = load("partial_delivery_alert", code / "research_chain_alert.py")
                self.assertIsNone(partial._RELAY._DELIVERY)
                _, _, evidence = partial.delivery_evidence({"date": DATE,
                    "delivery": self.delivery, "report_sha256": self.report_sha, "data_date": DAY})
                self.assertEqual(evidence["contract_status"], "insufficient")
                self.assertEqual(evidence["contract_reason"], "delivery_contract_unavailable")
                _, _, evidence = partial.telegram_delivery_evidence(self.project, DATE, self.report_sha)
                self.assertEqual(evidence["contract_status"], "insufficient")
        (code / "delivery_evidence.py").write_bytes(CONTRACT.read_bytes())
        installed = load("complete_delivery_alert", code / "research_chain_alert.py")
        self.assertEqual(installed.delivery_evidence({"date": DATE,
            "delivery": self.delivery, "report_sha256": self.report_sha, "data_date": DAY})[0], "confirmed")


if __name__ == "__main__":
    unittest.main()
