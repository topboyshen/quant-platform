"""Unit tests for the same-day EOD data_unavailable retry gate."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_swing_eod_retry", ROOT / "scripts" / "check_swing_eod_retry.py"
)
assert SPEC is not None and SPEC.loader is not None
retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retry)
should_retry_latest_run = retry.should_retry_latest_run


class RetryGateTests(unittest.TestCase):
    def test_missing_run_does_not_invent_a_first_attempt(self):
        self.assertFalse(should_retry_latest_run(None))

    def test_failed_data_unavailable_is_retryable(self):
        self.assertTrue(
            should_retry_latest_run(
                {"status": "failed", "decision_outcome": "data_unavailable"}
            )
        )

    def test_success_and_partial_are_not_retried(self):
        self.assertFalse(
            should_retry_latest_run({"status": "success", "decision_outcome": "no_signal"})
        )
        self.assertFalse(
            should_retry_latest_run(
                {"status": "partial", "decision_outcome": "data_unavailable"}
            )
        )

    def test_running_is_not_retried(self):
        self.assertFalse(
            should_retry_latest_run(
                {"status": "running", "decision_outcome": "data_unavailable"}
            )
        )


if __name__ == "__main__":
    unittest.main()
