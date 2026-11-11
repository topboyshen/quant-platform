#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "astock_shadow_adapter.py"
spec = importlib.util.spec_from_file_location("astock_shadow_adapter", MODULE_PATH)
adapter = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(adapter)


class AStockAdapterDataMinimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "AStock_AI_System"
        self.reports = self.project / "reports"
        self.orchestrator = self.project / "work" / "orchestrator"
        self.notifications = self.project / "work" / "notifications"
        for directory in (self.reports, self.orchestrator, self.notifications):
            directory.mkdir(parents=True)
        self.cas = self.base / "cas"
        self.manifests = self.base / "manifests"
        adapter.PROJECT_ROOT = self.project
        adapter.CAS_ROOTS = {"report": self.reports}
        adapter.METADATA_ONLY_ROOTS = {"orchestrator": self.orchestrator, "notification": self.notifications}
        adapter.MAX_FILE_BYTES = 1024

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_notification_body_never_enters_cas(self) -> None:
        report_body = b"approved report archival content"
        notification_body = b"telegram-message-body chat_id=private-recipient"
        (self.reports / "daily.md").write_bytes(report_body)
        (self.notifications / "event-success-telegram.json").write_bytes(notification_body)
        manifest_path = adapter.build_manifest(self.cas, self.manifests)
        manifest = json.loads(manifest_path.read_text())
        notification = [x for x in manifest["artifacts"] if x["kind"] == "notification"]
        self.assertEqual(len(notification), 1)
        self.assertEqual(notification[0]["entry_mode"], "METADATA_ONLY")
        self.assertNotIn("storage_uri", notification[0])
        self.assertNotIn("chat_id", json.dumps(notification[0]))
        self.assertNotIn("telegram-message-body", json.dumps(notification[0]))
        cas_blobs = [path.read_bytes() for path in self.cas.rglob("*") if path.is_file()]
        self.assertIn(report_body, cas_blobs)
        self.assertNotIn(notification_body, cas_blobs)

    def test_symlink_escape_special_file_and_oversize_are_rejected(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("outside")
        (self.reports / "escape.md").symlink_to(outside)
        (self.notifications / "oversize.json").write_bytes(b"x" * 2048)
        fifo = self.notifications / "pipe.json"
        os.mkfifo(fifo)
        manifest_path = adapter.build_manifest(self.cas, self.manifests)
        manifest = json.loads(manifest_path.read_text())
        paths = json.dumps(manifest["artifacts"])
        self.assertNotIn("escape.md", paths)
        self.assertNotIn("oversize", paths)
        self.assertNotIn("pipe", paths)

    def test_manifest_is_idempotent_for_unchanged_inputs(self) -> None:
        (self.reports / "daily.md").write_text("report")
        one = adapter.build_manifest(self.cas, self.manifests)
        two = adapter.build_manifest(self.cas, self.manifests)
        self.assertEqual(one, two)
        self.assertEqual(len(list(self.manifests.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
