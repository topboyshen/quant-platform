"""Precommit consumer admission tests. All writes are isolated temporary fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import candidate


HERE = Path(__file__).resolve().parent
REAL_HEALTH = HERE / "consumer-compat" / "data_health.py"
HELPERS = HERE / "consumer-compat" / "tests" / "test_qlib_incremental.py"


def fixture_helpers():
    """Reuse only offline constructors; do not run the imported test classes."""
    health = types.ModuleType("_candidate_fixture_health")
    health.__file__ = str(REAL_HEALTH)
    exec(compile(REAL_HEALTH.read_bytes(), str(REAL_HEALTH), "exec"), health.__dict__)
    helper = types.ModuleType("_candidate_fixture_helpers")
    helper.__file__ = str(HELPERS)
    with patch.dict(sys.modules, {"data_health": health}):
        exec(compile(HELPERS.read_bytes(), str(HELPERS), "exec"), helper.__dict__)
    return health, helper


class CandidateConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qlib-candidate-consumer-")
        self.addCleanup(self.temp.cleanup)
        # Canonicalize macOS /var aliases; the production helper forbids aliases.
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "stage"
        self.root.mkdir()
        self.health_file = self.base / "health.py"
        self.health_file.write_bytes(REAL_HEALTH.read_bytes())
        # A regressed inner guard must still never reach a real side effect;
        # guard fixture-module loading as well as the candidate check itself.
        for owner, name in ((socket.socket, "connect"), (sqlite3, "connect"),
                            (subprocess, "run"), (subprocess, "Popen")):
            guard = patch.object(owner, name, side_effect=AssertionError("escaped_candidate_guard"))
            guard.start()
            self.addCleanup(guard.stop)
        self.real_health, self.helpers = fixture_helpers()
        self.days = ["2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"]
        (self.root / "calendars").mkdir()
        (self.root / "calendars" / "day.txt").write_text("\n".join(self.days) + "\n")
        (self.base / ".tag").write_text("2026-09-06\n")
        (self.root / ".qlib-release").write_text("2026-09-06\n")
        for symbol in self.real_health.REFERENCES:
            directory = self.root / "features" / symbol
            directory.mkdir(parents=True)
            for field in self.real_health.FIELDS:
                (directory / (field + ".day.bin")).write_bytes(struct.pack("<5f", 0, 10, 11, 12, 13))
        added = self.days[2:]
        self.manifest = {
            "schemaVersion": 1, "kind": "same_source_dolt_incremental", "status": "validated",
            "sourceRepository": "chenditc/investment_data", "sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq",
            "generationId": "a" * 32, "baseReleaseTag": "2026-09-06", "baseDate": "2026-09-04",
            "targetDate": "2026-09-08", "calendarAdded": added, "sourceRowsSha256": "b" * 64,
            "historicalPrefixes": {"unchanged": True, "files": 17, "bytes": 2000, "sha256": "c" * 64},
            "sourceAbsence": {"sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq", "dates": {}},
            "priceValidation": self.helpers.price_claim(added),
            "legacyUnquotedCSI": self.helpers.legacy_claim(added),
            "indexPriceValidation": self.helpers.index_claim("2026-09-08"),
        }
        self.write_manifest()

    def write_manifest(self, document=None):
        (self.root / candidate.MANIFEST).write_text(json.dumps(self.manifest if document is None else document))

    def fingerprint(self):
        return {str(path.relative_to(self.base)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.base.rglob("*") if path.is_file() and not path.is_symlink()}

    def fake_consumer(self, *, on_import="", in_call="", change_result=""):
        text = "import hashlib, json, socket, sqlite3, subprocess\nfrom pathlib import Path\n" + on_import + "\n"
        text += "def qlib_evidence(root):\n"
        for line in in_call.splitlines():
            text += "    " + line + "\n"
        text += (
            f"    raw = (root / {candidate.MANIFEST!r}).read_bytes()\n"
            "    document = json.loads(raw)\n"
            "    calendar = (root / 'calendars' / 'day.txt').read_text().splitlines()\n"
            "    complete = calendar[-1]\n"
            "    generation = [root.stat().st_ino, document['baseReleaseTag'], hashlib.sha256(raw).hexdigest(), [document['generationId']]]\n"
        )
        for line in change_result.splitlines():
            text += "    " + line + "\n"
        text += "    return calendar, complete, generation\n"
        self.health_file.write_text(text)
        return text.encode()

    def test_real_reviewed_consumer_accepts_staged_generation_without_mutation(self):
        before = self.fingerprint()
        expected = self.health_file.read_bytes()
        result = candidate.consumer_check(self.health_file)(self.root, self.manifest)
        self.assertEqual(result, {"status": "passed", "referenceCompleteDate": "2026-09-08",
                                 "consumerSha256": hashlib.sha256(expected).hexdigest(),
                                 "manifestBytes": (self.root / candidate.MANIFEST).stat().st_size,
                                 "networkOrDatabaseCalls": False})
        self.assertEqual(before, self.fingerprint())

    def test_real_consumer_accepts_disclosed_partial_generation(self):
        missing = {"SH600825": ["2026-09-07", "2026-09-08"]}
        self.manifest["sourceAbsence"]["dates"] = missing
        self.manifest["priceValidation"] = self.helpers.price_claim(self.days[2:], missing,
            {"2026-09-07": {"SH600825": ["source_absent_today"]},
             "2026-09-08": {"SH600825": ["source_absent_today", "prior_close_source_absent"]}},
            {"SH600825": "2026-09-04"})
        self.manifest["indexPriceValidation"] = self.helpers.index_claim("2026-09-08",
            missing_quotes=["SH600825"], missing_previous=["SH600825"], absence=missing)
        self.write_manifest()
        before = self.fingerprint()
        self.assertEqual(candidate.consumer_check(self.health_file)(self.root, self.manifest)["status"], "passed")
        self.assertEqual(before, self.fingerprint())

    def test_module_change_after_capture_is_rejected_before_execution(self):
        check = candidate.consumer_check(self.health_file)
        self.health_file.write_text("raise AssertionError('changed_module_was_executed')\n")
        with self.assertRaisesRegex(ValueError, "consumer_module_changed"):
            check(self.root, self.manifest)

    def test_captured_bytes_execute_even_if_path_changes_after_identity_read(self):
        check = candidate.consumer_check(self.health_file)
        original = candidate.read_small
        replaced = False
        def substitute(path, *args, **kwargs):
            nonlocal replaced
            if path == self.root / candidate.MANIFEST and not replaced:
                self.health_file.write_text("raise AssertionError('replacement_bytes_executed')\n")
                replaced = True
            return original(path, *args, **kwargs)
        with patch.object(candidate, "read_small", side_effect=substitute):
            with self.assertRaisesRegex(ValueError, "candidate_or_consumer_changed_during_check"):
                check(self.root, self.manifest)

    def test_loader_exec_module_is_not_used_instead_of_captured_bytes(self):
        expected = self.fake_consumer()
        real_spec = candidate.importlib.util.spec_from_file_location
        def guarded_spec(*args, **kwargs):
            spec = real_spec(*args, **kwargs)
            spec.loader.exec_module = lambda _: (_ for _ in ()).throw(AssertionError("uncaptured_loader_execution"))
            return spec
        with patch.object(candidate.importlib.util, "spec_from_file_location", side_effect=guarded_spec):
            result = candidate.consumer_check(self.health_file)(self.root, self.manifest)
        self.assertEqual(result["consumerSha256"], hashlib.sha256(expected).hexdigest())

    def test_import_and_call_side_effects_are_blocked_by_specific_guards(self):
        cases = [("with socket.socket() as sock:\n    sock.connect(('127.0.0.1', 9))", "network_forbidden"),
                 ("sqlite3.connect(':memory:')", "database_forbidden"),
                 ("subprocess.run(['never-execute'])", "subprocess_forbidden"),
                 ("subprocess.Popen(['never-execute'])", "subprocess_forbidden")]
        for operation, message in cases:
            for phase in ("import", "call"):
                with self.subTest(operation=operation, phase=phase):
                    self.fake_consumer(**{"on_import" if phase == "import" else "in_call": operation})
                    with self.assertRaisesRegex(AssertionError, "^" + message + "$"):
                        candidate.consumer_check(self.health_file)(self.root, self.manifest)

    def test_target_calendar_complete_tag_hash_and_generation_id_mismatch_rejected(self):
        for mutation in ("calendar[-1] = '2026-09-07'", "complete = '2026-09-07'",
                         "generation[1] = '2026-09-05'", "generation[2] = '0' * 64",
                         "generation[3][0] = 'f' * 32"):
            with self.subTest(mutation=mutation):
                self.fake_consumer(change_result=mutation)
                with self.assertRaisesRegex(ValueError, "candidate_consumer_disagrees"):
                    candidate.consumer_check(self.health_file)(self.root, self.manifest)

    def test_passed_manifest_must_match_entire_installed_manifest(self):
        for key, value in (("targetDate", "2026-09-09"), ("generationId", "d" * 32),
                           ("baseReleaseTag", "2026-09-05"), ("sourceCommit", "c" * 32),
                           ("sourceRowsSha256", "d" * 64), ("scope", "changed_scope")):
            with self.subTest(key=key):
                altered = dict(self.manifest, **{key: value})
                with self.assertRaises(ValueError):
                    candidate.consumer_check(self.health_file)(self.root, altered)

    def test_malformed_or_inconsistent_disk_manifest_is_rejected(self):
        check = candidate.consumer_check(self.health_file)
        for raw in (b"not json", b"[]", b'{"x":1,"x":2}'):
            with self.subTest(raw=raw):
                (self.root / candidate.MANIFEST).write_bytes(raw)
                with self.assertRaises(ValueError): check(self.root, self.manifest)
        changed = copy.deepcopy(self.manifest)
        changed["calendarAdded"] = ["2026-09-08"]
        self.write_manifest(changed)
        with self.assertRaises(ValueError): check(self.root, changed)

    def test_manifest_change_during_consumer_read_is_detected(self):
        self.fake_consumer(change_result=f"with (root / {candidate.MANIFEST!r}).open('ab') as stream:\n    stream.write(b' ')")
        with self.assertRaisesRegex(ValueError, "candidate_or_consumer_changed_during_check"):
            candidate.consumer_check(self.health_file)(self.root, self.manifest)

    def test_health_change_during_consumer_read_is_detected(self):
        self.fake_consumer(change_result="Path(__file__).write_text('# changed during check\\n')")
        with self.assertRaisesRegex(ValueError, "candidate_or_consumer_changed_during_check"):
            candidate.consumer_check(self.health_file)(self.root, self.manifest)

    def test_root_inode_change_during_check_is_detected(self):
        moved = self.base / "displaced-stage"
        self.fake_consumer(change_result=f"root.rename({str(moved)!r})\nroot.mkdir()")
        with self.assertRaisesRegex(ValueError, "candidate_or_consumer_changed_during_check"):
            candidate.consumer_check(self.health_file)(self.root, self.manifest)

    def test_missing_and_symlinked_consumer_paths_are_rejected(self):
        with self.assertRaises(FileNotFoundError): candidate.consumer_check(self.base / "absent.py")
        link = self.base / "linked.py"
        link.symlink_to(self.health_file)
        with self.assertRaisesRegex(ValueError, "unsafe_consumer_module_path"):
            candidate.consumer_check(link)


if __name__ == "__main__":
    unittest.main()
