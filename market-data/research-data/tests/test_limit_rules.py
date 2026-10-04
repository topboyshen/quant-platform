"""Limit-up/limit-down must use raw prices, factor and board rules (fixed samples)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qlib_summary  # noqa: E402

f32 = np.float32


def build(symbols, dates, start_dates=None):
    """symbols: {sym: {field: [values...]}} aligned to dates; returns (root, loader)."""
    directory = tempfile.mkdtemp(prefix="limit-rules-")
    root = Path(directory) / "cn_data"
    (root / "calendars").mkdir(parents=True)
    (root / "instruments").mkdir()
    (root.parent / ".tag").write_text("t\n", encoding="utf-8")
    (root / "calendars" / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
    start_dates = start_dates or {}
    (root / "instruments" / "all.txt").write_text("".join(
        f"{s.upper()}\t{start_dates.get(s, dates[0])}\t{dates[-1]}\n" for s in symbols), encoding="utf-8")

    def loader(_root, symbol, fields):
        values = symbols[symbol]
        missing = [f for f in fields if f not in values]
        if missing:
            raise FileNotFoundError(missing)
        offset = dates.index(start_dates.get(symbol, dates[0]))
        return offset, {f: np.array(values[f], dtype=np.float32) for f in fields}
    return str(root), loader


def bar(raw_prev, raw_close, raw_high=None, raw_low=None, factor=0.5, factor_prev=None):
    factor_prev = factor if factor_prev is None else factor_prev
    raw_high = raw_close if raw_high is None else raw_high
    raw_low = raw_close if raw_low is None else raw_low
    return {
        "close": [raw_prev * factor_prev, raw_close * factor],
        "high": [raw_prev * factor_prev, raw_high * factor],
        "low": [raw_prev * factor_prev, raw_low * factor],
        "amount": [1.0, 1.0],
        "factor": [factor_prev, factor],
    }


def run(symbols, dates=("2026-06-10", "2026-06-11"), **kw):
    root, loader = build(symbols, list(dates), kw.pop("start_dates", None))
    gen = {"tag": "t", "inode": 1}
    return qlib_summary.qlib_market(dates[-1], qlib_root=root, min_eligible=1, min_coverage=0.0,
                                    feature_loader=loader, generation_reader=lambda: gen, **kw)


def syms(result, key):
    return {item["sym"] for item in result[key]}


class FixedSampleRegression(unittest.TestCase):
    def test_sh600421_2026_06_11_is_neither_limit_up_nor_down(self):
        # Exact float32 values from production Qlib (normalized scale ~0.039).
        sample = {"sh600421": {
            "close": [0.039245281368494034, 0.039245281368494034],
            "high": [0.042152341455221176, 0.040698811411857605],
            "low": [0.039245281368494034, 0.03779175505042076],
            "amount": [1.0, 1.0],
            "factor": [0.14535290002822876, 0.14535290002822876],
        }}
        result = run(sample)
        self.assertNotIn("sh600421", syms(result, "limitUp"))
        self.assertNotIn("sh600421", syms(result, "limitDown"))

    def test_sh600599_2026_06_04_not_double_counted(self):
        sample = {"sh600599": {
            "close": [0.04060538858175278, 0.039128828793764114],
            "high": [0.042229603976011276, 0.0415743812918663],
            "low": [0.03898117318749428, 0.037498462945222855],
            "amount": [1.0, 1.0],
            "factor": [0.08121077716350555, 0.08151839673519135],
        }}
        result = run(sample, dates=("2026-06-03", "2026-06-04"))
        self.assertFalse(syms(result, "limitUp") & syms(result, "limitDown"))
        self.assertNotIn("sh600599", syms(result, "limitUp") | syms(result, "limitDown"))

    def test_never_both_on_small_normalized_scale(self):
        # A flat day on a tiny normalized scale used to satisfy both tolerances.
        result = run({"sh600001": bar(0.30, 0.30, 0.31, 0.29, factor=0.01)})
        self.assertEqual(result["limitUpCount"], 0)
        self.assertEqual(result["limitDownCount"], 0)


class BoardRules(unittest.TestCase):
    def test_main_board_ten_percent_proven_on_raw_prices(self):
        result = run({"sh600000": bar(10.00, 11.00, factor=0.37)})
        self.assertEqual(syms(result, "limitUp"), {"sh600000"})
        self.assertEqual(result["limitStatus"]["unverified"], 0)

    def test_main_board_five_percent_is_unverified_without_st_status(self):
        result = run({"sh600000": bar(10.00, 10.50)})
        self.assertEqual(result["limitUpCount"], 0)
        self.assertEqual(result["limitStatus"]["unverified"], 1)
        self.assertEqual(result["limitStatus"]["unverifiedReasons"], {"st_status_unknown": 1})
        self.assertFalse(result["limitStatus"]["complete"])

    def test_known_st_five_percent_is_counted(self):
        result = run({"sh600000": bar(10.00, 9.50)}, st_symbols={"sh600000"})
        self.assertEqual(syms(result, "limitDown"), {"sh600000"})

    def test_chinext_requires_twenty_percent(self):
        result = run({"sz300001": bar(10.00, 11.00), "sz300002": bar(10.00, 12.00)})
        self.assertEqual(syms(result, "limitUp"), {"sz300002"})

    def test_star_market_twenty_percent_down(self):
        result = run({"sh688001": bar(10.00, 8.00)})
        self.assertEqual(syms(result, "limitDown"), {"sh688001"})

    def test_beijing_thirty_percent(self):
        result = run({"bj830001": bar(10.00, 13.00), "bj830002": bar(10.00, 12.00)})
        self.assertEqual(syms(result, "limitUp"), {"bj830001"})

    def test_new_listing_no_limit_days_excluded(self):
        # Calendar starts before the listing; listed 2026-06-08, evaluated on its 4th day.
        dates = ("2026-06-04", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11")
        values = {k: [v[0]] * 3 + [v[1]] for k, v in bar(10.0, 11.0).items()}
        result = run({"sh603999": values}, dates=dates, start_dates={"sh603999": "2026-06-08"})
        self.assertEqual(result["limitUpCount"], 0)
        self.assertEqual(result["limitStatus"]["excludedNoLimit"], 1)

    def test_half_up_rounding_of_limit_price(self):
        # 10.05 * 1.1 = 11.055 -> exchange price 11.06 (round half up).
        result = run({"sh600000": bar(10.05, 11.06)})
        self.assertEqual(syms(result, "limitUp"), {"sh600000"})

    def test_penny_price_where_both_rules_coincide_is_proven(self):
        # 0.10*1.05=0.105->0.11 and 0.10*1.10=0.11: same price, ST status irrelevant.
        result = run({"sh600000": bar(0.10, 0.11)})
        self.assertEqual(syms(result, "limitUp"), {"sh600000"})


class UnprovableInputs(unittest.TestCase):
    def test_missing_factor_marks_unavailable_not_guess(self):
        values = bar(10.0, 11.0)
        del values["factor"]
        result = run({"sh600000": values})
        self.assertEqual(result["limitUpCount"], 0)
        self.assertEqual(result["limitStatus"]["unverifiedReasons"], {"factor_unavailable": 1})
        self.assertFalse(result["limitStatus"]["complete"])
        self.assertEqual(result["coverage"]["valid"], 1, "breadth coverage must not change")

    def test_ex_rights_reference_disagreement_is_unverified(self):
        # factor moved 5% overnight (ex-rights): raw prev 10.00, adjusted-basis ref 9.52.
        # 10.47 (=9.52*1.1) is the limit only under the adjusted reference -> cannot be proven.
        result = run({"sh600000": bar(10.00, 10.47, factor=0.525, factor_prev=0.5)})
        self.assertEqual(result["limitUpCount"], 0)
        self.assertEqual(result["limitStatus"]["unverifiedReasons"], {"ex_rights_reference_unproven": 1})

    def test_ex_rights_bar_that_excludes_other_reference_is_proven(self):
        # +10% on the raw reference is impossible under the 9.52 reference: proven.
        result = run({"sh600000": bar(10.00, 11.00, factor=0.525, factor_prev=0.5)})
        self.assertEqual(syms(result, "limitUp"), {"sh600000"})

    def test_status_basis_is_declared(self):
        result = run({"sh600000": bar(10.0, 10.1)})
        self.assertEqual(result["limitStatus"]["basis"], "raw_price_board_rules_v1")
        self.assertTrue(result["limitStatus"]["complete"])


if __name__ == "__main__":
    unittest.main()
