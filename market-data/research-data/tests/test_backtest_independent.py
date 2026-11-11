"""Independent regression checks: synthetic arrays only, not profitability tests.

Never execute the application main(), load Qlib data, connect to a database,
or contact a network. These checks do not certify exchange execution, liquidity,
portfolio returns, or an investable edge.
"""
import ast
from contextlib import ExitStack
from pathlib import Path
import socket
import sqlite3
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import backtest as bt
import qlib_io
import strategies as scanner


PAIRS = (
    (scanner.turtle_trade, bt.sig_turtle),
    (scanner.ma_volume, bt.sig_ma_volume),
    (scanner.high_tight_flag, bt.sig_high_tight_flag),
    (scanner.limit_up_shakeout, bt.sig_limit_up_shakeout),
    (scanner.uptrend_limit_down, bt.sig_uptrend_limit_down),
)
BACKTEST_SOURCE = Path(bt.__file__).read_text(encoding="utf-8")


def denied(*args, **kwargs):
    raise AssertionError("Independent tests forbid real data, network and DB access")


def bars(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": np.full(len(close), 100.0),
        "amount": np.full(len(close), 200_000.0),
    })


def positive_fixtures():
    turtle = bars([10.0] * 20 + [12.0])
    ma = bars([10.0] * 15 + [9.0] * 5 + [20.0])
    ma.loc[20, "volume"] = 152.0
    flag = bars([150.0] * 30 + [190.0] * 10)
    flag.loc[:29, "low"] = 100.0
    flag.loc[30:, "high"] = 200.0
    flag.loc[30:, "low"] = 180.0
    flag.loc[39, "volume"] = 59.0
    shakeout = bars([10.0, 11.0, 11.1])
    shakeout.loc[2, ["open", "high", "low", "volume"]] = [11.4, 11.5, 11.0, 201.0]
    down = bars([100.0] * 40 + [120.0] * 20 + [108.0])
    down.loc[60, "volume"] = 201.0
    return (turtle, ma, flag, shakeout, down)


def one_signal(frame):
    signal = pd.Series(False, index=frame.index)
    signal.iloc[10] = True
    return signal


class IndependentBacktestTests(unittest.TestCase):
    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for target, name in (
            (socket.socket, "connect"), (socket.socket, "connect_ex"),
            (socket, "create_connection"), (socket, "getaddrinfo"),
            (sqlite3, "connect"),
            (bt, "load_df"), (bt, "load_qlib_features"), (bt, "read_bin"),
            (scanner, "load_stock"), (scanner, "load_qlib_features"),
            (scanner, "read_bin"),
            (qlib_io, "load_qlib_features"), (qlib_io, "read_qlib_day_bin"),
        ):
            self.guards.enter_context(mock.patch.object(target, name, side_effect=denied))

    def run_frame(self, frame, **kwargs):
        with mock.patch.object(bt, "load_df", return_value=frame):
            return bt.backtest("sh600000", one_signal, **kwargs)

    def assert_all_prefixes_agree(self, frame, scan_fn, vector_fn):
        before = frame.copy(deep=True)
        full = vector_fn(frame).fillna(False)
        self.assertEqual(len(full), len(frame))
        for length in range(1, len(frame) + 1):
            with self.subTest(signal=scan_fn.__name__, prefix_length=length):
                prefix = frame.iloc[:length].copy()
                data = {name: prefix[name].to_numpy(copy=True) for name in prefix}
                scalar = bool(scan_fn(data))
                vector_prefix = vector_fn(prefix).fillna(False)
                self.assertEqual(scalar, bool(vector_prefix.iloc[-1]))
                # Appending future rows must not retroactively change any signal.
                np.testing.assert_array_equal(
                    vector_prefix.to_numpy(), full.iloc[:length].to_numpy())
        pd.testing.assert_frame_equal(frame, before)

    def test_five_signals_agree_at_every_prefix_with_positive_examples(self):
        for (scan_fn, vector_fn), frame in zip(PAIRS, positive_fixtures()):
            with self.subTest(signal=scan_fn.__name__):
                self.assertTrue(bool(vector_fn(frame).iloc[-1]), "fixture must trigger")
                self.assert_all_prefixes_agree(frame, scan_fn, vector_fn)

    def test_five_signals_agree_at_every_prefix_on_seeded_varied_bars(self):
        rng = np.random.default_rng(20260908)
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.002, 0.07, size=150)))
        frame = bars(close)
        frame["open"] = close * np.exp(rng.normal(0.0, 0.035, size=len(close)))
        frame["high"] = frame[["open", "close"]].max(axis=1) * 1.02
        frame["low"] = frame[["open", "close"]].min(axis=1) * 0.98
        frame["volume"] = rng.uniform(20.0, 350.0, size=len(close))
        for scan_fn, vector_fn in PAIRS:
            self.assert_all_prefixes_agree(frame, scan_fn, vector_fn)

    def test_short_warmup_cannot_trigger_ma_or_uptrend(self):
        # Both insufficient-history cases triggered the former scalar scanners.
        ma = bars([10.0] * 14 + [9.0] * 5 + [20.0])
        ma.loc[19, "volume"] = 152.0
        down = bars([100.0] * 39 + [120.0] * 20 + [108.0])
        down.loc[59, "volume"] = 201.0
        for frame, scan_fn, vector_fn in (
            (ma, scanner.ma_volume, bt.sig_ma_volume),
            (down, scanner.uptrend_limit_down, bt.sig_uptrend_limit_down),
        ):
            with self.subTest(signal=scan_fn.__name__):
                data = {name: frame[name].to_numpy() for name in frame}
                self.assertFalse(bool(scan_fn(data)))
                self.assertFalse(bool(vector_fn(frame).iloc[-1]))

    def test_volume_thresholds_use_prior_twenty_rows_and_strict_inequalities(self):
        fixtures = positive_fixtures()
        for pair_index, threshold, triggering in ((1, 150.0, 150.0001),
                                                  (2, 60.0, 59.9999),
                                                  (4, 200.0, 200.0001)):
            scan_fn, vector_fn = PAIRS[pair_index]
            for volume, expected in ((threshold, False), (triggering, True)):
                with self.subTest(signal=scan_fn.__name__, volume=volume):
                    frame = fixtures[pair_index].copy()
                    frame.loc[frame.index[-1], "volume"] = volume
                    data = {name: frame[name].to_numpy() for name in frame}
                    self.assertEqual(bool(scan_fn(data)), expected)
                    self.assertEqual(bool(vector_fn(frame).iloc[-1]), expected)

    def test_each_signal_requires_its_full_history_window(self):
        for (scan_fn, vector_fn), frame, minimum in zip(
            PAIRS, positive_fixtures(), (21, 21, 40, 3, 61)
        ):
            with self.subTest(signal=scan_fn.__name__):
                self.assertEqual(len(frame), minimum)
                self.assertFalse(vector_fn(frame).iloc[:minimum - 1].any())
                self.assertTrue(bool(vector_fn(frame).iloc[-1]))

    def test_buy_day_final_high_low_close_do_not_change_existing_entry(self):
        base = bars([10.0] * 75)
        base["open"] = 10.0
        base.loc[16, "close"] = 10.2
        returns = []
        for high, low, close in ((11.0, 9.8, 11.0), (10.9, 9.8, 10.9),
                                 (11.5, 8.5, 9.0)):
            frame = base.copy()
            frame.loc[11, ["high", "low", "close"]] = [high, low, close]
            result = self.run_frame(frame, hold_days=5)
            self.assertEqual(len(result), 1)
            returns.append(result)
        np.testing.assert_allclose(returns, [returns[0]] * len(returns))

    def test_open_at_or_above_estimated_limit_is_not_assumed_fillable(self):
        for opening in (11.0, 11.01, 12.0):
            with self.subTest(opening=opening):
                frame = bars([10.0] * 75)
                frame.loc[11, ["open", "high", "close"]] = opening
                self.assertEqual(self.run_frame(frame), [])

    def test_invalid_entry_and_previous_prices_are_rejected(self):
        for row, column in ((11, "open"), (10, "close")):
            for invalid in (np.nan, np.inf, -np.inf, 0.0, -1.0):
                with self.subTest(row=row, column=column, invalid=invalid):
                    frame = bars([10.0] * 75)
                    frame.loc[row, column] = invalid
                    self.assertEqual(self.run_frame(frame, hold_days=5), [])

    def test_unpriced_exit_is_retained_and_not_silently_dropped(self):
        for invalid in (np.nan, np.inf, -np.inf, 0.0, -1.0):
            with self.subTest(exit_price=invalid):
                frame = bars([10.0] * 75)
                frame.loc[16, "close"] = invalid
                result = self.run_frame(frame, hold_days=5)
                self.assertEqual(len(result), 1)
                self.assertTrue(np.isnan(result[0]))
                summary = bt.describe_trade_returns(result)
                self.assertEqual(summary["excluded_unpriced_count"], 1)

    def test_hold_days_requires_positive_nonboolean_integer(self):
        for invalid in (0, -1, -100, 1.5, "1", True, False, None, np.nan, np.inf):
            with self.subTest(hold_days=invalid):
                with self.assertRaises(ValueError):
                    self.run_frame(bars([10.0] * 75), hold_days=invalid)

    def test_invalid_costs_cannot_create_valid_observations(self):
        for field in ("commission", "stamp_tax"):
            for invalid in (-0.001, np.nan, np.inf, -np.inf):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        self.run_frame(bars([10.0] * 75), **{field: invalid})

    def test_exit_uses_requested_future_session_and_does_not_wrap(self):
        frame = bars([10.0] * 75)
        frame["open"] = 10.0
        frame.loc[11, "close"] = 11.0
        frame.loc[12, "close"] = 10.2
        # D+1 entry, then one further session before the assumed close exit.
        result = self.run_frame(frame, hold_days=1, commission=0.0, stamp_tax=0.0)
        np.testing.assert_allclose(result, [0.02])
        unfinished = self.run_frame(frame, hold_days=64)
        self.assertEqual(len(unfinished), 1)
        self.assertTrue(np.isnan(unfinished[0]))

    def test_summary_does_not_claim_portfolio_return_or_iid_confidence(self):
        # Simultaneous independent observations cannot be chained as a portfolio.
        result = bt.describe_trade_returns([0.099, 0.099, np.nan])
        self.assertIsNone(result["portfolio_return"])
        self.assertEqual(result["portfolio_status"], "not_modeled")
        self.assertIsNone(result["confidence_interval"])
        self.assertEqual(result["excluded_unpriced_count"], 1)
        self.assertNotIn("cumulative_return", result)
        self.assertNotIn("total_return", result)

    def test_core_report_contains_no_trade_compounding_or_iid_interval(self):
        # Inspect source without executing main() or accessing the instrument list.
        tree = ast.parse(BACKTEST_SOURCE)
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        called = {node.func.attr for node in ast.walk(main)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertTrue({"prod", "cumprod", "sqrt"}.isdisjoint(called))
        print_literals = [part.value for call in ast.walk(main)
                          if isinstance(call, ast.Call)
                          and isinstance(call.func, ast.Name) and call.func.id == "print"
                          for arg in call.args for part in ast.walk(arg)
                          if isinstance(part, ast.Constant) and isinstance(part.value, str)]
        self.assertFalse(any("累计" in value or "±" in value for value in print_literals))


if __name__ == "__main__":
    unittest.main()
