#!/usr/bin/env python3
"""Import verified historical market turnover from SSE and SZSE summaries.

This importer never reuses today's quote as an older observation.  Each row is
fetched for its actual trading date from both exchange daily-summary endpoints,
keeps the two components, records conservative availability on the following
calendar day, and is committed with row and batch SHA-256 checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError
from radar.secrets import merge_env_into_config_data
from radar.swing.store import SwingStore

BEIJING = ZoneInfo("Asia/Shanghai")
SOURCE = "sse+szse_official_daily_summary"
SOURCE_REF = (
    "https://www.sse.com.cn/market/stockdata/overview/day/;"
    "https://www.szse.cn/market/overview/index.html"
)
logger = logging.getLogger("swing_factor_history")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value))
    os.replace(temporary, path)


def _read_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return merge_env_into_config_data(json.load(handle))


def _trading_dates(calendar_file: Path, *, end_date: str, sessions: int) -> List[str]:
    payload = json.loads(calendar_file.read_text(encoding="utf-8"))
    values = payload.get("trading_dates") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ValueError("交易日历缺少 trading_dates")
    valid = sorted(
        value
        for value in {str(item) for item in values}
        if len(value) == 10 and value <= end_date
    )
    if len(valid) < sessions:
        raise ValueError(f"交易日历不足 {sessions} 个观测日")
    return valid[-sessions:]


def _finite_positive(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"非法成交额 {value!r}")
    return number


def _fetch_exchange_components(observation_date: str) -> Dict[str, float]:
    import akshare as ak

    compact = observation_date.replace("-", "")
    sse = ak.stock_sse_deal_daily(date=compact)
    sse_rows = sse[sse["单日情况"].astype(str).str.strip() == "成交金额"]
    if len(sse_rows) != 1:
        raise ValueError("上交所每日概况缺少唯一成交金额行")
    sse_yi = _finite_positive(sse_rows.iloc[0]["股票"])

    szse = ak.stock_szse_summary(date=compact)
    szse_rows = szse[szse["证券类别"].astype(str).str.strip() == "股票"]
    if len(szse_rows) != 1:
        raise ValueError("深交所市场总貌缺少唯一股票行")
    szse_yuan = _finite_positive(szse_rows.iloc[0]["成交金额"])
    szse_yi = szse_yuan / 100_000_000
    total_yi = sse_yi + szse_yi
    if not (100 <= sse_yi <= 60_000 and 100 <= szse_yi <= 60_000 and 1_000 <= total_yi <= 80_000):
        raise ValueError(
            f"交易所成交额越界 sse={sse_yi} szse={szse_yi} total={total_yi}"
        )
    return {
        "sse_stock_amount_yi": round(sse_yi, 6),
        "szse_stock_amount_yuan": round(szse_yuan, 2),
        "szse_stock_amount_yi": round(szse_yi, 6),
        "total_amount_yi": round(total_yi, 2),
    }


def _observation(observation_date: str, components: Dict[str, float]) -> Dict[str, Any]:
    day = date.fromisoformat(observation_date)
    source_market_time = datetime.combine(day, time(15, 0), tzinfo=BEIJING)
    # The exact historical publication minute is not asserted.  Using next
    # midnight is deliberately conservative for replay: the official summary
    # can inform later decisions, never the same day's 15:30 decision.
    available_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=BEIJING)
    core = {
        "factor_name": "total_amount",
        "observation_date": observation_date,
        "value": components["total_amount_yi"],
        "source": SOURCE,
        "source_ref": SOURCE_REF,
        "source_market_time": source_market_time.isoformat(),
        "available_at": available_at.isoformat(),
        "components": components,
    }
    return {**core, "checksum": _sha256(core)}


def _verify_cached_observation(
    observation_date: str, item: Any
) -> Dict[str, Any]:
    if not isinstance(item, dict) or not isinstance(item.get("components"), dict):
        raise ValueError("缓存观测结构非法")
    expected = _observation(observation_date, item["components"])
    if _canonical(item) != _canonical(expected):
        raise ValueError("缓存观测内容或 SHA-256 不一致")
    return expected


def run(
    config_path: str,
    *,
    sessions: int = 250,
    minimum: int = 120,
    end_date: str | None = None,
    delay_seconds: float = 0.4,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if sessions < minimum or minimum < 120:
        raise ValueError("可信历史必须至少要求 120 个交易日")
    config = _read_config(config_path)
    swing = config["swing"]
    base_dir = Path(config_path).resolve().parent
    calendar_file = base_dir / "data" / "trading_calendar.json"
    if end_date is None:
        now = datetime.now(BEIJING)
        candidate = now.date() if now.time() >= time(18, 0) else now.date() - timedelta(days=1)
        end_date = candidate.isoformat()
    date.fromisoformat(end_date)
    dates = _trading_dates(calendar_file, end_date=end_date, sessions=sessions)
    work_file = (
        base_dir
        / "data"
        / "factor_import_work"
        / f"official_total_amount_{dates[0]}_{dates[-1]}.json"
    )
    cached: Dict[str, Any] = {}
    if work_file.exists():
        payload = json.loads(work_file.read_text(encoding="utf-8"))
        if payload.get("source") == SOURCE and isinstance(payload.get("observations"), dict):
            for cached_date, item in payload["observations"].items():
                try:
                    cached[cached_date] = _verify_cached_observation(cached_date, item)
                except (TypeError, ValueError) as exc:
                    logger.warning("忽略损坏的官方成交额缓存 %s: %s", cached_date, exc)

    failures: Dict[str, str] = {}
    for index, observation_date in enumerate(dates, start=1):
        if observation_date in cached:
            continue
        last_error = ""
        for attempt in range(3):
            try:
                components = _fetch_exchange_components(observation_date)
                cached[observation_date] = _observation(observation_date, components)
                _atomic_json(
                    work_file,
                    {"source": SOURCE, "source_ref": SOURCE_REF, "observations": cached},
                )
                logger.info(
                    "官方成交额 %s = %.2f亿元 (%d/%d)",
                    observation_date,
                    components["total_amount_yi"],
                    index,
                    len(dates),
                )
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    time_module.sleep(max(1.0, delay_seconds * (attempt + 2)))
        if last_error:
            failures[observation_date] = last_error[:300]
            logger.error("官方成交额 %s 获取失败: %s", observation_date, last_error)
        time_module.sleep(max(0.0, delay_seconds))

    observations = [cached[item] for item in dates if item in cached]
    if len(observations) < minimum:
        raise RuntimeError(
            f"可信历史仅 {len(observations)}/{minimum} 条，拒绝写库；失败 {len(failures)} 天"
        )
    if len(observations) != sessions:
        raise RuntimeError(
            f"请求 {sessions} 条但仅完整取得 {len(observations)} 条，拒绝部分批次写库"
        )

    batch_id = uuid.uuid4().hex
    observed_at = datetime.now(BEIJING).isoformat()
    batch_core = {
        "factor_name": "total_amount",
        "source": SOURCE,
        "source_ref": SOURCE_REF,
        "requested_start": dates[0],
        "requested_end": dates[-1],
        "observed_at": observed_at,
        "record_count": len(observations),
        "validation_status": "accepted",
        "observations": observations,
    }
    batch_checksum = _sha256(observations)
    batch = {
        **{key: value for key, value in batch_core.items() if key != "observations"},
        "batch_id": batch_id,
        "checksum": batch_checksum,
        "details": {
            "unit": "CNY 100 million",
            "component_units": {"sse": "CNY 100 million", "szse": "CNY"},
            "availability_policy": "next_calendar_day_00:00_Asia/Shanghai",
            "failures": failures,
        },
    }
    artifact = base_dir / "data" / "factor_import_batches" / f"{batch_id}.json"
    summary: Dict[str, Any] = {
        "batch_id": batch_id,
        "record_count": len(observations),
        "checksum": batch_checksum,
        "start": dates[0],
        "end": dates[-1],
        "failures": len(failures),
        "dry_run": dry_run,
    }
    if dry_run:
        return summary
    _atomic_json(artifact, {"batch": batch, "observations": observations})
    store = SwingStore(swing.get("store_file", "data/swing_macro.db"))
    summary.update(
        store.record_factor_import(batch=batch, observations=observations)
    )
    summary["artifact"] = str(artifact)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="导入交易所官方历史成交额")
    parser.add_argument("--config", default="config.swing.json")
    parser.add_argument("--sessions", type=int, default=250)
    parser.add_argument("--minimum", type=int, default=120)
    parser.add_argument("--end-date")
    parser.add_argument("--delay-seconds", type=float, default=0.4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        _lock = SingleInstanceLock("radar-swing-factor-history").acquire()
    except SingleInstanceLockError as exc:
        raise SystemExit(f"可信历史导入任务已在运行: {exc}") from exc
    summary = run(
        args.config,
        sessions=args.sessions,
        minimum=args.minimum,
        end_date=args.end_date,
        delay_seconds=args.delay_seconds,
        dry_run=args.dry_run,
    )
    logger.info("可信历史导入汇总: %s", json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
