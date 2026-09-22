#!/usr/bin/env python3
"""Wiring leftovers: OnFailure reset, target env, wrapper exit code, fake sender."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
WRAPPER = ROOT / "scripts" / "run_research_relay_with_alert.sh"

SPEC = importlib.util.spec_from_file_location(
    "research_chain_alert_wiring",
    ROOT / "scripts" / "research_chain_alert.py")
assert SPEC and SPEC.loader
rca = importlib.util.module_from_spec(SPEC)
sys.modules["research_chain_alert_wiring"] = rca
SPEC.loader.exec_module(rca)

DATE = "2026-09-23"
PRE = datetime(2026, 9, 23, 8, 12, tzinfo=rca.TZ)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class UnitFileTests(unittest.TestCase):
    def test_predeadline_and_digest_load_the_same_target_env(self):
        pre = read(SYSTEMD / "research-chain-alert.service")
        digest = read(SYSTEMD / "nng8-research-digest.service")
        generation = read(SYSTEMD / "research-chain-alert-generation.service")
        post = read(SYSTEMD / "research-chain-alert-post.service")
        env_line = "EnvironmentFile=/home/ubuntu/.config/systemd/user/research-chain-alert.env"
        for text, name in ((pre, "predeadline"), (digest, "digest"),
                           (generation, "generation"), (post, "post")):
            with self.subTest(unit=name):
                self.assertIn(env_line, text)
        self.assertNotIn("%i", pre)
        self.assertNotIn("Environment=RESEARCH_CHAIN_TELEGRAM_CHAT=", pre)

    def test_seal_and_draft_onfailure_reset_the_old_alert(self):
        for rel in ("astock-research-seal.service.d/failure-alert.conf",
                    "astock-xhs-draft.service.d/failure-alert.conf"):
            text = read(SYSTEMD / rel)
            with self.subTest(drop_in=rel):
                self.assertRegex(text, r"(?m)^OnFailure=$")
                self.assertRegex(text, r"(?m)^OnFailure=research-chain-alert-post\.service$")
                self.assertNotIn("astock-seal-failure-alert.service", text)

    def test_wrapper_keeps_the_relay_exit_status(self):
        text = read(WRAPPER)
        self.assertRegex(text, r'(?m)^exit "\$rc"$')
        self.assertNotRegex(text, r"(?m)^exit 0$")
        self.assertIn("RESEARCH_CHAIN_ENV_FILE", text)
        self.assertIn("source \"$ENV_FILE\"", text)


class WrapperIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.marker = self.root / "alert.json"
        self.env_file = self.root / "alert.env"
        self.env_file.write_text("RESEARCH_CHAIN_TELEGRAM_CHAT=wiring-test-chat\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        (self.scripts / "research_relay.py").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "payload = {\n"
            "  'status': os.environ.get('FAKE_RELAY_STATUS', 'held'),\n"
            "  'sent': int(os.environ.get('FAKE_RELAY_SENT', '0')),\n"
            "  'reason': os.environ.get('FAKE_RELAY_REASON', 'required_artifact_or_config_invalid'),\n"
            "}\n"
            "print(json.dumps(payload))\n"
            "sys.exit(int(os.environ.get('FAKE_RELAY_RC', '1')))\n".replace("'", '"'),
            encoding="utf-8")
        (self.scripts / "research_chain_alert.py").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALERT_MARKER']).write_text(json.dumps({\n"
            "  'argv': sys.argv[1:],\n"
            "  'chat_configured': bool(os.environ.get('RESEARCH_CHAIN_TELEGRAM_CHAT')),\n"
            "  'chat_matches': os.environ.get('RESEARCH_CHAIN_TELEGRAM_CHAT') == 'wiring-test-chat',\n"
            "}), encoding='utf-8')\n"
            "print(json.dumps({'phase': 'post', 'notified': 0}))\n",
            encoding="utf-8")

    def run_wrapper(self, **env) -> subprocess.CompletedProcess:
        merged = os.environ.copy()
        merged.update({
            "BUS_ROOT": str(self.root),
            "PYTHON": sys.executable,
            "RESEARCH_CHAIN_ENV_FILE": str(self.env_file),
            "ALERT_MARKER": str(self.marker),
            "HOME": str(self.root / "home"),
        })
        merged.update(env)
        return subprocess.run(
            ["bash", str(WRAPPER)], capture_output=True, text=True, env=merged, check=False)

    def test_relay_failure_exit_is_preserved_and_alert_sees_target(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.marker.is_file(), result.stdout)
        payload = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(payload["argv"], ["--phase", "post", "--json"])
        self.assertTrue(payload["chat_configured"])
        self.assertTrue(payload["chat_matches"])

    def test_confirmed_relay_is_success_and_does_not_alert(self):
        result = self.run_wrapper(FAKE_RELAY_STATUS="confirmed", FAKE_RELAY_SENT="1",
                                  FAKE_RELAY_REASON="", FAKE_RELAY_RC="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertIn("research_relay confirmed", result.stdout)

    def test_benign_hold_does_not_alert_and_keeps_zero(self):
        result = self.run_wrapper(FAKE_RELAY_STATUS="held", FAKE_RELAY_SENT="0",
                                  FAKE_RELAY_REASON="outside_send_window", FAKE_RELAY_RC="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_missing_target_env_is_visible_to_the_alert(self):
        result = self.run_wrapper(RESEARCH_CHAIN_ENV_FILE=str(self.root / "missing.env"))
        self.assertEqual(result.returncode, 1)
        payload = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertFalse(payload["chat_configured"])


class FakeSenderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.sent = []

        class FakeSender:
            def send(inner, message):
                self.sent.append(message)
                return "confirmed", "ack", {"message_id": "42", "chat_id": "wiring-test-chat"}

        self.original_sender = rca.HermesSender
        self.original_expected = rca.EXPECTED_CHAT
        rca.HermesSender = FakeSender
        rca.EXPECTED_CHAT = "wiring-test-chat"

    def tearDown(self):
        rca.HermesSender = self.original_sender
        rca.EXPECTED_CHAT = self.original_expected

    def test_predeadline_incident_notifies_once_through_fake_sender(self):
        items = [{
            "key": f"{DATE}:research_chain",
            "date": DATE,
            "causes": [{"code": "predeadline_not_ready", "detail": "08:12 检查：研究未就绪"}],
            "delivery_state": "missing",
            "delivery_wording": "尚未投递",
            "delivery_evidence": {},
            "success_evidence": {},
            "in_flight": False,
        }]
        first = rca.process(items, self.state, PRE, date=DATE, sender=rca.HermesSender())
        second = rca.process(items, self.state, PRE, date=DATE, sender=rca.HermesSender())
        self.assertEqual(first["notified"], 1)
        self.assertEqual(second["notified"], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("研究链路告警", self.sent[0])
        self.assertNotIn("演练", self.sent[0])

    def test_unconfigured_target_stays_uncertain_and_does_not_call_platform(self):
        rca.EXPECTED_CHAT = ""
        sender = self.original_sender()
        outcome, detail, extra = sender.send("should not go out")
        self.assertEqual(outcome, "uncertain")
        self.assertEqual(detail, "expected_chat_not_configured")
        self.assertEqual(self.sent, [])
        self.assertEqual(extra, {})


if __name__ == "__main__":
    unittest.main()
