import json
import os
from pathlib import Path
import tempfile
import unittest
import shutil

from atomic import MANIFEST, MANIFEST_LIMIT, Transaction, historical_prefixes, read_small


def mock_exchange(left, right):
    temp = left.with_name("mock-exchange-temp")
    left.rename(temp)
    right.rename(left)
    temp.rename(right)


class AtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name).resolve() / "data"
        self.old = self.data / "cn_data"
        (self.old / "features" / "sh600000").mkdir(parents=True)
        (self.old / "calendars").mkdir()
        (self.data / ".tag").write_text("2026-09-06\n")
        (self.old / ".qlib-release").write_text("2026-09-06\n")
        (self.old / "calendars/day.txt").write_text("2026-09-03\n2026-09-04\n")
        self.feature = "features/sh600000/close.day.bin"
        (self.old / self.feature).write_bytes(b"OLD-DATA")
        self.inode = self.old.stat().st_ino

    def candidate(self, txn):
        stage = txn.copy()
        with (stage / self.feature).open("ab") as stream:
            stream.write(b"NEW!")
        with (stage / "calendars/day.txt").open("a") as stream:
            stream.write("2026-09-07\n")
        return stage

    def test_failure_before_commit_preserves_active(self):
        with self.assertRaisesRegex(ValueError, "test_failure"):
            with Transaction(self.data, mock_exchange) as txn:
                self.candidate(txn)
                raise ValueError("test_failure")
        self.assertEqual(self.old.stat().st_ino, self.inode)
        self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATA")
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_commit_keeps_real_release_tag_and_historical_prefix(self):
        with Transaction(self.data, mock_exchange) as txn:
            self.candidate(txn)
            result = txn.commit({"sourceCommit": "a" * 32, "targetDate": "2026-09-07"})
            self.assertTrue(result["historicalPrefixes"]["unchanged"])
        self.assertNotEqual(self.old.stat().st_ino, self.inode)
        self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATANEW!")
        self.assertEqual((self.data / ".tag").read_text(), "2026-09-06\n")
        self.assertEqual(json.loads((self.old / MANIFEST).read_text())["targetDate"], "2026-09-07")
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_prepare_does_not_exchange_and_commit_is_identical(self):
        with Transaction(self.data, mock_exchange) as txn:
            stage = self.candidate(txn)
            prepared = txn.prepare_manifest({"sourceCommit": "a" * 32})
            first = read_small(stage / MANIFEST)
            self.assertEqual(self.old.stat().st_ino, self.inode)
            self.assertFalse((self.old / MANIFEST).exists())
            self.assertFalse(txn.committed)
            self.assertEqual(read_small(txn.workspace / ".tag", 128), txn.origin_tag)
            committed = txn.commit(prepared)
            self.assertEqual(committed, prepared)
            self.assertEqual(read_small(self.old / MANIFEST), first)

    def test_prepare_uses_authoritative_generation_and_prefix_proof(self):
        with Transaction(self.data, mock_exchange) as txn:
            self.candidate(txn)
            prepared = txn.prepare_manifest({"generationId": "forged", "historicalPrefixes": {}})
            self.assertEqual(prepared["generationId"], txn.identity)
            self.assertTrue(prepared["historicalPrefixes"]["unchanged"])
            self.assertEqual(prepared["historicalPrefixes"]["files"], 2)

    def test_failed_candidate_consumer_check_never_exchanges(self):
        with self.assertRaisesRegex(ValueError, "consumer_rejected"):
            with Transaction(self.data, mock_exchange) as txn:
                self.candidate(txn)
                txn.prepare_manifest({})
                raise ValueError("consumer_rejected")
        self.assertEqual(self.old.stat().st_ino, self.inode)
        self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATA")
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_oversize_prepare_rejects_before_writing_manifest(self):
        with Transaction(self.data, mock_exchange) as txn:
            stage = self.candidate(txn)
            with self.assertRaisesRegex(ValueError, "manifest_size_limit"):
                txn.prepare_manifest({"payload": "x" * MANIFEST_LIMIT})
            self.assertFalse((stage / MANIFEST).exists())
            self.assertFalse((stage / (MANIFEST + ".new")).exists())
            self.assertFalse((txn.workspace / ".tag").exists())
            self.assertFalse(txn.committed)
        self.assertEqual(self.old.stat().st_ino, self.inode)

    def test_direct_commit_cannot_bypass_manifest_size_limit(self):
        with Transaction(self.data, mock_exchange) as txn:
            self.candidate(txn)
            with self.assertRaisesRegex(ValueError, "manifest_size_limit"):
                txn.commit({"payload": "x" * MANIFEST_LIMIT})
            self.assertFalse(txn.committed)
        self.assertEqual(self.old.stat().st_ino, self.inode)

    def test_exact_manifest_size_boundary_and_rejected_reprepare(self):
        with Transaction(self.data, mock_exchange) as txn:
            stage = self.candidate(txn)
            manifest = txn.prepare_manifest({"payload": ""})
            fixed_size = len(read_small(stage / MANIFEST))
            manifest["payload"] = "x" * (MANIFEST_LIMIT - fixed_size)
            prepared = txn.prepare_manifest(manifest)
            accepted = read_small(stage / MANIFEST)
            self.assertEqual(len(accepted), MANIFEST_LIMIT)
            manifest["payload"] += "x"
            with self.assertRaisesRegex(ValueError, "manifest_size_limit"):
                txn.prepare_manifest(manifest)
            self.assertEqual(read_small(stage / MANIFEST), accepted)
            self.assertFalse((stage / (MANIFEST + ".new")).exists())
            txn.commit(prepared)
        self.assertEqual(len(read_small(self.old / MANIFEST)), MANIFEST_LIMIT)

    def test_unicode_size_is_actual_escaped_json_bytes(self):
        with Transaction(self.data, mock_exchange) as txn:
            stage = self.candidate(txn)
            with self.assertRaisesRegex(ValueError, "manifest_size_limit"):
                txn.prepare_manifest({"payload": "中" * 11000})
            self.assertFalse((stage / MANIFEST).exists())

    def test_non_finite_manifest_json_rejected_before_write(self):
        with Transaction(self.data, mock_exchange) as txn:
            stage = self.candidate(txn)
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.assertRaises(ValueError):
                    txn.prepare_manifest({"value": value})
                self.assertFalse((stage / MANIFEST).exists())

    def test_candidate_tag_mutation_refuses_commit(self):
        with Transaction(self.data, mock_exchange) as txn:
            self.candidate(txn)
            txn.prepare_manifest({})
            (txn.workspace / ".tag").write_bytes(b"2026-09-08\n")
            with self.assertRaisesRegex(ValueError, "candidate_release_identity_changed"):
                txn.commit({})
            self.assertFalse(txn.committed)
        self.assertEqual(self.old.stat().st_ino, self.inode)

    def test_candidate_tag_symlink_never_followed(self):
        with Transaction(self.data, mock_exchange) as txn:
            self.candidate(txn)
            (txn.workspace / ".tag").symlink_to(self.data / ".tag")
            try:
                with self.assertRaises(OSError):
                    txn.prepare_manifest({})
                self.assertFalse(txn.committed)
                self.assertEqual((self.data / ".tag").read_bytes(), txn.origin_tag)
            finally:
                (txn.workspace / ".tag").unlink()

    def test_rewrite_rejected(self):
        with self.assertRaisesRegex(ValueError, "historical_rewrite"):
            with Transaction(self.data, mock_exchange) as txn:
                stage = self.candidate(txn)
                (stage / self.feature).write_bytes(b"BAD-DATANEW!")
                txn.commit({})
        self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATA")

    def test_concurrent_lock_rejected(self):
        with Transaction(self.data, mock_exchange):
            with self.assertRaises(BlockingIOError):
                with Transaction(self.data, mock_exchange):
                    self.fail("must not acquire second lock")

    def test_source_mutation_rejected(self):
        with Transaction(self.data, mock_exchange) as txn:
            self.candidate(txn)
            (self.old / self.feature).write_bytes(b"USERDATA")
            with self.assertRaisesRegex(ValueError, "production_file_mutated"):
                txn.commit({})
        self.assertEqual((self.old / self.feature).read_bytes(), b"USERDATA")

    def test_symlink_rejected_without_following(self):
        (self.old / "external").symlink_to(self.data / ".tag")
        with self.assertRaisesRegex(ValueError, "not_regular_file"):
            with Transaction(self.data, mock_exchange):
                self.fail("unsafe tree accepted")

    def test_canonical_recovery_required(self):
        (self.data / ".cn_data.rollback").mkdir()
        with self.assertRaisesRegex(ValueError, "canonical_release_recovery_required"):
            with Transaction(self.data, mock_exchange):
                self.fail("canonical recovery bypassed")

    def test_broken_canonical_rollback_link_rejected(self):
        (self.data / ".cn_data.rollback").symlink_to(self.data / "does-not-exist")
        with self.assertRaisesRegex(ValueError, "canonical_release_recovery_required"):
            with Transaction(self.data, mock_exchange):
                self.fail("broken rollback bypassed")

    def test_stage_symlink_rejected_before_external_manifest_write(self):
        with Transaction(self.data, mock_exchange) as txn:
            stage = self.candidate(txn)
            outside = Path(self.tmp.name).resolve() / "outside"
            stage.rename(outside)
            stage.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, "candidate_generation_changed"):
                    txn.commit({})
                self.assertFalse((outside / MANIFEST).exists())
                self.assertEqual(self.old.stat().st_ino, self.inode)
            finally:
                stage.unlink()
                outside.rename(stage)

    def crash(self, exchanged):
        txn = Transaction(self.data, mock_exchange).__enter__()
        stage = self.candidate(txn)
        if exchanged:
            txn.commit({"targetDate": "2026-09-07"})
        os.close(txn.lock)
        txn.lock = None

    def test_recovery_before_swap(self):
        self.crash(False)
        with Transaction(self.data, mock_exchange):
            self.assertEqual(self.old.stat().st_ino, self.inode)
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_recovery_after_swap(self):
        self.crash(True)
        with Transaction(self.data, mock_exchange):
            self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATANEW!")
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_cleanup_crash_after_old_tree_removed(self):
        self.crash(True)
        workspace = next(self.data.glob(".qlib-incremental.*"))
        shutil.rmtree(workspace / "cn_data")
        with Transaction(self.data, mock_exchange):
            self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATANEW!")

    def test_cleanup_crash_after_marker_removed_empty_directory(self):
        self.crash(True)
        workspace = next(self.data.glob(".qlib-incremental.*"))
        shutil.rmtree(workspace / "cn_data")
        (workspace / ".tag").unlink()
        (workspace / "transaction.json").unlink()
        with Transaction(self.data, mock_exchange):
            self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATANEW!")

    def test_markerless_nonempty_directory_never_deleted(self):
        path = self.data / (".qlib-incremental." + "a" * 32)
        path.mkdir()
        (path / "user-file").write_text("keep")
        with self.assertRaises(OSError):
            with Transaction(self.data, mock_exchange):
                self.fail("unknown nonempty tree deleted")
        self.assertEqual((path / "user-file").read_text(), "keep")

    def test_unknown_staging_refused(self):
        (self.data / ".qlib-incremental.unrelated").mkdir()
        with self.assertRaisesRegex(ValueError, "unknown_staging_path"):
            with Transaction(self.data, mock_exchange):
                self.fail("unknown user directory removed")
        self.assertTrue((self.data / ".qlib-incremental.unrelated").exists())

    @unittest.skipUnless(os.uname().sysname == "Linux", "real atomic exchange requires Linux")
    def test_real_linux_exchange(self):
        with Transaction(self.data) as txn:
            self.candidate(txn)
            txn.commit({})
        self.assertEqual((self.old / self.feature).read_bytes(), b"OLD-DATANEW!")


if __name__ == "__main__":
    unittest.main()
