import gc
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import warnings

import numpy as np

import daily_snapshot


class QlibMarketJsonTests(unittest.TestCase):
    def test_numpy_feature_values_produce_json_serializable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            qlib_root = Path(directory)
            (qlib_root / "calendars").mkdir()
            (qlib_root / "instruments").mkdir()
            (qlib_root / "calendars" / "day.txt").write_text(
                "2026-08-20\n2026-08-21\n", encoding="utf-8"
            )
            (qlib_root / "instruments" / "all.txt").write_text(
                "SH600000 2026-08-20 2026-08-21\n"
                "SH600001 2026-08-20 2026-08-21\n",
                encoding="utf-8",
            )

            features_by_symbol = {
                "sh600000": {
                    "close": np.array([10.0, 11.0], dtype=np.float32),
                    "high": np.array([10.0, 11.0], dtype=np.float32),
                    "low": np.array([10.0, 10.5], dtype=np.float32),
                    "amount": np.array([1000.0, 2000.0], dtype=np.float32),
                },
                "sh600001": {
                    "close": np.array([10.0, 9.0], dtype=np.float32),
                    "high": np.array([10.0, 9.5], dtype=np.float32),
                    "low": np.array([10.0, 9.0], dtype=np.float32),
                    "amount": np.array([3000.0, 4000.0], dtype=np.float32),
                },
            }

            def fake_load(_root, symbol, fields):
                values = features_by_symbol[symbol]
                return 0, {field: values[field] for field in fields}

            with mock.patch.object(daily_snapshot, "QLIB", str(qlib_root)), mock.patch.object(
                daily_snapshot, "load_qlib_features", side_effect=fake_load
            ):
                with warnings.catch_warnings(record=True) as caught_warnings:
                    warnings.simplefilter("always", ResourceWarning)
                    result = daily_snapshot.qlib_market("2026-08-21")
                    gc.collect()

        resource_warnings = [
            str(item.message)
            for item in caught_warnings
            if issubclass(item.category, ResourceWarning)
        ]

        encoded = json.dumps(result, ensure_ascii=False)

        self.assertEqual(resource_warnings, [])
        self.assertIn('"date": "2026-08-21"', encoded)
        self.assertIsInstance(result["limitUp"][0]["chg"], float)
        self.assertIsInstance(result["limitUp"][0]["close"], float)
        self.assertIsInstance(result["limitDown"][0]["chg"], float)
        self.assertIsInstance(result["limitDown"][0]["close"], float)
        self.assertIsInstance(result["topGainers"][0]["chg"], float)
        self.assertIsInstance(result["topLosers"][0]["chg"], float)
        self.assertIsInstance(result["topAmount"][0]["amount"], float)


if __name__ == "__main__":
    unittest.main()
