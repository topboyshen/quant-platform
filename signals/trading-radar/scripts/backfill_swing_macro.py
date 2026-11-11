#!/usr/bin/env python3
"""安全补齐 Swing 当日宏观切片缺失字段。

用于修复历史 `data/swing_macro.db` 中 `total_amount` / `margin_balance` /
`margin_growth_pct` 等字段为空导致 Dashboard 大盘环境过滤器没有具体数字的问题。

实时行情接口不具备历史时点语义，因此本工具只允许补齐“今天”的已有切片，
不会把当前快照伪装成历史数据，也不覆盖已有非空字段。历史缺口必须使用可验证的
历史数据集另行修复。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Iterable

from radar.secrets import merge_env_into_config_data
from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError
from radar.swing.macro_snapshot import MacroSnapshotCollector
from radar.swing.store import MacroSlice, SwingStore

logger = logging.getLogger("backfill_swing_macro")


def _read_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return merge_env_into_config_data(json.load(f))


def _merge_slice(existing: MacroSlice, collected: MacroSlice) -> MacroSlice:
    statuses = (
        (collected.extra or {}).get("collect_status", {})
        if isinstance(collected.extra, dict)
        else {}
    )

    changed_status_factors = set()

    def status_usable(status_factor: str) -> bool:
        status = statuses.get(status_factor, {}) if isinstance(statuses, dict) else {}
        return bool(
            status.get("available") is True
            and not status.get("stale")
            and not status.get("anomalous")
        )

    total_amount = existing.total_amount
    if (
        total_amount is None
        and collected.total_amount is not None
        and status_usable("total_amount")
    ):
        total_amount = collected.total_amount
        changed_status_factors.add("total_amount")

    # 两融余额与增速必须来自同一观测快照；已有半组数据时不做猜测式拼接。
    margin_balance = existing.margin_balance
    margin_growth_pct = existing.margin_growth_pct
    if (
        margin_balance is None
        and margin_growth_pct is None
        and collected.margin_balance is not None
        and collected.margin_growth_pct is not None
        and status_usable("margin_balance")
        and status_usable("margin_growth_pct")
    ):
        margin_balance = collected.margin_balance
        margin_growth_pct = collected.margin_growth_pct
        changed_status_factors.update({"margin_balance", "margin_growth_pct"})

    # 涨停、跌停和平滑比是一个原子口径，禁止 72/20 却配 80/(20+1) 的比值。
    limit_up = existing.limit_up
    limit_down = existing.limit_down
    limit_ratio = existing.limit_ratio
    if (
        limit_up is None
        and limit_down is None
        and limit_ratio is None
        and collected.limit_up is not None
        and collected.limit_down is not None
        and collected.limit_ratio is not None
        and status_usable("limit_ratio")
    ):
        limit_up = collected.limit_up
        limit_down = collected.limit_down
        limit_ratio = collected.limit_ratio
        changed_status_factors.add("limit_ratio")

    values = {
        "total_amount": total_amount,
        "margin_balance": margin_balance,
        "margin_growth_pct": margin_growth_pct,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "limit_ratio": limit_ratio,
    }
    extra = dict(existing.extra or {})
    existing_statuses = extra.get("collect_status", {})
    merged_statuses = dict(existing_statuses) if isinstance(existing_statuses, dict) else {}
    for factor in changed_status_factors:
        if factor in statuses:
            merged_statuses[factor] = statuses[factor]
    if merged_statuses:
        extra["collect_status"] = merged_statuses

    return MacroSlice(
        date=existing.date,
        **values,
        source=(
            "+".join(dict.fromkeys(filter(None, [existing.source, collected.source])))
            if changed_status_factors
            else existing.source
        ),
        extra=extra,
    )


def _changed_fields(before: MacroSlice, after: MacroSlice) -> list[str]:
    fields = ["total_amount", "margin_balance", "margin_growth_pct", "limit_up", "limit_down", "limit_ratio"]
    return [name for name in fields if getattr(before, name) is None and getattr(after, name) is not None]


def _target_slices(store: SwingStore, days: int) -> Iterable[MacroSlice]:
    recent = store.get_recent(days)
    return reversed(recent)


def run(config_path: str, *, days: int = 30, dry_run: bool = False) -> dict:
    cfg = _read_config(config_path)
    swing_cfg = cfg.get("swing", {})
    macro_cfg = swing_cfg.get("macro", {})
    store = SwingStore(swing_cfg.get("store_file", "data/swing_macro.db"))
    collector = MacroSnapshotCollector(
        store,
        logger,
        extreme_low=macro_cfg.get("extreme_percentile_low", 5),
        extreme_high=macro_cfg.get("extreme_percentile_high", 95),
        min_history_days=macro_cfg.get("min_history_days", 20),
    )

    today = SwingStore.today_str()
    summary = {
        "checked": 0,
        "updated": 0,
        "historical_skipped": 0,
        "unusable_skipped": 0,
        "fields": {},
        "dry_run": dry_run,
    }
    for existing in _target_slices(store, days):
        summary["checked"] += 1
        if existing.date != today:
            summary["historical_skipped"] += 1
            logger.warning("%s 是历史切片：实时源无法按该日期回填，已安全跳过", existing.date)
            continue
        collected = collector.collect(date=existing.date, persist=False)
        merged = _merge_slice(existing, collected)
        changed = _changed_fields(existing, merged)
        if not changed:
            if any(
                getattr(existing, field) is None and getattr(collected, field) is not None
                for field in (
                    "total_amount",
                    "margin_balance",
                    "margin_growth_pct",
                    "limit_up",
                    "limit_down",
                    "limit_ratio",
                )
            ):
                summary["unusable_skipped"] += 1
            logger.info("%s 无需回填", existing.date)
            continue
        summary["updated"] += 1
        for field in changed:
            summary["fields"][field] = summary["fields"].get(field, 0) + 1
        logger.info("%s 回填字段: %s", existing.date, ", ".join(changed))
        if not dry_run:
            store.upsert(merged)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 Swing 宏观切片缺失字段")
    parser.add_argument("--config", default="config.swing.json", help="Swing 配置文件路径")
    parser.add_argument("--days", type=int, default=30, help="检查最近 N 条宏观切片")
    parser.add_argument("--dry-run", action="store_true", help="只打印回填计划，不写库")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        lock = SingleInstanceLock("radar-swing-eod").acquire()
    except SingleInstanceLockError as exc:
        raise SystemExit(f"Swing EOD/回填任务已在运行，拒绝并发写库: {exc}") from exc
    summary = run(args.config, days=args.days, dry_run=args.dry_run)
    logger.info("回填汇总: %s", json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
