#!/usr/bin/env python3
"""Offline tests for the read-only service collector.

Fixtures only: subprocess is mocked with canned `systemctl show` output. No
system access, no writes outside a temp directory.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("collect_services", ROOT / "collect_services.py")
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)

CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 9, 20, 11, 0, tzinfo=CST)
NEXT_EPOCH = int(dt.datetime(2026, 9, 21, 8, 0, tzinfo=CST).timestamp())
LAST_EPOCH = int(dt.datetime(2026, 9, 18, 8, 0, 14, tzinfo=CST).timestamp())


def show(**properties) -> str:
    return "".join(f"{key}={value}\n" for key, value in properties.items())


def collect_with(mapping):
    """mapping: unit -> (returncode, stdout)"""
    def fake(command, **kwargs):
        unit = command[-1]
        code, stdout = mapping.get(unit, (1, ""))
        return mock.Mock(returncode=code, stdout=stdout, stderr="")
    with mock.patch.object(collector.subprocess, "run", side_effect=fake) as run:
        payload = collector.collect(NOW)
        return payload, run


class ClassificationTests(unittest.TestCase):
    def entry(self, payload, unit):
        return next(item for item in payload["units"] if item["unit"] == unit)

    def test_normally_finished_oneshot_is_healthy(self):
        payload, _ = collect_with({"astock-xhs-draft.service": (0, show(
            Id="astock-xhs-draft.service", LoadState="loaded", Type="oneshot",
            ActiveState="inactive", SubState="dead", Result="success", ExecMainStatus="0",
            UnitFileState="static", ExecMainExitTimestamp=f"@{LAST_EPOCH}", NRestarts="0"))})
        entry = self.entry(payload, "astock-xhs-draft.service")
        self.assertEqual(entry["health"], "ok")
        self.assertIn("oneshot", entry["note"])
        self.assertEqual(entry["execMainStatus"], "0")
        self.assertTrue(entry["lastExit"])

    def test_stopped_long_running_service_is_not_reported_as_a_finished_oneshot(self):
        """Real counterexample: an inactive long-running service used to read ok."""
        payload, _ = collect_with({"research-ledger.service": (0, show(
            Id="research-ledger.service", LoadState="loaded", Type="simple",
            ActiveState="inactive", SubState="dead", Result="success", ExecMainStatus="0",
            UnitFileState="enabled", ExecMainExitTimestamp=f"@{LAST_EPOCH}",
            ExecMainStartTimestamp=f"@{LAST_EPOCH}"))})
        entry = self.entry(payload, "research-ledger.service")
        self.assertEqual(entry["health"], "stopped")
        self.assertIn("常驻服务未在运行", entry["note"])
        self.assertNotIn("正常结束", entry["note"])

    def test_oneshot_that_never_ran_is_unknown_not_ok(self):
        """Real shape of a never-executed oneshot: Result=success, no timestamps."""
        payload, _ = collect_with({"research-data-health-alert.service": (0, show(
            Id="research-data-health-alert.service", LoadState="loaded", Type="oneshot",
            ActiveState="inactive", SubState="dead", Result="success", ExecMainStatus="0",
            UnitFileState="static", ExecMainStartTimestamp="", ExecMainExitTimestamp=""))})
        entry = self.entry(payload, "research-data-health-alert.service")
        self.assertEqual(entry["health"], "unknown")
        self.assertIn("从未执行", entry["note"])

    def test_disabled_unit_is_disabled_without_attributing_a_cause(self):
        payload, _ = collect_with({"nng8-research-digest.timer": (0, show(
            Id="nng8-research-digest.timer", LoadState="loaded", ActiveState="inactive",
            SubState="dead", UnitFileState="disabled"))})
        entry = self.entry(payload, "nng8-research-digest.timer")
        self.assertEqual(entry["health"], "disabled")
        self.assertIn("UnitFileState=disabled", entry["note"])
        self.assertNotIn("FROZEN", entry["note"])

    def test_enabled_but_inactive_timer_is_stopped(self):
        """An enabled timer that is not running must not be excused as disabled."""
        payload, _ = collect_with({"astock-xhs-source.timer": (0, show(
            Id="astock-xhs-source.timer", LoadState="loaded", ActiveState="inactive",
            SubState="dead", UnitFileState="enabled", NextElapseUSecRealtime=""))})
        entry = self.entry(payload, "astock-xhs-source.timer")
        self.assertEqual(entry["health"], "stopped")
        self.assertIn("已启用但当前未在运行", entry["note"])
        self.assertNotIn("FROZEN", entry["note"])

    def test_active_calendar_timer_reports_next_elapse(self):
        """Real format: NextElapseUSecRealtime stays human readable even with
        --timestamp=unix, while Exec timestamps become @<epoch>."""
        payload, _ = collect_with({"astock-xhs-source.timer": (0, show(
            Id="astock-xhs-source.timer", LoadState="loaded", ActiveState="active",
            SubState="waiting", UnitFileState="enabled",
            NextElapseUSecRealtime="Mon 2026-09-21 08:00:00 CST",
            NextElapseUSecMonotonic="0",
            LastTriggerUSec="Fri 2026-09-18 08:00:14 CST"))})
        entry = self.entry(payload, "astock-xhs-source.timer")
        self.assertEqual(entry["health"], "ok")
        self.assertEqual(entry["nextElapse"], dt.datetime.fromtimestamp(NEXT_EPOCH, CST).isoformat())
        self.assertEqual(entry["nextElapseSource"], "realtime")
        self.assertEqual(entry["lastTrigger"], dt.datetime.fromtimestamp(LAST_EPOCH, CST).isoformat())

    def test_monotonic_timer_is_reduced_by_the_current_monotonic_clock(self):
        """Real values: uptime ~1w5d, target 1w 5d 30min -> about 1 min from now."""
        baseline = dt.datetime(2026, 9, 19, 21, 16, 0, tzinfo=CST)
        target = 604800 + 5 * 86400 + 30 * 60 + 11.194797   # 1038611.194797 s since boot
        reference = 1038544.806                              # time.monotonic() at that moment
        expected = baseline + dt.timedelta(seconds=target - reference)
        with mock.patch.object(collector.subprocess, "run", side_effect=lambda command, **kw: (
                mock.Mock(returncode=0, stdout=show(
                    Id="hermes-telegram-watchdog.timer", LoadState="loaded", ActiveState="active",
                    SubState="waiting", UnitFileState="enabled", NextElapseUSecRealtime="",
                    NextElapseUSecMonotonic="1w 5d 30min 11.194797s"), stderr=""))):
            payload = collector.collect(baseline, monotonic_now=reference)
        entry = self.entry(payload, "hermes-telegram-watchdog.timer")
        self.assertEqual(entry["nextElapseSource"], "monotonic")
        self.assertEqual(entry["nextElapse"], expected.isoformat())
        self.assertEqual(collector.parse_timestamp("1w 5d 30min 11.194797s", baseline, reference),
                         expected)

    def test_failed_service_is_a_fault(self):
        for result, active in (("failed", "failed"), ("exit-code", "inactive"),
                               ("timeout", "failed")):
            with self.subTest(result=result):
                payload, _ = collect_with({"research-ledger.service": (0, show(
                    Id="research-ledger.service", LoadState="loaded", Type="simple",
                    ActiveState=active, SubState="failed", Result=result, ExecMainStatus="1"))})
                entry = self.entry(payload, "research-ledger.service")
                self.assertEqual(entry["health"], "failed")
                self.assertIn(result, entry["note"])

    def test_missing_unit_is_unknown_never_a_silent_pass(self):
        payload, _ = collect_with({"research-data-health.timer": (0, show(
            Id="research-data-health.timer", LoadState="not-found", ActiveState="inactive",
            SubState="dead", UnitFileState=""))})
        entry = self.entry(payload, "research-data-health.timer")
        self.assertEqual(entry["health"], "unknown")
        self.assertIn("不存在", entry["note"])

    def test_systemctl_failure_is_recorded_not_hidden(self):
        payload, _ = collect_with({})
        self.assertTrue(payload["readErrors"])
        self.assertTrue(all(item["health"] == "unknown" for item in payload["units"]))
        self.assertEqual(len(payload["units"]), len(collector.UNITS))

    def test_all_three_printed_shapes_parse_and_garbage_does_not(self):
        self.assertEqual(collector.parse_timestamp("@1789689614"),
                         dt.datetime.fromtimestamp(1789689614, CST))
        self.assertEqual(collector.parse_timestamp("Mon 2026-09-21 08:00:00 CST"),
                         dt.datetime(2026, 9, 21, 8, 0, tzinfo=CST))
        base = dt.datetime(2026, 9, 19, 21, 16, tzinfo=CST)
        # Monotonic instants are always evaluated against an explicit reference,
        # never against the test host's own clock.
        self.assertEqual(collector.parse_timestamp("30min", base, 0.0),
                         base + dt.timedelta(minutes=30))
        self.assertEqual(collector.parse_timestamp("11.194797s", base, 0.0),
                         base + dt.timedelta(seconds=11.194797))
        for value in ("infinity", "", "  ", "0", "not-a-number", "@", "0s", None, "1 fortnight",
                      "800d"):  # ~788 days ahead of the reference: implausible, refused
            self.assertIsNone(collector.parse_timestamp(value, base, 1_000_000.0), str(value))
        # A monotonic instant already in the past beyond the skew allowance is
        # refused rather than rendered as a time in the past.
        self.assertIsNone(collector.parse_timestamp("100s", base, 1_000_000.0))
        health, note = collector.classify("timer", {"ActiveState": "activating"})
        self.assertEqual(health, "unknown")
        self.assertTrue(note)


class SafetyTests(unittest.TestCase):
    def test_property_lists_never_ask_for_secrets(self):
        for name in collector.SERVICE_PROPERTIES + collector.TIMER_PROPERTIES:
            self.assertIsNone(collector.FORBIDDEN_PROPERTY.search(name), name)
        self.assertNotIn("Environment", collector.SERVICE_PROPERTIES)
        self.assertNotIn("ExecStart", collector.SERVICE_PROPERTIES)
        self.assertIn("Type", collector.SERVICE_PROPERTIES)

    def test_commands_always_pass_an_explicit_property_list(self):
        _, run = collect_with({})
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("-p", command)
            self.assertIn("--timestamp=unix", command)
            self.assertNotIn("--all", command)
            self.assertNotIn("--no-pager", command[1:2])
        user_calls = [call.args[0] for call in run.call_args_list if "--user" in call.args[0]]
        system_calls = [call.args[0] for call in run.call_args_list if "--user" not in call.args[0]]
        self.assertEqual(len(user_calls) + len(system_calls), len(collector.UNITS))
        self.assertTrue(all("--user" in call.args[0] or call.args[0][1] == "show"
                            for call in run.call_args_list))

    def test_secret_bearing_lines_are_dropped_even_if_printed(self):
        payload, _ = collect_with({"research-ledger.service": (0, show(
            Id="research-ledger.service", LoadState="loaded", ActiveState="active",
            SubState="running", Result="success",
            Environment="TELEGRAM_TOKEN=super-secret-value",
            ExecStart="/usr/bin/python3 /opt/secret/path",
            ExecStartPre="--api-key=abc123"))})
        entry = next(item for item in payload["units"] if item["unit"] == "research-ledger.service")
        dumped = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn("super-secret-value", dumped)
        self.assertNotIn("abc123", dumped)
        self.assertNotIn("/opt/secret/path", dumped)
        self.assertEqual(entry["health"], "ok")

    def test_output_is_private_and_group_readable_only(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "nested" / "services.json"
            collector.write_atomic(target, b'{"ok":true}\n')
            self.assertEqual(os.stat(target).st_mode & 0o777, collector.FILE_MODE)
            self.assertEqual(os.stat(target.parent).st_mode & 0o777, collector.DIR_MODE)
            self.assertEqual(target.read_bytes(), b'{"ok":true}\n')
            decoy = Path(temp) / "decoy.json"
            decoy.write_text("keep")
            (target.parent / "link.json").symlink_to(decoy)
            with self.assertRaises(ValueError):
                collector.write_atomic(target.parent / "link.json", b"x")
            self.assertEqual(decoy.read_text(), "keep")

    def test_cli_writes_a_snapshot_and_summarizes(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "services.json"
            with mock.patch.object(collector, "collect", return_value={
                    "schemaVersion": 1, "checkedAt": NOW.isoformat(), "scope": "x",
                    "readErrors": [], "units": [
                        {"unit": "a.timer", "scope": "user", "kind": "timer", "health": "ok"},
                        {"unit": "b.service", "scope": "system", "kind": "service",
                         "health": "disabled"}]}), mock.patch("sys.stdout"):
                code = collector.main(["--out", str(target), "--now", NOW.isoformat()])
            self.assertEqual(code, 0)
            payload = json.loads(target.read_text())
            self.assertEqual(len(payload["units"]), 2)
            self.assertEqual(os.stat(target).st_mode & 0o777, collector.FILE_MODE)

    def test_cli_defaults_and_no_environment_access(self):
        args = collector.build_parser().parse_args([])
        self.assertEqual(args.out, "/var/lib/nng8-dashboard/services.json")
        self.assertIsNone(args.group)
        source = (ROOT / "collect_services.py").read_text(encoding="utf-8")
        # Strip docstrings and comments: the prose names these fields, the code
        # must not ask for them.
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        code = re.sub(r"(?m)^\s*#.*$", "", code)
        for forbidden in ("Environment", "EnvironmentFile", "ExecStart", "ExecStop",
                          "ExecReload", "credentials", "os.environ"):
            self.assertNotIn(forbidden, code, forbidden)
        self.assertNotIn("import sqlite3", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
