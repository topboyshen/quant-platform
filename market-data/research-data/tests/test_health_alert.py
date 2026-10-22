"""Offline tests for the health-alert status, message and receipt contract."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import health_alert as alert  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 9, 20, 22, 46, tzinfo=CST)


def artifact(**changes):
    value = {"schemaVersion": 1, "generatedAt": NOW.isoformat(), "status": "critical",
             "issues": [{"code": "unresolved_trading_day_gaps", "severity": "critical"},
                        {"code": "qlib_source_absence", "severity": "warning"}]}
    value.update(changes)
    return value


class DescribeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "last.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload))
        return self.path

    def test_accepted_freeze_does_not_page_on_stale_or_gap_growth(self):
        path = self.write(artifact(
            acceptedFreeze=True,
            issues=[{"code": "unresolved_trading_day_gaps", "severity": "information",
                     "acceptedFreeze": True, "dates": ["2026-10-21"]},
                    {"code": "historical_rows_changed_or_missing", "severity": "critical",
                     "dates": ["2026-08-28"]}],
            supplyFreshness={"schema": "research.supply-freshness.v1", "source": "qlib",
                             "status": "stale", "dueDate": "2026-10-21",
                             "expectedTargetDate": "2026-10-21", "actualTargetDate": "2026-10-20",
                             "snapshotRequired": False}))
        info = alert.describe(path, now=NOW)
        atoms = alert.alert_atoms(info, "inactive", now=NOW)
        self.assertTrue(info["acceptedFreeze"])
        self.assertIn("historical_rows_changed_or_missing|2026-08-28", atoms)
        self.assertFalse(any(atom.startswith("unresolved_trading_day_gaps|") for atom in atoms))
        self.assertFalse(any(atom.startswith("qlib_supply_stale|") for atom in atoms))

    def test_missing_and_invalid_artifacts_alert(self):
        info = alert.describe(self.path, now=NOW)
        self.assertEqual((info["status"], info["reason"]), ("missing", "health_json_missing"))
        self.assertTrue(info["stale"])
        self.assertTrue(alert.should_alert(info, "inactive"))
        for body in ("not json", "[]", '{"status":NaN}'):
            self.path.write_text(body)
            info = alert.describe(self.path, now=NOW)
            self.assertEqual(info["status"], "unknown")
            self.assertTrue(alert.should_alert(info, "inactive"))

    def test_status_and_codes_are_validated_and_bounded(self):
        info = alert.describe(self.write(artifact()), now=NOW)
        self.assertEqual(info["status"], "critical")
        self.assertEqual(info["codes"], ["unresolved_trading_day_gaps", "qlib_source_absence"])
        self.assertFalse(info["stale"])
        self.assertTrue(alert.should_alert(info, "inactive"))
        # An unknown status word is never trusted, and odd issue codes are dropped.
        info = alert.describe(self.write(artifact(status="CRITICAL", issues=[
            {"code": "Bad Code"}, {"code": "ok_code"}, {"code": "x" * 100}])), now=NOW)
        self.assertEqual(info["status"], "unknown")
        self.assertEqual(info["codes"], ["ok_code"])
        # warning is a payload state, not an alert.
        info = alert.describe(self.write(artifact(status="warning")), now=NOW)
        self.assertFalse(alert.should_alert(info, "inactive"))
        # A failed audit unit alerts even when the stored artifact looks fine.
        self.assertTrue(alert.should_alert(alert.describe(self.write(artifact(status="ok")), now=NOW), "failed"))

    def test_stale_ok_or_warning_result_still_alerts(self):
        old = (NOW - dt.timedelta(hours=10)).isoformat()
        for status in ("ok", "warning"):
            with self.subTest(status=status, freshness="stale"):
                info = alert.describe(self.write(artifact(status=status, generatedAt=old)), now=NOW)
                self.assertTrue(info["stale"])
                self.assertTrue(alert.should_alert(info, "inactive"))
                lines = dict(line.split("=", 1) for line in alert._shell_lines(info, "inactive"))
                self.assertEqual(lines["ALERT"], "yes")
            with self.subTest(status=status, freshness="current"):
                info = alert.describe(self.write(artifact(status=status)), now=NOW)
                self.assertFalse(info["stale"])
                self.assertFalse(alert.should_alert(info, "inactive"))
                lines = dict(line.split("=", 1) for line in alert._shell_lines(info, "inactive"))
                self.assertEqual(lines["ALERT"], "no")
        # An unreadable timestamp is stale too, so an unparsable ok artifact alerts.
        info = alert.describe(self.write(artifact(status="ok", generatedAt="2026-09-20 22:45")), now=NOW)
        self.assertTrue(alert.should_alert(info, "inactive"))

    def test_stale_or_unreadable_time_is_reported_not_hidden(self):
        old = (NOW - dt.timedelta(hours=10)).isoformat()
        info = alert.describe(self.write(artifact(generatedAt=old)), now=NOW)
        self.assertTrue(info["stale"])
        self.assertEqual(info["reason"], "health_json_stale")
        self.assertAlmostEqual(info["ageHours"], 10, places=3)
        future = (NOW + dt.timedelta(hours=2)).isoformat()
        self.assertTrue(alert.describe(self.write(artifact(generatedAt=future)), now=NOW)["stale"])
        info = alert.describe(self.write(artifact(generatedAt="yesterday")), now=NOW)
        self.assertTrue(info["stale"])
        self.assertEqual(info["reason"], "health_generated_at_invalid")
        self.assertIsNone(info["generatedAt"])

    def test_shell_lines_only_expose_allowlisted_values(self):
        info = alert.describe(self.write(artifact(status="critical")), now=NOW)
        lines = dict(line.split("=", 1) for line in alert._shell_lines(info, "failed"))
        self.assertEqual(lines["STATUS"], "critical")
        self.assertEqual(lines["UNIT_STATE"], "failed")
        self.assertEqual(lines["ALERT"], "yes")
        self.assertEqual(lines["GENERATED_AT"], NOW.isoformat())
        weird = alert.describe(self.write(artifact(generatedAt="2026-09-20 22:45")), now=NOW)
        lines = dict(line.split("=", 1) for line in alert._shell_lines(weird, "not-a-state"))
        self.assertEqual(lines["GENERATED_AT"], "unknown")
        self.assertEqual(lines["UNIT_STATE"], "unknown")
        self.assertEqual(lines["ALERT"], "yes")
        quiet = alert.describe(self.write(artifact(status="ok")), now=NOW)
        self.assertEqual(dict(line.split("=", 1) for line in alert._shell_lines(quiet, "not-a-state"))["ALERT"], "no")


class MessageTests(unittest.TestCase):
    def test_message_is_bounded_and_carries_the_entry_context(self):
        info = alert.describe(Path("/nonexistent"), now=NOW)
        text = alert.message(info, hostname="VM-0-3-ubuntu", unit_state="failed",
                             now=NOW, entry_reason="audit_failed")
        for fragment in ("状态：missing", "审计单元：failed", "触发：audit_failed",
                         "审计时间：unknown", "数据新鲜度：陈旧（health_json_missing）",
                         "主机：VM-0-3-ubuntu", "本消息由发送回执确认"):
            self.assertIn(fragment, text)
        self.assertLessEqual(len(text), alert.MAX_MESSAGE_CHARS)
        # Injected values cannot smuggle arbitrary text into the alert.
        text = alert.message({"status": "ok", "codes": ["a" * 200], "reason": "bad reason",
                              "generatedAt": "now", "stale": False},
                             hostname="evil host\nsecond line", unit_state="bogus", entry_reason="x y")
        self.assertNotIn("evil host", text)
        self.assertIn("状态：ok", text)
        self.assertIn("主机：unknown", text)
        self.assertIn("审计单元：unknown", text)


class ReceiptTests(unittest.TestCase):
    def test_only_a_reported_success_with_message_id_is_delivered(self):
        raw = json.dumps({"success": True, "message_id": "12345", "target": "telegram:-100:7"}).encode()
        result = alert.parse_receipt(raw, 0)
        self.assertTrue(result["delivered"])
        self.assertEqual(result["reason"], "delivered")
        self.assertEqual(result["receipt"]["messageId"], "12345")
        self.assertEqual(result["receipt"]["target"], "telegram:-100:7")
        self.assertEqual(result["receipt"]["receiptSha256"], alert.hashlib.sha256(raw).hexdigest())

    def test_failure_paths_are_not_delivered(self):
        cases = [
            (json.dumps({"success": True, "skipped": True, "reason": "cron_auto_delivery_duplicate_target"}).encode(), 0, "receipt_skipped"),
            (json.dumps({"success": True}).encode(), 0, "receipt_missing_message_id"),
            (json.dumps({"success": True, "message_id": True}).encode(), 0, "receipt_missing_message_id"),
            (json.dumps({"error": "Adapter send failed: boom"}).encode(), 0, "receipt_not_success"),
            (b"", 0, "receipt_invalid_json"),
            (b"[]", 0, "receipt_not_object"),
            (json.dumps({"success": True, "message_id": "1"}).encode(), 1, "hermes_exit_nonzero"),
            (b"x" * (alert.MAX_RECEIPT_BYTES + 1), 0, "receipt_too_large"),
            (None, 0, "receipt_unreadable"),
        ]
        for raw, code, reason in cases:
            with self.subTest(reason=reason):
                result = alert.parse_receipt(raw, code)
                self.assertFalse(result["delivered"])
                self.assertEqual(result["reason"], reason)
                # Raw backend text never reaches the stored receipt.
                self.assertNotIn("boom", json.dumps(result["receipt"]))
                self.assertNotIn("cron_auto_delivery_duplicate_target", json.dumps(result["receipt"]))

    def test_receipt_is_stored_privately_and_rejects_symlinks(self):
        raw = json.dumps({"success": True, "message_id": "m" * 400, "target": "bad target!"}).encode()
        stored = {**alert.parse_receipt(raw, 0)["receipt"], "delivered": True, "reason": "delivered"}
        path = Path(self.temp_path()) / "receipt.json"
        alert.write_receipt(path, stored)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        written = json.loads(path.read_text())
        self.assertEqual(len(written["messageId"]), 128)
        self.assertNotIn("target", written)
        decoy = path.parent / "decoy.json"
        decoy.write_text("keep")
        link = path.parent / "link.json"
        link.symlink_to(decoy)
        with self.assertRaises(ValueError):
            alert.write_receipt(link, stored)
        self.assertEqual(decoy.read_text(), "keep")

    def temp_path(self):
        if not hasattr(self, "_temp"):
            self._temp = tempfile.TemporaryDirectory()
            self.addCleanup(self._temp.cleanup)
        return self._temp.name


if __name__ == "__main__":
    unittest.main()
