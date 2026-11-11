"""Deployment boundary tests: temporary filesystem only, no real subprocesses."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deploy


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qlib-deploy-offline-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.research = self.base / "research"
        self.install = self.base / "scripts" / "qlib_dolt_incremental"
        (self.source / "consumer-compat" / "tests").mkdir(parents=True)
        (self.research / "tests").mkdir(parents=True)
        self.install.parent.mkdir()
        self.head = "a" * 40
        self.dirty = ""
        self.old = {"data_health.py": b"# old health\n", "tests/test_data_health.py": b"# old health tests\n"}
        self.new = {"data_health.py": b"# reviewed health\n", "tests/test_data_health.py": b"# reviewed health tests\n",
                    "tests/test_qlib_incremental.py": b"# reviewed new incremental tests\n"}
        for name, contents in self.old.items():
            (self.research / name).write_bytes(contents)
        (self.research / "data_health.py").chmod(0o600)
        (self.research / "tests/test_data_health.py").chmod(0o640)
        self.unrelated = self.research / "unrelated.txt"
        self.unrelated.write_bytes(b"unrelated user data\n")
        for name, contents in self.new.items():
            (self.source / "consumer-compat" / name).write_bytes(contents)
        baseline = [{"file": name, "gitHead": self.head, "sha256": deploy.digest(contents)} for name, contents in self.old.items()]
        (self.source / "consumer-compat/deployment-base.json").write_text(json.dumps(baseline))
        for name in ("run.py", "source.py", "README.md", "api_contract.md", "scratch.txt"):
            (self.source / name).write_text("offline fixture: " + name + "\n")
        self.manifest_file = self.base / "promoted-manifest.json"
        self.promoted_manifest = {"status": "validated", "targetDate": "2026-09-08", "generationId": "c" * 32}
        self.manifest_file.write_text(json.dumps(self.promoted_manifest))
        self.canonical = self.base / "canonical-updater.sh"
        self.canonical.write_bytes(b"# canonical importer kept untouched\n")
        self.snapshot_line = "30 22 * * * /home/ubuntu/research-data/snapshot.sh # retain exactly"
        self.cron = ("MAILTO=\n# scheduled tasks\n" + deploy.OLD_LINE + "\n" + self.snapshot_line + "\n0 8 * * * other-task\n").encode()
        self.cron_reads = 0
        self.run_calls = []
        self.output_calls = []
        self.fail_test = None
        self.before_test_failure = None
        self.final_cron_change = False
        self.verify_cron_mismatch = False
        self.receipt_override = {}
        self.receipt_calls = 0
        self.change_manifest_before_schedule = False
        for name, value in (("SOURCE", self.source), ("RESEARCH", self.research), ("INSTALL", self.install),
                            ("CRON_SHA", deploy.digest(self.cron)), ("OLD_SCRIPT_SHA", deploy.digest(self.canonical.read_bytes()))):
            self.start_patch(deploy, name, value)
        original_read = deploy.read
        redirects = {Path("/home/ubuntu/qlib/data/cn_data/.qlib-dolt-incremental.json"): self.manifest_file,
                     Path("/home/ubuntu/qlib/scripts/update_qlib_data.sh"): self.canonical}
        def bounded_read(path):
            path = redirects.get(Path(path), Path(path))
            if not path.is_relative_to(self.base):
                raise AssertionError("read_outside_temporary_fixture")
            return original_read(path)
        self.start_patch(deploy, "read", bounded_read)
        original_replace = deploy.replace
        def bounded_replace(path, data, mode):
            if not path.is_relative_to(self.base) or not path.parent.resolve().is_relative_to(self.base):
                raise AssertionError("write_outside_temporary_fixture")
            return original_replace(path, data, mode)
        self.start_patch(deploy, "replace", bounded_replace)
        self.start_patch(deploy.subprocess, "check_output", self.check_output)
        self.start_patch(deploy.subprocess, "run", self.mock_run)
        self.start_patch(deploy.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real_subprocess_forbidden")))
        self.start_patch(deploy, "verify_promoted", self.promoted_receipt)
        self.start_patch(deploy.os, "access", lambda path, mode: path in {
            "/usr/bin/timeout", "/usr/bin/nice", "/usr/bin/ionice", "/usr/bin/python3"} and mode == os.X_OK)

    def start_patch(self, owner, name, value):
        guard = patch.object(owner, name, value)
        guard.start()
        self.addCleanup(guard.stop)

    def check_output(self, command, **kwargs):
        self.output_calls.append((list(command), dict(kwargs)))
        if command == ["git", "-C", str(self.research), "rev-parse", "HEAD"]:
            self.assertEqual(kwargs, {"text": True})
            return self.head + "\n"
        if command == ["git", "-C", str(self.research), "status", "--porcelain"]:
            self.assertEqual(kwargs, {"text": True})
            return self.dirty
        if command == ["crontab", "-l"]:
            self.assertEqual(kwargs, {})
            self.cron_reads += 1
            if self.final_cron_change and self.cron_reads == 2:
                self.cron += b"# parallel operator edit\n"
            if self.change_manifest_before_schedule and self.cron_reads == 2:
                self.manifest_file.write_bytes(self.manifest_file.read_bytes() + b" ")
            if self.verify_cron_mismatch and self.cron_reads == 3:
                return self.cron + b"# verification differs\n"
            return self.cron
        raise AssertionError("unexpected_subprocess_read")

    def promoted_receipt(self):
        self.receipt_calls += 1
        raw = self.manifest_file.read_bytes()
        receipt = {"status": "postcheck_passed", "manifestSha256": deploy.digest(raw),
                   "generationId": json.loads(raw)["generationId"]}
        receipt.update(self.receipt_override)
        return receipt

    def mock_run(self, command, **kwargs):
        self.run_calls.append((list(command), dict(kwargs)))
        if command == ["crontab", "-"]:
            self.assertEqual(set(kwargs), {"input", "check"})
            self.assertIs(kwargs["check"], True)
            self.assertIsInstance(kwargs["input"], bytes)
            self.cron = kwargs["input"]
            return subprocess.CompletedProcess(command, 0)
        allowed_prefix = ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p"]
        if command[:8] == allowed_prefix and command[-1:] == ["-q"] and command[8] in {"test_data_health.py", "test_qlib_incremental.py"}:
            self.assertEqual(kwargs, {"cwd": self.research, "check": True})
            if command[8] == self.fail_test:
                if self.before_test_failure:
                    self.before_test_failure()
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError("job_or_unexpected_subprocess_would_run")

    def fingerprint(self):
        return {str(path.relative_to(self.base)): (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode))
                for path in self.base.rglob("*") if path.is_file() and not path.is_symlink()}

    def ready_schedule(self):
        (self.research / "data_health.py").write_bytes(self.new["data_health.py"])

    def test_consumer_success_changes_only_reviewed_three_files_without_backup(self):
        before = self.fingerprint()
        result = deploy.consumer()
        self.assertEqual(result["status"], "consumer_installed")
        self.assertEqual(set(result["files"]), set(self.new))
        self.assertEqual(len(self.run_calls), 2)
        for name, content in self.new.items():
            self.assertEqual((self.research / name).read_bytes(), content)
        self.assertEqual(stat.S_IMODE((self.research / "data_health.py").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.research / "tests/test_data_health.py").stat().st_mode), 0o640)
        after = self.fingerprint()
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        self.assertEqual(changed, {"research/" + name for name in self.new})

    def test_consumer_head_hash_and_worktree_cas_mismatch_never_writes(self):
        for mismatch in ("head", "hash", "dirty"):
            with self.subTest(mismatch=mismatch):
                self.head = "a" * 40
                self.dirty = ""
                (self.research / "data_health.py").write_bytes(self.old["data_health.py"])
                if mismatch == "head": self.head = "b" * 40
                elif mismatch == "hash": (self.research / "data_health.py").write_bytes(b"parallel user content")
                else: self.dirty = " M unrelated.txt\n"
                before = self.fingerprint()
                with self.assertRaises(ValueError): deploy.consumer()
                self.assertEqual(before, self.fingerprint())
                self.assertEqual(self.run_calls, [])

    def test_existing_new_test_is_not_overwritten_even_if_same_candidate_bytes(self):
        path = self.research / "tests/test_qlib_incremental.py"
        path.write_bytes(self.new["tests/test_qlib_incremental.py"])
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, "new_consumer_test_already_exists"):
            deploy.consumer()
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.run_calls, [])

    def test_each_failed_test_rolls_back_only_its_three_files_without_backup(self):
        for pattern in ("test_data_health.py", "test_qlib_incremental.py"):
            with self.subTest(pattern=pattern):
                self.fail_test = pattern
                before = self.fingerprint()
                with self.assertRaises(subprocess.CalledProcessError): deploy.consumer()
                self.assertEqual(before, self.fingerprint())
                self.assertFalse((self.research / "tests/test_qlib_incremental.py").exists())
                self.assertFalse(list(self.base.rglob(".qlib-install-*")))

    def test_partial_install_write_failure_restores_only_already_written_files(self):
        original = deploy.replace
        calls = 0
        def fail_second(path, data, mode):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError("simulated_write_failure")
            return original(path, data, mode)
        before = self.fingerprint()
        with patch.object(deploy, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "simulated_write_failure"): deploy.consumer()
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.run_calls, [])

    def test_post_replace_failure_is_still_accounted_for_in_rollback(self):
        original = deploy.replace
        failed = False
        def fail_after_replace(path, data, mode):
            nonlocal failed
            original(path, data, mode)
            if not failed:
                failed = True
                raise OSError("simulated_post_replace_sync_failure")
        before = self.fingerprint()
        with patch.object(deploy, "replace", side_effect=fail_after_replace):
            with self.assertRaisesRegex(OSError, "simulated_post_replace_sync_failure"):
                deploy.consumer()
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.run_calls, [])

    def test_rollback_refuses_to_overwrite_parallel_operator_change(self):
        self.fail_test = "test_data_health.py"
        self.before_test_failure = lambda: (self.research / "data_health.py").write_bytes(b"parallel operator content")
        with self.assertRaisesRegex(ValueError, "parallel_consumer_change_requires_manual_review"):
            deploy.consumer()
        self.assertEqual((self.research / "data_health.py").read_bytes(), b"parallel operator content")
        self.assertEqual((self.research / "tests/test_data_health.py").read_bytes(), self.old["tests/test_data_health.py"])
        self.assertFalse((self.research / "tests/test_qlib_incremental.py").exists())
        self.assertEqual(self.unrelated.read_bytes(), b"unrelated user data\n")
        self.assertFalse(list(self.base.rglob(".qlib-install-*")))

    def test_schedule_replaces_only_22h_entry_keeps_2230_and_never_starts_job(self):
        self.ready_schedule()
        original_script = self.canonical.read_bytes()
        original = self.cron
        result = deploy.schedule()
        self.assertEqual(result["status"], "single_22h_entry_installed")
        self.assertEqual(self.cron, original.replace(deploy.OLD_LINE.encode(), deploy.NEW_LINE.encode(), 1))
        self.assertEqual(self.cron.decode().splitlines().count(self.snapshot_line), 1)
        self.assertEqual(self.canonical.read_bytes(), original_script)
        self.assertEqual([call[0] for call in self.run_calls], [["crontab", "-"]])
        self.assertTrue((self.install / "run.py").is_file())
        self.assertFalse((self.install / "scratch.txt").exists())
        self.assertFalse(list(self.base.rglob(".qlib-install-*")))
        self.assertEqual(self.receipt_calls, 1)

    def test_schedule_replaces_exact_active_line_not_earlier_comment_substring(self):
        self.ready_schedule()
        comment = "# Old command retained for reference: " + deploy.OLD_LINE
        self.cron = (comment + "\n").encode() + self.cron
        deploy.CRON_SHA = deploy.digest(self.cron)
        deploy.schedule()
        lines = self.cron.decode().splitlines()
        self.assertIn(comment, lines)
        self.assertEqual(lines.count(deploy.NEW_LINE), 1)
        self.assertEqual(lines.count(deploy.OLD_LINE), 0)
        self.assertEqual(lines.count(self.snapshot_line), 1)

    def test_schedule_initial_crontab_cas_mismatch_does_not_install_or_submit(self):
        self.ready_schedule()
        self.cron += b"# prior edit\n"
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, "crontab_changed"):
            deploy.schedule()
        self.assertEqual(before, self.fingerprint())
        self.assertFalse(self.install.exists())
        self.assertEqual(self.run_calls, [])

    def test_schedule_final_cas_rejects_parallel_crontab_and_does_not_submit(self):
        self.ready_schedule()
        self.final_cron_change = True
        original = self.cron
        with self.assertRaisesRegex(ValueError, "crontab_changed_before_install"):
            deploy.schedule()
        self.assertEqual(self.cron, original + b"# parallel operator edit\n")
        self.assertEqual(self.run_calls, [])
        self.assertEqual(self.canonical.read_bytes(), b"# canonical importer kept untouched\n")

    def test_schedule_verification_mismatch_is_not_reported_as_success(self):
        self.ready_schedule()
        self.verify_cron_mismatch = True
        with self.assertRaisesRegex(ValueError, "installed_crontab_disagrees"):
            deploy.schedule()
        self.assertEqual([call[0] for call in self.run_calls], [["crontab", "-"]])

    def test_schedule_refuses_existing_install_without_overwrite(self):
        self.ready_schedule()
        self.install.mkdir()
        marker = self.install / "existing.txt"
        marker.write_bytes(b"existing installation")
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, "install_target_exists_or_unsafe"):
            deploy.schedule()
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.run_calls, [])

    def test_schedule_rejects_wrong_generation_consumer_or_canonical_importer(self):
        self.ready_schedule()
        for mismatch in ("generation", "consumer", "canonical"):
            with self.subTest(mismatch=mismatch):
                self.manifest_file.write_text(json.dumps(self.promoted_manifest))
                (self.research / "data_health.py").write_bytes(self.new["data_health.py"])
                self.canonical.write_bytes(b"# canonical importer kept untouched\n")
                if mismatch == "generation": self.manifest_file.write_text(json.dumps({"status": "validated", "targetDate": "2026-09-07"}))
                elif mismatch == "consumer": (self.research / "data_health.py").write_bytes(b"unreviewed consumer")
                else: self.canonical.write_bytes(b"changed importer")
                before = self.fingerprint()
                with self.assertRaises(ValueError): deploy.schedule()
                self.assertEqual(before, self.fingerprint())
                self.assertEqual(self.run_calls, [])

    def test_schedule_rejects_disagreeing_mock_postcheck_before_any_install(self):
        self.ready_schedule()
        for key, value in (("status", "failed"), ("manifestSha256", "0" * 64), ("generationId", "a" * 32)):
            with self.subTest(key=key):
                self.receipt_override = {key: value}
                before = self.fingerprint()
                with self.assertRaisesRegex(ValueError, "promoted_postcheck_disagrees"):
                    deploy.schedule()
                self.assertEqual(before, self.fingerprint())
                self.assertFalse(self.install.exists())
                self.assertEqual(self.run_calls, [])

    def test_schedule_final_generation_cas_rejects_mutation_without_submitting_cron(self):
        self.ready_schedule()
        self.change_manifest_before_schedule = True
        original = self.cron
        with self.assertRaisesRegex(ValueError, "promoted_generation_changed_before_schedule"):
            deploy.schedule()
        self.assertEqual(self.cron, original)
        self.assertEqual(self.run_calls, [])


if __name__ == "__main__":
    unittest.main()
