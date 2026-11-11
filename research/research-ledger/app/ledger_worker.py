#!/usr/bin/env python3
"""Deterministic Research Ledger worker: calibration snapshot only.

This worker cannot issue approvals, activate models, send messages, fetch data, or write to
external systems. It snapshots only sealed, prospective, independently reviewed Ledger data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger_app import calibration_status, connect, now_utc, write_calibration_snapshot  # noqa: E402


def main() -> int:
    with connect() as conn:
        computed_at = now_utc()
        status = calibration_status(conn, computed_at)
        snapshot_id = write_calibration_snapshot(
            conn,
            actor="ledger_calibration_worker",
            payload=status,
            computed_at=computed_at,
        )
    print(json.dumps({
        "snapshot_id": str(snapshot_id),
        "status": status["status"],
        "maturity_reason_code": status["maturity_reason_code"],
        "raw_due_forecast_count": status["raw_cohort"]["due_forecast_count"],
        "raw_due_independent_batch_count": status["raw_cohort"]["independent_batch_count"],
        "pending_review_count": status["pending_review_count"],
        "eligible_forecast_count": status["eligible_forecast_count"],
        "independent_batch_count": status["independent_batch_count"],
        "review_coverage_rate": status["review_coverage_rate"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
