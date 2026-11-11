"""Tiny synthetic trees only; no real provider, market download or production IO."""

import hashlib
import importlib.util
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("qlib_incremental_tree_test_module", Path(__file__).with_name("tree.py"))
tree = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tree
spec.loader.exec_module(tree)
COMMIT = "80q7jtqjrga15h61uufm60h6uqirk6iq"
SYM = "SH600000"
HISTORICAL = "SH000985"
OLD_DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
NEW_DAYS = ["2026-09-07", "2026-09-08"]


def rows_for(symbol, days, initial=100):
    return [dict(symbol=symbol, source_commit=COMMIT, date=day,
                 open=initial + i - 1, high=initial + i + 2, low=initial + i - 2,
                 close=initial + i, adjclose=(initial + i) / 2,
                 volume=1000, amount=10000) for i, day in enumerate(days)]


def normalized(rows):
    scale = 1 / rows[0]["adjclose"]
    result = []
    for index, row in enumerate(rows):
        factor = row["adjclose"] / row["close"] * scale
        result.append(dict(adjclose=row["adjclose"], amount=row["amount"],
            change=row["close"] / rows[index - 1]["close"] - 1 if index else float("nan"),
            factor=factor, volume=row["volume"] / factor,
            vwap=row["amount"] / row["volume"] * 10 * factor,
            **{field: row[field] * factor for field in ("open", "high", "low", "close")}))
    return result


def write_symbol(root, symbol, days, rows, calendar):
    directory = root / "features" / symbol.lower()
    directory.mkdir(parents=True)
    values = normalized(rows)
    for field in tree.FIELDS:
        blob = struct.pack("<f", calendar.index(days[0])) + b"".join(struct.pack("<f", row[field]) for row in values)
        (directory / f"{field}.day.bin").write_bytes(blob)
    with (root / "instruments" / "all.txt").open("a", encoding="ascii") as handle:
        handle.write(f"{symbol}\t{days[0]}\t{days[-1]}\n")


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qlib-conversion-tree-test-")
        self.parent = Path(self.temporary.name)
        self.old = self.parent / "old"
        self.old.mkdir()
        (self.old / "instruments").mkdir()
        (self.old / "calendars").mkdir()
        (self.old / "calendars" / "day.txt").write_text("\n".join(OLD_DAYS) + "\n")
        self.raw = rows_for(SYM, OLD_DAYS + NEW_DAYS)
        write_symbol(self.old, SYM, OLD_DAYS, self.raw[:4], OLD_DAYS)
        write_symbol(self.old, HISTORICAL, OLD_DAYS[:1], rows_for(HISTORICAL, OLD_DAYS[:1]), OLD_DAYS)
        self.stage = self.parent / "stage"
        shutil.copytree(self.old, self.stage)
        self.before = hashes(self.old)

    def tearDown(self):
        self.assertEqual(hashes(self.old), self.before, "the original data tree was modified")
        self.temporary.cleanup()

    def args(self, **changes):
        result = dict(source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=self.raw,
                      first_dates={}, min_eligible=1, min_coverage=0.999, required_symbols=(SYM,))
        result.update(changes)
        return result

    def apply(self, **changes):
        return tree.apply_prices(self.old, self.stage, OLD_DAYS, OLD_DAYS + NEW_DAYS, **self.args(**changes))

    def test_inspect_mature_and_new_does_not_touch_historical_short_end(self):
        info = tree.inspect_symbols(self.old, [SYM, "SZ001999"], OLD_DAYS)
        self.assertEqual(info[SYM]["last3_valid_dates"], OLD_DAYS[-3:])
        self.assertEqual(info[SYM]["previous_date"], OLD_DAYS[1])
        self.assertEqual([row["date"] for row in info[SYM]["old_anchors"]], OLD_DAYS[-2:])
        self.assertFalse(info[SYM]["short_history"])
        self.assertTrue(info["SZ001999"]["new"])

    def test_successful_append_preserves_old_prefixes_historical_files_and_intervals(self):
        result = self.apply()
        self.assertEqual(result["valid"], 1)
        self.assertEqual(result["coverage"], 1)
        self.assertEqual(result["old_prefix_files_preserved"], 10)
        self.assertEqual(result["untouched_symbols"], 1)
        for field in tree.FIELDS:
            old = (self.old / "features" / SYM.lower() / f"{field}.day.bin").read_bytes()
            new = (self.stage / "features" / SYM.lower() / f"{field}.day.bin").read_bytes()
            self.assertEqual(new[:len(old)], old)
            self.assertEqual(len(new), len(old) + 8)
            relative = Path("features") / HISTORICAL.lower() / f"{field}.day.bin"
            self.assertEqual((self.old / relative).read_bytes(), (self.stage / relative).read_bytes())
        self.assertIn(f"{HISTORICAL}\t2026-09-01\t2026-09-01\n", (self.stage / "instruments/all.txt").read_text())
        self.assertIn(f"{SYM}\t2026-09-01\t2026-09-08\n", (self.stage / "instruments/all.txt").read_text())
        # Calendar is intentionally owned by the integration transaction, not this function.
        self.assertEqual((self.stage / "calendars/day.txt").read_bytes(), (self.old / "calendars/day.txt").read_bytes())

    def test_stage_hardlinks_are_replaced_without_mutating_original_inodes(self):
        relative = Path("features") / SYM.lower() / "close.day.bin"
        (self.stage / relative).unlink()
        os.link(self.old / relative, self.stage / relative)
        self.assertTrue(os.path.samefile(self.old / relative, self.stage / relative))
        self.apply()
        self.assertFalse(os.path.samefile(self.old / relative, self.stage / relative))

    def test_same_nested_and_symlink_stage_rejected(self):
        nested = self.old / "nested"
        nested.mkdir()
        link = self.parent / "stage-link"
        link.symlink_to(self.stage, target_is_directory=True)
        for stage in (self.old, nested, self.parent, link):
            with self.subTest(stage=stage), self.assertRaises(tree.TreeValidationError):
                tree.apply_prices(self.old, stage, OLD_DAYS, OLD_DAYS + NEW_DAYS, **self.args())
        nested.rmdir()

    def test_symlinked_stage_feature_cannot_write_original(self):
        relative = Path("features") / SYM.lower() / "close.day.bin"
        (self.stage / relative).unlink()
        (self.stage / relative).symlink_to(self.old / relative)
        with self.assertRaisesRegex(tree.TreeValidationError, "symlink"):
            self.apply()

    def test_changed_staged_feature_or_all_file_rejected_before_any_write(self):
        target = self.stage / "features" / SYM.lower() / "amount.day.bin"
        target.write_bytes(target.read_bytes()[:-4] + struct.pack("<f", 2))
        snapshot = hashes(self.stage)
        with self.assertRaisesRegex(tree.TreeValidationError, "not_exact_copy"):
            self.apply()
        self.assertEqual(hashes(self.stage), snapshot)

    def test_unknown_stage_field_and_misaligned_old_fixture_rejected(self):
        (self.stage / "features" / SYM.lower() / "mystery.day.bin").write_bytes(b"data")
        with self.assertRaisesRegex(tree.TreeValidationError, "unknown_or_missing"):
            self.apply()
        # Separate isolated malformed tree so the actual old fixture is unchanged.
        malformed = self.parent / "malformed"
        shutil.copytree(self.old, malformed)
        path = malformed / "features" / SYM.lower() / "amount.day.bin"
        path.write_bytes(path.read_bytes()[:-4])
        with self.assertRaisesRegex(tree.TreeValidationError, "misaligned"):
            tree.inspect_symbols(malformed, [SYM], OLD_DAYS)

    def test_old_tail_gap_reported_but_large_historical_gap_rejected(self):
        info = tree.inspect_symbols(self.old, [HISTORICAL], OLD_DAYS)[HISTORICAL]
        self.assertEqual(info["gap_dates"], OLD_DAYS[1:])
        long_calendar = [f"2026-09-{day:02d}" for day in range(1, 16)]
        with self.assertRaisesRegex(tree.TreeValidationError, "historical_tail_gap_exceeds"):
            tree.inspect_symbols(self.old, [HISTORICAL], long_calendar)

    def test_missing_interior_new_day_or_overlap_and_mixed_commit_rejected(self):
        cases = [(self.raw[:4] + self.raw[5:], "missing_or_extra_source_dates"),
                 (self.raw[:1] + self.raw[2:], "missing_raw_overlap")]
        for rows, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.apply(raw_rows=rows)
        rows = [dict(row) for row in self.raw]
        rows[-1]["source_commit"] = "0" * 32
        with self.assertRaisesRegex(tree.TreeValidationError, "commit_missing_or_mixed"):
            self.apply(raw_rows=rows)

    def test_duplicate_rows_and_rebased_old_history_fail_before_stage_write(self):
        snapshot = hashes(self.stage)
        with self.assertRaisesRegex(tree.TreeValidationError, "duplicate_raw"):
            self.apply(raw_rows=self.raw + self.raw[-1:])
        rows = [dict(row, adjclose=row["adjclose"] * 2) for row in self.raw]
        with self.assertRaisesRegex(ValueError, "overlap_mismatch"):
            self.apply(raw_rows=rows)
        self.assertEqual(hashes(self.stage), snapshot)

    def test_new_symbol_true_first_day_encoded_and_excluded_from_first_day_coverage(self):
        new_symbol = "SZ001999"
        result = self.apply(raw_rows=self.raw + rows_for(new_symbol, NEW_DAYS[-1:]),
                            first_dates={new_symbol: NEW_DAYS[-1]})
        self.assertEqual(result["new_symbols"], 1)
        self.assertEqual(result["eligible"], 1)
        blob = (self.stage / "features" / new_symbol.lower() / "close.day.bin").read_bytes()
        self.assertEqual(struct.unpack("<ff", blob), (5.0, 1.0))
        self.assertIn(f"{new_symbol}\t2026-09-08\t2026-09-08\n", (self.stage / "instruments/all.txt").read_text())

    def test_new_symbol_cannot_hide_unimported_older_history(self):
        rows = self.raw + rows_for("SZ001999", NEW_DAYS)
        for first_dates in ({}, {"SZ001999": OLD_DAYS[0]}):
            with self.assertRaises(ValueError):
                self.apply(raw_rows=rows, first_dates=first_dates)

    def test_new_symbol_missing_final_day_cannot_escape_coverage_denominator(self):
        new_symbol = "SZ001999"
        with self.assertRaisesRegex(tree.TreeValidationError, "source_missing_target_day"):
            self.apply(raw_rows=self.raw + rows_for(new_symbol, NEW_DAYS[:1]),
                       first_dates={new_symbol: NEW_DAYS[0]})

    def test_second_active_symbol_missing_final_day_cannot_escape_coverage_denominator(self):
        symbol = "SZ000001"
        other_old, other_stage = self.parent / "second-old", self.parent / "second-stage"
        shutil.copytree(self.old, other_old)
        rows = rows_for(symbol, OLD_DAYS + NEW_DAYS)
        write_symbol(other_old, symbol, OLD_DAYS, rows[:4], OLD_DAYS)
        shutil.copytree(other_old, other_stage)
        before = hashes(other_old)
        with self.assertRaisesRegex(tree.TreeValidationError, "source_missing_target_day"):
            tree.apply_prices(other_old, other_stage, OLD_DAYS, OLD_DAYS + NEW_DAYS,
                **self.args(raw_rows=self.raw + rows[:-1]))
        self.assertEqual(hashes(other_old), before)

    def test_old_active_symbol_missing_new_interval_cannot_shrink_denominator(self):
        symbol = "SZ000001"
        write_symbol(self.stage, symbol, OLD_DAYS, rows_for(symbol, OLD_DAYS), OLD_DAYS)
        # Use an independent original copy with a second active symbol.
        alternate_old = self.parent / "alternate-old"
        shutil.copytree(self.stage, alternate_old)
        with self.assertRaisesRegex(tree.TreeValidationError, "active_symbols_missing_new_source"):
            tree.apply_prices(alternate_old, self.stage, OLD_DAYS, OLD_DAYS + NEW_DAYS, **self.args())

    def test_coverage_pause_is_not_treated_as_success_and_old_tree_unchanged(self):
        rows = [dict(row) for row in self.raw]
        rows[-2]["volume"] = 0
        with self.assertRaisesRegex(tree.TreeValidationError, "invalid_current_field"):
            self.apply(raw_rows=rows)

    def test_readonly_validation_requires_actual_features_not_just_calendar(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "not_at_target"):
            tree.validate_tree(self.old, OLD_DAYS + NEW_DAYS, NEW_DAYS[-1], 1, required_symbols=(SYM,))
        with self.assertRaisesRegex(tree.TreeValidationError, "target_is_not"):
            tree.validate_tree(self.old, OLD_DAYS, NEW_DAYS[-1], 1, required_symbols=(SYM,))
        with self.assertRaisesRegex(tree.TreeValidationError, "reference_or_index_missing"):
            tree.validate_tree(self.old, OLD_DAYS, OLD_DAYS[-1], 1)

    def test_default_5000_threshold_not_silently_relaxed(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "coverage_incomplete"):
            tree.validate_tree(self.old, OLD_DAYS, OLD_DAYS[-1], required_symbols=(SYM,))

    def test_current_noncore_nan_or_infinity_cannot_hide_in_coverage_allowance(self):
        for field in ("volume", "factor", "vwap", "open", "change", "adjclose"):
            for value in (float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    path = self.stage / "features" / SYM.lower() / f"{field}.day.bin"
                    original = path.read_bytes()
                    path.write_bytes(original[:-4] + struct.pack("<f", value))
                    try:
                        with self.assertRaisesRegex(tree.TreeValidationError, "invalid_current_field"):
                            # Even a deliberately loose fixture coverage threshold
                            # must not classify corrupt non-core fields as a pause.
                            tree.validate_tree(self.stage, OLD_DAYS, OLD_DAYS[-1], 1, 0.1, required_symbols=(SYM,))
                    finally:
                        path.write_bytes(original)

    def test_current_nonpositive_scale_price_and_negative_cash_fields_rejected(self):
        cases = [("volume", 0, "nonpositive"), ("factor", -1, "nonpositive"),
                 ("adjclose", 0, "nonpositive"), ("open", 0, "nonpositive"),
                 ("vwap", -1, "negative"), ("amount", -1, "negative"),
                 ("high", 0.1, "inconsistent_current_ohlc")]
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                path = self.stage / "features" / SYM.lower() / f"{field}.day.bin"
                original = path.read_bytes()
                path.write_bytes(original[:-4] + struct.pack("<f", value))
                try:
                    with self.assertRaisesRegex(tree.TreeValidationError, reason):
                        tree.validate_tree(self.stage, OLD_DAYS, OLD_DAYS[-1], 1, required_symbols=(SYM,))
                finally:
                    path.write_bytes(original)

    def test_first_listing_change_nan_allowed_but_other_fields_and_inf_rejected(self):
        new_symbol = "SZ001999"
        result = self.apply(raw_rows=self.raw + rows_for(new_symbol, NEW_DAYS[-1:]),
                            first_dates={new_symbol: NEW_DAYS[-1]})
        self.assertEqual(result["first_day_listings_checked"], 1)
        for field, value in (("change", float("inf")), ("volume", float("nan"))):
            path = self.stage / "features" / new_symbol.lower() / f"{field}.day.bin"
            original = path.read_bytes()
            path.write_bytes(original[:-4] + struct.pack("<f", value))
            try:
                with self.assertRaisesRegex(tree.TreeValidationError, "invalid_current_field"):
                    tree.validate_tree(self.stage, OLD_DAYS + NEW_DAYS, NEW_DAYS[-1], 1, required_symbols=(SYM,))
            finally:
                path.write_bytes(original)


class ShortTreeTests(unittest.TestCase):
    def test_one_two_three_day_existing_ipo_uses_full_history_not_fake_anchor(self):
        for length in (1, 2, 3):
            with self.subTest(length=length), tempfile.TemporaryDirectory(prefix="qlib-short-tree-") as temp:
                parent = Path(temp)
                old, stage = parent / "old", parent / "stage"
                (old / "instruments").mkdir(parents=True)
                days = OLD_DAYS[-length:]
                rows = rows_for(SYM, days + NEW_DAYS)
                write_symbol(old, SYM, days, rows[:length], OLD_DAYS)
                shutil.copytree(old, stage)
                before = hashes(old)
                info = tree.inspect_symbols(old, [SYM], OLD_DAYS)[SYM]
                self.assertTrue(info["short_history"])
                self.assertEqual(len(info["old_rows"]), length)
                result = tree.apply_prices(old, stage, OLD_DAYS, OLD_DAYS + NEW_DAYS,
                    source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=rows,
                    first_dates={SYM: days[0]}, min_eligible=1, required_symbols=(SYM,))
                self.assertEqual(result["short_history_symbols"], 1)
                self.assertEqual(hashes(old), before)

    def _second_generation(self, *, resume):
        ipo = "SZ001999"
        with tempfile.TemporaryDirectory(prefix="qlib-short-cross-generation-") as temp:
            parent = Path(temp)
            old, first_stage, second_stage = parent / "old", parent / "first", parent / "second"
            (old / "instruments").mkdir(parents=True)
            all_days = OLD_DAYS + NEW_DAYS + ["2026-09-09"]
            normal = rows_for(SYM, all_days)
            ipo_rows = rows_for(ipo, [NEW_DAYS[0], "2026-09-09"], initial=20)
            write_symbol(old, SYM, OLD_DAYS, normal[:4], OLD_DAYS)
            shutil.copytree(old, first_stage)
            tree.apply_prices(old, first_stage, OLD_DAYS, OLD_DAYS + NEW_DAYS,
                source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=normal[:6] + ipo_rows[:1],
                first_dates={ipo: NEW_DAYS[0]}, min_eligible=1, min_coverage=0.5, required_symbols=(SYM,),
                confirmed_missing_new_days={ipo: NEW_DAYS[-1:]}, confirmed_missing_new_days_source_commit=COMMIT,
                source_absence={"sourceCommit": COMMIT, "dates": {ipo: NEW_DAYS[-1:]}})
            info = tree.inspect_symbols(first_stage, [ipo], OLD_DAYS + NEW_DAYS)[ipo]
            self.assertTrue(info["short_history"])
            first_hashes = hashes(first_stage)
            shutil.copytree(first_stage, second_stage)
            absent = NEW_DAYS[-1:] + ([] if resume else ["2026-09-09"])
            result = tree.apply_prices(first_stage, second_stage, OLD_DAYS + NEW_DAYS, all_days,
                source_commit=COMMIT, expected_source_commit=COMMIT,
                raw_rows=normal + (ipo_rows if resume else ipo_rows[:1]), first_dates={ipo: NEW_DAYS[0]},
                min_eligible=1, min_coverage=0.5, required_symbols=(SYM,),
                confirmed_missing_new_days={} if resume else {ipo: ["2026-09-09"]},
                confirmed_missing_new_days_source_commit=COMMIT,
                source_absence={"sourceCommit": COMMIT, "dates": {ipo: absent}})
            self.assertEqual(result["short_history_symbols"], 1)
            self.assertEqual(result["eligible"], 2)
            self.assertEqual(result["invalid_reasons"][ipo],
                ["prior_close_source_absent"] if resume else ["source_absent_today", "prior_close_source_absent"])
            self.assertEqual(hashes(first_stage), first_hashes)
            for field in tree.FIELDS:
                relative = Path("features") / ipo.lower() / f"{field}.day.bin"
                before, after = (first_stage / relative).read_bytes(), (second_stage / relative).read_bytes()
                self.assertEqual(after[:len(before)], before)
                if not resume:
                    value = struct.unpack("<f", after[-4:])[0]
                    if field == "factor":
                        self.assertEqual(value, struct.unpack("<f", before[-4:])[0])
                    else:
                        self.assertTrue(math.isnan(value))

    def test_new_ipo_with_stored_absence_resumes_in_next_generation(self):
        self._second_generation(resume=True)

    def test_new_ipo_with_stored_absence_stays_absent_in_next_generation(self):
        self._second_generation(resume=False)


class HistoricalGapTests(unittest.TestCase):
    FEATURE_DAYS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
    GAP_DAYS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qlib-proven-gap-")
        self.parent = Path(self.temporary.name)
        self.old, self.stage = self.parent / "old", self.parent / "stage"
        (self.old / "instruments").mkdir(parents=True)
        self.calendar = self.FEATURE_DAYS + self.GAP_DAYS
        self.raw = rows_for(SYM, self.FEATURE_DAYS + NEW_DAYS)
        write_symbol(self.old, SYM, self.FEATURE_DAYS, self.raw[:5], self.calendar)
        self.healthy_symbol = "SZ000001"
        self.healthy_rows = rows_for(self.healthy_symbol, self.calendar + NEW_DAYS)
        write_symbol(self.old, self.healthy_symbol, self.calendar, self.healthy_rows[:len(self.calendar)], self.calendar)
        self.raw += self.healthy_rows
        # Preserve a noncanonical stored NaN payload as an additional byte check.
        path = self.old / "features" / SYM.lower() / "change.day.bin"
        blob = path.read_bytes()
        path.write_bytes(blob[:4] + struct.pack("<I", 0x7FC01234) + blob[8:])
        shutil.copytree(self.old, self.stage)
        self.before = hashes(self.old)

    def tearDown(self):
        self.assertEqual(hashes(self.old), self.before)
        self.temporary.cleanup()

    def apply(self, **changes):
        args = dict(source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=self.raw,
            first_dates={}, min_eligible=1, min_coverage=0.5, required_symbols=(self.healthy_symbol,),
            confirmed_missing_history={SYM: tuple(self.GAP_DAYS)}, confirmed_missing_history_source_commit=COMMIT)
        args.update(changes)
        return tree.apply_prices(self.old, self.stage, self.calendar, self.calendar + NEW_DAYS, **args)

    def test_same_source_absence_proof_appends_nan_without_rewriting_history(self):
        info = tree.inspect_symbols(self.old, [SYM], self.calendar)[SYM]
        self.assertEqual(info["last_date"], "2026-08-28")
        self.assertEqual(info["gap_dates"], self.GAP_DAYS)
        result = self.apply()
        self.assertEqual(result["source_absent_history"], {SYM: self.GAP_DAYS})
        self.assertEqual(result["historical_tail_gaps"], {SYM: self.GAP_DAYS})
        self.assertEqual(result["coverage"], 1)
        for field in tree.FIELDS:
            old = (self.old / "features" / SYM.lower() / f"{field}.day.bin").read_bytes()
            staged = (self.stage / "features" / SYM.lower() / f"{field}.day.bin").read_bytes()
            self.assertEqual(staged[:len(old)], old)
            values = struct.unpack("<" + "f" * 7, staged[len(old):])
            for value in values[:5]:
                if field == "factor":
                    self.assertAlmostEqual(value, 0.01, places=7)
                else:
                    self.assertTrue(math.isnan(value), field)
        change = (self.stage / "features" / SYM.lower() / "change.day.bin").read_bytes()
        values = struct.unpack("<" + "f" * 12, change[4:])
        self.assertAlmostEqual(values[10], 105 / 104 - 1, places=7)
        self.assertAlmostEqual(values[11], 106 / 105 - 1, places=7)

    def test_real_historical_rows_need_no_missing_evidence_and_empty_proof_is_harmless(self):
        rows = rows_for(SYM, self.calendar + NEW_DAYS) + self.healthy_rows
        result = self.apply(raw_rows=rows, confirmed_missing_history={SYM: ()})
        self.assertEqual(result["source_absent_history"], {})
        close = (self.stage / "features" / SYM.lower() / "close.day.bin").read_bytes()
        self.assertTrue(all(math.isfinite(value[0]) for value in struct.iter_unpack("<f", close[4:])))

    def test_absent_gap_without_complete_proof_is_rejected(self):
        for proof in ({}, {SYM: tuple(self.GAP_DAYS[:-1])}):
            with self.subTest(proof=proof), self.assertRaisesRegex(ValueError, "missing_or_extra_source_dates"):
                self.apply(confirmed_missing_history=proof)

    def test_absence_proof_must_match_fixed_source_commit(self):
        for source in (None, "0" * 32):
            with self.subTest(source=source), self.assertRaisesRegex(tree.TreeValidationError, "evidence_source_mismatch"):
                self.apply(confirmed_missing_history_source_commit=source)

    def test_absence_proof_conflicting_with_real_row_is_rejected(self):
        row = rows_for(SYM, self.GAP_DAYS[:1])[0]
        with self.assertRaisesRegex(tree.TreeValidationError, "conflicts_with_source_row"):
            self.apply(raw_rows=self.raw + [row])

    def test_proof_cannot_cover_new_day_or_stored_old_day_or_other_symbol(self):
        for proof, reason in [({SYM: tuple(self.GAP_DAYS + NEW_DAYS[:1])}, "outside_old_tail_gap"),
                              ({SYM: (self.FEATURE_DAYS[-1],)}, "outside_old_tail_gap"),
                              ({"SZ000002": tuple(self.GAP_DAYS)}, "unknown_or_new_symbol")]:
            with self.subTest(reason=reason), self.assertRaisesRegex(tree.TreeValidationError, reason):
                self.apply(confirmed_missing_history=proof)

    def test_new_day_absence_remains_blocked_despite_valid_historical_gap_proof(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "source_missing_target_day"):
            self.apply(raw_rows=self.raw[:-1])

    def test_bad_interval_is_not_reinterpreted_as_historical_gap(self):
        alternate = self.parent / "wrong-interval"
        shutil.copytree(self.old, alternate)
        path = alternate / "instruments/all.txt"
        path.write_text(path.read_text().replace("2026-08-28", "2026-08-27"))
        with self.assertRaisesRegex(tree.TreeValidationError, "instrument_feature_interval_mismatch"):
            tree.inspect_symbols(alternate, [SYM], self.calendar)

    def test_historical_gap_keeps_old_anchor_revision_gate(self):
        rows = [dict(row, adjclose=row["adjclose"] * 2) for row in self.raw]
        with self.assertRaisesRegex(ValueError, "overlap_mismatch"):
            self.apply(raw_rows=rows)


class NewSourceAbsenceTests(unittest.TestCase):
    AFFECTED = "SZ002743"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qlib-new-source-absence-")
        self.parent = Path(self.temporary.name)
        self.old, self.stage = self.parent / "old", self.parent / "stage"
        (self.old / "instruments").mkdir(parents=True)
        self.normal_rows = rows_for(SYM, OLD_DAYS + NEW_DAYS)
        self.affected_rows = rows_for(self.AFFECTED, OLD_DAYS + NEW_DAYS)
        write_symbol(self.old, SYM, OLD_DAYS, self.normal_rows[:4], OLD_DAYS)
        write_symbol(self.old, self.AFFECTED, OLD_DAYS, self.affected_rows[:4], OLD_DAYS)
        shutil.copytree(self.old, self.stage)
        self.before = hashes(self.old)

    def tearDown(self):
        self.assertEqual(hashes(self.old), self.before)
        self.temporary.cleanup()

    def apply(self, missing, **changes):
        proof = {self.AFFECTED: list(missing)}
        args = dict(source_commit=COMMIT, expected_source_commit=COMMIT,
            raw_rows=self.normal_rows + [row for row in self.affected_rows if row["date"] not in missing],
            first_dates={}, min_eligible=1, min_coverage=0.5, required_symbols=(SYM,),
            confirmed_missing_new_days=proof, confirmed_missing_new_days_source_commit=COMMIT,
            source_absence={"sourceCommit": COMMIT, "dates": proof})
        args.update(changes)
        return tree.apply_prices(self.old, self.stage, OLD_DAYS, OLD_DAYS + NEW_DAYS, **args)

    def test_active_union_keeps_no_row_stock_and_all_days_missing_is_explicit_partial(self):
        self.assertEqual(tree.active_update_symbols(self.old, OLD_DAYS[-1], [SYM]), [SYM, self.AFFECTED])
        result = self.apply(NEW_DAYS)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["valid"], 1)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["lastQuotedDate"], {self.AFFECTED: OLD_DAYS[-1]})
        self.assertEqual(result["sourceAbsentDates"], {self.AFFECTED: NEW_DAYS})
        self.assertEqual(result["daily_coverage"][0]["invalid_reasons"], {self.AFFECTED: ["source_absent_today"]})
        self.assertEqual(result["daily_coverage"][1]["invalid_reasons"],
                         {self.AFFECTED: ["source_absent_today", "prior_close_source_absent"]})
        for day in result["daily_coverage"]:
            self.assertEqual(day["eligible"], 2)
            self.assertEqual(day["coverage"], 0.5)
        self.assertIn(f"{self.AFFECTED}\t2026-09-01\t2026-09-08\n", (self.stage / "instruments/all.txt").read_text())
        for field in tree.FIELDS:
            relative = Path("features") / self.AFFECTED.lower() / f"{field}.day.bin"
            old, staged = (self.old / relative).read_bytes(), (self.stage / relative).read_bytes()
            self.assertEqual(staged[:len(old)], old)
            first, second = struct.unpack("<ff", staged[len(old):])
            if field == "factor":
                self.assertEqual(first, struct.unpack("<f", old[-4:])[0])
                self.assertEqual(second, first)
            else:
                self.assertTrue(math.isnan(first) and math.isnan(second), field)

    def test_resumed_quote_is_not_claimed_as_available_one_day_return(self):
        result = self.apply(NEW_DAYS[:1])
        self.assertEqual(result["invalid_reasons"], {self.AFFECTED: ["prior_close_source_absent"]})
        self.assertEqual(result["lastQuotedDate"], {self.AFFECTED: NEW_DAYS[-1]})
        self.assertEqual(result["daily_coverage"][0]["invalid_reasons"], {self.AFFECTED: ["source_absent_today"]})
        close = (self.stage / "features" / self.AFFECTED.lower() / "close.day.bin").read_bytes()
        self.assertTrue(math.isfinite(struct.unpack("<f", close[-4:])[0]))

    def test_only_last_day_absent_does_not_shrink_inventory(self):
        result = self.apply(NEW_DAYS[-1:])
        self.assertEqual(result["daily_coverage"][0]["status"], "complete")
        self.assertEqual(result["daily_coverage"][1]["status"], "partial")
        self.assertEqual(result["lastQuotedDate"], {self.AFFECTED: NEW_DAYS[0]})

    def test_missing_proof_mixed_ref_or_real_row_conflict_blocks(self):
        cases = [({"confirmed_missing_new_days": {}}, "source_missing_target_day"),
                 ({"confirmed_missing_new_days_source_commit": "0" * 32}, "source_mismatch"),
                 ({"raw_rows": self.normal_rows + self.affected_rows}, "conflicts_with_source_row"),
                 ({"source_absence": {"sourceCommit": COMMIT, "dates": {}}}, "missing_conversion_proof")]
        for changes, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.apply(NEW_DAYS[-1:], **changes)

    def test_newday_proof_cannot_migrate_into_old_history(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "outside_new_days"):
            self.apply((), confirmed_missing_new_days={self.AFFECTED: [OLD_DAYS[-1]]})

    def test_proof_pattern_and_factor_carry_are_reverified_readonly(self):
        self.apply(NEW_DAYS)
        envelope = {"sourceCommit": COMMIT, "dates": {self.AFFECTED: NEW_DAYS}}
        for field, value, reason in [("amount", 0, "conflicts_with_stored_values"),
                                     ("factor", 0.03, "factor_not_carried")]:
            path = self.stage / "features" / self.AFFECTED.lower() / f"{field}.day.bin"
            original = path.read_bytes()
            path.write_bytes(original[:-4] + struct.pack("<f", value))
            try:
                with self.assertRaisesRegex(tree.TreeValidationError, reason):
                    tree.validate_tree(self.stage, OLD_DAYS + NEW_DAYS, NEW_DAYS[-1], 1, 0.5,
                        required_symbols=(SYM,), source_absence=envelope, expected_source_commit=COMMIT)
            finally:
                path.write_bytes(original)

    def test_resumed_current_ten_fields_checked_even_when_previous_day_absent(self):
        self.apply(NEW_DAYS[:1])
        path = self.stage / "features" / self.AFFECTED.lower() / "volume.day.bin"
        path.write_bytes(path.read_bytes()[:-4] + struct.pack("<f", float("nan")))
        with self.assertRaisesRegex(tree.TreeValidationError, "invalid_current_field"):
            tree.validate_tree(self.stage, OLD_DAYS + NEW_DAYS, NEW_DAYS[-1], 1, 0.5, required_symbols=(SYM,),
                source_absence={"sourceCommit": COMMIT, "dates": {self.AFFECTED: NEW_DAYS[:1]}}, expected_source_commit=COMMIT)

    def test_source_absent_reference_is_still_a_hard_block(self):
        with self.assertRaisesRegex(tree.TreeValidationError, "reference_or_index_not_at_target"):
            self.apply(NEW_DAYS, required_symbols=(SYM, self.AFFECTED))

    def test_early_new_day_large_gap_cannot_hide_behind_final_day_recovery(self):
        new_days = NEW_DAYS + ["2026-09-09", "2026-09-10"]
        normal = rows_for(SYM, OLD_DAYS + new_days)
        affected = [row for row in rows_for(self.AFFECTED, OLD_DAYS + new_days) if row["date"] not in NEW_DAYS]
        with self.assertRaisesRegex(tree.TreeValidationError, "market_coverage_incomplete"):
            tree.apply_prices(self.old, self.stage, OLD_DAYS, OLD_DAYS + new_days,
                source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=normal + affected, first_dates={},
                confirmed_missing_new_days={self.AFFECTED: NEW_DAYS}, confirmed_missing_new_days_source_commit=COMMIT,
                min_eligible=1, min_coverage=0.75, required_symbols=(SYM,))

    def test_new_ipo_after_first_quote_can_be_absent_but_not_before_it(self):
        ipo = "SZ001999"
        rows = self.normal_rows + self.affected_rows + rows_for(ipo, NEW_DAYS[:1])
        result = self.apply((), raw_rows=rows, first_dates={ipo: NEW_DAYS[0]},
            confirmed_missing_new_days={ipo: NEW_DAYS[-1:]},
            source_absence={"sourceCommit": COMMIT, "dates": {ipo: NEW_DAYS[-1:]}})
        self.assertEqual(result["daily_coverage"][0]["first_day_listings_checked"], 1)
        self.assertEqual(result["daily_coverage"][0]["eligible"], 2)
        self.assertEqual(result["daily_coverage"][1]["eligible"], 3)
        self.assertEqual(result["lastQuotedDate"], {ipo: NEW_DAYS[0]})
        other_stage = self.parent / "ipo-bad-stage"
        shutil.copytree(self.old, other_stage)
        with self.assertRaisesRegex(tree.TreeValidationError, "before_first_quote"):
            tree.apply_prices(self.old, other_stage, OLD_DAYS, OLD_DAYS + NEW_DAYS,
                source_commit=COMMIT, expected_source_commit=COMMIT, raw_rows=rows, first_dates={ipo: NEW_DAYS[0]},
                confirmed_missing_new_days={ipo: NEW_DAYS}, confirmed_missing_new_days_source_commit=COMMIT,
                min_eligible=1, min_coverage=0.5, required_symbols=(SYM,))

    def test_exact_coverage_threshold_remains_5000_and_point999(self):
        symbols = [f"SH{600000 + index:06d}" for index in range(5000)]
        records = {symbol: {"start": OLD_DAYS[0], "end": OLD_DAYS[-1]} for symbol in symbols}
        normal = {field: (1.0, 1.0, 1.0, 1.0) for field in tree.FIELDS}
        normal["change"] = (float("nan"), 0.0, 0.0, 0.0)
        absent = {field: ((1.0, 1.0, 1.0, 1.0) if field == "factor" else (1.0, 1.0, 1.0, float("nan")))
                  for field in tree.FIELDS}
        for count in (5, 6, 26):
            missing = set(symbols[:count])
            envelope = {"sourceCommit": COMMIT, "dates": {symbol: OLD_DAYS[-1:] for symbol in missing}}
            def load(root, symbol, calendar):
                return 0, 4, {}, absent if symbol in missing else normal
            with self.subTest(count=count), patch.object(tree, "_instruments", return_value=(records, [], b"")), patch.object(tree, "_load", side_effect=load):
                if count <= 6:
                    result = tree.validate_tree(self.old, OLD_DAYS, OLD_DAYS[-1], source_absence=envelope,
                                                expected_source_commit=COMMIT, required_symbols=())
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["invalid"], count)
                    self.assertTrue(math.isclose(result["coverage"], (5000 - count) / 5000, rel_tol=0, abs_tol=1e-12))
                else:
                    with self.assertRaisesRegex(tree.TreeValidationError, "source_absence_exceeds_bound"):
                        tree.validate_tree(self.old, OLD_DAYS, OLD_DAYS[-1], source_absence=envelope,
                                           expected_source_commit=COMMIT, required_symbols=())


if __name__ == "__main__":
    unittest.main()
