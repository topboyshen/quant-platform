import math
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from qlib_io import QlibBinFormatError, load_qlib_features, read_qlib_day_bin


class ReadQlibDayBinTests(unittest.TestCase):
    def write_fixture(self, content: bytes) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "close.day.bin"
        path.write_bytes(content)
        return path

    def test_reads_four_byte_header_and_all_payload_values(self):
        path = self.write_fixture(
            struct.pack("<f", 123.0) + struct.pack("<4f", 1.25, -2.5, math.nan, 4.0)
        )

        start, values = read_qlib_day_bin(path)

        self.assertEqual(start, 123)
        self.assertEqual(values.dtype, np.dtype("<f4"))
        np.testing.assert_allclose(values[:2], [1.25, -2.5])
        self.assertTrue(np.isnan(values[2]))
        self.assertEqual(float(values[3]), 4.0)

    def test_rejects_short_headers(self):
        for content in (b"", b"x", b"xx", b"xxx"):
            with self.subTest(length=len(content)):
                path = self.write_fixture(content)
                with self.assertRaisesRegex(QlibBinFormatError, "expected 4 bytes"):
                    read_qlib_day_bin(path)

    def test_rejects_non_finite_start_indexes(self):
        for start in (math.nan, math.inf, -math.inf):
            with self.subTest(start=start):
                path = self.write_fixture(struct.pack("<f", start) + struct.pack("<f", 1.0))
                with self.assertRaisesRegex(QlibBinFormatError, "not finite"):
                    read_qlib_day_bin(path)

    def test_rejects_non_integer_start_index(self):
        path = self.write_fixture(struct.pack("<f", 1.5) + struct.pack("<f", 1.0))

        with self.assertRaisesRegex(QlibBinFormatError, "not an integer"):
            read_qlib_day_bin(path)

    def test_rejects_negative_start_index(self):
        path = self.write_fixture(struct.pack("<f", -1.0) + struct.pack("<f", 1.0))

        with self.assertRaisesRegex(QlibBinFormatError, "negative"):
            read_qlib_day_bin(path)

    def test_rejects_empty_payload(self):
        path = self.write_fixture(struct.pack("<f", 0.0))

        with self.assertRaisesRegex(QlibBinFormatError, "payload is empty"):
            read_qlib_day_bin(path)

    def test_rejects_unaligned_payload(self):
        for content in (b"x", b"xx", b"xxx", b"xxxxx"):
            with self.subTest(length=len(content)):
                path = self.write_fixture(struct.pack("<f", 0.0) + content)
                with self.assertRaisesRegex(QlibBinFormatError, "not divisible by 4"):
                    read_qlib_day_bin(path)


class LoadQlibFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.symbol = "sh600000"
        (self.root / "features" / self.symbol).mkdir(parents=True)

    def write_feature(self, field: str, start: float, values) -> None:
        content = struct.pack("<f", start) + struct.pack(f"<{len(values)}f", *values)
        (self.root / "features" / self.symbol / f"{field}.day.bin").write_bytes(content)

    def test_loads_fields_with_matching_start_and_length(self):
        self.write_feature("close", 10.0, [1.0, 2.0])
        self.write_feature("high", 10.0, [1.5, 2.5])

        start, fields = load_qlib_features(self.root, self.symbol, ("close", "high"))

        self.assertEqual(start, 10)
        self.assertEqual(tuple(fields), ("close", "high"))
        np.testing.assert_allclose(fields["close"], [1.0, 2.0])
        np.testing.assert_allclose(fields["high"], [1.5, 2.5])

    def test_rejects_mismatched_start_indexes(self):
        self.write_feature("close", 10.0, [1.0, 2.0])
        self.write_feature("high", 11.0, [1.5, 2.5])

        with self.assertRaisesRegex(QlibBinFormatError, "start index mismatch"):
            load_qlib_features(self.root, self.symbol, ("close", "high"))

    def test_rejects_mismatched_lengths(self):
        self.write_feature("close", 10.0, [1.0, 2.0])
        self.write_feature("high", 10.0, [1.5])

        with self.assertRaisesRegex(QlibBinFormatError, "length mismatch"):
            load_qlib_features(self.root, self.symbol, ("close", "high"))

    def test_preserves_file_not_found_error(self):
        self.write_feature("close", 10.0, [1.0, 2.0])

        with self.assertRaises(FileNotFoundError):
            load_qlib_features(self.root, self.symbol, ("close", "high"))


if __name__ == "__main__":
    unittest.main()
