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
from ledger_app import calibration_status, connect, write_calibration_snapshot  # noqa: E402


def main() -> int:
    with connect() as conn:
        snapshot_id = write_calibration_snapshot(conn, actor="ledger_calibration_worker")
        status = calibration_status(conn)
    print(json.dumps({"snapshot_id": str(snapshot_id), "status": status["status"], "eligible_forecast_count": status["eligible_forecast_count"], "independent_batch_count": status["independent_batch_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
