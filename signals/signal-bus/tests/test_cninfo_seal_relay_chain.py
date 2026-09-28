"""Full-chain regression: CNINFO disclosure+observed sources -> xhs seal -> relay dry-run.

No HTTP send. Uses temporary trees only. Run:

  cd /home/ubuntu/signal-bus
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
    tests.test_cninfo_seal_relay_chain -v

Or:

  PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_cninfo_seal_relay_chain.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch
import socket

DAY = "2026-09-09"
NOW = dt.datetime(2026, 9, 9, 0, 5, tzinfo=dt.timezone.utc)
BUS = Path(__file__).resolve().parents[1]
SIBLING = BUS.parent / "astock"
ASTOCK = Path(os.environ.get("ASTOCK_TEST_ROOT", str(SIBLING if SIBLING.is_dir() else "/home/ubuntu/AStock_AI_System")))
NODE = os.environ.get("NODE", shutil.which("node") or "/usr/local/bin/node")
BUILDER = BUS / "scripts" / "build_cninfo_seal_fixture.mjs"

MODULE = BUS / "scripts" / "research_relay.py"
spec = importlib.util.spec_from_file_location("research_relay", MODULE)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class CninfoSealRelayChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project"
        self.bus = Path(self.temp.name).resolve() / "bus"
        self.root.mkdir()
        self.bus.mkdir()
        self.network_guard = patch.object(
            socket, "socket", side_effect=AssertionError("test attempted network")
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def _occurrence(self):
        return {
            "schemaVersion": "1.0",
            "task": "morning-report",
            "targetDate": DAY,
            "startedAt": "2026-09-09T00:00:04.000Z",
            "completedAt": "2026-09-09T00:00:25.000Z",
            "status": "success",
            "childExitCode": 0,
            "childSignal": None,
            "timedOut": False,
            "interrupted": False,
        }

    def _build_seal_via_xhs_draft(self):
        self.assertTrue(BUILDER.is_file(), f"missing builder {BUILDER}")
        proc = subprocess.run(
            [
                NODE,
                str(BUILDER),
                "--root",
                str(self.root),
                "--day",
                DAY,
                "--now",
                "2026-09-09T00:30:00.000Z",
            ],
            cwd=str(ASTOCK),
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ASTOCK_TEST_ROOT": str(ASTOCK)},
            check=False,
        )
        if proc.returncode != 0:
            self.fail(
                f"xhs-draft seal build failed rc={proc.returncode}\n"
                f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
            )
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "draft_ready")
        seal_path = self.root / result["manifestFile"]
        self.assertTrue(seal_path.is_file(), result["manifestFile"])
        occ = self._occurrence()
        occ_path = self.root / f"work/orchestrator/states/morning-report-{DAY}.json"
        occ_path.parent.mkdir(parents=True, exist_ok=True)
        occ_path.write_bytes(relay.encode_payload(occ))
        return result

    def test_cninfo_sources_seal_then_relay_prepare_dry_run(self):
        """CNINFO 披露日期+实际采集时间 report seals and passes relay prepare (no send)."""
        self._build_seal_via_xhs_draft()
        prepared = relay.prepare_research(self.root, NOW)
        self.assertEqual(prepared["status"], "ready", prepared)
        self.assertEqual(prepared["target_date"], DAY)
        self.assertIn("report_sha256", prepared["proof"])
        self.assertIn("seal_sha256", prepared["proof"])

        result = relay.run_relay(
            self.root,
            self.bus,
            now=NOW,
            dry_run=True,
            sender=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("sender called")
            ),
        )
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["reason"], "read_only_preview")
        self.assertFalse(result["would_send"])
        self.assertEqual(result["sent"], 0)
        self.assertTrue(result.get("within_send_window"))
        self.assertFalse((self.bus / "out" / "relay-delivery").exists())


if __name__ == "__main__":
    unittest.main()
