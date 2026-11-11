"""SZ301139 (2026-08-31..09-29 exchange suspension) long-tail-gap regression.

Synthetic trees and in-memory rows only; no network, no production data writes.
The generic ten-day gate must stay; only the exact evidence-bound record passes.
"""

import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

import run
import test_tree
from test_tree import COMMIT, rows_for, write_symbol, hashes

tree = test_tree.tree
SYM = "SZ301139"
HEALTHY = "SZ000001"
FEATURE_DAYS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
GAP = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08",
       "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
       "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28", "2026-09-29"]
NEW = ["2026-09-30"]
# Exact rows returned by the pinned source d5ouv06svuoat4193aiefibgu2fa3ose.
ROW_0828 = {"tradedate": "2026-08-28", "symbol": SYM, "open": "3.53", "high": "3.56", "low": "3.31",
            "close": "3.31", "adjclose": "3.34", "volume": "106858", "amount": "36224.43"}
ROW_0930 = {"tradedate": "2026-09-30", "symbol": SYM, "open": "0.8", "high": "1.27", "low": "0.75",
            "close": "0.91", "adjclose": "0.92", "volume": "329007.61", "amount": "28660.5614"}
REF = "d5ouv06svuoat4193aiefibgu2fa3ose"


class InspectGate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qlib-tail-gap-")
        self.root = Path(self.temporary.name) / "old"
        (self.root / "instruments").mkdir(parents=True)
        self.calendar = FEATURE_DAYS + GAP
        write_symbol(self.root, SYM, FEATURE_DAYS, rows_for(SYM, FEATURE_DAYS), self.calendar)
        self.before = hashes(self.root)

    def tearDown(self):
        self.assertEqual(hashes(self.root), self.before)
        self.temporary.cleanup()

    def test_21_day_gap_still_blocked_without_exact_allowance(self):
        for allowance in (None, {}, {SYM: tuple(GAP[:-1]) + ("2026-09-30",)}, {SYM: tuple(GAP[1:]) + ("2026-09-30",)},
                          {"SZ301266": tuple(GAP)}):
            with self.subTest(allowance=allowance), \
                    self.assertRaisesRegex(tree.TreeValidationError, "historical_tail_gap_exceeds_10_days:SZ301139"):
                tree.inspect_symbols(self.root, [SYM], self.calendar, tail_gap_allowances=allowance)

    def test_exact_allowance_reports_full_gap_without_shortening_it(self):
        info = tree.inspect_symbols(self.root, [SYM], self.calendar, tail_gap_allowances={SYM: tuple(GAP)})[SYM]
        self.assertEqual(info["gap_dates"], GAP)
        self.assertEqual(info["last_date"], "2026-08-28")

    def test_allowance_cannot_be_used_for_short_or_malformed_gaps(self):
        for allowance, reason in (({SYM: tuple(GAP[:10])}, "allowance_not_needed"),
                                  ({"sz301139": tuple(GAP)}, "allowance_symbol"),
                                  ([SYM], "invalid_tail_gap_allowances")):
            with self.subTest(reason=reason), self.assertRaisesRegex(tree.TreeValidationError, reason):
                tree.inspect_symbols(self.root, [SYM], self.calendar, tail_gap_allowances=allowance)

    def test_generic_bound_unchanged_for_eleven_days(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "exceeds_10_days"):
            tree.inspect_symbols(self.root, [SYM], FEATURE_DAYS + GAP[:11])
        self.assertEqual(tree.inspect_symbols(self.root, [SYM], FEATURE_DAYS + GAP[:10])[SYM]["gap_dates"], GAP[:10])


class ApplyLongGap(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qlib-tail-gap-apply-")
        parent = Path(self.temporary.name)
        self.old, self.stage = parent / "old", parent / "stage"
        (self.old / "instruments").mkdir(parents=True)
        self.calendar = FEATURE_DAYS + GAP
        self.raw = rows_for(SYM, FEATURE_DAYS + NEW)
        write_symbol(self.old, SYM, FEATURE_DAYS, self.raw[:5], self.calendar)
        healthy = rows_for(HEALTHY, self.calendar + NEW)
        write_symbol(self.old, HEALTHY, self.calendar, healthy[:len(self.calendar)], self.calendar)
        self.raw += healthy
        shutil.copytree(self.old, self.stage)
        self.before = hashes(self.old)

    def tearDown(self):
        self.assertEqual(hashes(self.old), self.before)
        self.temporary.cleanup()

    def apply(self, **changes):
        args = dict(source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=self.raw, first_dates={},
                    min_eligible=1, min_coverage=0.5, required_symbols=(HEALTHY,),
                    confirmed_missing_history={SYM: tuple(GAP)}, confirmed_missing_history_source_commit=COMMIT,
                    tail_gap_allowances={SYM: tuple(GAP)})
        args.update(changes)
        return tree.apply_prices(self.old, self.stage, self.calendar, self.calendar + NEW, **args)

    def test_without_allowance_apply_is_blocked_as_before(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "exceeds_10_days:SZ301139"):
            self.apply(tail_gap_allowances=None)

    def test_allowed_gap_is_nan_with_carried_factor_and_history_prefix_kept(self):
        result = self.apply()
        self.assertEqual(result["historical_tail_gaps"], {SYM: GAP})
        self.assertEqual(result["source_absent_history"], {SYM: GAP})
        for field in tree.FIELDS:
            old = (self.old / "features" / SYM.lower() / f"{field}.day.bin").read_bytes()
            staged = (self.stage / "features" / SYM.lower() / f"{field}.day.bin").read_bytes()
            self.assertEqual(staged[:len(old)], old, field)
            appended = struct.unpack("<" + "f" * (len(GAP) + 1), staged[len(old):])
            last_old = struct.unpack("<f", old[-4:])[0]
            for value in appended[:-1]:
                if field == "factor":
                    self.assertEqual(value, last_old)
                else:
                    self.assertTrue(math.isnan(value), field)
            self.assertTrue(math.isfinite(appended[-1]), field)
        line = [l for l in (self.stage / "instruments/all.txt").read_text().splitlines() if l.startswith(SYM)]
        self.assertEqual(line, [f"{SYM}\t2026-08-24\t2026-09-30"])

    def test_partial_source_fill_inside_allowed_gap_rejected(self):
        row = rows_for(SYM, GAP[:1])[0]
        with self.assertRaisesRegex(tree.TreeValidationError, "conflicts_with_source_row"):
            self.apply(raw_rows=self.raw + [row])
        self.stage_reset()
        with self.assertRaisesRegex(tree.TreeValidationError, "tail_gap_allowance_requires_complete_absence"):
            self.apply(raw_rows=self.raw + [row], confirmed_missing_history={SYM: tuple(GAP[1:])})

    def test_incomplete_absence_proof_rejected(self):
        with self.assertRaises(ValueError):
            self.apply(confirmed_missing_history={SYM: tuple(GAP[:-1])})

    def stage_reset(self):
        shutil.rmtree(self.stage)
        shutil.copytree(self.old, self.stage)


class FetchGap(unittest.TestCase):
    def setUp(self):
        self.calendar = FEATURE_DAYS + GAP
        self.info = {SYM: {"last_date": "2026-08-28", "gap_dates": list(GAP)}}

    def call(self, allowances, returned=()):
        with patch.object(run, "fetch_prices", return_value=list(returned)):
            return run.fetch_historical_gaps(object(), self.info, self.calendar, {}, tail_gap_allowances=allowances)

    def test_long_gap_query_requires_exact_allowance(self):
        for allowance in (None, {SYM: tuple(GAP[:-1])}, {"SZ000001": tuple(GAP)}):
            with self.subTest(allowance=allowance), self.assertRaisesRegex(ValueError, "invalid_historical_gap_request"):
                self.call(allowance)

    def test_complete_empty_pinned_response_proves_every_gap_day(self):
        missing, proof = self.call({SYM: tuple(GAP)})
        self.assertEqual(missing, {SYM: tuple(GAP)})
        self.assertEqual(proof[SYM]["meaning"], "source_absent_not_confirmed_suspension")


class Record(unittest.TestCase):
    def setUp(self):
        self.old_calendar = ["2026-08-26", "2026-08-27"] + FEATURE_DAYS[-1:] + GAP
        self.raw_map = {(SYM, "2026-08-28"): dict(ROW_0828), (SYM, "2026-09-30"): dict(ROW_0930)}
        self.inspections = {SYM: {"last_date": "2026-08-28", "gap_dates": list(GAP)}}

    def test_real_record_is_hash_pinned_and_active_only_at_base_0929(self):
        exception = run.TailGapException(self.old_calendar)
        self.assertEqual(exception.allowances, {SYM: tuple(GAP)})
        self.assertEqual(run.TailGapException(self.old_calendar + NEW).allowances, {})
        self.assertEqual(run.TailGapException(self.old_calendar[:-1]).allowances, {})

    def test_confirm_binds_source_rows_and_complete_absence(self):
        exception = run.TailGapException(self.old_calendar)
        evidence = exception.confirm(self.inspections, {SYM: tuple(GAP)}, self.raw_map, NEW, REF)
        self.assertEqual(evidence["gapDates"], GAP)
        self.assertEqual(evidence["resumeDate"], "2026-09-30")
        self.assertEqual(evidence["recordSha256"], run.TailGapException.RECORD_SHA256)
        self.assertEqual(len(evidence["officialEvidenceSha256"]), 2)
        self.assertIsNone(exception.confirm({}, {}, self.raw_map, NEW, REF))

    def test_confirm_fails_closed(self):
        exception = run.TailGapException(self.old_calendar)
        cases = [
            ({SYM: tuple(GAP[:-1])}, self.raw_map, NEW, "source_rows_present"),
            ({SYM: tuple(GAP)}, {**self.raw_map, (SYM, GAP[3]): dict(ROW_0930, tradedate=GAP[3])}, NEW, "source_rows_present"),
            ({SYM: tuple(GAP)}, {(SYM, "2026-08-28"): ROW_0828}, NEW, "resume_row_missing"),
            ({SYM: tuple(GAP)}, self.raw_map, ["2026-10-08"], "resume_row_missing"),
            ({SYM: tuple(GAP)}, {**self.raw_map, (SYM, "2026-09-30"): dict(ROW_0930, close="0.92")}, NEW, "differs_from_record"),
            ({SYM: tuple(GAP)}, {**self.raw_map, (SYM, "2026-08-28"): dict(ROW_0828, adjclose="3.35")}, NEW, "differs_from_record"),
        ]
        for missing, raw_map, new_days, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                exception.confirm(self.inspections, missing, raw_map, new_days, REF)

    def test_tampered_record_or_evidence_rejected(self):
        with tempfile.TemporaryDirectory(prefix="qlib-tail-gap-record-") as directory:
            target = Path(directory) / "exceptions"
            shutil.copytree(run.TailGapException.RECORD.parent, target)
            record = target / run.TailGapException.RECORD.name
            text = record.read_text()
            record.write_text(text.replace('"2026-09-29"', '"2026-09-30"', 1))
            with self.assertRaisesRegex(ValueError, "record_changed"):
                run.TailGapException(self.old_calendar, record_path=record)
            record.write_text(text)
            pdf = target / json.loads(text)["officialEvidence"][0]["file"]
            pdf.write_bytes(pdf.read_bytes() + b"x")
            with self.assertRaisesRegex(ValueError, "evidence_changed"):
                run.TailGapException(self.old_calendar, record_path=record)


class Record002731(unittest.TestCase):
    """SZ002731 (2026-09-01..10-20 delisting suspension) record, same contract."""

    SYM2 = "SZ002731"
    GAP2 = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08",
            "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16",
            "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
            "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12",
            "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
    ROW_0831 = {"tradedate": "2026-08-31", "symbol": "SZ002731", "open": "0.77", "high": "0.77",
                "low": "0.77", "close": "0.77", "adjclose": "1.35", "volume": "118410", "amount": "9117.57"}
    ROW_1023 = {"tradedate": "2026-10-23", "symbol": "SZ002731", "open": "0.34", "high": "0.38",
                "low": "0.3", "close": "0.31", "adjclose": "0.54", "volume": "492748.41", "amount": "15987.04139"}
    NEW2 = ["2026-10-21", "2026-10-22", "2026-10-23"]

    def setUp(self):
        self.old_calendar = ["2026-08-28", "2026-08-31"] + self.GAP2
        self.raw_map = {(self.SYM2, "2026-08-31"): dict(self.ROW_0831),
                        (self.SYM2, "2026-10-23"): dict(self.ROW_1023)}
        self.inspections = {self.SYM2: {"last_date": "2026-08-31", "gap_dates": list(self.GAP2)}}

    def test_record_is_hash_pinned_and_active_only_at_base_1020(self):
        exception = run.TailGapException(self.old_calendar)
        self.assertEqual(exception.allowances, {self.SYM2: tuple(self.GAP2)})
        self.assertEqual(run.TailGapException(self.old_calendar + self.NEW2).allowances, {})
        self.assertEqual(run.TailGapException(self.old_calendar[:-1]).allowances, {})

    def test_both_records_registered_and_independently_pinned(self):
        exception = run.TailGapException(self.old_calendar)
        self.assertEqual(sorted(exception.records), [self.SYM2, SYM])
        self.assertEqual(len(run.TailGapException.RECORD_REGISTRY), 2)
        for path, sha in run.TailGapException.RECORD_REGISTRY:
            import hashlib
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)

    def test_confirm_binds_source_rows_and_complete_absence(self):
        exception = run.TailGapException(self.old_calendar)
        evidence = exception.confirm(self.inspections, {self.SYM2: tuple(self.GAP2)},
                                     self.raw_map, self.NEW2, "an0isj66dqbjsdnq3j9u144h2jg1tdi8")
        self.assertEqual(evidence["policyId"], "qlib-tail-gap-SZ002731-20260901-v1")
        self.assertEqual(evidence["symbol"], self.SYM2)
        self.assertEqual(evidence["gapDates"], self.GAP2)
        self.assertEqual(evidence["resumeDate"], "2026-10-23")
        self.assertEqual(evidence["recordSha256"], run.TailGapException.RECORD_REGISTRY[1][1])
        self.assertEqual(len(evidence["officialEvidenceSha256"]), 2)

    def test_confirm_fails_closed(self):
        exception = run.TailGapException(self.old_calendar)
        cases = [
            ({self.SYM2: tuple(self.GAP2[:-1])}, self.raw_map, self.NEW2, "source_rows_present"),
            ({self.SYM2: tuple(self.GAP2)},
             {**self.raw_map, (self.SYM2, self.GAP2[3]): dict(self.ROW_1023, tradedate=self.GAP2[3])},
             self.NEW2, "source_rows_present"),
            ({self.SYM2: tuple(self.GAP2)}, {(self.SYM2, "2026-08-31"): self.ROW_0831},
             self.NEW2, "resume_row_missing"),
            ({self.SYM2: tuple(self.GAP2)}, self.raw_map, ["2026-10-27"], "resume_row_missing"),
            ({self.SYM2: tuple(self.GAP2)},
             {**self.raw_map, (self.SYM2, "2026-10-23"): dict(self.ROW_1023, close="0.32")},
             self.NEW2, "differs_from_record"),
        ]
        for missing, raw_map, new_days, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                exception.confirm(self.inspections, missing, raw_map, new_days,
                                  "an0isj66dqbjsdnq3j9u144h2jg1tdi8")


if __name__ == "__main__":
    unittest.main()
