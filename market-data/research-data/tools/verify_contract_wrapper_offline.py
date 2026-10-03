#!/usr/bin/env python3
"""Execute a path-relocated wrapper against fixed stubs only; no live collector."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STUB = r'''#!PYTHON_INTERPRETER
import datetime as dt, importlib.util, json, os, pathlib, sys
root=pathlib.Path(__file__).resolve().parents[2]
args=sys.argv[1:]
if args[0].endswith("snapshot_contract.py"):
    spec=importlib.util.spec_from_file_location("selector",root/"snapshot_contract.py")
    selector=importlib.util.module_from_spec(spec);spec.loader.exec_module(selector)
    try:
        policy=selector.read_policy(pathlib.Path(args[args.index("--policy")+1]))
        selector.assert_core_history(policy,pathlib.Path(args[args.index("--core-db")+1]))
        chosen=selector.select(policy,dt.datetime.fromisoformat(os.environ["FIXED_INSTANT"]))
    except (OSError,ValueError,TypeError):
        raise SystemExit(78)
    print("core" if chosen==selector.CORE else "full")
elif args[0]=="-":
    path=pathlib.Path(args[1])
    assert path.is_relative_to(root)
    print(json.loads(path.read_text()).get(args[2],""))
elif args[0].endswith(("daily_snapshot.py","core_market.py")):
    assert pathlib.Path(args[0]).parent==root
    is_core=args[0].endswith("core_market.py")
    state=root/"state"/("core-market" if is_core else "")
    state.mkdir(parents=True,exist_ok=True)
    (state/"last-run.json").write_text(json.dumps({"status":os.environ.get("STUB_STATUS","inserted"),"timestamp":"2026-09-28T22:30:00+08:00","runId":"fixed-stub"}))
    (root/"dispatch.json").write_text(json.dumps(args))
    raise SystemExit(int(os.environ.get("STUB_EXIT","0")))
else:
    raise AssertionError("unexpected command: "+args[0])
'''


class WrapperFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="core-wrapper-stub-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "bin").mkdir()
        date_stub = self.root / "bin/date"
        date_stub.write_text("#!/bin/sh\nprintf '2026-09-28\\n'\n")
        date_stub.chmod(0o700)
        (self.root / "state").mkdir()
        self.old_state = self.root / "state/last-run.json"
        self.old_state.write_text('{"status":"failed","reason":"old-full-state"}')
        self.old_bytes = self.old_state.read_bytes()
        python = self.root / ".venv/bin/python"
        python.write_text(STUB.replace("PYTHON_INTERPRETER", sys.executable))
        python.chmod(0o700)
        # Relocate only literal paths. Do not repurpose HOME or run production files.
        text = (ROOT / "scripts/run_daily_snapshot_with_alert.sh").read_text()
        text = text.replace('readonly RD_ROOT="/home/ubuntu/research-data"', f'readonly RD_ROOT="{self.root}"')
        text = text.replace('${HOME}/.local/state/research-data', str(self.root / "state"))
        self.wrapper = self.root / "wrapper.sh"
        self.wrapper.write_text(text)
        for name in ("daily_snapshot.py", "core_market.py"):
            (self.root / name).write_text("raise AssertionError('must never execute real writer')\n")
        (self.root / "snapshot_contract.py").write_text((ROOT / "snapshot_contract.py").read_text())
        self.policy = self.root / "config/snapshot-contract.json"
        self.env = {**os.environ, "FIXED_INSTANT": "2026-09-28T22:30:00+08:00", "ALERT_DRY_RUN": "1",
                    "RESEARCH_DATA_CORE_CALENDAR_PROJECT_ROOT": str(self.root / "calendar-project"),
                    "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"]}

    def run_wrapper(self):
        # Only this relocated bash wrapper and the above fixed Python stub run.
        return subprocess.run(["/bin/bash", str(self.wrapper)], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=15)

    def policy_core(self, effective):
        self.policy.write_text(json.dumps({"activeContract": "research.core-market.v1", "effectiveTradeDate": effective}))

    def test_no_policy_preserves_legacy_argv_and_state(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "dispatch.json").read_text()), [str(self.root / "daily_snapshot.py")])
        self.assertFalse((self.root / "state/core-market").exists())
        self.assertEqual(result.stdout, "")

    def test_future_policy_stays_legacy(self):
        self.policy_core("2026-09-29")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "dispatch.json").read_text()), [str(self.root / "daily_snapshot.py")])

    def test_effective_policy_switches_writer_and_state_only(self):
        self.policy_core("2026-09-28")
        self.env["FIXED_INSTANT"] = "2026-09-27T16:00:00+00:00"
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "dispatch.json").read_text())
        self.assertEqual(args, [str(self.root / "core_market.py"), "--contract-policy", str(self.policy),
                                "--db", str(self.root / "core-market.db"), "--state-dir", str(self.root / "state/core-market"),
                                "--calendar-evidence", str(self.root / "calendar-project")])
        self.assertEqual(self.old_state.read_bytes(), self.old_bytes)
        self.assertEqual(result.stdout, "")  # old failed full state must not trigger an alert

    def test_malformed_policy_starts_no_writer(self):
        self.policy.write_text("{")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 78)
        self.assertFalse((self.root / "dispatch.json").exists())
        self.assertEqual(self.old_state.read_bytes(), self.old_bytes)

    def test_core_failure_never_direct_sends_and_exit_is_not_swallowed(self):
        self.policy_core("2026-09-28")
        self.env.update(STUB_STATUS="deferred", STUB_EXIT="75")
        for instant in ("2026-09-28T22:30:00+08:00", "2026-09-29T22:30:00+08:00"):
            self.env["FIXED_INSTANT"] = instant
            result = self.run_wrapper()
            self.assertEqual(result.returncode, 75)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.old_state.read_bytes(), self.old_bytes)

    def test_no_policy_full_failure_preserves_exit_without_direct_send(self):
        self.env.update(STUB_STATUS="failed", STUB_EXIT="1")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_v2_forward_full_restores_dispatch_without_deleting_core_state(self):
        self.policy.write_text(json.dumps({"schema": "research.contract-policy.v2", "transitions": [
            {"activeContract": "research.core-market.v1", "effectiveTradeDate": "2026-10-08"},
            {"activeContract": "research.full-snapshot.v1", "effectiveTradeDate": "2026-10-09"}]}))
        self.env["FIXED_INSTANT"] = "2026-10-08T22:30:00+08:00"
        self.assertEqual(self.run_wrapper().returncode, 0)
        sealed = (self.root / "state/core-market/last-run.json").read_bytes()
        self.env["FIXED_INSTANT"] = "2026-10-09T22:30:00+08:00"
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(json.loads((self.root / "dispatch.json").read_text()), [str(self.root / "daily_snapshot.py")])
        self.assertEqual((self.root / "state/core-market/last-run.json").read_bytes(), sealed)

    def test_dangling_symlink_is_not_absent_policy_fallback(self):
        self.policy.symlink_to(self.root / "missing.json")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 78)
        self.assertFalse((self.root / "dispatch.json").exists())
        self.assertEqual(self.old_state.read_bytes(), self.old_bytes)

    def test_missing_policy_with_unreadable_core_history_starts_no_writer(self):
        (self.root / "core-market.db").mkdir()
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 78)
        self.assertFalse((self.root / "dispatch.json").exists())
        self.assertEqual(self.old_state.read_bytes(), self.old_bytes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
