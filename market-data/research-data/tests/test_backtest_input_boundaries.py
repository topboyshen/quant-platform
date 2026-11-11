"""Final input boundaries; synthetic arrays, no loaders or business main()."""
import json
import unittest
from unittest import mock
import numpy as np
import pandas as pd
import backtest as core
import backtest_sweep as sweep


class InputBoundaryTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({"open": [10., 10., 10.], "close": [10., 10., 11.]})

    def test_large_numpy_hold_does_not_wrap_to_negative_row(self):
        result = core.trade_returns_from_frame(
            self.frame(), [True, False, False], "sh600000", np.int64(np.iinfo(np.int64).max))
        self.assertEqual(len(result), 1)
        self.assertTrue(np.isnan(result[0]))

    def test_wrong_signal_shape_or_index_fails_explicitly(self):
        for signal in ([True], [[True], [False], [False]],
                       pd.Series([True, False, False], index=[1, 2, 3])):
            with self.subTest(signal=type(signal).__name__):
                with self.assertRaises(ValueError):
                    core.trade_returns_from_frame(self.frame(), signal, "sh600000", 1)

    def test_nonboolean_signals_are_not_truthy_orders(self):
        for value in (1, 0, "False", "True", float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    core.trade_returns_from_frame(
                        self.frame(), [value, False, False], "sh600000", 1)

    def test_explicitly_missing_signal_is_not_an_entry(self):
        for value in (None, pd.NA, float("nan")):
            self.assertEqual(core.trade_returns_from_frame(
                self.frame(), [value, False, False], "sh600000", 1), [])

    def test_final_signal_does_not_invent_next_open(self):
        self.assertEqual(core.trade_returns_from_frame(
            self.frame(), [False, False, True], "sh600000", 1), [])

    def test_short_valid_frames_are_consistent_with_legacy_wrapper(self):
        signal = pd.Series([True, False, False])
        expected = core.trade_returns_from_frame(self.frame(), signal, "sh600000", 1)
        with mock.patch.object(sweep, "load_df", return_value=self.frame()):
            self.assertEqual(sweep.backtest("sh600000", signal, 1), expected)

    def test_empty_and_unpriced_summaries_are_json_safe_and_not_profit(self):
        for values in ([], [np.nan, np.inf, -np.inf]):
            summary = core.describe_trade_returns(values)
            self.assertEqual(summary["priced_count"], 0)
            self.assertEqual(summary["excluded_unpriced_count"], len(values))
            self.assertIsNone(summary["mean_trade_return"])
            self.assertIsNone(summary["portfolio_return"])
            self.assertIsNone(summary["confidence_interval"])
            json.dumps(summary, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
