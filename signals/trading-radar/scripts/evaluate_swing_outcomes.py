#!/usr/bin/env python3
"""Collect matured 1/3/5/10/20-session outcomes for live OOS decisions."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from radar.data.market_data_v2 import MarketDataFetcher
from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError
from radar.secrets import merge_env_into_config_data
from radar.swing.outcome_evaluator import OutcomeSettings, SwingOutcomeEvaluator
from radar.swing.outcome_provenance import recorder_from_config
from radar.swing.store import SwingStore


def _config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return merge_env_into_config_data(json.load(handle))


def run(config_path: str, *, as_of: str | None = None, limit: int = 500) -> dict:
    config = _config(config_path)
    swing = config["swing"]
    outcome = swing.get("outcome_evaluation", {})
    logger = logging.getLogger("swing_outcomes")
    store = SwingStore(swing.get("store_file", "data/swing_macro.db"))
    fetcher = MarketDataFetcher(logger, use_akshare=True)
    evaluator = SwingOutcomeEvaluator(
        store,
        fetcher,
        settings=OutcomeSettings(
            benchmark_code=str(outcome.get("benchmark_code", "510300")),
            notional=float(outcome.get("notional", 100_000)),
            commission_rate=float(outcome.get("commission_rate", 0.0003)),
            min_commission=float(outcome.get("min_commission", 5.0)),
            slippage_rate=float(outcome.get("slippage_rate", 0.0005)),
            stamp_duty_rate=float(outcome.get("stamp_duty_rate", 0.0)),
        ),
        logger=logger,
        evidence_recorder=recorder_from_config(swing, config_path=config_path,
            repo=Path(__file__).resolve().parents[1], logger=logger),
    )
    return evaluator.evaluate_pending(as_of=as_of, limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing live OOS outcome collector")
    parser.add_argument("--config", default="config.swing.json")
    parser.add_argument("--as-of", help="只使用该日期及以前可见行情 (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        SingleInstanceLock("radar-swing-outcomes").acquire()
    except SingleInstanceLockError as exc:
        raise SystemExit(f"结果采集任务已在运行: {exc}") from exc
    summary = run(args.config, as_of=args.as_of, limit=args.limit)
    logging.getLogger("swing_outcomes").info(
        "结果采集汇总: %s", json.dumps(summary, ensure_ascii=False, sort_keys=True)
    )


if __name__ == "__main__":
    main()
