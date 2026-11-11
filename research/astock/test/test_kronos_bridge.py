from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
import math
from pathlib import Path
import sys
import unittest

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from kronos_bridge import build_forecast, normalize_request, summarize_state


def bars(count: int = 80) -> list[dict[str, float | str]]:
    start = date(2026, 1, 1)
    output = []
    for index in range(count):
        close = 10 + index * 0.05
        output.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close - 0.02,
                "high": close + 0.08,
                "low": close - 0.08,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
                "amount": 10_000_000 + index * 10_000,
            }
        )
    return output


class FakePredictor:
    def predict_batch(self, *, df_list, x_timestamp_list, y_timestamp_list, pred_len, **_kwargs):
        results = []
        for frame, future in zip(df_list, y_timestamp_list):
            last = float(frame.iloc[-1]["close"])
            records = []
            for index in range(pred_len):
                close = last * (1 + 0.01 * (index + 1))
                records.append(
                    {
                        "open": close * 0.995,
                        "high": close * 1.01,
                        "low": close * 0.99,
                        "close": close,
                        "volume": 1_100_000,
                        "amount": 11_000_000,
                    }
                )
            results.append(pd.DataFrame(records, index=future))
        return results


class NoisyPredictor(FakePredictor):
    def __init__(self):
        print("fake weight loader diagnostic")

    def predict_batch(self, **kwargs):
        print("fake predictor diagnostic")
        return super().predict_batch(**kwargs)


class InvalidOhlcvPredictor(FakePredictor):
    def predict_batch(self, **kwargs):
        results = super().predict_batch(**kwargs)
        # Reproduce the observed failure without repairing model prices:
        # one bar has open above high and close below low; two more have
        # negative volume/amount.
        results[0].iloc[0, results[0].columns.get_loc("open")] = 20
        results[0].iloc[0, results[0].columns.get_loc("high")] = 19
        results[0].iloc[0, results[0].columns.get_loc("close")] = 8
        results[0].iloc[0, results[0].columns.get_loc("low")] = 9
        results[0].iloc[1, results[0].columns.get_loc("volume")] = -1
        results[0].iloc[2, results[0].columns.get_loc("amount")] = -1
        return results


def request(**overrides):
    value = {
        "schemaVersion": "1.0",
        "task": "kronos_market_state",
        "lookback": 60,
        "predictionDays": 5,
        "series": [{"symbol": "600875.SH", "bars": bars()}],
    }
    value.update(overrides)
    return value


class KronosBridgeTests(unittest.TestCase):
    def test_normalize_rejects_paths_commands_and_short_history(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            normalize_request({**request(), "modelPath": "/tmp/model"})
        with self.assertRaisesRegex(ValueError, "at least 60 bars"):
            normalize_request(request(series=[{"symbol": "600875.SH", "bars": bars(20)}]))

    def test_build_forecast_uses_fixed_shape_and_labels_model_output(self):
        result = build_forecast(
            request(),
            predictor_factory=FakePredictor,
            future_session_loader=lambda _day, count: (
                [(date(2026, 4, 1) + timedelta(days=index)).isoformat() for index in range(count)],
                "test calendar",
            ),
        )
        self.assertEqual(result["status"], "experimental")
        self.assertEqual(result["model"]["name"], "Kronos-mini")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(result["results"][0]["predictedBars"]), 5)
        self.assertEqual(result["results"][0]["stateSummary"]["state"], "uptrend_candidate")
        self.assertIn("不是行情事实", result["warnings"][0])

    def test_summarize_state_rejects_invalid_model_close(self):
        summary = summarize_state(bars(), [{"close": math.nan, "volume": 1}])
        self.assertEqual(summary["state"], "unavailable")

    def test_invalid_ohlcv_bars_are_counted_excluded_and_cannot_drive_state(self):
        result = build_forecast(
            request(),
            predictor_factory=InvalidOhlcvPredictor,
            future_session_loader=lambda _day, count: (
                [(date(2026, 4, 1) + timedelta(days=index)).isoformat() for index in range(count)],
                "test calendar",
            ),
        )
        forecast = result["results"][0]
        self.assertEqual(forecast["status"], "partial")
        self.assertEqual(forecast["invalidPredictedBars"], 3)
        self.assertEqual(forecast["validPredictedBars"], 2)
        self.assertEqual(len(forecast["predictedBars"]), 2)
        self.assertEqual(forecast["stateSummary"]["state"], "unavailable")
        self.assertNotIn("horizonReturnPercent", forecast["stateSummary"])
        self.assertIn("high_below_open_close_or_low", forecast["validationIssues"])
        self.assertIn("low_above_open_close_or_high", forecast["validationIssues"])
        self.assertIn("negative_volume", forecast["validationIssues"])
        self.assertIn("negative_amount", forecast["validationIssues"])
        self.assertTrue(any("600875.SH" in warning and "安全校验" in warning for warning in result["warnings"]))

    def test_all_invalid_forecast_bars_make_symbol_unavailable(self):
        class AllInvalidPredictor(FakePredictor):
            def predict_batch(self, **kwargs):
                results = super().predict_batch(**kwargs)
                results[0]["volume"] = -1
                return results

        result = build_forecast(
            request(),
            predictor_factory=AllInvalidPredictor,
            future_session_loader=lambda _day, count: (
                [(date(2026, 4, 1) + timedelta(days=index)).isoformat() for index in range(count)],
                "test calendar",
            ),
        )
        forecast = result["results"][0]
        self.assertEqual(forecast["status"], "unavailable")
        self.assertEqual(forecast["invalidPredictedBars"], 5)
        self.assertEqual(forecast["predictedBars"], [])
        self.assertIsNone(forecast["forecastThrough"])
        self.assertEqual(forecast["requestedForecastThrough"], "2026-04-05")
        self.assertNotEqual(forecast["stateSummary"]["state"], "uptrend_candidate")

    def test_model_diagnostics_never_pollute_protocol_stdout(self):
        protocol_stdout = io.StringIO()
        diagnostics = io.StringIO()
        with redirect_stdout(protocol_stdout), redirect_stderr(diagnostics):
            result = build_forecast(
                request(),
                predictor_factory=NoisyPredictor,
                future_session_loader=lambda _day, count: (
                    [(date(2026, 4, 1) + timedelta(days=index)).isoformat() for index in range(count)],
                    "test calendar",
                ),
            )
        self.assertEqual(result["status"], "experimental")
        self.assertEqual(protocol_stdout.getvalue(), "")
        self.assertIn("fake weight loader diagnostic", diagnostics.getvalue())
        self.assertIn("fake predictor diagnostic", diagnostics.getvalue())


if __name__ == "__main__":
    unittest.main()
