"""Fixed offline equivalence cases for the 62087983 admission semantics.

The reference selection intentionally retains the old full-history algorithm.
It is an independent test oracle, not an alternate production admission path.
"""

from datetime import date, timedelta
import math
from pathlib import Path
import random
import struct
import tempfile
import unittest

import alignment
from conversion import FIELDS
from indices import INDEX_MAP
import tree


SYMBOL = "SH600000"


def normalized(value):
    if isinstance(value, float):
        return value.hex()  # Includes the sign of zero; treats NaNs consistently.
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(item) for item in value]
    return value


def old_selection(values, days):
    """The pre-optimization algorithm, including short-history branch order."""
    length = len(days)
    close_valid = [index for index, value in enumerate(values["close"])
                   if math.isfinite(value) and value > 0]
    complete = [index for index in close_valid
                if all(math.isfinite(values[field][index]) for field in FIELDS)]
    info = dict(new=False, start_index=0, length=length, first_date=days[0], last_date=days[-1],
                old_anchors=[], previous_date=None, last3_valid_dates=[], gap_dates=[])
    if len(complete) < 3 and length <= 10:
        info.update(short_history=True, old_rows=[{"date": days[index],
                    **{field: values[field][index] for field in FIELDS}} for index in range(length)])
    else:
        if len(close_valid) < 3:
            raise ValueError(f"insufficient_mature_valid_history:{SYMBOL}")
        previous, first_anchor, last_anchor = close_valid[-3:]
        if first_anchor not in complete or last_anchor not in complete:
            raise ValueError(f"latest_old_anchors_incomplete:{SYMBOL}")
        info.update(short_history=False, previous_date=days[previous],
                    last3_valid_dates=[days[index] for index in (previous, first_anchor, last_anchor)],
                    old_anchors=[{"date": days[index],
                                 **{field: values[field][index] for field in FIELDS}}
                                 for index in (first_anchor, last_anchor)])
    return {SYMBOL: info}


class HistoryEquivalenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qlib-history-equivalence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "instruments").mkdir()
        self.features = self.root / "features" / SYMBOL.lower()
        self.features.mkdir(parents=True)

    def write(self, values):
        length = len(values["close"])
        days = [(date(2000, 1, 1) + timedelta(days=index)).isoformat() for index in range(length)]
        (self.root / "instruments/all.txt").write_text(f"{SYMBOL}\t{days[0]}\t{days[-1]}\n")
        for field in FIELDS:
            self.features.joinpath(field + ".day.bin").write_bytes(
                struct.pack(f"<{length + 1}f", 0, *values[field]))
        return days

    def ordinary(self, length):
        return {field: [1.0] * length for field in FIELDS}

    def assert_selection_equivalent(self, values):
        days = self.write(values)
        before = {path.name: path.read_bytes() for path in self.features.iterdir()}
        # Decode the physical float32 representation with the old decoder.
        previous_values = {field: tuple(item[0] for item in struct.iter_unpack(
            "<f", before[field + ".day.bin"][4:])) for field in FIELDS}
        try:
            expected = old_selection(previous_values, days)
        except ValueError as error:
            with self.assertRaises(tree.TreeValidationError) as actual:
                tree.inspect_symbols(self.root, [SYMBOL], days)
            self.assertEqual(str(actual.exception), str(error))
        else:
            actual = tree.inspect_symbols(self.root, [SYMBOL], days)
            self.assertEqual(normalized(actual), normalized(expected))
        self.assertEqual({path.name: path.read_bytes() for path in self.features.iterdir()}, before)

    def test_mature_early_incomplete_rows_and_previous_nonclose_fields_unchanged(self):
        values = self.ordinary(3500)
        for field in ("amount", "factor", "volume"):
            values[field][3] = float("nan")
            values[field][-3] = float("inf")
        self.assert_selection_equivalent(values)

    def test_latest_two_anchors_still_reject_instead_of_searching_older_complete_rows(self):
        for index in (-1, -2):
            for field in ("volume", "adjclose", "factor", "change"):
                for value in (float("nan"), float("inf"), float("-inf")):
                    with self.subTest(index=index, field=field, value=value):
                        values = self.ordinary(40)
                        values[field][index] = value
                        self.assert_selection_equivalent(values)

    def test_mature_tail_missing_zero_negative_and_infinite_close_use_same_last_three(self):
        values = self.ordinary(40)
        values["close"][-7:] = [float("nan"), float("inf"), float("-inf"), 0.0, -0.0, -1.0, float("nan")]
        self.assert_selection_equivalent(values)

    def test_mature_insufficient_valid_close_still_rejected(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                values = self.ordinary(40)
                values["close"] = [float("nan")] * 40
                values["close"][:count] = [1.0] * count
                self.assert_selection_equivalent(values)

    def test_short_history_and_ten_eleven_boundary_keep_old_branch(self):
        for length in (1, 2, 3, 4, 9, 10, 11):
            for first_change in (0.0, float("nan")):
                for tail in (1.0, float("nan"), float("inf"), -0.0):
                    with self.subTest(length=length, first_change=first_change, tail=tail):
                        values = self.ordinary(length)
                        values["change"][0] = first_change
                        values["volume"][-1] = tail
                        self.assert_selection_equivalent(values)

    def test_deterministic_mixed_history_decisions_equal_old_implementation(self):
        generator = random.Random(20261015)
        choices = [1.0, 2.5, 0.0, -0.0, -2.0, float("nan"), float("inf"), float("-inf")]
        for sample in range(100):
            length = generator.choice((1, 2, 3, 9, 10, 11, 32, 64))
            values = self.ordinary(length)
            for _ in range(generator.randrange(1, 20)):
                values[generator.choice(FIELDS)][generator.randrange(length)] = generator.choice(choices)
            with self.subTest(sample=sample, length=length):
                self.assert_selection_equivalent(values)

    def test_both_loaders_preserve_float32_special_values_and_original_blobs(self):
        # Signed zero, two NaN payloads, infinities, subnormal and ordinary value.
        payload = bytes.fromhex("00000080 00000000 0100c07f 0200c0ff 0000807f 000080ff 01000000 0000803f")
        days = self.write(self.ordinary(8))
        raw = struct.pack("<f", 0) + payload
        for field in FIELDS:
            self.features.joinpath(field + ".day.bin").write_bytes(raw)
        expected = tuple(item[0] for item in struct.iter_unpack("<f", payload))
        start, length, blobs, actual = tree._load(self.root, SYMBOL, days)
        index_start, index_actual = alignment._load(self.root, SYMBOL, days, (days[0], days[-1]))
        self.assertEqual((start, length, index_start), (0, 8, 0))
        for field in FIELDS:
            self.assertEqual(normalized(actual[field]), normalized(expected))
            self.assertEqual(normalized(index_actual[field]), normalized(expected))
            self.assertEqual(blobs[field], raw)
            self.assertEqual(self.features.joinpath(field + ".day.bin").read_bytes(), raw)

    def test_both_loaders_keep_invalid_binary_and_header_gates(self):
        days = self.write(self.ordinary(3))
        path = self.features / "close.day.bin"
        original = path.read_bytes()
        cases = ((b"", "invalid_feature_binary", "index_member_bad_feature"),
                 (original[:-1], "invalid_feature_binary", "index_member_bad_feature"),
                 (struct.pack("<4f", float("nan"), 1, 1, 1), "invalid_feature_header", "index_member_bad_feature_header"),
                 (struct.pack("<4f", -1, 1, 1, 1), "invalid_feature_header", "index_member_bad_feature_header"),
                 (struct.pack("<4f", 0.5, 1, 1, 1), "invalid_feature_header", "index_member_bad_feature_header"),
                 (struct.pack("<4f", 1, 1, 1, 1), "feature_exceeds_calendar", "index_member_feature_not_at_target"))
        for raw, tree_error, alignment_error in cases:
            with self.subTest(tree_error=tree_error, raw=raw.hex()):
                path.write_bytes(raw)
                with self.assertRaisesRegex(tree.TreeValidationError, tree_error):
                    tree._load(self.root, SYMBOL, days)
                with self.assertRaisesRegex(ValueError, alignment_error):
                    alignment._load(self.root, SYMBOL, days, (days[0], days[-1]))
        path.write_bytes(original)

    def test_alignment_public_admission_still_rejects_nonfinite_quote_and_nonpositive_close(self):
        values = self.ordinary(3)
        values["high"] = [2.0] * 3
        values["low"] = [0.5] * 3
        days = self.write(values)
        for name in INDEX_MAP:
            (self.root / "instruments" / (name + ".txt")).write_text(
                f"{SYMBOL}\t{days[0]}\t{days[-1]}\n")
        self.assertEqual(alignment.validate_index_market_alignment(self.root, days)["status"], "complete")
        for close in (float("nan"), float("inf"), float("-inf"), -0.0, 0.0, -1.0):
            with self.subTest(close=close):
                (self.features / "close.day.bin").write_bytes(struct.pack("<4f", 0, 1, 1, close))
                with self.assertRaisesRegex(ValueError, "index_member_invalid_target_value"):
                    alignment.validate_index_market_alignment(self.root, days)


if __name__ == "__main__":
    unittest.main()
