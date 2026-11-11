"""Core retry uses only fixed child records; no live core/market entry is started."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import snapshot_supervisor as supervisor
from snapshot_contract import CORE


class CoreRetryProcessTests(unittest.TestCase):
    def execute(self, root, mode):
        with mock.patch.dict(os.environ, {"SNAPSHOT_WORKER_TEST_MODE": mode}), \
                mock.patch.object(supervisor, "START_LATEST", dt.time(23, 59, 59)), \
                mock.patch.object(supervisor, "QLIB_RETRY_SECONDS", .02):
            return supervisor.run_supervised(contract=CORE,
                worker_path=str(Path(__file__).parents[1] / "tests/fixtures/snapshot_worker_stub.py"),
                state_dir=str(root / "state"), db_path=str(root / "core.db"),
                stage_dir=str(root / "absent-stage"), log_path=str(root / "log"),
                deadline_seconds_override=1.5)

    def test_core_stale_then_success_same_run_without_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final, code = self.execute(root, "core_stale_then_success")
            attempts = json.loads((root / "state/fixture-core-attempts.json").read_text())
            self.assertEqual((code, final["status"], final["attemptCount"]), (0, "inserted", 2))
            self.assertEqual(attempts[0]["runId"], attempts[1]["runId"])
            self.assertEqual(attempts[0]["soft"], attempts[1]["soft"])
            self.assertEqual(attempts[1]["day"], final["target"])
            self.assertEqual(len(attempts[1]["calendarSha"]), 64)
            self.assertFalse(any(a["stageRetry"] for a in attempts))
            self.assertFalse((root / "absent-stage").exists())

    def test_core_hanging_retry_keeps_original_hard_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); started = time.monotonic()
            final, code = self.execute(root, "core_stale_then_hang")
            self.assertNotEqual(code, 0)
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(final["attemptCount"], 2)
            self.assertEqual(final["reconciliation"]["stage"], "not_required")
            self.assertFalse((root / "absent-stage").exists())
