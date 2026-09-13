"""Pure in-memory legacy entry-point and signal-drift regressions."""
import unittest
from unittest import mock
import numpy as np
import pandas as pd
import backtest as core
import backtest_sweep as sweep
import backtest_benchmark as benchmark
import backtest_oos as oos


class LegacyGuardTests(unittest.TestCase):
    def test_duplicate_signals_are_canonical_aliases(self):
        for module in (sweep, benchmark):
            self.assertIs(module.sig_ma_volume, core.sig_ma_volume)
        for module in (sweep, benchmark, oos):
            self.assertIs(module.sig_high_tight_flag, core.sig_high_tight_flag)

    def test_sweep_trade_helper_uses_the_same_opening_decision(self):
        signal = pd.Series([True] + [False] * 59)
        frame = pd.DataFrame({k: np.full(60, 10.)
                              for k in ("open", "close", "high", "low")})
        frame.loc[6, "close"] = 10.2
        values = []
        for late_value in (10.9, 11.):
            changed = frame.copy()
            changed.loc[1, ["close", "high", "low"]] = late_value
            with mock.patch.object(sweep, "load_df", return_value=changed):
                result = sweep.backtest("sh600000", signal, 5)
            expected = core.trade_returns_from_frame(changed, signal, "sh600000", 5)
            self.assertEqual(result, expected)
            values.append(result)
        self.assertEqual(values[0], values[1])
        self.assertEqual(len(values[0]), 1)

    def test_parameter_sweep_stops_before_iterating_real_inputs(self):
        with self.assertRaisesRegex(RuntimeError, "protocol"):
            sweep.sweep("unused", mock.Mock(), object(), object())

    def test_mismatched_benchmark_fails_without_fetching(self):
        with self.assertRaisesRegex(RuntimeError, "matched"):
            benchmark.backtest_benchmark("sh600000", mock.Mock(), 5, {}, [])
        with self.assertRaisesRegex(RuntimeError, "protocol"):
            benchmark.get_index()

    def test_unregistered_oos_fails_without_fetching(self):
        with self.assertRaisesRegex(RuntimeError, "pre-registered"):
            oos.backtest_oos("sh600000", 5, {}, [], 20)
        with self.assertRaisesRegex(RuntimeError, "pre-registered"):
            oos.get_index()

    def test_invalid_sweep_parameters_fail_before_data_load(self):
        with mock.patch.object(sweep, "load_df") as loader:
            with self.assertRaises(ValueError):
                sweep.backtest("sh600000", pd.Series(dtype=bool), 0)
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
