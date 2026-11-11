"""Synthetic, offline tests: no real market/auth files, network or business code."""

import copy
from datetime import date, timedelta
import importlib.util
import math
from pathlib import Path
import struct
import sys
import unittest


spec = importlib.util.spec_from_file_location("qlib_incremental_conversion", Path(__file__).with_name("conversion.py"))
conversion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = conversion
spec.loader.exec_module(conversion)

COMMIT = "80q7jtqjrga15h61uufm60h6uqirk6iq"
SYMBOL = "SH600000"
OLD_DAYS = ["2026-09-02", "2026-09-03", "2026-09-04"]
NEW_DAYS = ["2026-09-07", "2026-09-08"]


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def raw(day, close, *, adj_ratio=0.5, volume=1000, amount=10000):
    return dict(date=day, open=close - 1, high=close + 2, low=close - 2,
                close=close, adjclose=close * adj_ratio, volume=volume, amount=amount)


def independently_normalized(row, previous_close, scale=0.02):
    """Explicit expected exporter equations, independent of converter helpers."""
    factor = row["adjclose"] / row["close"] * scale
    result = {
        "adjclose": row["adjclose"], "amount": row["amount"],
        "change": row["close"] / previous_close - 1,
        "factor": factor, "volume": row["volume"] / factor,
        "vwap": row["amount"] / row["volume"] * 10 * factor,
        **{field: row[field] * factor for field in ("open", "high", "low", "close")},
    }
    return {"date": row["date"], **{key: f32(value) for key, value in result.items()}}


def existing_arguments():
    anchors = [raw(OLD_DAYS[1], 100), raw(OLD_DAYS[2], 102)]
    return dict(symbol=SYMBOL, source_commit=COMMIT, expected_source_commit=COMMIT,
                old_anchors=[independently_normalized(anchors[0], 98), independently_normalized(anchors[1], 100)],
                raw_anchors=anchors, raw_previous_valid={"date": OLD_DAYS[0], "close": 98},
                new_rows=[raw(NEW_DAYS[0], 104), raw(NEW_DAYS[1], 106)], new_dates=NEW_DAYS)


class CalendarCacheTests(unittest.TestCase):
    def setUp(self):
        conversion._validated_days_cached.cache_clear()

    def tearDown(self):
        conversion._validated_days_cached.cache_clear()

    def test_list_and_tuple_equal_uncached_validation_and_share_result(self):
        expected = conversion._validate_days_tuple(tuple(OLD_DAYS), "calendar", False)
        first = conversion._days(list(OLD_DAYS), "calendar")
        second = conversion._days(tuple(OLD_DAYS), "calendar")
        self.assertEqual(first, expected)
        self.assertIs(first, second)
        self.assertEqual(conversion._validated_days_cached.cache_info().hits, 1)

    def test_mutable_input_cannot_reuse_stale_result(self):
        values = list(OLD_DAYS)
        first = conversion._days(values, "calendar")
        values[-1] = "2026-09-07"
        self.assertEqual(conversion._days(values, "calendar"), tuple(values))
        self.assertEqual(first, tuple(OLD_DAYS))
        values[-1] = values[0]
        with self.assertRaisesRegex(conversion.ConversionError, "unordered_or_empty_calendar"):
            conversion._days(values, "calendar")
        values[:] = ["2026-02-30"]
        with self.assertRaisesRegex(conversion.ConversionError, "invalid_date"):
            conversion._days(values, "calendar")

    def test_types_and_real_date_validation_are_not_bypassed(self):
        for values in ([None], [True], [123], [[]], [{}], ["2026-02-30"], ["2026-9-02"]):
            with self.subTest(values=values), self.assertRaisesRegex(conversion.ConversionError, "invalid_date"):
                conversion._days(values, "calendar")
        for values in ("2026-09-02", b"2026-09-02"):
            with self.subTest(values=values), self.assertRaisesRegex(conversion.ConversionError, "invalid_calendar"):
                conversion._days(values, "calendar")
        self.assertEqual(conversion._validated_days_cached.cache_info().currsize, 0)

    def test_string_subclass_does_not_enter_cache(self):
        class UnhashableString(str):
            __hash__ = None
        value = UnhashableString("2026-09-02")
        self.assertEqual(conversion._days([value], "calendar"), (value,))
        self.assertEqual(conversion._validated_days_cached.cache_info().currsize, 0)

    def test_empty_permission_and_error_label_are_not_cached_across_callers(self):
        self.assertEqual(conversion._days([], "allowed", allow_empty=True), ())
        with self.assertRaisesRegex(conversion.ConversionError, "unordered_or_empty_allowed"):
            conversion._days([], "allowed")
        for label in ("old_calendar", "new_calendar"):
            with self.subTest(label=label), self.assertRaisesRegex(conversion.ConversionError, "unordered_or_empty_" + label):
                conversion._days(OLD_DAYS[::-1], label)

    def test_cache_is_bounded_and_evicted_input_is_revalidated(self):
        first = (date(2020, 1, 1).isoformat(),)
        for offset in range(129):
            conversion._days(((date(2020, 1, 1) + timedelta(days=offset)).isoformat(),), "calendar")
        info = conversion._validated_days_cached.cache_info()
        self.assertEqual(info.maxsize, 128)
        self.assertEqual(info.currsize, 128)
        self.assertEqual(info.misses, 129)
        self.assertEqual(conversion._days(first, "calendar"), first)
        self.assertEqual(conversion._validated_days_cached.cache_info().misses, 130)
        self.assertEqual(conversion._validated_days_cached.cache_info().currsize, 128)


class ExistingConversionTests(unittest.TestCase):
    def reject(self, args, reason):
        with self.assertRaisesRegex(conversion.ConversionError, reason):
            conversion.convert_existing(**args)

    def test_all_ten_fields_match_source_equations_and_f32_binary(self):
        args = existing_arguments()
        result = conversion.convert_existing(**args)
        self.assertEqual(set(result.values), set(conversion.FIELDS))
        self.assertEqual(result.dates, tuple(NEW_DAYS))
        self.assertEqual(result.overlap_dates, tuple(OLD_DAYS[1:]))
        self.assertAlmostEqual(result.scale, 0.02, places=8)
        for i, previous in enumerate((102, 104)):
            expected = independently_normalized(args["new_rows"][i], previous)
            for field in conversion.FIELDS:
                self.assertAlmostEqual(result.values[field][i], expected[field], delta=abs(expected[field]) * 2e-6 + 1e-9)
                self.assertEqual(struct.unpack("<ff", result.append_bytes[field])[i], result.values[field][i])

    def test_inputs_are_not_mutated(self):
        args = existing_arguments()
        original = copy.deepcopy(args)
        conversion.convert_existing(**args)
        self.assertEqual(args, original)

    def test_change_uses_unadjusted_previous_close_not_adjclose(self):
        args = existing_arguments()
        args["new_rows"][0] = raw(NEW_DAYS[0], 51, adj_ratio=1.0)
        result = conversion.convert_existing(**args)
        self.assertEqual(result.values["change"][0], -0.5)
        self.assertAlmostEqual(result.values["close"][0], 1.02, places=6)
        self.assertAlmostEqual(result.values["factor"][0], 0.02, places=7)

    def test_amount_and_adjclose_stay_raw_volume_and_vwap_are_scaled(self):
        result = conversion.convert_existing(**existing_arguments())
        self.assertEqual(result.values["amount"][0], 10000)
        self.assertEqual(result.values["adjclose"][0], 52)
        self.assertAlmostEqual(result.values["volume"][0], 100000, delta=0.1)
        self.assertAlmostEqual(result.values["vwap"][0], 1, places=6)

    def test_every_overlap_field_checked_on_both_days(self):
        for day in (0, 1):
            for field in conversion.FIELDS:
                with self.subTest(day=day, field=field):
                    args = existing_arguments()
                    args["old_anchors"][day][field] += max(abs(args["old_anchors"][day][field]) * 0.01, 0.001)
                    self.reject(args, "overlap_mismatch")

    def test_global_raw_adjclose_rebase_rejected_even_if_close_can_align(self):
        args = existing_arguments()
        for row in args["raw_anchors"] + args["new_rows"]:
            row["adjclose"] *= 2
        self.reject(args, "overlap_mismatch.*adjclose")

    def test_two_different_anchor_scales_rejected(self):
        args = existing_arguments()
        for field in ("close", "open", "high", "low", "factor", "vwap"):
            args["old_anchors"][0][field] *= 1.01
        args["old_anchors"][0]["volume"] /= 1.01
        self.reject(args, "overlap_mismatch")

    def test_previous_close_verified_via_first_anchor_change(self):
        args = existing_arguments()
        args["raw_previous_valid"]["close"] = 97
        self.reject(args, "overlap_mismatch.*change")

    def test_previous_date_and_invalid_old_values_rejected(self):
        args = existing_arguments()
        args["raw_previous_valid"]["date"] = OLD_DAYS[-1]
        self.reject(args, "invalid_previous_raw_date")
        for value in (float("nan"), float("inf"), True):
            args = existing_arguments()
            args["old_anchors"][0]["amount"] = value
            self.reject(args, "number")

    def test_raw_fields_have_ulp_not_broad_relative_revision_tolerance(self):
        args = existing_arguments()
        args["raw_anchors"][0]["amount"] += 0.01
        # 1e-6 relative is below the derived-field tolerance, but exceeds one ULP.
        self.reject(args, "overlap_mismatch.*amount")

    def test_large_change_absolute_difference_not_hidden_by_relative_tolerance(self):
        args = existing_arguments()
        args["old_anchors"][0]["change"] += 1e-6
        self.reject(args, "overlap_mismatch.*change")

    def test_one_anchor_insufficient(self):
        args = existing_arguments()
        args["old_anchors"] = args["old_anchors"][-1:]
        args["raw_anchors"] = args["raw_anchors"][-1:]
        self.reject(args, "two_valid_overlap")

    def test_every_new_day_requires_explicit_row(self):
        args = existing_arguments()
        args["new_rows"] = args["new_rows"][-1:]
        self.reject(args, "missing_or_extra_source_dates")

    def test_duplicate_reverse_extra_and_old_new_dates_rejected(self):
        cases = [([raw(NEW_DAYS[0], 104)] * 2, NEW_DAYS, "unordered"),
                 (list(reversed(existing_arguments()["new_rows"])), NEW_DAYS, "unordered"),
                 (existing_arguments()["new_rows"], NEW_DAYS[:1], "missing_or_extra"),
                 ([raw(OLD_DAYS[-1], 104)], [OLD_DAYS[-1]], "overlap_history")]
        for rows, dates, reason in cases:
            with self.subTest(reason=reason):
                args = existing_arguments()
                args.update(new_rows=rows, new_dates=dates)
                self.reject(args, reason)

    def test_explicit_pause_never_fills_stale_prices_and_carries_factor(self):
        args = existing_arguments()
        args["new_rows"][0] = dict(date=NEW_DAYS[0], **{field: None for field in conversion.RAW_FIELDS})
        result = conversion.convert_existing(**args)
        for field in conversion.FIELDS:
            if field == "factor":
                self.assertAlmostEqual(result.values[field][0], 0.01, places=8)
            else:
                self.assertTrue(math.isnan(result.values[field][0]), field)
        # The next change skips the explicit pause but retains the last raw close.
        self.assertAlmostEqual(result.values["change"][1], 106 / 102 - 1, places=7)

    def test_zero_volume_explicit_row_is_nan_not_close_forward_fill(self):
        args = existing_arguments()
        args["new_rows"][0]["volume"] = 0
        result = conversion.convert_existing(**args)
        self.assertTrue(math.isnan(result.values["close"][0]))
        self.assertTrue(math.isnan(result.values["amount"][0]))

    def test_invalid_positive_volume_ohlc_and_missing_fields_rejected(self):
        for mutation, reason in [({"high": 90}, "inconsistent_ohlc"), ({"close": None}, "invalid_number"),
                                 ({"open": -1}, "nonpositive"), ({"amount": -1}, "invalid_amount")]:
            with self.subTest(mutation=mutation):
                args = existing_arguments()
                args["new_rows"][0].update(mutation)
                self.reject(args, reason)
        args = existing_arguments()
        del args["new_rows"][0]["adjclose"]
        self.reject(args, "missing_raw_field")

    def test_nan_inf_boolean_and_string_numbers_rejected(self):
        for value in (float("nan"), float("inf"), True, "104"):
            with self.subTest(value=value):
                args = existing_arguments()
                args["new_rows"][0]["close"] = value
                self.reject(args, "number")

    def test_mixed_snapshot_or_symbol_rejected(self):
        for field, value, reason in [("source_commit", "0" * 32, "mixed_source"),
                                     ("symbol", "SZ000001", "mixed_symbol")]:
            args = existing_arguments()
            args["new_rows"][0][field] = value
            self.reject(args, reason)
        args = existing_arguments()
        args["expected_source_commit"] = "0" * 32
        self.reject(args, "mixed_source")
        args = existing_arguments()
        args["source_commit"] = "master"
        self.reject(args, "unfixed_source")

    def test_invalid_conflicting_and_noncanonical_dates_rejected(self):
        for day in ("2026-09-31", "2026-9-07", True, "2026-09-07T00:00:00Z"):
            args = existing_arguments()
            args["new_rows"][0]["date"] = day
            self.reject(args, "invalid_date")
        args = existing_arguments()
        args["new_rows"][0]["tradedate"] = NEW_DAYS[1]
        self.reject(args, "conflicting_row_dates")

    def test_100x_heuristic_cannot_silently_rewrite_history(self):
        args = existing_arguments()
        args["new_rows"][0] = raw(NEW_DAYS[0], 10200)
        self.reject(args, "100x_correction")

    def test_raw_vwap_formula_check_and_date_alias(self):
        args = existing_arguments()
        args["new_rows"][0]["vwap"] = 100
        args["new_rows"][0]["tradedate"] = args["new_rows"][0].pop("date")
        conversion.convert_existing(**args)
        args["new_rows"][0]["vwap"] = 10
        self.reject(args, "source_vwap_mismatch")

    def test_float32_overflow_rejected(self):
        args = existing_arguments()
        args["new_rows"][0]["amount"] = 1e45
        self.reject(args, "float32_overflow")


class NewSymbolTests(unittest.TestCase):
    def args(self):
        return dict(symbol="SZ001999", source_commit=COMMIT, expected_source_commit=COMMIT,
                    authoritative_first_date=NEW_DAYS[0], old_calendar_end=OLD_DAYS[-1],
                    new_rows=[raw(NEW_DAYS[0], 20), raw(NEW_DAYS[1], 22)], new_dates=NEW_DAYS)

    def test_true_first_row_normalizes_to_one_without_invented_change(self):
        result = conversion.convert_new_symbol(**self.args())
        self.assertEqual(result.values["close"][0], 1)
        self.assertTrue(math.isnan(result.values["change"][0]))
        self.assertAlmostEqual(result.values["change"][1], 0.1, places=7)
        self.assertEqual(result.overlap_dates, ())

    def test_listing_can_start_on_later_append_day_without_earlier_filler(self):
        args = self.args()
        args.update(authoritative_first_date=NEW_DAYS[1], new_rows=[args["new_rows"][1]], new_dates=NEW_DAYS[1:])
        result = conversion.convert_new_symbol(**args)
        self.assertEqual(result.dates, (NEW_DAYS[1],))
        self.assertEqual(len(result.append_bytes["close"]), 4)

    def test_earlier_source_history_blocks_new_symbol_shortcut(self):
        for first in ("2026-07-01", OLD_DAYS[-1], NEW_DAYS[1]):
            args = self.args()
            args["authoritative_first_date"] = first
            with self.assertRaisesRegex(conversion.ConversionError, "true_source_history_start"):
                conversion.convert_new_symbol(**args)

    def test_paused_first_row_cannot_supply_normalization_base(self):
        args = self.args()
        args["new_rows"][0]["volume"] = 0
        with self.assertRaisesRegex(conversion.ConversionError, "first_row_not_valid"):
            conversion.convert_new_symbol(**args)


class BinaryBoundaryTests(unittest.TestCase):
    def test_append_preserves_every_old_byte_including_nan_payload(self):
        # Include a noncanonical NaN bit pattern; repacking old values would lose it.
        blob = struct.pack("<f", 0) + struct.pack("<I", 0x7FC01234) + struct.pack("<ff", 1, 1.02)
        result = conversion.convert_existing(**existing_arguments())
        for field in conversion.FIELDS:
            merged = conversion.append_feature_bytes(blob, old_calendar=OLD_DAYS,
                new_calendar=OLD_DAYS + NEW_DAYS, append_dates=NEW_DAYS, appended_bytes=result.append_bytes[field])
            self.assertEqual(merged[:len(blob)], blob)
            self.assertEqual(merged[len(blob):], result.append_bytes[field])
            self.assertEqual(len(merged), len(blob) + 8)

    def test_historical_ended_feature_is_not_extended_across_gap(self):
        blob = struct.pack("<fff", 0, 1, 1.02)
        with self.assertRaisesRegex(conversion.ConversionError, "does_not_reach"):
            conversion.append_feature_bytes(blob, old_calendar=OLD_DAYS, new_calendar=OLD_DAYS + NEW_DAYS,
                append_dates=NEW_DAYS, appended_bytes=struct.pack("<ff", 1, 2))

    def test_old_calendar_cannot_be_changed_reordered_or_truncated(self):
        blob = struct.pack("<ffff", 0, 1, 1.01, 1.02)
        for calendar in (["2026-09-01"] + OLD_DAYS[1:] + NEW_DAYS, OLD_DAYS[1:] + NEW_DAYS, OLD_DAYS + NEW_DAYS[:1]):
            with self.subTest(calendar=calendar), self.assertRaises(conversion.ConversionError):
                conversion.append_feature_bytes(blob, old_calendar=OLD_DAYS, new_calendar=calendar,
                    append_dates=NEW_DAYS, appended_bytes=struct.pack("<ff", 1, 2))

    def test_malformed_header_length_or_infinity_rejected(self):
        good = struct.pack("<ffff", 0, 1, 1.01, 1.02)
        for blob, tail in [(good + b"x", b"\0" * 8), (struct.pack("<ffff", 0.5, 1, 2, 3), b"\0" * 8),
                           (good, b"\0" * 4), (good, struct.pack("<ff", float("inf"), 2))]:
            with self.subTest(blob_length=len(blob)), self.assertRaises(conversion.ConversionError):
                conversion.append_feature_bytes(blob, old_calendar=OLD_DAYS, new_calendar=OLD_DAYS + NEW_DAYS,
                    append_dates=NEW_DAYS, appended_bytes=tail)

    def test_new_binary_start_index_and_first_nan_change(self):
        result = conversion.convert_new_symbol(**NewSymbolTests().args())
        blob = conversion.encode_new_feature(start_index=3, values=result.values["change"])
        self.assertEqual(struct.unpack("<f", blob[:4])[0], 3)
        self.assertTrue(math.isnan(struct.unpack("<f", blob[4:8])[0]))
        for index in (-1, True, 0.5, 2**24 + 1):
            with self.assertRaises(conversion.ConversionError):
                conversion.encode_new_feature(start_index=index, values=(1,))


class ShortHistoryTests(unittest.TestCase):
    def args(self, length):
        dates = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]
        rows = [raw(day, 20 + index) for index, day in enumerate(dates)]
        old = []
        for index in range(length):
            row = independently_normalized(rows[index], rows[max(0, index - 1)]["close"], scale=0.1)
            if index == 0:
                row["change"] = float("nan")
            old.append(row)
        return dict(symbol=SYMBOL, source_commit=COMMIT, expected_source_commit=COMMIT,
                    authoritative_first_date=dates[0], old_rows=old,
                    raw_history=rows[:length + 1], new_dates=dates[length:length + 1])

    def test_one_two_three_old_days_are_full_history_verified(self):
        for length in (1, 2, 3):
            with self.subTest(length=length):
                args = self.args(length)
                result = conversion.convert_short_existing(**args)
                self.assertEqual(len(result.values["close"]), 1)
                self.assertAlmostEqual(result.values["close"][0], (20 + length) / 20, places=6)
                self.assertEqual(len(result.overlap_dates), length)

    def test_true_history_start_mismatch_rejected(self):
        args = self.args(2)
        args["authoritative_first_date"] = "2026-09-01"
        with self.assertRaisesRegex(conversion.ConversionError, "history_start_mismatch"):
            conversion.convert_short_existing(**args)

    def test_old_first_change_must_really_be_nan(self):
        args = self.args(1)
        args["old_rows"][0]["change"] = 0
        with self.assertRaisesRegex(conversion.ConversionError, "short_history_mismatch.*change"):
            conversion.convert_short_existing(**args)

    def test_old_revision_or_omitted_history_rejected(self):
        args = self.args(3)
        args["old_rows"][1]["amount"] += 1
        with self.assertRaisesRegex(conversion.ConversionError, "short_history_mismatch"):
            conversion.convert_short_existing(**args)
        args = self.args(3)
        args["raw_history"].pop(1)
        with self.assertRaisesRegex(conversion.ConversionError, "history_incomplete"):
            conversion.convert_short_existing(**args)

    def test_short_history_with_separately_proven_tail_gap_keeps_initial_base(self):
        args = self.args(1)
        args["raw_history"][1]["date"] = "2026-09-07"
        args.update(new_dates=("2026-09-03", "2026-09-04", "2026-09-07"),
            historical_missing_dates=("2026-09-03", "2026-09-04"), history_boundary="2026-09-04",
            missing_source_commit=COMMIT)
        result = conversion.convert_short_existing(**args)
        self.assertTrue(math.isnan(result.values["close"][0]))
        self.assertTrue(math.isnan(result.values["close"][1]))
        self.assertAlmostEqual(result.values["close"][2], 21 / 20, places=6)
        self.assertAlmostEqual(result.values["change"][2], 0.05, places=7)


class StoredShortAbsenceTests(unittest.TestCase):
    def args(self, absent_count=1):
        dates = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09"]
        first = raw(dates[0], 20)
        first_old = independently_normalized(first, 20, scale=0.1)
        first_old["change"] = float("nan")
        missing = dates[1:absent_count + 1]
        old = [first_old] + [{"date": day, **{field: first_old["factor"] if field == "factor" else float("nan")
                                            for field in conversion.FIELDS}} for day in missing]
        return dict(symbol=SYMBOL, source_commit=COMMIT, expected_source_commit=COMMIT,
                    authoritative_first_date=dates[0], old_rows=old,
                    raw_history=[first, raw(dates[absent_count + 1], 22)],
                    new_dates=[dates[absent_count + 1]], stored_missing_dates=missing,
                    stored_missing_source_commit=COMMIT)

    def test_previously_stored_absence_then_resume_replays_true_first_price(self):
        for absent_count in (1, 2, 4):
            with self.subTest(absent_count=absent_count):
                args = self.args(absent_count)
                result = conversion.convert_short_existing(**args)
                self.assertAlmostEqual(result.values["close"][0], 1.1, places=6)
                self.assertAlmostEqual(result.values["change"][0], 0.1, places=7)
                self.assertEqual(result.source_absent_dates, tuple(args["stored_missing_dates"]))

    def test_previously_stored_absence_then_another_absence_carries_exact_factor(self):
        args = self.args(2)
        args["raw_history"].pop()
        args.update(confirmed_missing_new_dates=args["new_dates"], missing_new_source_commit=COMMIT,
                    history_boundary=args["old_rows"][-1]["date"])
        result = conversion.convert_short_existing(**args)
        self.assertEqual(result.values["factor"][0], args["old_rows"][-1]["factor"])
        self.assertTrue(all(math.isnan(result.values[field][0]) for field in conversion.FIELDS if field != "factor"))

    def test_stored_absence_requires_fresh_same_ref_evidence(self):
        for changes, reason in [({"stored_missing_dates": ()}, "history_incomplete"),
                                ({"stored_missing_source_commit": "0" * 32}, "stored_missing_evidence_source_mismatch")]:
            args = self.args()
            args.update(changes)
            with self.subTest(reason=reason), self.assertRaisesRegex(conversion.ConversionError, reason):
                conversion.convert_short_existing(**args)

    def test_stored_proof_cannot_authorize_first_or_new_date(self):
        for date in ("2026-09-02", "2026-09-04"):
            args = self.args()
            args["stored_missing_dates"] = [date]
            with self.subTest(date=date), self.assertRaisesRegex(conversion.ConversionError, "outside_old_history"):
                conversion.convert_short_existing(**args)

    def test_later_source_backfill_conflicting_with_old_absence_is_rejected(self):
        args = self.args()
        args["raw_history"].insert(1, raw("2026-09-03", 21))
        with self.assertRaisesRegex(conversion.ConversionError, "conflicts_with_source_row"):
            conversion.convert_short_existing(**args)

    def test_stored_slots_still_require_all_ten_old_values_to_match(self):
        for field, value in (("amount", 0), ("factor", 0.2)):
            args = self.args()
            args["old_rows"][1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(conversion.ConversionError, "short_history_mismatch"):
                conversion.convert_short_existing(**args)


class HistoricalAbsenceConversionTests(unittest.TestCase):
    def args(self):
        args = existing_arguments()
        # A caller's real feature boundary precedes an old-global-calendar gap.
        args["new_dates"] = ["2026-09-07", "2026-09-08", "2026-09-09"]
        args["new_rows"][0]["date"] = "2026-09-08"
        args["new_rows"][1]["date"] = "2026-09-09"
        args.update(historical_missing_dates=("2026-09-07",), history_boundary="2026-09-07",
                    missing_source_commit=COMMIT)
        return args

    def test_proven_absence_carries_factor_and_previous_raw_close_without_price_fill(self):
        result = conversion.convert_existing(**self.args())
        self.assertEqual(result.source_absent_dates, ("2026-09-07",))
        self.assertTrue(math.isnan(result.values["close"][0]))
        self.assertTrue(math.isnan(result.values["volume"][0]))
        self.assertAlmostEqual(result.values["factor"][0], 0.01, places=8)
        self.assertAlmostEqual(result.values["change"][1], 104 / 102 - 1, places=7)

    def test_mixed_source_or_after_history_boundary_evidence_rejected(self):
        args = self.args()
        args["missing_source_commit"] = "0" * 32
        with self.assertRaisesRegex(conversion.ConversionError, "evidence_source_mismatch"):
            conversion.convert_existing(**args)


    def test_historical_proof_cannot_cover_new_boundary(self):
        args = self.args()
        args["history_boundary"] = "2026-09-04"
        with self.assertRaisesRegex(conversion.ConversionError, "evidence_outside_boundary"):
            conversion.convert_existing(**args)


class CurrentSourceAbsenceTests(unittest.TestCase):
    def args(self):
        args = existing_arguments()
        args.update(new_rows=[], confirmed_missing_new_dates=NEW_DAYS,
                    history_boundary=OLD_DAYS[-1], missing_new_source_commit=COMMIT)
        return args

    def test_all_new_days_can_be_confirmed_absent_without_inventing_quotes(self):
        args = self.args()
        result = conversion.convert_existing(**args)
        self.assertEqual(result.source_absent_dates, tuple(NEW_DAYS))
        for field in conversion.FIELDS:
            for value in result.values[field]:
                if field == "factor":
                    self.assertEqual(value, args["old_anchors"][-1]["factor"])
                else:
                    self.assertTrue(math.isnan(value), field)

    def test_resume_change_uses_last_real_raw_close_not_fake_prior_day(self):
        args = self.args()
        args["new_rows"] = existing_arguments()["new_rows"][-1:]
        args["confirmed_missing_new_dates"] = NEW_DAYS[:1]
        result = conversion.convert_existing(**args)
        self.assertTrue(math.isnan(result.values["close"][0]))
        self.assertAlmostEqual(result.values["change"][1], 106 / 102 - 1, places=7)

    def test_first_absent_factor_is_exact_stored_f32_not_reconstructed_scale(self):
        args = self.args()
        bits = struct.unpack("<I", struct.pack("<f", args["old_anchors"][-1]["factor"]))[0]
        args["old_anchors"][-1]["factor"] = struct.unpack("<f", struct.pack("<I", bits + 1))[0]
        result = conversion.convert_existing(**args)
        self.assertEqual(result.values["factor"][0], args["old_anchors"][-1]["factor"])
        self.assertEqual(result.values["factor"][1], result.values["factor"][0])

    def test_new_evidence_ref_boundary_duplicate_and_conflict_rejected(self):
        cases = [(dict(missing_new_source_commit="0" * 32), "source_mismatch"),
                 (dict(history_boundary=NEW_DAYS[0]), "outside_boundary"),
                 (dict(confirmed_missing_new_dates=[NEW_DAYS[0], NEW_DAYS[0]]), "unordered"),
                 (dict(new_rows=existing_arguments()["new_rows"]), "conflicts_with_source_row")]
        for changes, reason in cases:
            args = self.args()
            args.update(changes)
            with self.subTest(reason=reason), self.assertRaisesRegex(conversion.ConversionError, reason):
                conversion.convert_existing(**args)

    def test_short_existing_can_carry_known_initial_factor_into_new_absence(self):
        args = ShortHistoryTests().args(1)
        args["raw_history"] = args["raw_history"][:1]
        args.update(confirmed_missing_new_dates=args["new_dates"], history_boundary=args["old_rows"][-1]["date"],
                    missing_new_source_commit=COMMIT)
        result = conversion.convert_short_existing(**args)
        self.assertTrue(math.isnan(result.values["close"][0]))
        self.assertEqual(result.values["factor"][0], args["old_rows"][-1]["factor"])

    def test_new_ipo_must_have_first_quote_but_later_absence_is_explicit(self):
        args = NewSymbolTests().args()
        args.update(new_rows=args["new_rows"][:1], confirmed_missing_new_dates=NEW_DAYS[-1:],
                    missing_new_source_commit=COMMIT)
        result = conversion.convert_new_symbol(**args)
        self.assertEqual(result.values["close"][0], 1)
        self.assertTrue(math.isnan(result.values["close"][1]))
        self.assertEqual(result.values["factor"][0], result.values["factor"][1])
        args["confirmed_missing_new_dates"] = NEW_DAYS
        with self.assertRaisesRegex(conversion.ConversionError, "outside_boundary"):
            conversion.convert_new_symbol(**args)

    def test_missing_evidence_does_not_hide_conflicting_actual_source_row(self):
        args = self.args()
        args["new_rows"] = [raw("2026-09-07", 103)] + args["new_rows"]
        with self.assertRaisesRegex(conversion.ConversionError, "conflicts_with_source_row"):
            conversion.convert_existing(**args)

if __name__ == "__main__":
    unittest.main()
