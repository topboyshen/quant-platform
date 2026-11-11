"""Offline tests for the 07:50 CNINFO staging contract (no network, no production paths)."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

# Import the staged-artifact contract from mcp/ directly so this suite also
# runs on its own; a full discovery run previously worked only because a
# sibling test happened to extend sys.path first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from cninfo_staging import (MAX_BYTES, StagingError, load_staging, staging_path,
                            write_staging)

CST = dt.timezone(dt.timedelta(hours=8))
TARGET = "2026-09-22"
QUERIED = dt.datetime(2026, 9, 22, 7, 50, tzinfo=CST)
QUERIED_LATE = dt.datetime(2026, 9, 22, 7, 55, tzinfo=CST)
BATCH = dt.datetime(2026, 9, 22, 7, 56, tzinfo=CST)
CUTOFF = dt.datetime(2026, 9, 22, 8, 0, tzinfo=CST)
CODES = ("002617", "600875", "300033")


def row(**changes):
    value = {"title": "关于召开2026年半年度业绩说明会的公告", "date": "2026-09-19",
             "dateKind": "source_disclosure_date", "observedAt": "2026-09-22T07:49:00+08:00",
             "nextDayDisclosure": False, "url": "https://static.cninfo.com.cn/finalpage/2026-09-19/1.PDF"}
    value.update(changes)
    return value


def document(**changes):
    value = {"status": "available", "kind": "financial_summary", "code": "002617",
             "title": "2026年半年度报告摘要",
             "url": "https://static.cninfo.com.cn/finalpage/2026-08-26/0.PDF",
             "disclosureDate": "2026-08-26", "reportDate": "2026-06-30",
             "observedAt": "2026-09-22T07:55:30+08:00", "dataAsOf": "2026-09-22T07:55:30+08:00",
             "pdfSha256": "b" * 64,
             "metrics": [{"field": "operatingCashFlow", "label": "经营活动产生的现金流量净额",
                         "currentToken": "1,000.00", "previousToken": "900.00",
                         "quote": "经营活动产生的现金流量净额 1,000.00 900.00",
                         "unitLabel": "元", "currency": "CNY", "pageNumber": 2,
                         "pageTextSha256": "c" * 64}]}
    value.update(changes)
    return value


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "staging"

    def entries(self, overrides=None):
        values = {code: {"queriedAt": QUERIED.isoformat(), "rows": [row()]} for code in CODES}
        values.update(overrides or {})
        return values

    def write(self, entries=None, documents=None, batch=BATCH, target=TARGET):
        return write_staging(self.directory, target, batch,
                             self.entries() if entries is None else entries,
                             documents if documents is not None else {}, "a" * 64)

    def load(self, target=TARGET, cutoff=CUTOFF, expected=CODES):
        return load_staging(self.directory, target, cutoff,
                            expected_codes=expected)

    def test_round_trip_keeps_per_stock_and_batch_times_distinct(self):
        path = self.write()
        self.assertTrue(path.is_file())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        loaded = self.load()
        self.assertEqual(loaded["targetDate"], TARGET)
        self.assertEqual(loaded["batchCompletedAt"], BATCH)
        self.assertEqual(loaded["codes"]["002617"]["queriedAt"], QUERIED)
        self.assertEqual(loaded["codes"]["002617"]["rows"][0]["title"], row()["title"])
        self.assertEqual(loaded["documents"], {})

    def test_late_or_other_day_observations_fall_back(self):
        self.assertIsNone(self.load())
        self.write()
        self.assertIsNone(self.load(target="2026-09-23"))
        # Batch completion after the frozen cutoff is never used.
        self.assertIsNone(self.load(cutoff=dt.datetime(2026, 9, 22, 7, 55, 30, tzinfo=CST)))
        # Per-stock query after the cutoff is never used.
        self.write(entries=self.entries({"600875": {"queriedAt": "2026-09-22T08:00:30+08:00", "rows": []}}))
        self.assertIsNone(self.load())
        # A previous day's query cannot be presented as today's coverage.
        self.write(entries=self.entries({"300033": {"queriedAt": "2026-09-21T07:50:00+08:00", "rows": []}}))
        self.assertIsNone(self.load())
        self.write(batch=dt.datetime(2026, 9, 21, 7, 56, tzinfo=CST))
        self.assertIsNone(self.load())
        # Rows observed on another day are rejected too.
        self.write(entries=self.entries({"002617": {"queriedAt": QUERIED.isoformat(),
                                                    "rows": [row(observedAt="2026-09-21T07:49:00+08:00")]}}))
        self.assertIsNone(self.load())
        # Batch cannot precede one of its own per-stock queries.
        self.write(batch=dt.datetime(2026, 9, 22, 7, 45, tzinfo=CST))
        self.assertIsNone(self.load())

    def test_watchlist_completeness_is_required(self):
        self.write()
        self.assertIsNotNone(self.load())
        self.write(entries={code: {"queriedAt": QUERIED.isoformat(), "rows": []}
                            for code in CODES[:2]})
        self.assertIsNone(self.load())
        self.write(entries={**{code: {"queriedAt": QUERIED.isoformat(), "rows": []} for code in CODES},
                            "601398": {"queriedAt": QUERIED.isoformat(), "rows": []}})
        self.assertIsNone(self.load())
        self.write()
        self.assertIsNone(self.load(expected=("002617",)))

    def test_malformed_or_tampered_files_fall_back(self):
        path = self.write()
        original = path.read_text()
        for mutate in (
            lambda body: body.replace('"targetDate":"2026-09-22"', '"targetDate":"2026-09-21"'),
            lambda body: body.replace('"observedAt":"2026-09-22T07:49:00+08:00"',
                                      '"observedAt":"2026-09-22T07:59:00+08:00"'),
            lambda body: body.replace('"nextDayDisclosure":false', '"nextDayDisclosure":"no"'),
            lambda body: body.replace('"date":"2026-09-19"', '"date":"2026-09-23"'),
            lambda body: body.replace('"dateKind":"source_disclosure_date"', '"dateKind":"observed"'),
            lambda body: body.replace('"queriedAt":"2026-09-22T07:50:00+08:00"',
                                      '"queriedAt":"2026-09-22T07:50:00"'),
        ):
            with self.subTest(mutate=mutate.__code__.co_firstlineno):
                body = mutate(original)
                self.assertNotEqual(body, original)
                path.write_text(body)
                self.assertIsNone(self.load())
        path.write_text(original)
        self.assertIsNotNone(self.load())

    def test_non_json_nonfinite_symlink_and_oversize_fall_back(self):
        path = staging_path(self.directory, TARGET)
        self.directory.mkdir(parents=True, exist_ok=True)
        for body in (b"not json", b'{"schemaVersion":2,"x":NaN}', b"[]"):
            path.write_bytes(body)
            self.assertIsNone(self.load())
        path.write_bytes(b"x" * (MAX_BYTES + 1))
        self.assertIsNone(self.load())
        path.unlink()
        decoy = Path(self.temp.name) / "decoy.json"
        decoy.write_text("{}")
        path.symlink_to(decoy)
        self.assertIsNone(self.load())
        path.unlink()
        self.assertIsNone(self.load())

    def test_documents_are_bounded_by_the_batch_and_validated(self):
        self.write(documents={"002617": document()})
        loaded = self.load()
        self.assertEqual(loaded["documents"]["002617"]["pdfSha256"], "b" * 64)
        # PDF evidence is fetched after that stock's query but before the batch.
        self.assertEqual(loaded["documents"]["002617"]["observedAt"], "2026-09-22T07:55:30+08:00")
        for bad in (document(pdfSha256="short"), document(status="unavailable"),
                    document(kind="news"), document(code="600875"),
                    document(url="https://example.com/0.PDF"),
                    document(observedAt="2026-09-22T08:30:00+08:00"),
                    document(observedAt="2026-09-21T07:55:30+08:00"),
                    document(metrics=[]), document(dataAsOf="2026-09-22T07:50:00+08:00")):
            with self.subTest(bad=bad.get("status") or bad.get("observedAt") or bad.get("pdfSha256")):
                self.write(documents={"002617": bad})
                self.assertIsNone(self.load())
        self.write(documents={"601398": document(code="601398")})
        self.assertIsNone(self.load())

    def test_writer_never_follows_a_symlink_at_the_target_path(self):
        decoy = Path(self.temp.name) / "decoy.json"
        decoy.write_text("keep")
        self.directory.mkdir(parents=True, exist_ok=True)
        staging_path(self.directory, TARGET).symlink_to(decoy)
        with self.assertRaises(StagingError):
            self.write()
        self.assertEqual(decoy.read_text(), "keep")

    def test_invalid_inputs_are_rejected_at_write_time(self):
        with self.assertRaises(StagingError):
            write_staging(self.directory, "2026/09/22", BATCH, {}, {}, "a" * 64)
        with self.assertRaises(StagingError):
            write_staging(self.directory, TARGET, dt.datetime(2026, 9, 22, 7, 56), {}, {}, "a" * 64)
        with self.assertRaises((StagingError, KeyError)):
            write_staging(self.directory, TARGET, BATCH, {"002617": {"rows": []}}, {}, "a" * 64)
        # The writer stores what the collector produced; the reader is the gate.
        self.write(entries=self.entries({"002617": {"queriedAt": QUERIED.isoformat(),
                                                    "rows": [row(nextDayDisclosure="no")]}}),
                   documents={}, batch=BATCH)
        self.assertIsNone(self.load())


if __name__ == "__main__":
    unittest.main()
