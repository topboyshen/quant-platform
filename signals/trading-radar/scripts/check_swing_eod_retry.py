#!/usr/bin/env python3
"""ExecCondition: retry EOD only after same-day data_unavailable.

Does not start a first run, use stale K-lines, or clear a successful audit.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

# Resolve the shared, side-effect-free contract from this checkout, even when
# ExecCondition is invoked from another working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_contracts.eod import retryable_data_failure

BEIJING = ZoneInfo("Asia/Shanghai")


def should_retry_latest_run(
    latest: Optional[Mapping[str, Any]], *, decision_date: Optional[str] = None
) -> bool:
    return retryable_data_failure(latest, decision_date=decision_date)


def _latest_run(store_file: str, decision_date: str) -> Optional[Mapping[str, Any]]:
    from radar.swing.store import SwingStore

    store = SwingStore(store_file)
    rows = store.get_eod_runs_for_date(decision_date)
    return rows[0] if rows else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing EOD same-day data retry gate")
    parser.add_argument("--config", default="/home/ubuntu/trading-radar/config.swing.json")
    parser.add_argument("--now", default="")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(BEIJING)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    now = now.astimezone(BEIJING)
    window_path = Path(__file__).resolve().parent / "check_swing_eod_window.py"
    spec = importlib.util.spec_from_file_location("check_swing_eod_window", window_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load EOD window gate")
    window = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(window)
    if not window.within_eod_catchup_window(now):
        raise SystemExit(1)
    with open(args.config, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    store_file = str(config["swing"]["store_file"])
    latest = _latest_run(store_file, now.date().isoformat())
    raise SystemExit(0 if should_retry_latest_run(latest, decision_date=now.date().isoformat()) else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"EOD retry gate failed closed: {exc}", file=sys.stderr)
        raise SystemExit(1)
