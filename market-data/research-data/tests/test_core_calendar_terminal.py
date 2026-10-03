"""Fixed observed-schema fixtures, not a replay of the lost 09-23 context."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest import mock

import core_market as core
import calendar_evidence as calendar_adapter
import daily_snapshot as legacy
import data_health
import test_core_calendar_evidence as calendar_tests
from test_core_market_contract import qlib
DAY, NOW = calendar_tests.DAY, calendar_tests.NOW


class TerminalCalendarTests(unittest.TestCase):
    setUp = calendar_tests.CalendarAdapterTests.setUp
    write_json = calendar_tests.CalendarAdapterTests.write_json
    frozen_calendar = calendar_tests.CalendarAdapterTests.frozen_calendar
    archive = calendar_tests.CalendarAdapterTests.archive

    def fixture(self, status="failed"):
        source_id = "akshare:sina:trade-calendar"
        c = self.frozen_calendar()
        context = {"schemaVersion": "1.0", "meta": {"targetDate": DAY, "timezone": "Asia/Shanghai",
                   "asOf": DAY + "T08:00:10+08:00", "status": "partial"},
                   "data": {"tradingCalendar": c},
                   "sources": [{"id": source_id, "dataAsOf": c["dataAsOf"],
                                "url": "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"}]}
        cp = self.write_json("work/morning-context.json", context)
        last = {"schemaVersion": "1.0", "targetDate": DAY, "status": status,
                "phase": "collect" if status == "failed" else "publish",
                "startedAt": DAY + "T08:00:03.526+08:00", "frozenCutoffInstant": DAY + "T08:00:02.699+08:00",
                "contextFile": "work/morning-context.json", "contextSha256": hashlib.sha256(cp.read_bytes()).hexdigest(),
                "publicationGate": "passed" if status == "success" else "blocked"}
        last["failedAt" if status == "failed" else "completedAt"] = DAY + "T08:00:10.841+08:00"
        if status == "failed":
            last.update(failureCode="workflow_internal_error", retryable=False)
        state = {"schemaVersion": "1.0", "task": "morning-report", "targetDate": DAY,
                 "invocationId": "4d330bd6-14da-4428-bc25-8952ef32a057", "status": status,
                 "startedAt": DAY + "T08:00:02.699+08:00", "completedAt": DAY + "T08:00:10.932+08:00",
                 "childExitCode": 0 if status == "success" else 2 if status == "blocked" else 1, "timedOut": False, "interrupted": False}
        lp = self.write_json("work/last-run.json", last)
        sp = self.write_json("work/orchestrator/states/morning-report-" + DAY + ".json", state)
        return {"context": cp, "last": lp, "state": sp}

    def mutate(self, paths, part, change, rebind=False):
        path = paths[part]
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))
        if part == "context" and rebind:
            self.mutate(paths, "last", lambda v: v.update(contextSha256=hashlib.sha256(path.read_bytes()).hexdigest()))

    def test_failed_generation_valid_calendar_can_archive_qlib_without_published(self):
        paths = self.fixture()
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("no network")):
            evidence = core.load_calendar_evidence(self.root, NOW)
            self.assertEqual(evidence["binding"]["kind"], "validated_terminal_context")
            self.assertEqual(evidence["binding"]["runStatus"], "failed")
            self.assertEqual(evidence["binding"]["contextSha256"], hashlib.sha256(paths["context"].read_bytes()).hexdigest())
            self.assertFalse((self.root / "work/report-archives").exists())
            q = qlib(); q["date"] = DAY
            with mock.patch.object(core, "qlib_market", return_value=q):
                result = core.run_core(db_path=self.root / "core.db", now=NOW.isoformat(), calendar_evidence=evidence)
            self.assertEqual(result["status"], "inserted")
            self.assertEqual(data_health.core_market_rows(self.root / "core.db", NOW)[0][DAY]["state"], "valid")
        self.assertEqual(json.loads(paths["last"].read_text())["status"], "failed")

    def test_success_and_blocked_terminal_runs_without_published_are_eligible(self):
        for status in ("success", "blocked"):
            self.fixture(status)
            evidence = core.load_calendar_evidence(self.root, NOW)
            self.assertEqual(evidence["binding"]["runStatus"], status)
            self.assertEqual(evidence["document"]["status"], "open")

    def test_partial_archive_without_publication_does_not_reintroduce_dependency(self):
        self.fixture()
        self.write_json("work/report-archives/" + DAY + "/input.json", {"incomplete": True})
        self.assertEqual(core.load_calendar_evidence(self.root, NOW)["binding"]["kind"], "validated_terminal_context")

    def test_stable_absence_is_rechecked_at_the_exact_archive_path(self):
        self.fixture()
        self.write_json("work/report-archives/" + DAY + "/input.json", {"incomplete": True})
        with mock.patch.object(calendar_adapter, "_assert_publication_absent", wraps=calendar_adapter._assert_publication_absent) as check:
            evidence = core.load_calendar_evidence(self.root, NOW)
        self.assertEqual(evidence["document"]["status"], "open")
        self.assertEqual(check.call_count, 2)
        for call in check.call_args_list:
            self.assertEqual(call.args, (self.root, self.root / "work/report-archives" / DAY, DAY))

    def test_published_appearance_during_context_read_blocks_live_evidence(self):
        published = self.root / "work/report-archives" / DAY / "published.json"
        parse = calendar_adapter.strict_json
        for contents in ('{"status":"generation_failed"}', '{"status":"sealed"}', '{', None):
            self.fixture()
            published.unlink(missing_ok=True)
            def race(raw):
                value = parse(raw)
                if isinstance(value, dict) and "meta" in value and "data" in value and not published.is_symlink():
                    published.parent.mkdir(parents=True, exist_ok=True)
                    if contents is None: published.symlink_to(self.root / "missing-publication")
                    else: published.write_text(contents)
                return value
            with self.subTest(contents=contents), mock.patch.object(calendar_adapter, "strict_json", side_effect=race):
                with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)

    def test_archive_parent_symlink_appearing_during_context_read_is_rejected(self):
        self.fixture()
        parent = self.root / "work/report-archives"
        target = self.root / "elsewhere"
        target.mkdir()
        parse = calendar_adapter.strict_json
        def race(raw):
            value = parse(raw)
            if isinstance(value, dict) and "meta" in value and "data" in value:
                parent.symlink_to(target, target_is_directory=True)
            return value
        with mock.patch.object(calendar_adapter, "strict_json", side_effect=race):
            with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)

    def test_unpublished_check_refuses_a_different_archive_identity(self):
        with self.assertRaisesRegex(ValueError, "unexpected unpublished archive path"):
            calendar_adapter._assert_publication_absent(self.root, self.root / "work/report-archives/2026-09-23", DAY)

    def test_required_directory_disappearing_after_final_file_read_is_rejected(self):
        paths = self.fixture()
        records = {str(path.relative_to(self.root)): path.read_bytes() for path in paths.values()}
        real_lstat = Path.lstat
        for missing in (self.root, self.root / "work"):
            gone, context_reads = False, 0
            def read(name, limit):
                nonlocal gone, context_reads
                if name == "work/morning-context.json":
                    context_reads += 1
                    gone = context_reads == 2
                return records[name]
            def lstat(path, *args, **kwargs):
                if gone and path == missing:
                    raise FileNotFoundError(str(path))
                return real_lstat(path, *args, **kwargs)
            with self.subTest(missing=missing.name), mock.patch.object(Path, "lstat", new=lstat):
                with self.assertRaises(FileNotFoundError):
                    calendar_adapter._open_context_evidence(read, DAY, NOW, records[str(paths["state"].relative_to(self.root))],
                                                records[str(paths["last"].relative_to(self.root))],
                                                project_root=self.root, unpublished_archive=self.root / "work/report-archives" / DAY)

    def test_invalid_existing_published_cannot_fall_back_to_live_context(self):
        self.fixture()
        archive = self.archive()
        for raw in ('{"status":"generation_failed"}', '{'):
            (archive / "published.json").write_text(raw)
            with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)

    def test_missing_or_tampered_or_overwritten_context_rejected(self):
        for mode in ("missing", "missing_last", "missing_state", "tampered", "overwritten", "missing_hash"):
            paths = self.fixture()
            if mode == "missing": paths["context"].unlink()
            elif mode == "missing_last": paths["last"].unlink()
            elif mode == "missing_state": paths["state"].unlink()
            elif mode == "tampered": paths["context"].write_text('{}')
            elif mode == "overwritten": self.mutate(paths, "context", lambda v: v["meta"].update(asOf=DAY + "T10:00:00+08:00"))
            else: self.mutate(paths, "last", lambda v: v.pop("contextSha256"))
            with self.subTest(mode=mode), self.assertRaises(legacy.SnapshotDeferredError):
                core.load_calendar_evidence(self.root, NOW)

    def test_nonterminal_wrong_day_and_conflicting_parent_rejected(self):
        changes = [("state", {"status": "running"}), ("last", {"status": "running"}),
                   ("state", {"targetDate": "2026-09-23"}), ("last", {"targetDate": "2026-09-23"}),
                   ("state", {"startedAt": DAY + "T10:00:00+08:00"}), ("state", {"invocationId": ""}),
                   ("state", {"timedOut": True}), ("state", {"interrupted": True}),
                   ("state", {"childExitCode": 0}), ("last", {"phase": "unknown"}),
                   ("last", {"contextFile": "work/../secret.json"})]
        for part, change in changes:
            paths = self.fixture()
            self.mutate(paths, part, lambda v: v.update(change))
            with self.subTest(change=change), self.assertRaises(legacy.SnapshotDeferredError):
                core.load_calendar_evidence(self.root, NOW)

    def test_invalid_terminal_times_and_stale_calendar_rejected(self):
        changes = [("last", {"failedAt": DAY + "T08:00:10"}),
                   ("last", {"failedAt": DAY + "T23:00:00+08:00"}),
                   ("last", {"startedAt": DAY + "T07:00:00+08:00"}),
                   ("last", {"frozenCutoffInstant": DAY + "T07:59:59+08:00"}),
                   ("state", {"completedAt": DAY + "T08:00:01+08:00"})]
        for part, change in changes:
            paths = self.fixture(); self.mutate(paths, part, lambda v: v.update(change))
            with self.subTest(change=change), self.assertRaises(legacy.SnapshotDeferredError):
                core.load_calendar_evidence(self.root, NOW)
        for stamp in ("2026-09-26T08:00:00+08:00", DAY + "T08:00:04+08:00"):
            paths = self.fixture()
            def change(v):
                v["data"]["tradingCalendar"]["dataAsOf"] = stamp
                v["sources"][0]["dataAsOf"] = stamp
            self.mutate(paths, "context", change, True)
            with self.subTest(stamp=stamp), self.assertRaises(legacy.SnapshotDeferredError):
                core.load_calendar_evidence(self.root, NOW)

    def test_source_registry_and_calendar_must_be_same_object_identity(self):
        changes = [lambda v: v["sources"].clear(),
                   lambda v: v["sources"].append(copy.deepcopy(v["sources"][0])),
                   lambda v: v["sources"][0].update(dataAsOf=DAY + "T07:59:59+08:00"),
                   lambda v: v["sources"][0].update(url="https://example.invalid/calendar"),
                   lambda v: v["data"]["tradingCalendar"].update(status="non_trading_day"),
                   lambda v: v["data"]["tradingCalendar"].update(targetDate="2026-09-23"),
                   lambda v: v["meta"].update(targetDate="2026-09-23")]
        for change in changes:
            paths = self.fixture(); self.mutate(paths, "context", change, True)
            with self.assertRaises(legacy.SnapshotDeferredError): core.load_calendar_evidence(self.root, NOW)

    def test_compiler_context_hash_is_not_used_as_raw_file_hash(self):
        paths = self.fixture("success")
        self.mutate(paths, "last", lambda v: v.update(compilerContextSha256="f" * 64))
        evidence = core.load_calendar_evidence(self.root, NOW)
        self.assertEqual(evidence["binding"]["contextSha256"], hashlib.sha256(paths["context"].read_bytes()).hexdigest())
        self.assertNotEqual(evidence["binding"]["contextSha256"], "f" * 64)

    def test_mid_read_occurrence_change_rejected(self):
        paths = self.fixture()
        records = {str(p.relative_to(self.root)): p.read_bytes() for p in paths.values()}
        def changed_read(name, limit):
            if name == "work/orchestrator/states/morning-report-" + DAY + ".json": return b'{}'
            return records[name]
        with self.assertRaisesRegex(ValueError, "changed during read"):
            calendar_adapter._open_context_evidence(changed_read, DAY, NOW, paths["state"].read_bytes(), paths["last"].read_bytes(),
                                        project_root=self.root, unpublished_archive=self.root / "work/report-archives" / DAY)

    def test_persisted_binding_revalidation_rejects_rehashed_claim_changes(self):
        self.fixture()
        evidence = core.load_calendar_evidence(self.root, NOW)
        for change in ({"stateStatus": "running"}, {"childExitCode": 0}, {"cutoffInstant": DAY + "T07:00:00+08:00"}):
            modified = copy.deepcopy(evidence); modified["binding"].update(change)
            modified["sha256"] = core.digest({k:v for k,v in modified.items() if k != "sha256"})
            with self.subTest(change=change), self.assertRaises(ValueError):
                core.validate_calendar_evidence(modified, NOW)


if __name__ == "__main__":
    unittest.main()
