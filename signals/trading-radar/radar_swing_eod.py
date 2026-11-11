#!/usr/bin/env python3
"""趋势波段 EOD 核心入口 (EOD = End Of Day)。

这是 ETF 趋势波段系统的收盘后机械决策入口，而不是盘中盯盘进程。
每个交易日 15:30（Asia/Shanghai）由 cron 触发一次，脚本内部再用
交易日守卫过滤节假日：

1. 采集全市场宏观情绪切片（成交额 / 两融增速 / 涨跌停比）并入库
2. 遍历 config.swing.json 中的宽基/行业 ETF，确认周线 MACD 金叉与日线放量突破 MA60
3. 用历史 5%/95% 分位识别情绪冰点/沸点，只在趋势确立或情绪极值时输出事实

用法：
    python radar_swing_eod.py [--config config.swing.json] [--dry-run] [--force]

--dry-run : 只计算与日志，不写宏观库/信号库/风控状态，也不推送 Telegram
--force   : 非交易日也强制运行（默认非交易日直接跳过）
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from radar.config_schema import validate_swing_config_dict
from radar.core.scan_scheduler import ScanScheduler
from radar.data.market_data_v2 import MarketDataFetcher
from radar.notify.outbox import DurableOutboxNotifier
from radar.notify.telegram import TelegramNotifier
from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError
from radar.swing.macro_snapshot import MacroSnapshotCollector
from radar.swing.reporter import SwingReporter
from radar.swing.store import SwingStore
from radar.swing.trend_strategy import TrendConfirmStrategy

_GROWTH_RISK_SECTORS = {"成长", "硬科技", "港股科技"}


def _is_growth_risk_target(target: Dict) -> bool:
    sector = str(target.get("sector", ""))
    name = str(target.get("name", ""))
    return sector in _GROWTH_RISK_SECTORS or any(
        keyword in name for keyword in ("半导体", "芯片", "科技", "创业", "科创")
    )


def _apply_eod_risk_gate(signals: List, targets: List[Dict], risk_state: Optional[Dict], logger) -> tuple[List, List]:
    """Apply the existing macro-risk policy to bullish Swing entry signals.

    Swing's two trend confirmations are entry signals.  A missing/invalid risk
    state therefore fails closed; exits are not represented in this signal set.
    """
    target_by_code = {str(target.get("code", "")): target for target in targets}
    level = str((risk_state or {}).get("level", "unavailable")).lower()
    allowed = []
    blocked = []
    for signal in signals:
        target = target_by_code.get(str(getattr(signal, "code", "")), {})
        should_block = level in {"critical", "warning", "unavailable"}
        if level == "growth_blocked":
            should_block = _is_growth_risk_target(target)
        elif level == "ok":
            should_block = False
        elif level not in {"critical", "warning", "unavailable"}:
            should_block = True
        if should_block:
            blocked.append(signal)
            logger.warning(
                "[SwingRiskGate] %s 拦截新增趋势买入 %s(%s): %s",
                level,
                getattr(signal, "name", ""),
                getattr(signal, "code", ""),
                (risk_state or {}).get("message", "风控状态缺失或未知"),
            )
        else:
            allowed.append(signal)
    return allowed, blocked


def _load_eod_risk_state(
    config_path: str,
    swing_cfg: Dict,
    logger,
    *,
    persist_refresh: bool = True,
) -> Optional[Dict]:
    """Read the Monitor-published state from the shared data volume, then refresh if stale."""
    gate_cfg = swing_cfg.get("risk_gate", {})
    if not gate_cfg.get("enabled", False):
        return {"level": "ok", "message": "Swing EOD 风控闸门未启用"}

    from radar.risk.macro_risk_control import MacroRiskControl

    base_dir = Path(config_path).resolve().parent
    state_path = Path(gate_cfg.get("state_file", "data/radar_risk_state.json"))
    override_path = Path(gate_cfg.get("override_file", "data/radar_risk_override.json"))
    if not state_path.is_absolute():
        state_path = base_dir / state_path
    if not override_path.is_absolute():
        override_path = base_dir / override_path
    MacroRiskControl._STATE_FILE = state_path
    MacroRiskControl._OVERRIDE_FILE = override_path

    state = MacroRiskControl.load_state_from_file()
    if state is not None:
        return state

    logger.warning("[SwingRiskGate] 共享风控状态缺失/过期，执行一次现场刷新: %s", state_path)
    try:
        control = MacroRiskControl(logger_=logger)
        refreshed = control.check_with_fetch()
        if persist_refresh:
            control.save_state(refreshed)
        return refreshed.to_dict()
    except Exception as exc:
        logger.error("[SwingRiskGate] 风控状态刷新失败，新增趋势买入将保守拦截: %s", exc)
        return None


def _read_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    from radar.secrets import merge_env_into_config_data

    return merge_env_into_config_data(data)


def _setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("radar_swing")
    logger.setLevel(level)
    if logger.handlers:
        return logger

    from radar.observability import build_log_formatter

    formatter = build_log_formatter()

    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger


def _check_runtime_dependencies(logger: logging.Logger, store_file: str) -> dict:
    """检查 Swing EOD 关键运行依赖，缺失时给出可执行修复建议。"""
    status = {
        "akshare": importlib.util.find_spec("akshare") is not None,
        "requests": importlib.util.find_spec("requests") is not None,
        "store_writable": True,
    }
    if not status["akshare"]:
        logger.error(
            "Swing EOD dependency check failed: akshare missing. Run: %s -m pip install akshare",
            sys.executable,
        )
    if not status["requests"]:
        logger.error(
            "Swing EOD dependency check failed: requests missing. Run: %s -m pip install requests",
            sys.executable,
        )
    try:
        store_dir = os.path.dirname(os.path.abspath(store_file)) or "."
        os.makedirs(store_dir, exist_ok=True)
        probe = os.path.join(store_dir, ".swing_write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as exc:
        status["store_writable"] = False
        logger.error("Swing EOD dependency check failed: store directory not writable: %s", exc)
    return status


def _capture_trigger_diagnostics(strategy, code: str) -> Optional[Dict]:
    """Snapshot bounded finite JSON; supplementary evidence never gates business."""
    try:
        value = getattr(strategy, "last_diagnostics", None)
        if not isinstance(value, dict) or value.get("code") != code:
            return None
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if len(encoded) > 12000:
            return None
        return json.loads(encoded)
    except Exception:
        return None


def _run_core(
    config_path: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    audit_run_id: Optional[str] = None,
    frozen_data: Optional[Dict] = None,
) -> Dict:
    # run() freezes the same in-memory configuration for execution and attribution.
    data = frozen_data if frozen_data is not None else _read_config(config_path)
    validate_swing_config_dict(data)

    swing_cfg = data["swing"]
    logger = _setup_logging(swing_cfg.get("log_file", "radar_swing.log"))
    audit_reason_codes: List[str] = []
    logger.info(
        ">>> Radar Swing EOD 核心入口启动 <<< config=%s dry_run=%s force=%s",
        config_path,
        dry_run,
        force,
    )

    # 交易日守卫
    scheduler = ScanScheduler(data["market"], logger)
    if not scheduler.is_trading_day() and not force:
        logger.info("今日非交易日，跳过（如需强制运行加 --force）")
        return {"skipped": True, "reason": "non_trading_day"}

    macro_cfg = swing_cfg.get("macro", {})
    runtime_status = _check_runtime_dependencies(logger, swing_cfg.get("store_file", "data/swing_macro.db"))
    store = SwingStore(swing_cfg.get("store_file", "data/swing_macro.db"))
    collector = MacroSnapshotCollector(
        store,
        logger,
        extreme_low=macro_cfg.get("extreme_percentile_low", 5),
        extreme_high=macro_cfg.get("extreme_percentile_high", 95),
        min_history_days=macro_cfg.get("min_history_days", 20),
    )

    trend_targets = [t for t in data["targets"] if t.get("mode", "trend") == "trend"]
    logger.info(
        "核心纪律: %s EOD 决策 | 趋势ETF=%d | MA=%s | 放量阈值=%.2fx | 情绪极值=%s%%/%s%%",
        swing_cfg.get("decision_time", "15:30 Asia/Shanghai"),
        len(trend_targets),
        swing_cfg.get("ma_periods", [20, 60]),
        float(swing_cfg.get("volume_breakout_ratio", 1.5)),
        macro_cfg.get("extreme_percentile_low", 5),
        macro_cfg.get("extreme_percentile_high", 95),
    )

    # 1. 宏观情绪切片
    macro_slice = collector.collect(persist=not dry_run)
    if not dry_run:
        persisted_slice = store.get_latest()
        if persisted_slice is None or persisted_slice.date != macro_slice.date:
            audit_reason_codes.append("macro_persist_failed")
    extremes = collector.detect_extremes(macro_slice)
    collect_status = (
        collector.get_collect_status()
        if hasattr(collector, "get_collect_status")
        else {}
    )

    risk_state = _load_eod_risk_state(
        config_path,
        swing_cfg,
        logger,
        persist_refresh=not dry_run,
    )
    decision_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    risk_snapshot_id = None
    if audit_run_id and not dry_run:
        risk_snapshot_id = store.record_risk_snapshot(
            audit_run_id,
            decision_at=decision_at,
            risk_state=risk_state,
        )
        total_status = collect_status.get("total_amount", {})
        if (
            macro_slice.total_amount is not None
            and total_status.get("available") is True
            and not total_status.get("stale")
            and not total_status.get("anomalous")
        ):
            observed_date = str(total_status.get("observed_date") or macro_slice.date)
            source = str(total_status.get("source") or macro_slice.source or "unknown")
            source_refs = {
                "tencent": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                "akshare": "akshare.stock_zh_a_spot_em",
                "eastmoney": "https://push2.eastmoney.com/api/qt/clist/get",
            }
            observation_core = {
                "factor_name": "total_amount",
                "observation_date": observed_date,
                "value": float(macro_slice.total_amount),
                "source": source,
                "source_ref": source_refs.get(source, source),
                "source_market_time": f"{observed_date}T15:00:00+08:00",
                "available_at": decision_at,
                "components": {"collection_status": total_status},
            }
            observation = {
                **observation_core,
                "checksum": hashlib.sha256(
                    json.dumps(
                        observation_core,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            batch_checksum = hashlib.sha256(
                json.dumps(
                    [observation],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            try:
                store.record_factor_import(
                    batch={
                        "batch_id": uuid.uuid4().hex,
                        "factor_name": "total_amount",
                        "source": source,
                        "source_ref": source_refs.get(source, source),
                        "requested_start": observed_date,
                        "requested_end": observed_date,
                        "observed_at": decision_at,
                        "record_count": 1,
                        "checksum": batch_checksum,
                        "validation_status": "accepted",
                        "details": {"live_eod_observation": True},
                    },
                    observations=[observation],
                    apply_to_macro_slice=False,
                )
            except Exception as exc:
                audit_reason_codes.append("factor_provenance_failed")
                logger.error("当日成交额来源证据写入失败: %s", exc)

    # 2. ETF 趋势确认
    fetcher = MarketDataFetcher(logger, use_akshare=True)
    strategy = TrendConfirmStrategy(
        fetcher,
        logger,
        ma_periods=swing_cfg.get("ma_periods", [20, 60]),
        volume_breakout_ratio=swing_cfg.get("volume_breakout_ratio", 1.5),
        history_days=swing_cfg.get("history_days", 250),
        required_data_date=SwingStore.today_str(),
        daily_breakout_enabled=bool((swing_cfg.get("technical") or {}).get("daily_volume_breakout", True)),
        volume_window=int((swing_cfg.get("technical") or {}).get("volume_window", 20)),
    )

    signals: List = []
    trend_data_ok = 0
    trend_data_failed: List[str] = []
    target_evaluations: List[Dict] = []
    for t in trend_targets:
        code = t["code"]
        name = t.get("name", code)
        target_signals: List = []
        try:
            target_signals = strategy.evaluate(code, name)
            signals.extend(target_signals)
            if strategy.last_data_ok:
                trend_data_ok += 1
            else:
                trend_data_failed.append(f"{name}({code})")
        except Exception as e:
            trend_data_failed.append(f"{name}({code})")
            logger.error("趋势评估 %s 失败: %s", code, e)
        target_evaluations.append(
            {
                "target": t,
                "signals": target_signals,
                "data_ok": bool(strategy.last_data_ok),
                "data_date": getattr(strategy, "last_data_date", None),
                "reference_close": getattr(strategy, "last_close", None),
                "data_source": getattr(strategy, "last_data_source", None),
                "trigger_diagnostics": _capture_trigger_diagnostics(strategy, code),
            }
        )

    if trend_data_failed:
        logger.warning(
            "ETF 趋势评估数据状态: ok=%d failed=%d failed_targets=%s",
            trend_data_ok,
            len(trend_data_failed),
            ", ".join(trend_data_failed),
        )

    generated_signals = list(signals)
    generated_signal_count = len(generated_signals)
    allowed_signals, risk_blocked_signals = _apply_eod_risk_gate(
        generated_signals,
        trend_targets,
        risk_state,
        logger,
    )
    if risk_blocked_signals:
        logger.warning(
            "宏观风控闸门拦截 %d/%d 条新增趋势买入，level=%s",
            len(risk_blocked_signals),
            generated_signal_count,
            (risk_state or {}).get("level", "unavailable"),
        )

    macro_unavailable = [
        factor
        for factor in ("total_amount", "margin_growth_pct", "limit_ratio")
        if factor in collect_status
        and (
            collect_status[factor].get("available") is not True
            or collect_status[factor].get("stale")
            or collect_status[factor].get("anomalous")
        )
    ]

    blocked_ids = {id(signal) for signal in risk_blocked_signals}
    if audit_run_id and not dry_run:
        risk_level = str((risk_state or {}).get("level", "unavailable"))
        target_decisions = []
        for evaluation in target_evaluations:
            target = evaluation["target"]
            raw_signals = evaluation["signals"]
            serialized_signals = [
                {
                    "signal_type": signal.signal_type,
                    "period": signal.period,
                    "title": signal.title,
                    "confidence": signal.confidence,
                    "detail": signal.detail,
                }
                for signal in raw_signals
            ]
            blocked_count = sum(id(signal) in blocked_ids for signal in raw_signals)
            if not evaluation["data_ok"]:
                state = "data_failed"
                risk_action = "not_applicable"
                reason_code = "trend_data_unavailable"
            elif not raw_signals:
                state = "no_trigger"
                risk_action = "not_applicable"
                reason_code = "signal_not_triggered"
            elif blocked_count == len(raw_signals):
                state = "blocked"
                risk_action = "blocked"
                reason_code = "macro_risk_blocked"
            else:
                state = "allowed"
                risk_action = "allowed"
                reason_code = "signal_allowed"
            target_decisions.append(
                {
                    "code": target["code"],
                    "name": target.get("name", target["code"]),
                    "sector": target.get("sector", ""),
                    "data_status": "ok" if evaluation["data_ok"] else "failed",
                    "decision_state": state,
                    "raw_trigger_count": len(raw_signals),
                    "raw_signals": serialized_signals,
                    "risk_action": risk_action,
                    "risk_level": risk_level,
                    "reason_code": reason_code,
                    "data_date": evaluation["data_date"],
                    "reference_close": evaluation["reference_close"],
                    "validation_split": "live_oos",
                }
            )
        store.record_target_decisions(
            audit_run_id,
            decision_at=decision_at,
            snapshot_id=risk_snapshot_id,
            decisions=target_decisions,
        )
    signal_records = []
    for signal in generated_signals:
        detail = dict(signal.detail) if isinstance(signal.detail, dict) else {}
        detail["risk_gate"] = {
            "blocked": id(signal) in blocked_ids,
            "level": str((risk_state or {}).get("level", "unavailable")),
            "message": str((risk_state or {}).get("message", "风控状态缺失或未知"))[:200],
        }
        signal_records.append(
            {
                "code": signal.code,
                "name": signal.name,
                "signal_type": signal.signal_type,
                "period": signal.period,
                "title": signal.title,
                "detail": detail,
                "confidence": signal.confidence,
            }
        )
    existing_other_dates = store.get_signal_keys(exclude_date=macro_slice.date)
    notified_keys = store.get_signal_keys(notified_only=True)
    storage_records = []
    storage_blocked_count = 0
    notification_signals = []
    unique_signals = []
    seen_keys = set()
    allowed_ids = {id(signal) for signal in allowed_signals}
    for signal, record in zip(generated_signals, signal_records):
        key = store.signal_key(record)
        if key is not None and key in seen_keys:
            continue
        if key is not None:
            seen_keys.add(key)
        # 未通知的跨日同一 data_date 需要迁移到本次决策日，尤其是前一日被
        # 风控拦截、今日恢复放行的信号；已通知记录则保留原审计日期。
        if key is None or key not in existing_other_dates or key not in notified_keys:
            storage_records.append(record)
            if id(signal) in blocked_ids:
                storage_blocked_count += 1
        if id(signal) in allowed_ids:
            unique_signals.append(signal)
        if id(signal) in allowed_ids and (key is None or key not in notified_keys):
            notification_signals.append(signal)

    suppressed = len(unique_signals) - len(notification_signals)
    if suppressed:
        logger.info("同一 data_date 已成功通知，抑制 %d 条重复趋势信号", suppressed)

    # dry-run must not consume a signal identity; otherwise a later real run
    # would incorrectly believe the notification had already been handled.
    if dry_run:
        logger.info("dry-run：跳过趋势信号持久化，保留正式运行的通知机会")
    else:
        try:
            store.replace_signals_for_date(macro_slice.date, storage_records)
        except Exception as e:
            logger.warning("趋势信号持久化失败: %s", e)
            audit_reason_codes.append("signal_persist_failed")

    # 3. 右侧拔剑通知器
    base_notifier = TelegramNotifier(
        data["telegram"]["bot_token"], str(data["telegram"]["chat_id"]), logger
    )
    notifier = (
        base_notifier
        if dry_run
        else DurableOutboxNotifier(
            store,
            base_notifier,
            logger,
            # Stable business namespace: the data date, NOT the per-run audit id, so a
            # same-day retry run cannot obtain a fresh send entitlement for the same event.
            scope=f"eod:{macro_slice.date}",
        )
    )
    reporter = SwingReporter(notifier, logger)
    if trend_targets and trend_data_ok == 0:
        message = (
            f"ETF 趋势确认信号未完成：{len(trend_targets)} 只监控 ETF 历史日 K 全部获取失败，"
            "本次结果不能视为‘无推荐’。请检查东财/akshare 网络连通性、"
            "RADAR_ALL_PROXY 或本地 K 线缓存。"
        )
        logger.error(message)
        # Expected 15:30 all-kline gap is folded into the 15:50 daily report.
        result = {"sent": 0, "silent": False, "facts": [message], "deferred_to_daily_report": True}
    else:
        result = reporter.report(
            extremes,
            unique_signals if dry_run else notification_signals,
            dry_run=dry_run,
            dispatch_macro=False,
        )
        if not dry_run:
            try:
                # sent_signal_keys only ever contains CONFIRMED deliveries (see SwingReporter);
                # uncertain / not-sent / paused results must not consume the signal identity.
                store.mark_signals_notified(result.get("sent_signal_keys", []))
            except Exception as e:
                logger.warning("趋势通知状态持久化失败: %s", e)
                audit_reason_codes.append("notification_state_persist_failed")

    operational_facts = []
    if macro_unavailable:
        message = "大盘环境过滤器数据降级: " + ", ".join(macro_unavailable)
        operational_facts.append(message)
        logger.error(message)
    if trend_data_failed and trend_data_ok > 0:
        message = f"ETF历史K线部分失败: {len(trend_data_failed)}/{len(trend_targets)}"
        operational_facts.append(message)
    if operational_facts:
        result["facts"] = list(result.get("facts", [])) + operational_facts

    logger.info(
        ">>> Radar Swing EOD 完成 <<< 趋势ETF=%d 极值=%d 趋势信号=%d 通知候选=%d 发送=%d",
        len(trend_targets),
        len(extremes),
        len(storage_records) if not dry_run else len(unique_signals),
        len(unique_signals) if dry_run else len(notification_signals),
        result.get("sent", 0),
    )
    return {
        "skipped": False,
        "trend_targets": len(trend_targets),
        "extremes": len(extremes),
        "signals": len(storage_records) if not dry_run else len(unique_signals),
        "generated_signals": generated_signal_count,
        "risk_blocked_signals": len(risk_blocked_signals),
        # The daily decision is based on newly retained signal identities. Raw
        # detections can repeat across days while pointing at the same data bar.
        "decision_signals": len(storage_records),
        "decision_risk_blocked_signals": storage_blocked_count,
        "deduplicated_signals": max(0, generated_signal_count - len(storage_records)),
        "risk_state": risk_state or {"level": "unavailable"},
        "notification_candidates": len(unique_signals) if dry_run else len(notification_signals),
        "sent": result.get("sent", 0),
        "notification_deliveries": result.get("deliveries", []),
        "notification_unconfirmed": int(result.get("unconfirmed", 0) or 0),
        "facts": result.get("facts", []),
        "deferred_to_daily_report": bool(result.get("deferred_to_daily_report")),
        "trend_data_ok": trend_data_ok,
        "trend_data_failed": trend_data_failed,
        "runtime_status": runtime_status,
        "collect_status": collect_status,
        "audit_reason_codes": audit_reason_codes,
        "data_date": macro_slice.date,
        "decision_at": decision_at,
        "target_decision_count": len(target_evaluations) if audit_run_id else 0,
        "trigger_diagnostics": {
            "schema_version": "quant.swing-trigger-diagnostics.v1",
            "run_id": audit_run_id,
            "decision_date": macro_slice.date,
            "targets": [
                item["trigger_diagnostics"] for item in target_evaluations[:200]
                if item["trigger_diagnostics"] is not None
            ],
        },
    }


def _audit_result(result: Dict) -> Dict:
    """Derive a bounded, conservative audit outcome from one EOD attempt."""
    target_count = max(0, int(result.get("trend_targets", 0) or 0))
    if result.get("skipped"):
        return {
            "status": "skipped",
            "outcome": "skipped",
            "target_count": target_count,
            "evaluated_count": 0,
            "failed_count": 0,
            "signal_count": 0,
            "allowed_signal_count": 0,
            "blocked_signal_count": 0,
            "reason_codes": ["non_trading_day"],
        }
    evaluated_count = max(0, int(result.get("trend_data_ok", 0) or 0))
    failed_targets = result.get("trend_data_failed", [])
    failed_count = len(failed_targets) if isinstance(failed_targets, list) else 0
    decision_count = max(0, int(result.get("decision_signals", 0) or 0))
    blocked_count = max(
        0, int(result.get("decision_risk_blocked_signals", 0) or 0)
    )
    blocked_count = min(blocked_count, decision_count)
    allowed_count = decision_count - blocked_count
    raw_reason_codes = result.get("audit_reason_codes", [])
    reason_codes = [
        str(value)[:64]
        for value in raw_reason_codes
        if isinstance(value, str) and value
    ] if isinstance(raw_reason_codes, list) else []

    collect_status = result.get("collect_status", {})
    if isinstance(collect_status, dict) and any(
        isinstance(item, dict)
        and (
            item.get("available") is not True
            or item.get("stale")
            or item.get("anomalous")
        )
        for item in collect_status.values()
    ):
        reason_codes.append("macro_data_partial")

    if target_count > 0 and evaluated_count == 0:
        status = "failed"
        outcome = "data_unavailable"
        reason_codes.append("trend_data_unavailable")
    else:
        status = "partial" if failed_count > 0 or reason_codes else "success"
        if decision_count > 0:
            outcome = "blocked" if allowed_count == 0 else "emitted"
        else:
            outcome = "no_signal" if status == "success" else "data_unavailable"
        if failed_count:
            reason_codes.append("trend_data_partial")

    return {
        "status": status,
        "outcome": outcome,
        "target_count": target_count,
        "evaluated_count": evaluated_count,
        "failed_count": failed_count,
        "signal_count": decision_count,
        "allowed_signal_count": allowed_count,
        "blocked_signal_count": blocked_count,
        "reason_codes": list(dict.fromkeys(reason_codes))[:8],
    }


def _rule_attribution(data: Dict, run_id: str, decision_date: str) -> Dict:
    """Attribute the diagnostic switch/pool of this run, never rewrite history.

    This digest is not a full strategy-version or profitability attestation.
    Execution still consumes the exact same frozen configuration object.
    """
    swing = data["swing"]
    technical = swing.get("technical") or {}
    codes = sorted({str(t["code"]) for t in data["targets"] if t.get("mode", "trend") == "trend"})
    payload = {
        "daily_volume_breakout": bool(technical.get("daily_volume_breakout", True)),
        "target_codes": codes,
        # The executable strategy uses max(ma_periods), not the legacy config alias.
        "breakout_ma": max(swing.get("ma_periods") or [20, 60]),
        "volume_window": int(technical.get("volume_window", 20)),
    }
    return {
        "schema_version": "quant.etf-run-rule.v1",
        "scope": "diagnostic_switch_and_pool_not_full_strategy",
        "run_id": run_id,
        "decision_date": decision_date,
        **payload,
        "target_count": len(codes),
        "rule_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest(),
        "decision_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _emit_eod_evidence_identity(logger, run_id, decision_date, audit, attribution, data):
    """Emit only after the existing audit commit; never change the business result.

    This binds future journal evidence to the actual audit identity. Config is
    the already-consumed object, not a second read; no credentials or body text
    are retained. Source bytes at attribution time are not a loaded-code proof.
    """
    try:
        identity = {
            "schema": "quant.eod-completion.v1",
            "run_id": run_id,
            "decision_date": decision_date,
            "status": audit["status"],
            "outcome": audit["outcome"],
            "rule_sha256": attribution["rule_sha256"],
            "decision_source_sha256": attribution["decision_source_sha256"],
            "effective_config_sha256": hashlib.sha256(json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                allow_nan=False).encode("utf-8")).hexdigest(),
        }
        logger.info("eod_evidence_identity %s", json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False))
    except Exception:
        # Metadata failure cannot undo/duplicate the already committed run.
        try:
            logger.warning("eod_evidence_identity_unavailable")
        except Exception:
            pass


def run(config_path: str, *, dry_run: bool = False, force: bool = False) -> Dict:
    """Run EOD and append an auditable outcome for every real trading-day attempt."""
    data = _read_config(config_path)
    validate_swing_config_dict(data)
    swing_cfg = data["swing"]
    logger = _setup_logging(swing_cfg.get("log_file", "radar_swing.log"))
    scheduler = ScanScheduler(data["market"], logger)
    if not scheduler.is_trading_day() and not force:
        return _run_core(config_path, dry_run=dry_run, force=force, frozen_data=data)
    if dry_run:
        return _run_core(config_path, dry_run=True, force=force, frozen_data=data)

    store = SwingStore(swing_cfg.get("store_file", "data/swing_macro.db"))
    decision_date = SwingStore.today_str()
    target_count = len(
        [target for target in data["targets"] if target.get("mode", "trend") == "trend"]
    )
    run_id = store.begin_eod_run(decision_date, target_count=target_count)
    rule_attribution = None
    try:
        rule_attribution = _rule_attribution(data, run_id, decision_date)
        result = _run_core(
            config_path,
            dry_run=False,
            force=force,
            audit_run_id=run_id,
            frozen_data=data,
        )
        audit = _audit_result(result)
        store.finish_eod_run(
            run_id,
            status=audit["status"],
            decision_outcome=audit["outcome"],
            data_date=result.get("data_date") or decision_date,
            evaluated_count=audit["evaluated_count"],
            failed_count=audit["failed_count"],
            signal_count=audit["signal_count"],
            allowed_signal_count=audit["allowed_signal_count"],
            blocked_signal_count=audit["blocked_signal_count"],
            reason_codes=audit["reason_codes"],
            details={
                "failed_targets": result.get("trend_data_failed", [])[:20],
                "raw_detected_signal_count": result.get("generated_signals", 0),
                "deduplicated_signal_count": result.get("deduplicated_signals", 0),
                "target_decision_count": result.get("target_decision_count", 0),
                "decision_at": result.get("decision_at"),
                "trigger_diagnostics": result.get("trigger_diagnostics"),
                "rule_attribution": rule_attribution,
            },
        )
        _emit_eod_evidence_identity(logger, run_id, decision_date, audit, rule_attribution, data)
        result["audit_status"] = audit["status"]
        result["decision_outcome"] = audit["outcome"]
        result["decision_date"] = decision_date
        return result
    except Exception:
        try:
            store.finish_eod_run(
                run_id,
                status="failed",
                decision_outcome="data_unavailable",
                data_date=decision_date,
                evaluated_count=0,
                failed_count=target_count,
                signal_count=0,
                allowed_signal_count=0,
                blocked_signal_count=0,
                reason_codes=["unhandled_error"],
            )
            _emit_eod_evidence_identity(
                logger, run_id, decision_date,
                {"status": "failed", "outcome": "data_unavailable"},
                rule_attribution, data)
        except Exception:
            logger.exception("EOD 运行审计收口失败 run_id=%s", run_id)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Radar Swing EOD - 收盘后波段趋势确认")
    parser.add_argument("--config", default="config.swing.json", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只计算不推送")
    parser.add_argument("--force", action="store_true", help="非交易日也运行")
    args = parser.parse_args()

    try:
        _lock = SingleInstanceLock("radar-swing-eod").acquire()
    except SingleInstanceLockError as e:
        print(f"波段趋势任务锁冲突: {e}", file=sys.stderr)
        sys.exit(75)

    try:
        result = run(args.config, dry_run=args.dry_run, force=args.force)
        if result.get("audit_status") == "failed":
            raise SystemExit(1)
    except Exception as e:
        print(f"Radar Swing EOD 运行失败: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

