"""Synthetic files only: batched coverage must retain every single-day gate."""

import gc
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import weakref

from test_tree import tree, rows_for, write_symbol, COMMIT, SYM


OTHER = "SZ000001"
DAYS = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]
TARGETS = DAYS[2:]


class CoverageBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="coverage-batch-test-")
        self.root = Path(self.temporary.name)
        (self.root / "instruments").mkdir()
        for symbol in (SYM, OTHER):
            write_symbol(self.root, symbol, DAYS, rows_for(symbol, DAYS), DAYS)
        self.kwargs = dict(min_eligible=1, min_coverage=0.5, required_symbols=(SYM,))

    def tearDown(self):
        self.temporary.cleanup()

    def set_value(self, symbol, field, index, value):
        path = self.root / "features" / symbol.lower() / f"{field}.day.bin"
        data = bytearray(path.read_bytes())
        struct.pack_into("<f", data, 4 * (index + 1), value)
        path.write_bytes(data)

    def single_outcome(self, **changes):
        kwargs = dict(self.kwargs, **changes)
        try:
            return ("ok", [tree.validate_coverage(self.root, DAYS, target_date=day, **kwargs)
                           for day in TARGETS])
        except Exception as exc:
            return (type(exc).__name__, str(exc))

    def batch_outcome(self, **changes):
        kwargs = dict(self.kwargs, **changes)
        try:
            return ("ok", tree.validate_coverage_days(self.root, DAYS, target_dates=TARGETS, **kwargs))
        except Exception as exc:
            return (type(exc).__name__, str(exc))

    def assert_equivalent(self, **changes):
        expected = self.single_outcome(**changes)
        self.assertEqual(self.batch_outcome(**changes), expected)
        return expected

    def test_complete_results_and_load_count_one_per_symbol(self):
        original = tree._load
        with patch.object(tree, "_load", wraps=original) as loader:
            expected = self.single_outcome()
            self.assertEqual(loader.call_count, 2 * len(TARGETS))
        with patch.object(tree, "_load", wraps=original) as loader:
            self.assertEqual(self.batch_outcome(), expected)
            self.assertEqual(loader.call_count, 2)

    def test_first_listing_excluded_then_included_same_as_each_day(self):
        ipo = "SZ001999"
        write_symbol(self.root, ipo, TARGETS, rows_for(ipo, TARGETS), DAYS)
        outcome = self.assert_equivalent()
        self.assertEqual(outcome[0], "ok")
        self.assertEqual([day["eligible"] for day in outcome[1]], [2, 3, 3])
        self.assertEqual([day["first_day_listings_checked"] for day in outcome[1]], [1, 0, 0])

    def test_source_absent_today_and_previous_close_keep_separate_reasons(self):
        absent_index = 2
        for field in tree.FIELDS:
            if field != "factor":
                self.set_value(OTHER, field, absent_index, float("nan"))
        outcome = self.assert_equivalent(source_absence={"sourceCommit": COMMIT,
            "dates": {OTHER: [TARGETS[0]]}}, expected_source_commit=COMMIT)
        self.assertEqual(outcome[0], "ok")
        self.assertEqual(outcome[1][0]["invalid_reasons"], {OTHER: ["source_absent_today"]})
        self.assertEqual(outcome[1][1]["invalid_reasons"], {OTHER: ["prior_close_source_absent"]})
        self.assertEqual(outcome[1][2]["invalid_reasons"], {})

    def test_independent_threshold_failure_not_hidden_by_final_day(self):
        for field in tree.FIELDS:
            if field != "factor":
                self.set_value(OTHER, field, 2, float("nan"))
        outcome = self.assert_equivalent(min_coverage=1.0,
            source_absence={"sourceCommit": COMMIT, "dates": {OTHER: [TARGETS[0]]}},
            expected_source_commit=COMMIT)
        self.assertIn("market_coverage_incomplete", outcome[1])

    def test_earlier_day_failure_wins_even_if_later_day_bad_symbol_comes_first(self):
        self.set_value(SYM, "amount", 3, -1)
        self.set_value(OTHER, "amount", 2, -1)
        outcome = self.assert_equivalent()
        self.assertEqual(outcome[1], f"negative_current_field:{OTHER}:amount")

    def test_first_day_failure_stops_before_loading_later_symbols(self):
        self.set_value(SYM, "amount", 2, -1)
        with patch.object(tree, "_load", wraps=tree._load) as load:
            outcome = self.batch_outcome()
        self.assertEqual(outcome, ("TreeValidationError", f"negative_current_field:{SYM}:amount"))
        self.assertEqual([call.args[1] for call in load.call_args_list], [SYM])

    def test_later_day_failure_allows_both_earlier_days_to_find_first_error(self):
        third = "SZ002000"
        write_symbol(self.root, third, DAYS, rows_for(third, DAYS), DAYS)
        self.set_value(SYM, "amount", 4, -1)  # Third target fails first in symbol order.
        self.set_value(OTHER, "amount", 3, -1)  # Second target must keep checking.
        self.set_value(third, "amount", 2, -1)  # First target ultimately wins.
        expected = self.single_outcome()
        with patch.object(tree, "_load", wraps=tree._load) as load:
            self.assertEqual(self.batch_outcome(), expected)
        self.assertEqual(expected, ("TreeValidationError", f"negative_current_field:{third}:amount"))
        self.assertEqual([call.args[1] for call in load.call_args_list], [SYM, OTHER, third])

    def test_initial_first_day_failure_does_not_construct_later_validators(self):
        with patch.object(tree, "_coverage_steps", wraps=tree._coverage_steps) as steps, \
                patch.object(tree, "_load", wraps=tree._load) as load:
            outcome = self.batch_outcome(required_symbols=("SH999999",))
        self.assertEqual(outcome, ("TreeValidationError", "required_reference_or_index_missing"))
        self.assertEqual(steps.call_count, 1)
        self.assertEqual(load.call_count, 0)

    def test_malformed_header_is_rejected_with_same_reason(self):
        self.set_value(OTHER, "close", -1, float("nan"))
        self.assertIn("invalid_feature_header", self.assert_equivalent()[1])

    def test_missing_field_is_rejected_with_same_reason(self):
        (self.root / "features" / OTHER.lower() / "amount.day.bin").unlink()
        self.assertIn("unknown_or_missing_field", self.assert_equivalent()[1])

    def test_unproven_nan_remains_fatal(self):
        self.set_value(OTHER, "close", 2, float("nan"))
        self.assertIn("invalid_current_field", self.assert_equivalent()[1])

    def test_required_membership_is_still_checked(self):
        self.assertEqual(self.assert_equivalent(required_symbols=("SH999999",))[1],
                         "required_reference_or_index_missing")

    def test_bad_absence_factor_is_still_checked_before_coverage(self):
        self.set_value(OTHER, "factor", 2, 99)
        outcome = self.assert_equivalent(source_absence={"sourceCommit": COMMIT,
            "dates": {OTHER: [TARGETS[0]]}}, expected_source_commit=COMMIT)
        self.assertIn("source_absence_conflicts_with_stored_values", outcome[1])

    def test_batch_does_not_cache_files_between_calls(self):
        self.assertEqual(self.batch_outcome()[0], "ok")
        self.set_value(OTHER, "amount", 4, -1)
        self.assertIn(f"negative_current_field:{OTHER}:amount", self.assert_equivalent()[1])

    def test_failure_tracebacks_release_prior_symbol_and_chained_payloads(self):
        # A later day fails on the first symbol while an earlier day must keep
        # checking the next symbol. Neither the failed day's history nor a
        # chained exception's payload may survive into that next file load.
        self.set_value(SYM, "amount", 3, -1)
        original_load, original_validate = tree._load, tree._validate_quote

        class Tracked(dict):
            pass

        for explicit_cause in (False, True):
            with self.subTest(explicit_cause=explicit_cause):
                snapshots, payloads, observed = [], [], []

                def load(root, symbol, calendar):
                    gc.collect()
                    observed.append(symbol)
                    self.assertTrue(all(reference() is None for reference in snapshots))
                    self.assertTrue(all(reference() is None for reference in payloads))
                    start, length, blobs, values = original_load(root, symbol, calendar)
                    tracked = Tracked(values)
                    snapshots.append(weakref.ref(tracked))
                    return start, length, blobs, tracked

                def validate(symbol, current, *, first_day):
                    if current["amount"] < 0:
                        def underlying_failure():
                            payload = Tracked(large_history="synthetic")
                            payloads.append(weakref.ref(payload))
                            raise ValueError("synthetic_cause")

                        try:
                            underlying_failure()
                        except ValueError as cause:
                            error = tree.TreeValidationError(f"negative_current_field:{symbol}:amount")
                            if explicit_cause:
                                raise error from cause
                            raise error
                    return original_validate(symbol, current, first_day=first_day)

                with patch.object(tree, "_load", side_effect=load), \
                        patch.object(tree, "_validate_quote", side_effect=validate):
                    outcome = self.batch_outcome()
                self.assertEqual(outcome, ("TreeValidationError", f"negative_current_field:{SYM}:amount"))
                self.assertEqual(observed, [SYM, OTHER])
                self.assertEqual(len(payloads), 1)

    def test_base_exception_interrupts_immediately_not_retained_as_day_error(self):
        class Interrupted(BaseException):
            pass

        with patch.object(tree, "_load", side_effect=Interrupted()) as load:
            with self.assertRaises(Interrupted):
                tree.validate_coverage_days(self.root, DAYS, target_dates=TARGETS, **self.kwargs)
        self.assertEqual(load.call_count, 1)

    def test_load_failure_traceback_released_before_earlier_day_continues(self):
        # This IPO is irrelevant to the earliest target; only later dates load
        # it. An earlier date must continue to the following inventory symbol.
        ipo, following = "SZ001999", "SZ002000"
        write_symbol(self.root, ipo, TARGETS[1:], rows_for(ipo, TARGETS[1:]), DAYS)
        write_symbol(self.root, following, DAYS, rows_for(following, DAYS), DAYS)
        original_load = tree._load
        payloads, observed = [], []

        class Tracked(dict):
            pass

        def load(root, symbol, calendar):
            observed.append(symbol)
            if symbol == ipo:
                payload = Tracked(partial_feature_history="synthetic")
                payloads.append(weakref.ref(payload))
                raise tree.TreeValidationError("synthetic_load_failure")
            if symbol == following:
                gc.collect()
                self.assertTrue(payloads)
                self.assertTrue(all(reference() is None for reference in payloads))
            return original_load(root, symbol, calendar)

        with patch.object(tree, "_load", side_effect=load):
            outcome = self.batch_outcome()
        self.assertEqual(outcome, ("TreeValidationError", "synthetic_load_failure"))
        self.assertEqual(observed, [SYM, OTHER, ipo, following])

    def test_invalid_target_and_parameter_rejections(self):
        for changes in ({"min_eligible": True}, {"min_coverage": True},
                        {"required_symbols": ("SH999999",)}):
            with self.subTest(changes=changes):
                self.assert_equivalent(**changes)
        with self.assertRaisesRegex(ValueError, "target_outside_calendar"):
            tree.validate_coverage_days(self.root, DAYS, target_dates=["2026-10-15"], **self.kwargs)


if __name__ == "__main__":
    unittest.main()
