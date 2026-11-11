"""Isolated metadata/progress regressions; no production paths or network."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import atomic


def exchange(left, right):
    temporary = left.with_name("test-exchange")
    left.rename(temporary)
    right.rename(left)
    temporary.rename(right)


class ManifestInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.active = self.data / "cn_data"
        (self.active / "features" / "sh600000").mkdir(parents=True)
        (self.active / "calendars").mkdir()
        (self.active / "instruments").mkdir()
        (self.data / ".tag").write_bytes(b"2026-09-06\n")
        (self.active / ".qlib-release").write_bytes(b"2026-09-06\n")
        (self.active / "calendars" / "day.txt").write_bytes(b"2026-09-03\n")
        (self.active / "instruments" / "all.txt").write_bytes(b"SH600000\t2026-09-03\t2026-09-03\n")
        self.relative = "features/sh600000/close.day.bin"
        (self.active / self.relative).write_bytes(b"OLD-DATA")
        self.old_inode = self.active.stat().st_ino

    def candidate(self, txn):
        stage = txn.copy()
        with (stage / self.relative).open("ab") as stream:
            stream.write(b"NEW!")
        with (stage / "calendars" / "day.txt").open("ab") as stream:
            stream.write(b"2026-09-04\n")
        return stage

    def test_inventory_sizes_and_identities_equal_previous_inventory(self):
        (self.active / "empty-directory").mkdir()
        (self.active / ".empty-file").write_bytes(b"")
        expected = {}
        identities = {}
        for directory, directories, names in os.walk(self.active, followlinks=False):
            for name in directories:
                path = Path(directory) / name
                self.assertFalse(path.is_symlink())
                self.assertTrue(path.is_dir())
            for name in names:
                path = Path(directory) / name
                info = path.lstat()
                self.assertTrue(stat.S_ISREG(info.st_mode))
                relative = path.relative_to(self.active).as_posix()
                expected[relative] = info.st_size
                identities[relative] = (info.st_ino, info.st_mtime_ns, info.st_size)
        sizes, actual_identities = atomic._tree_inventory(self.active)
        self.assertEqual(sizes, expected)
        self.assertEqual(actual_identities, identities)
        self.assertEqual(atomic.scan_tree(self.active), expected)

    def test_file_and_directory_symlinks_rejected_without_reading_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"do-not-read")
        for target in (outside / "secret", outside):
            with self.subTest(target=target.name):
                link = self.active / "link"
                link.symlink_to(target, target_is_directory=target.is_dir())
                try:
                    with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must not read target")):
                        with self.assertRaisesRegex(ValueError, "not_regular_file"):
                            atomic.scan_tree(self.active)
                finally:
                    link.unlink()
        link = self.root / "root-link"
        link.symlink_to(self.active, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "root_not_real_directory"):
            atomic.scan_tree(link)

    def test_hardlink_rejected(self):
        linked = self.active / "linked"
        os.link(self.active / self.relative, linked)
        with self.assertRaisesRegex(ValueError, "linked_data_file"):
            atomic.scan_tree(self.active)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_fifo_rejected_without_opening_it(self):
        fifo = self.active / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "not_regular_file:pipe"):
            atomic.scan_tree(self.active)

    def test_prepare_and_commit_both_recompute_full_prefix_proof(self):
        events = []
        with atomic.Transaction(self.data, exchange) as txn:
            self.candidate(txn)
            txn.progress = lambda phase, **fields: events.append((phase, fields))
            with mock.patch.object(atomic, "historical_prefixes", wraps=atomic.historical_prefixes) as proof:
                prepared = txn.prepare_manifest({"targetDate": "2026-09-04"})
                result = txn.commit(prepared)
                self.assertEqual(proof.call_count, 2)
            self.assertEqual(result, prepared)
        phases = [
            "manifest_generation_check", "manifest_candidate_inventory_initial",
            "manifest_production_inventory", "manifest_historical_prefixes",
            "manifest_write", "manifest_candidate_tag", "manifest_candidate_inventory_final",
        ]
        self.assertEqual([phase for phase, fields in events if fields["state"] == "start"], phases * 2)
        self.assertEqual([phase for phase, fields in events if fields["state"] == "completed"], phases * 2)
        for _, fields in events:
            self.assertIs(type(fields["elapsedMs"]), int)
            self.assertGreaterEqual(fields["elapsedMs"], 0)

    def test_same_size_production_mutation_after_prepare_rejects_commit(self):
        with atomic.Transaction(self.data, exchange) as txn:
            self.candidate(txn)
            prepared = txn.prepare_manifest({})
            (self.active / self.relative).write_bytes(b"USERDATA")
            with self.assertRaisesRegex(ValueError, "production_file_mutated"):
                txn.commit(prepared)
            self.assertFalse(txn.committed)
        self.assertEqual(self.active.stat().st_ino, self.old_inode)

    def test_stage_prefix_mutation_after_prepare_rejects_commit(self):
        with atomic.Transaction(self.data, exchange) as txn:
            stage = self.candidate(txn)
            prepared = txn.prepare_manifest({})
            (stage / self.relative).write_bytes(b"BAD-DATANEW!")
            with self.assertRaisesRegex(ValueError, "historical_rewrite"):
                txn.commit(prepared)
            self.assertFalse(txn.committed)
        self.assertEqual((self.active / self.relative).read_bytes(), b"OLD-DATA")

    def test_source_bytes_rechecked_even_if_size_inode_and_mtime_restored(self):
        with atomic.Transaction(self.data, exchange) as txn:
            self.candidate(txn)
            prepared = txn.prepare_manifest({})
            path = self.active / self.relative
            before = path.stat()
            path.write_bytes(b"USERDATA")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(ValueError, "historical_rewrite"):
                txn.commit(prepared)
            self.assertFalse(txn.committed)
        self.assertEqual(self.active.stat().st_ino, self.old_inode)

    def test_stage_hardlink_after_prepare_rejects_commit(self):
        with atomic.Transaction(self.data, exchange) as txn:
            stage = self.candidate(txn)
            prepared = txn.prepare_manifest({})
            path = stage / self.relative
            path.unlink()
            os.link(self.active / self.relative, path)
            try:
                with self.assertRaisesRegex(ValueError, "linked_data_file"):
                    txn.commit(prepared)
                self.assertFalse(txn.committed)
            finally:
                path.unlink()
        self.assertEqual(self.active.stat().st_ino, self.old_inode)

    def test_active_inventory_addition_after_prepare_rejects_commit(self):
        with atomic.Transaction(self.data, exchange) as txn:
            self.candidate(txn)
            prepared = txn.prepare_manifest({})
            (self.active / "new-production-file").write_bytes(b"not-ours")
            with self.assertRaisesRegex(ValueError, "production_files_changed"):
                txn.commit(prepared)
            self.assertFalse(txn.committed)
        self.assertEqual(self.active.stat().st_ino, self.old_inode)

    def test_callback_interruption_never_publishes_or_claims_completion(self):
        class Interrupted(BaseException):
            pass

        for stopped_phase in (
            "manifest_generation_check", "manifest_candidate_inventory_initial",
            "manifest_production_inventory", "manifest_historical_prefixes",
            "manifest_write", "manifest_candidate_tag", "manifest_candidate_inventory_final",
        ):
            with self.subTest(phase=stopped_phase):
                events = []
                def interrupt(phase, **fields):
                    events.append((phase, fields["state"]))
                    if phase == stopped_phase and fields["state"] == "start":
                        raise Interrupted()

                with self.assertRaises(Interrupted):
                    with atomic.Transaction(self.data, exchange) as txn:
                        self.candidate(txn)
                        txn.progress = interrupt
                        txn.commit({})
                self.assertIn((stopped_phase, "start"), events)
                self.assertNotIn((stopped_phase, "completed"), events)
                self.assertEqual(self.active.stat().st_ino, self.old_inode)
                self.assertEqual((self.active / self.relative).read_bytes(), b"OLD-DATA")
                self.assertFalse(list(self.data.glob(atomic.PREFIX + "*")))

    def test_direct_commit_still_runs_manifest_and_durability_guards(self):
        with atomic.Transaction(self.data, exchange) as txn:
            self.candidate(txn)
            with mock.patch.object(atomic, "historical_prefixes", wraps=atomic.historical_prefixes) as proof:
                result = txn.commit({"targetDate": "2026-09-04"})
                self.assertEqual(proof.call_count, 1)
            self.assertTrue(result["historicalPrefixes"]["unchanged"])
        self.assertNotEqual(self.active.stat().st_ino, self.old_inode)


if __name__ == "__main__":
    unittest.main()
