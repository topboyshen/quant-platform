import copy
import datetime as dt
import hashlib
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock

import core_market as core
import daily_snapshot as legacy
import research_appendix as appendix

DAY = "2026-09-24"
NOW = DAY + "T22:30:00+08:00"


def qlib():
    return {"date": DAY, "generation": {"tag": "fixed-source", "inode": 100},
            "coverage": {"valid": 5556, "eligible": 5567, "invalid": 11, "ratio": round(5556 / 5567, 6)},
            "breadth": {"up": 3000, "down": 2000, "flat": 556, "total": 5556}}


def stage():
    market = {"sectors": [{"name": "sector" + str(i), "chg": 1., "netInflow": 1.,
                            "upCount": 20, "downCount": 10, "leader": "sample"} for i in range(60)],
              "activity": {"上涨": 3000, "下跌": 2000, "平盘": 100, "涨停": 50, "跌停": 5,
                           "统计日期": DAY + " 15:00:00"}}
    value = {"schemaVersion": 1, "tradeDate": DAY, "akshare": market,
             "collectionWindow": {"startedAt": DAY + "T22:29:00+08:00", "completedAt": DAY + "T22:29:30+08:00"},
             "payloadSha256": core.digest(market)}
    value["recordSha256"] = core.digest(value)
    return value


def record():
    return core.build_core(qlib(), run_id="test-run", generated_at=NOW, calendar_evidence=calendar())


def calendar(day=DAY, status="open"):
    target = dt.date.fromisoformat(day)
    value = {"document": {"schemaVersion": 1, "targetDate": day, "status": status,
              "previousTradingDate": (target - dt.timedelta(days=1)).isoformat(),
              "nextTradingDate": (target + dt.timedelta(days=1)).isoformat(),
              "sourceIds": ["akshare:sina:trade-calendar"], "observedAt": day + "T07:50:00+08:00",
              "validUntil": day + "T23:59:59+08:00"},
             "binding": {"archiveDate": day, "inputSha256": "a" * 64,
                         "cutoffInstant": day + "T08:00:00+08:00", "publishedStatus": "sealed"}}
    value["sha256"] = core.digest(value)
    return value


def closed_calendar(day=DAY):
    value = calendar(day, "closed")
    value["binding"] = {"kind": "validated_non_trading_skip", "targetDate": day,
                        "contextSha256": "a" * 64, "lastRunSha256": "b" * 64, "stateSha256": "c" * 64,
                        "completedAt": day + "T08:00:01+08:00"}
    value["sha256"] = core.digest({k: v for k, v in value.items() if k != "sha256"})
    return value


class CoreContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "core.db"
        self.no_network = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def test_core_declares_reduced_scope(self):
        value = record()
        self.assertFalse(value["fullSnapshot"])
        self.assertEqual(value["schema"], "research.core-market.v1")
        self.assertEqual(value["appendix"]["status"], "not_requested")
        self.assertEqual(len(value["appendix"]["sources"]), 15)
        self.assertEqual(value["requiredSources"], ["qlib"])
        self.assertEqual(value["artifactKind"], "qlib_market_summary")
        self.assertFalse(value["includesCompletePriceArchive"])

    def test_false_full_or_missing_schema_rejected(self):
        for key, val in [("fullSnapshot", True), ("schema", "snapshot"), ("scope", "full_snapshot")]:
            value = record(); value[key] = val
            with self.subTest(key=key), self.assertRaises(ValueError):
                core.store_core(value, self.db)
        self.assertFalse(self.db.exists())

    def test_wrong_date_and_tampered_calendar_rejected(self):
        for change in (lambda r: r["qlib"].update(date="2026-09-23"),
                       lambda r: r["calendarEvidence"]["document"].update(status="closed"),
                       lambda r: r["calendarEvidence"].update(sha256="0" * 64)):
            value = record(); change(value)
            with self.assertRaises((ValueError, legacy.SnapshotValidationError)):
                core.store_core(value, self.db)

    def test_original_coverage_threshold_and_consistency_preserved(self):
        for change in (lambda c: c.update(valid=5400, invalid=167, ratio=round(5400/5567, 6)),
                       lambda c: c.update(eligible=4000), lambda c: c.update(invalid=12),
                       lambda c: c.update(ratio=1.0), lambda c: c.update(valid=True)):
            value = record(); change(value["qlib"]["coverage"])
            with self.assertRaises(ValueError): core.store_core(value, self.db)

    def test_nonfinite_and_unzoned_time_rejected(self):
        for key, val in [("generatedAt", DAY + "T22:30:00"), ("generatedAt", DAY + "T07:00:00+08:00")]:
            value = record(); value[key] = val
            with self.assertRaises(ValueError): core.store_core(value, self.db)
        value = record(); value["qlib"]["extra"] = float("nan")
        with self.assertRaises(ValueError): core.store_core(value, self.db)

    def test_append_only_and_exact_idempotency(self):
        value = record()
        self.assertEqual(core.store_core(value, self.db)["status"], "inserted")
        self.assertEqual(core.read_core(DAY, self.db), value)
        self.assertEqual(core.store_core(value, self.db)["status"], "idempotent")
        changed = copy.deepcopy(value); changed["runId"] = "different"
        with self.assertRaisesRegex(ValueError, "conflict"): core.store_core(changed, self.db)
        self.assertEqual(core.read_core(DAY, self.db), value)

    def test_legacy_database_bytes_unchanged_and_refused(self):
        legacy_db = Path(self.temp.name) / "snapshot.db"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY,payload TEXT)")
            conn.execute("INSERT INTO snapshot VALUES ('2026-09-22','{\"legacy\":true}')")
        before = legacy_db.read_bytes()
        with self.assertRaisesRegex(ValueError, "legacy"):
            core.store_core(record(), legacy_db)
        core.store_core(record(), self.db)
        self.assertEqual(legacy_db.read_bytes(), before)

    def test_stored_payload_corruption_rejected(self):
        core.store_core(record(), self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE core_market SET payload_sha256=?", ("0" * 64,))
        with self.assertRaisesRegex(ValueError, "hash mismatch"): core.read_core(DAY, self.db)

    def run_fixture(self, **kwargs):
        with mock.patch.object(legacy, "qlib_market", return_value=qlib()), \
             mock.patch.object(legacy, "load_akshare_market_stage", return_value=stage()), \
             mock.patch.object(legacy, "capture_current_akshare_market", return_value={"tradeDate": DAY}), \
             mock.patch.object(legacy, "collect_cninfo_announcements", side_effect=AssertionError("research source called")), \
             mock.patch.object(legacy, "collect_concepts", side_effect=AssertionError("research source called")), \
             mock.patch.object(legacy, "spot_market", side_effect=AssertionError("research source called")):
            return core.run_core(db_path=self.db, run_id="natural-run", now=NOW, calendar_evidence=calendar(), **kwargs)

    def test_core_does_not_collect_research_sources(self):
        self.assertEqual(self.run_fixture()["status"], "inserted")
        old = core.read_core(DAY, self.db)
        self.assertEqual(self.run_fixture()["status"], "skipped_existing")
        self.assertEqual(core.read_core(DAY, self.db), old)

    def test_changed_same_day_qlib_defers_without_rewriting_sealed_core(self):
        self.run_fixture()
        before = self.db.read_bytes()
        changed = qlib(); changed["generation"]["tag"] = "new-same-day-source"
        with mock.patch.object(legacy, "qlib_market", return_value=changed):
            with self.assertRaisesRegex(legacy.SnapshotDeferredError, "differs"):
                core.run_core(db_path=self.db, now=NOW, calendar_evidence=calendar())
        self.assertEqual(self.db.read_bytes(), before)

    def test_calendar_missing_defers_without_row(self):
        with mock.patch.object(legacy, "qlib_market", return_value=qlib()), \
             mock.patch.object(legacy, "load_akshare_market_stage", return_value=None), \
             mock.patch.object(legacy, "capture_current_akshare_market", side_effect=legacy.SnapshotDeferredError("fixed failure")):
            with self.assertRaises(legacy.SnapshotDeferredError): core.run_core(db_path=self.db)
        self.assertFalse(self.db.exists())

    def test_old_target_not_success_when_new_trading_day_due(self):
        core.store_core(record(), self.db)
        with mock.patch.object(legacy, "qlib_market", return_value=qlib()), \
             mock.patch.object(legacy, "load_akshare_market_stage", return_value=stage()), \
             mock.patch.object(legacy, "capture_current_akshare_market", return_value={"tradeDate": "2026-09-25"}):
            with self.assertRaises(legacy.SnapshotDeferredError):
                core.run_core(db_path=self.db, now="2026-09-25T22:30:00+08:00", calendar_evidence=calendar("2026-09-25"))

    def test_no_tables_found_in_optional_market_source_cannot_block_core(self):
        with mock.patch.object(legacy, "qlib_market", return_value=qlib()), \
             mock.patch.object(legacy, "capture_current_akshare_market", side_effect=ValueError("No tables found")) as capture, \
             mock.patch.object(legacy, "akshare_market", side_effect=ValueError("No tables found")) as market:
            result = core.run_core(db_path=self.db, now=NOW, calendar_evidence=calendar())
            self.assertEqual(result["status"], "inserted")
            capture.assert_not_called(); market.assert_not_called()
            value = appendix.build_appendix(core.read_core(DAY, self.db), ["akshareMarket"], ["002617"], clock=lambda: NOW)
            self.assertEqual(value["status"], "partial")
            self.assertEqual(value["sources"]["akshareMarket"]["errorType"], "ValueError")
            self.assertEqual(core.read_core(DAY, self.db)["date"], DAY)

    def test_historical_cli_arguments_rejected_before_sources(self):
        with mock.patch.object(legacy, "qlib_market") as scan:
            with self.assertRaises(SystemExit): core.main(["2026-09-22"])
            scan.assert_not_called()

    def test_legacy_last_run_cannot_be_overwritten(self):
        with self.assertRaises(SystemExit): core.main(["--state-dir", legacy.STATE_DIR])

    def test_appendix_only_explicit_sources_and_distinct_hash(self):
        core.store_core(record(), self.db)
        before = self.db.read_bytes()
        chosen = mock.Mock(return_value={"facts": ["fixed"]})
        other = mock.Mock(side_effect=AssertionError("unrequested"))
        value = appendix.build_appendix(record(), ["news"], ["002617"],
                    source_collectors={"news": chosen, "concepts": other}, clock=lambda: NOW)
        chosen.assert_called_once(); other.assert_not_called()
        self.assertEqual(value["corePayloadSha256"], core.digest(record()))
        self.assertEqual(value["status"], "collected_unreviewed")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(value["fullSnapshot"])

    def test_appendix_failure_preserves_core(self):
        core.store_core(record(), self.db); before = self.db.read_bytes()
        value = appendix.build_appendix(record(), ["news", "concepts"], ["002617"],
            source_collectors={"news": mock.Mock(side_effect=TimeoutError("sensitive raw body")),
                               "concepts": lambda: {"002617": {"error": "fixed"}}}, clock=lambda: NOW)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(value["sources"]["news"]["errorType"], "TimeoutError")
        self.assertNotIn("sensitive", core.encode(value))
        self.assertEqual(self.db.read_bytes(), before)

    def test_appendix_requires_explicit_sources_and_same_observation_day(self):
        for requested, codes, clock in [([], ["002617"], NOW), (["unknown"], ["002617"], NOW),
                                       (["news"], [], NOW), (["news"], ["002617"], "2026-09-25T08:00:00+08:00")]:
            with self.assertRaises(ValueError):
                appendix.build_appendix(record(), requested, codes, source_collectors={}, clock=lambda: clock)

    def test_appendix_database_is_separate_append_only(self):
        value = appendix.build_appendix(record(), ["news"], ["002617"], source_collectors={"news": lambda: []}, clock=lambda: NOW)
        db = Path(self.temp.name) / "appendix.db"
        self.assertEqual(appendix.store_appendix(value, record(), db)["status"], "inserted")
        self.assertEqual(appendix.store_appendix(value, record(), db)["status"], "idempotent")
        changed = copy.deepcopy(value); changed["securityCodes"] = ["600875"]
        with self.assertRaisesRegex(ValueError, "conflict"): appendix.store_appendix(changed, record(), db)
        core.store_core(record(), self.db)
        with self.assertRaisesRegex(ValueError, "refusing"): appendix.store_appendix(value, record(), self.db)
        with self.assertRaisesRegex(ValueError, "appendix"): core.store_core(record(), db)

    def test_appendix_tampered_core_binding_or_time_rejected(self):
        value = appendix.build_appendix(record(), ["news"], ["002617"], source_collectors={"news": lambda: []}, clock=lambda: NOW)
        for change in (lambda r: r.update(corePayloadSha256="0" * 64),
                       lambda r: r["collectionWindow"].update(startedAt=DAY + "T08:00:00+08:00"),
                       lambda r: r.update(status="passed")):
            changed = copy.deepcopy(value); change(changed)
            with self.assertRaises(ValueError): appendix.store_appendix(changed, record(), Path(self.temp.name) / "appendix.db")


if __name__ == "__main__":
    unittest.main()
