import unittest

from collection_window import validate_stage_collection_time


class StageCollectionTimeTests(unittest.TestCase):
    def assert_rejected(self, reason, day, start, end):
        with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
            validate_stage_collection_time(day, start, end)

    def test_premarket_same_day_rejected(self):
        self.assert_rejected("collection_before_cst_close", "2026-09-10",
                             "2026-09-10T08:00:00+08:00", "2026-09-10T08:00:01+08:00")

    def test_intraday_same_day_rejected(self):
        self.assert_rejected("collection_before_cst_close", "2026-09-10",
                             "2026-09-10T13:20:00+08:00", "2026-09-10T13:20:10+08:00")

    def test_1509_same_day_rejected(self):
        self.assert_rejected("collection_before_cst_close", "2026-09-10",
                             "2026-09-10T15:09:59.999999+08:00", "2026-09-10T15:10:01+08:00")

    def test_end_after_cutoff_does_not_redeem_early_start(self):
        self.assert_rejected("collection_before_cst_close", "2026-09-10",
                             "2026-09-10T14:59:59+08:00", "2026-09-10T15:15:00+08:00")

    def test_exact_1510_same_day_allowed(self):
        self.assertIsNone(validate_stage_collection_time(
            "2026-09-10", "2026-09-10T15:10:00+08:00", "2026-09-10T15:10:00+08:00"))

    def test_nightly_same_day_allowed(self):
        validate_stage_collection_time(
            "2026-09-10", "2026-09-10T22:30:00.123456+08:00", "2026-09-10T22:30:12+08:00")

    def test_utc_offset_is_converted_to_cst(self):
        validate_stage_collection_time(
            "2026-09-10", "2026-09-10T07:10:00+00:00", "2026-09-10T07:11:00+00:00")

    def test_z_utc_earlier_than_cst_cutoff_rejected(self):
        self.assert_rejected("collection_before_cst_close", "2026-09-10",
                             "2026-09-10T07:09:59Z", "2026-09-10T07:10:00Z")

    def test_different_offsets_compare_same_instant(self):
        validate_stage_collection_time(
            "2026-09-10", "2026-09-10T21:10:00+14:00", "2026-09-10T07:10:00Z")

    def test_earlier_source_day_morning_observation_allowed_unchanged(self):
        values = ("2026-09-09", "2026-09-10T07:48:11.123456+08:00",
                  "2026-09-10T07:48:12.234567+08:00")
        original = tuple(values)
        self.assertIsNone(validate_stage_collection_time(*values))
        self.assertEqual(values, original)

    def test_earlier_source_day_uses_cst_date_not_raw_utc_date(self):
        validate_stage_collection_time(
            "2026-09-09", "2026-09-09T23:30:00Z", "2026-09-10T07:31:00+08:00")

    def test_future_source_day_rejected(self):
        self.assert_rejected("source_trade_date_in_future", "2026-09-11",
                             "2026-09-10T22:30:00+08:00", "2026-09-10T22:31:00+08:00")

    def test_future_source_day_uses_cst_not_raw_offset_date(self):
        self.assert_rejected("source_trade_date_in_future", "2026-09-11",
                             "2026-09-11T01:00:00+14:00", "2026-09-11T01:01:00+14:00")

    def test_reversed_window_rejected(self):
        self.assert_rejected("collection_window_reversed", "2026-09-10",
                             "2026-09-10T22:31:00+08:00", "2026-09-10T22:30:00+08:00")

    def test_reversal_is_compared_after_timezone_conversion(self):
        self.assert_rejected("collection_window_reversed", "2026-09-10",
                             "2026-09-10T15:10:01+08:00", "2026-09-10T07:10:00Z")

    def test_cst_midnight_crossing_rejected(self):
        self.assert_rejected("collection_window_crosses_cst_date", "2026-09-09",
                             "2026-09-09T23:59:59+08:00", "2026-09-10T00:00:00+08:00")

    def test_utc_same_date_can_cross_cst_date(self):
        self.assert_rejected("collection_window_crosses_cst_date", "2026-09-09",
                             "2026-09-09T15:59:59Z", "2026-09-09T16:00:00Z")

    def test_multiday_window_rejected_even_with_older_source(self):
        self.assert_rejected("collection_window_crosses_cst_date", "2026-09-07",
                             "2026-09-09T22:00:00+08:00", "2026-09-11T22:00:00+08:00")

    def test_naive_start_rejected(self):
        self.assert_rejected("invalid_started_at", "2026-09-10",
                             "2026-09-10T22:30:00", "2026-09-10T22:31:00+08:00")

    def test_naive_end_rejected(self):
        self.assert_rejected("invalid_completed_at", "2026-09-10",
                             "2026-09-10T22:30:00+08:00", "2026-09-10T22:31:00")

    def test_trade_date_is_strict(self):
        for value in (None, True, 20260910, "20260910", "2026-9-10", "2026-09-10 ",
                      "2026-09-10T00:00:00", "2026-02-30", "0000-01-01", "２０２６-０９-１０"):
            with self.subTest(value=value):
                self.assert_rejected("invalid_trade_date", value,
                                     "2026-09-10T22:30:00+08:00", "2026-09-10T22:31:00+08:00")

    def test_timestamp_syntax_is_strict(self):
        for value in (None, True, "", "2026-09-10 22:30:00+08:00", "20260910T223000+0800",
                      "2026-09-10T22:30+08:00", "2026-09-10T22:30:00+0800",
                      "2026-09-10T22:30:00+08:00 ", "2026-09-10T22:30:00+25:00",
                      "2026-09-10T22:30:00+08:99", "2026-09-10T24:00:00+08:00",
                      "2026-02-30T22:30:00+08:00", "2026-09-10T22:30:00.1234567+08:00"):
            with self.subTest(value=value):
                self.assert_rejected("invalid_started_at", "2026-09-10", value,
                                     "2026-09-10T22:31:00+08:00")

    def test_timezone_conversion_overflow_rejected_with_fixed_reason(self):
        self.assert_rejected("invalid_started_at", "9999-12-31",
                             "9999-12-31T23:59:00Z", "9999-12-31T23:59:00+08:00")

    def test_historical_after_close_window_does_not_depend_on_today(self):
        validate_stage_collection_time(
            "2000-01-04", "2000-01-04T22:30:00+08:00", "2000-01-04T22:31:00+08:00")


if __name__ == "__main__":
    unittest.main()
