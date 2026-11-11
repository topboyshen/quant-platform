"""Offline fetch/local-phase ordering tests using the existing run fixture."""
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

import atomic
import deploy
import run
import test_run as fixture


class TrackedClient(fixture.Client):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.future_error = None
        self.future_rows = None
        self.max_seconds = 1000

    def network(self, label):
        if self.owner.prices_started:
            raise AssertionError("network_after_prices_started:" + label)
        if self.owner.clock[0] >= self.max_seconds:
            raise AssertionError("network_budget_exhausted")
        self.owner.order.append("network:" + label)

    def latest_ref(self):
        self.network("pin")
        return super().latest_ref()

    def query(self, sql):
        self.network("query")
        if "ts_index_weight" in sql:
            self.requests += 1
            return [{"row_count": "1"}]
        return super().query(sql)

    def paginated_rows(self, table, columns, where, fields):
        future = table == "ts_trade_day_calendar" and "date > '" in where
        self.network("future" if future else table)
        if future and self.future_error:
            raise self.future_error
        if future and self.future_rows is not None:
            self.requests += 1
            return self.future_rows
        return super().paginated_rows(table, columns, where, fields)


class FetchOrderTests(unittest.TestCase):
    def setUp(self):
        # Reuse setup/execute methods without inheriting/re-running its tests.
        fixture.RunTests.setUp(self)
        self.order = []
        self.clock = [0.0]
        self.prices_started = False
        self.consume_network_budget_in_prices = False
        self.csi_error = None
        self.price_error = None
        self.consumer_error = None
        self.last_stage = None
        self.client = TrackedClient(self)
        self.csi_file = "instruments/csi300.txt"
        (self.active / "instruments").mkdir()
        (self.active / self.csi_file).write_bytes(b"SH600000\t2026-09-02\t2026-09-04\n")
        self.price_file = "features/sh600000/close.day.bin"
        (self.active / "features/sh600000").mkdir()
        (self.active / self.price_file).write_bytes(b"OLD-PRICE-PREFIX")
        self.before = self.fingerprint()
        self.tree.apply_prices = self.apply_prices
        self.indices.update_indices = self.update_indices
        self.patch(run.time, "monotonic", lambda: self.clock[0])
        self.patch(run, "validate_index_market_alignment", self.alignment)
        original_copy = atomic.Transaction.copy
        original_prepare = atomic.Transaction.prepare_manifest
        original_commit = atomic.Transaction.commit
        original_prefix = atomic.historical_prefixes
        def copied(txn):
            result = original_copy(txn)
            self.last_stage = result
            self.order.append("copy")
            return result
        def prepared(txn, manifest):
            self.order.append("prepare")
            return original_prepare(txn, manifest)
        def committed(txn, manifest):
            self.assertIn("consumer", self.order)
            result = original_commit(txn, manifest)
            self.order.append("commit")
            return result
        def prefixes(old, stage, sizes):
            self.assertEqual(run.read_calendar(stage), fixture.DAYS)
            self.assertEqual((stage / "calendars/day_future.txt").read_text().splitlines(), ["2026-09-09", "2026-09-10"])
            self.order.append("prefix")
            return original_prefix(old, stage, sizes)
        self.patch(atomic.Transaction, "copy", copied)
        self.patch(atomic.Transaction, "prepare_manifest", prepared)
        self.patch(atomic.Transaction, "commit", committed)
        self.patch(atomic, "historical_prefixes", prefixes)
        for owner, name in ((socket.socket, "connect"), (sqlite3, "connect"),
                            (subprocess, "run"), (subprocess, "Popen"), (subprocess, "check_output")):
            self.patch(owner, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("external_operation_forbidden")))

    def patch(self, owner, name, replacement):
        guard = patch.object(owner, name, replacement)
        guard.start()
        self.addCleanup(guard.stop)

    def fingerprint(self):
        return {str(path.relative_to(self.active)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.active.rglob("*") if path.is_file()}

    def assert_active_untouched_and_candidate_cleaned(self):
        self.assertEqual(self.active.stat().st_ino, self.inode)
        self.assertEqual(self.fingerprint(), self.before)
        self.assertEqual((self.data / ".tag").read_text(), "2026-09-06\n")
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))
        if self.last_stage is not None:
            self.assertFalse(self.last_stage.exists())

    def execute(self, **options):
        return fixture.RunTests.execute(self, client=self.client, candidate_check=self.check_consumer, **options)

    def update_indices(self, old, stage, client, base, target, calendar):
        self.assertIn("copy", self.order)
        self.assertFalse(self.prices_started)
        self.assertEqual(old, self.active)
        self.assertEqual(self.fingerprint(), self.before)
        self.order.append("csi")
        client.query(f"SELECT COUNT(*) AS row_count FROM {client.table('ts_index_weight')}")
        self.assertNotEqual((old / self.csi_file).stat().st_ino, (stage / self.csi_file).stat().st_ino)
        (stage / self.csi_file).write_bytes(b"SH600000\t2026-09-02\t2026-09-08\n")
        if self.csi_error:
            raise self.csi_error
        return {"validated": True}

    def apply_prices(self, old, stage, old_calendar, calendar, **kwargs):
        self.assertIn("csi", self.order)
        self.assertIn("network:future", self.order)
        self.assertEqual((stage / self.csi_file).read_bytes(), b"SH600000\t2026-09-02\t2026-09-08\n")
        self.assertEqual(run.read_calendar(stage), fixture.DAYS[:3])
        self.assertEqual(self.fingerprint(), self.before)
        self.prices_started = True
        self.order.append("prices")
        with (stage / self.price_file).open("ab") as stream:
            stream.write(b"-NEW-PRICE-SUFFIX")
        if self.consume_network_budget_in_prices:
            self.clock[0] = 1001.0
        if self.price_error:
            raise self.price_error
        return {"status": "complete", "eligible": 1, "valid": 1, "invalid": 0}

    def alignment(self, stage, calendar, **kwargs):
        self.assertTrue(self.prices_started)
        self.assertEqual(run.read_calendar(stage), fixture.DAYS[:3])
        self.assertEqual((stage / self.price_file).read_bytes(), b"OLD-PRICE-PREFIX-NEW-PRICE-SUFFIX")
        self.order.append("alignment")
        return {"status": "complete"}

    def check_consumer(self, stage, manifest):
        self.assertEqual(self.active.stat().st_ino, self.inode)
        self.assertEqual(self.fingerprint(), self.before)
        self.assertEqual(run.read_calendar(stage), fixture.DAYS)
        self.assertEqual(json.loads((stage / atomic.MANIFEST).read_bytes()), manifest)
        self.assertEqual((stage.parent / ".tag").read_bytes(), (self.data / ".tag").read_bytes())
        self.assertEqual(manifest["sourceCommit"], fixture.REF)
        self.assertEqual(manifest["targetDate"], "2026-09-08")
        self.assertEqual(manifest["dataQuality"], "complete")
        self.assertTrue(manifest["historicalPrefixes"]["unchanged"])
        self.order.append("consumer")
        if self.consumer_error:
            raise self.consumer_error
        return {"status": "passed"}

    def test_all_source_io_finishes_before_prices_and_success_order_is_preserved(self):
        result = self.execute()
        for earlier, later in zip(("copy", "csi", "network:future", "prices", "alignment", "prepare", "prefix", "consumer"),
                                  ("csi", "network:future", "prices", "alignment", "prepare", "prefix", "consumer", "commit")):
            self.assertLess(self.order.index(earlier), self.order.index(later))
        self.assertEqual(self.order.count("prefix"), 2)
        self.assertLess(self.order.index("consumer"), len(self.order) - 1 - self.order[::-1].index("prefix"))
        self.assertFalse(any(event.startswith("network:") for event in self.order[self.order.index("prices") + 1:]))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["targetDate"], "2026-09-08")
        self.assertEqual(result["dataQuality"], "complete")
        self.assertEqual(run.read_calendar(self.active), fixture.DAYS)
        self.assertEqual((self.data / ".tag").read_text(), "2026-09-06\n")
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_csi_query_failure_never_starts_prices_and_cleans_candidate(self):
        self.csi_error = ValueError("csi_source_failed")
        with self.assertRaisesRegex(ValueError, "csi_source_failed"): self.execute()
        self.assertNotIn("prices", self.order)
        self.assertNotIn("network:future", self.order)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_future_query_failure_never_starts_prices_and_cleans_candidate(self):
        self.client.future_error = ValueError("future_source_failed")
        with self.assertRaisesRegex(ValueError, "future_source_failed"): self.execute()
        self.assertIn("csi", self.order)
        self.assertNotIn("prices", self.order)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_future_validation_failure_also_precedes_prices(self):
        cases = [[], [{"exchange": "SZSE", "date": "2026-09-09", "is_open": "1"}],
                 [{"exchange": "SSE", "date": "2026-09-08", "is_open": "1"}],
                 [{"exchange": "SSE", "date": "2026-09-09", "is_open": "1"}] * 2]
        for rows in cases:
            with self.subTest(rows=rows):
                self.client.future_rows = rows
                with self.assertRaises(ValueError): self.execute()
                self.assertNotIn("prices", self.order)
                self.assert_active_untouched_and_candidate_cleaned()

    def test_prices_failure_after_csi_candidate_mutation_never_changes_active(self):
        self.price_error = ValueError("price_validation_failed")
        with self.assertRaisesRegex(ValueError, "price_validation_failed"): self.execute()
        self.assertLess(self.order.index("csi"), self.order.index("prices"))
        self.assertNotIn("alignment", self.order)
        self.assertNotIn("consumer", self.order)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_dry_run_still_prepares_and_checks_consumer_without_commit(self):
        result = self.execute(dry_run=True)
        self.assertEqual(result["status"], "dry_run_validated")
        self.assertLess(self.order.index("prepare"), self.order.index("consumer"))
        self.assertNotIn("commit", self.order)
        self.assertEqual(self.order.count("prefix"), 1)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_prefix_tamper_before_prepare_is_still_rejected(self):
        original = self.alignment
        def corrupt(stage, calendar, **kwargs):
            result = original(stage, calendar, **kwargs)
            (stage / self.price_file).write_bytes(b"BAD-PRICE-PREFIX-NEW")
            return result
        self.patch(run, "validate_index_market_alignment", corrupt)
        with self.assertRaisesRegex(ValueError, "historical_rewrite"):
            self.execute()
        self.assertNotIn("consumer", self.order)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_prefix_tamper_after_consumer_is_rejected_before_exchange(self):
        original = self.check_consumer
        def corrupt(stage, manifest):
            result = original(stage, manifest)
            (stage / self.price_file).write_bytes(b"BAD-PRICE-PREFIX-NEW")
            return result
        self.check_consumer = corrupt
        with self.assertRaisesRegex(ValueError, "historical_rewrite"):
            self.execute()
        self.assertIn("consumer", self.order)
        self.assertNotIn("commit", self.order)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_consumer_failure_after_local_validation_does_not_promote(self):
        self.consumer_error = ValueError("consumer_refused")
        with self.assertRaisesRegex(ValueError, "consumer_refused"): self.execute()
        self.assertIn("alignment", self.order)
        self.assertIn("prepare", self.order)
        self.assertNotIn("commit", self.order)
        self.assert_active_untouched_and_candidate_cleaned()

    def test_network_clock_expiry_during_prices_does_not_block_local_finish(self):
        self.consume_network_budget_in_prices = True
        result = self.execute()
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["elapsedSeconds"], 1001.0)
        self.assertGreater(self.clock[0], self.client.max_seconds)
        self.assertLess(self.clock[0], 25 * 60)
        self.assertIn("consumer", self.order)
        self.assertIn("commit", self.order)
        self.assertFalse(any(event.startswith("network:") for event in self.order[self.order.index("prices") + 1:]))

    def test_runtime_network_and_external_schedule_limits_are_not_relaxed(self):
        client = run.source_client()
        self.assertEqual(client.max_seconds, 1000)
        self.assertEqual(client.max_requests, 320)
        self.assertEqual(client.transport.timeout, 40)
        self.assertEqual(client.transport.total_timeout, 84)
        words = deploy.NEW_LINE.split()
        self.assertEqual(words[5:9], ["/usr/bin/timeout", "--signal=TERM", "--kill-after=30s", "25m"])
        self.assertIn("/usr/bin/python3", words)
        self.assertIn("-B", words)


if __name__ == "__main__":
    unittest.main()
