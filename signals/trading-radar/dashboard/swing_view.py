"""波段化模块的 Dashboard 视图数据加载。

从 ``radar_swing_eod.py`` 写入的 SwingStore（默认 data/swing_macro.db）读取大盘
情绪切片 + 趋势信号，整理成 ``/swing`` 页面渲染所需的结构。从 ``dashboard/app.py``
拆出以保持入口文件精简（见 tests/test_engineering_structure.py 行数预算）。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_SWING_FACTOR_META = {
    "total_amount": ("沪深市场成交额", "亿元"),
    "margin_growth_pct": ("两融余额增速", "%"),
    "limit_ratio": ("涨跌停平滑比", ""),
}

_BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _beijing_now() -> datetime:
    return datetime.now(_BEIJING_TZ)


def _latest_completed_trading_date(now: datetime) -> str:
    """Return the trading date whose 15:30 EOD slice should already exist."""
    from radar.core.scan_scheduler import HolidayCalendar

    def is_trading_day(day: date) -> bool:
        probe = datetime.combine(day, time(12, 0), tzinfo=_BEIJING_TZ)
        return day.weekday() < 5 and not HolidayCalendar.is_holiday(probe)

    candidate = now.date()
    if not (is_trading_day(candidate) and now.time() >= time(15, 30)):
        candidate -= timedelta(days=1)
    for _ in range(15):
        if is_trading_day(candidate):
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    return candidate.isoformat()


def _filter_legacy_partial_weekly_signals(signals):
    """Hide pre-fix partial-week artifacts without deleting historical rows.

    New rows carry ``detail.data_date`` and are deduplicated by that immutable
    weekly bar. Legacy weekly rows lack it; only Friday decisions could have
    represented a completed normal trading week, so weekday copies are unsafe
    to present as confirmed history.
    """
    filtered = []
    removed = 0
    for signal in signals:
        if signal.get("period") != "W" or (signal.get("detail") or {}).get("data_date"):
            filtered.append(signal)
            continue
        try:
            is_friday = date.fromisoformat(str(signal.get("date"))).weekday() == 4
        except (TypeError, ValueError):
            is_friday = False
        if is_friday:
            filtered.append(signal)
        else:
            removed += 1
    return filtered, removed


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_swing_overview(base_dir: Path) -> Dict[str, Any]:
    """加载波段化概览（大盘环境切片 + 趋势信号）。

    任何缺失/异常都降级为 ``available=False``，保证页面可渲染。
    """
    overview: Dict[str, Any] = {
        "available": False,
        "updated": None,
        "source": None,
        "strategy": {},
        "factors": [],
        "limit_up": None,
        "limit_down": None,
        "margin_balance": None,
        "signals": [],
        "recent_signals": [],
        "latest_signal_date": None,
        "signal_history_filtered": 0,
        "risk_blocked_signal_count": 0,
        "risk_blocked_signals": [],
        "targets": [],
        "error": None,
        "environment_state": "unavailable",
        "environment_ready": False,
        "environment_stale": False,
        "environment_message": "尚无可判定的大盘环境数据",
        "environment_slice_stale": False,
        "environment_expected_date": None,
        "validity": {
            "label": "样本不足",
            "sample_size": 0,
            "minimum_samples": 30,
            "primary_horizon_sessions": 20,
            "metrics": None,
            "risk_counterfactual": {"sample_size": 0},
        },
        "daily_run": None,
        "decision_state_counts": {},
        "outbox_counts": {},
        "notification_health": {"available": False, "reason": "not_loaded"},
    }
    try:
        base_dir = Path(base_dir)
        swing_cfg = _read_json(base_dir / "config.swing.json")
        swing_block = swing_cfg.get("swing", {}) if isinstance(swing_cfg, dict) else {}
        store_file = swing_block.get("store_file", "data/swing_macro.db")
        macro_cfg = swing_block.get("macro", {})
        overview["targets"] = swing_cfg.get("targets", []) if isinstance(swing_cfg, dict) else []
        overview["strategy"] = {
            "role": swing_block.get("system_role", "core_trend_swing_eod"),
            "decision_time": swing_block.get("decision_time", "15:30 Asia/Shanghai"),
            "ma_periods": swing_block.get("ma_periods", [20, 60]),
            "volume_breakout_ratio": swing_block.get("volume_breakout_ratio", 1.5),
            "extreme_low": macro_cfg.get("extreme_percentile_low", 5),
            "extreme_high": macro_cfg.get("extreme_percentile_high", 95),
            "target_count": len(overview["targets"]),
        }

        db_path = base_dir / store_file
        if not db_path.exists():
            overview["error"] = f"尚无数据：{store_file} 未生成（EOD 批处理还没跑过）"
            return overview

        from radar.swing.macro_snapshot import MacroSnapshotCollector, _percentile_rank
        from radar.swing.store import NUMERIC_FACTORS, SwingStore

        store = SwingStore(str(db_path))
        from radar.swing.validity import ValidityPolicy, build_validity_summary

        validity_cfg = swing_block.get("validity", {})
        validity_policy = ValidityPolicy(
            min_samples=max(1, int(validity_cfg.get("min_samples", 30))),
            primary_horizon=int(validity_cfg.get("primary_horizon_sessions", 20)),
        )
        overview["validity"] = build_validity_summary(
            store.get_validity_rows(
                horizon_sessions=validity_policy.primary_horizon
            ),
            policy=validity_policy,
        )
        overview["daily_run"] = store.get_latest_eod_run()
        if overview["daily_run"]:
            overview["decision_state_counts"] = store.decision_state_counts(
                overview["daily_run"].get("decision_date")
            )
        overview["outbox_counts"] = store.outbox_counts()
        overview["notification_health"] = store.notification_health()
        latest = store.get_latest()
        raw_recent_signals = store.get_recent_signals(200)
        filtered_signals, filtered_count = _filter_legacy_partial_weekly_signals(raw_recent_signals)
        risk_blocked_signals = [
            signal
            for signal in filtered_signals
            if ((signal.get("detail") or {}).get("risk_gate") or {}).get("blocked")
        ]
        recent_signals = [
            signal
            for signal in filtered_signals
            if not ((signal.get("detail") or {}).get("risk_gate") or {}).get("blocked")
        ][:50]
        overview["recent_signals"] = recent_signals
        overview["signal_history_filtered"] = filtered_count
        overview["latest_signal_date"] = recent_signals[0]["date"] if recent_signals else None

        if latest is None:
            overview["error"] = "尚无宏观切片记录"
            overview["available"] = bool(recent_signals)
            return overview

        current_risk_blocked_signals = [
            signal for signal in risk_blocked_signals if signal.get("date") == latest.date
        ]
        overview["risk_blocked_signals"] = current_risk_blocked_signals
        overview["risk_blocked_signal_count"] = len(current_risk_blocked_signals)

        # “今日机械决策”只能展示与当前宏观切片同日的信号；历史信号单独保留，
        # 禁止在无新信号时用旧记录填充，从而造成仍在触发的错觉。
        overview["signals"] = [s for s in recent_signals if s["date"] == latest.date]

        min_history_days = int(macro_cfg.get("min_history_days", 20))
        collector = MacroSnapshotCollector(
            store,
            extreme_low=macro_cfg.get("extreme_percentile_low", 5),
            extreme_high=macro_cfg.get("extreme_percentile_high", 95),
            min_history_days=min_history_days,
        )
        extreme_map = {
            e.factor: e
            for e in collector.detect_extremes(latest, suppress_replayed=False)
        }
        collect_status = (latest.extra or {}).get("collect_status", {}) if isinstance(latest.extra, dict) else {}

        factors = []
        for factor in NUMERIC_FACTORS:
            raw_value = getattr(latest, factor)
            status = collect_status.get(factor, {}) if isinstance(collect_status, dict) else {}
            current_anomalous = raw_value is not None and not collector.is_valid_factor_value(
                factor, raw_value
            )
            value = raw_value if collector.is_valid_factor_value(factor, raw_value) else None
            value_date = status.get("observed_date") or latest.date
            is_stale = bool(status.get("stale"))
            if value is None:
                # 页面可以展示最近一次有效值作上下文，但必须明确标成“滞后”，
                # 且不得拿它冒充当前切片参与环境判定。
                for previous in reversed(store.get_recent(250)):
                    candidate = getattr(previous, factor)
                    previous_status = collector._status_for_slice(previous, factor)
                    if not collector.is_valid_factor_value(factor, candidate):
                        continue
                    if previous_status.get("anomalous"):
                        continue
                    value = float(candidate)
                    value_date = previous_status.get("observed_date") or previous.date
                    is_stale = previous.date != latest.date or bool(previous_status.get("stale"))
                    break
            label, unit = _SWING_FACTOR_META.get(factor, (factor, ""))
            if value is None and not status:
                status = {
                    "available": False,
                    "source": latest.source or "unknown",
                    "message": "最新切片该因子为空；请运行 EOD/回填并检查 akshare、RADAR_ALL_PROXY 或 MX_APIKEY",
                }
            ex = extreme_map.get(factor)
            series = []
            seen_observations = set()
            current_observed = status.get("observed_date") or latest.date
            for previous in reversed(store.get_recent(250)):
                if previous.date == latest.date or not collector._factor_is_usable(previous, factor):
                    continue
                previous_status = collector._status_for_slice(previous, factor)
                observed = previous_status.get("observed_date") or previous.date
                if observed == current_observed:
                    continue
                if observed in seen_observations:
                    continue
                seen_observations.add(observed)
                series.append(float(getattr(previous, factor)))
            history_ready = len(series) >= min_history_days
            collect_available = bool(status.get("available", raw_value is not None))
            if current_anomalous or status.get("anomalous"):
                factor_state = "anomalous"
                level = "异常"
            elif value is None or not collect_available:
                factor_state = "missing"
                level = "缺失"
            elif is_stale:
                factor_state = "stale"
                level = "滞后"
            elif not history_ready:
                factor_state = "insufficient"
                level = "样本不足"
            elif ex is not None:
                factor_state = "extreme"
                level = ex.level
            else:
                factor_state = "normal"
                level = "正常"

            if ex is not None and factor_state == "extreme":
                pct = ex.percentile
                sample_size = ex.sample_size
                history_ready = True
            elif value is not None and history_ready and factor_state == "normal":
                pct = round(_percentile_rank(series, float(value)), 1)
                sample_size = len(series)
            else:
                pct = None
                sample_size = len(series)
            factors.append(
                {
                    "factor": factor,
                    "label": label,
                    "unit": unit,
                    "value": value,
                    "value_date": value_date,
                    "is_stale": is_stale,
                    "percentile": pct,
                    "level": level,
                    "state": factor_state,
                    "sample_size": sample_size,
                    "history_ready": history_ready,
                    "collect_available": collect_available,
                    "collect_source": status.get("source", ""),
                    "collect_message": status.get("message", ""),
                    "observed_date": status.get("observed_date"),
                    "collect_stale": bool(status.get("stale")),
                    "collect_anomalous": bool(status.get("anomalous")) or current_anomalous,
                }
            )

        states = [factor["state"] for factor in factors]
        if not factors or all(state in {"missing", "anomalous"} for state in states):
            environment_state = "unavailable"
            environment_message = "三项因子均不可用，不能判定大盘环境"
        elif any(state in {"missing", "stale", "anomalous"} for state in states):
            environment_state = "degraded"
            environment_message = "部分因子缺失、滞后或异常；当前环境结论不完整"
        elif any(state == "insufficient" for state in states):
            environment_state = "insufficient"
            environment_message = f"有效历史样本尚未全部达到 {min_history_days} 天，暂不判定正常或极值"
        elif any(state == "extreme" for state in states):
            environment_state = "extreme"
            environment_message = "至少一项大盘情绪因子触及历史极值"
        else:
            environment_state = "normal"
            environment_message = "三项因子数据新鲜且样本充足，未触及历史极值"

        expected_date = _latest_completed_trading_date(_beijing_now())
        slice_stale = latest.date < expected_date
        if slice_stale:
            if environment_state != "unavailable":
                environment_state = "degraded"
            environment_message = (
                f"最新切片为 {latest.date}，应有交易日 {expected_date} 的 EOD 数据；"
                "任务可能停跑或失败"
            )

        limit_status = collect_status.get("limit_ratio", {}) if isinstance(collect_status, dict) else {}
        margin_status = collect_status.get("margin_balance", {}) if isinstance(collect_status, dict) else {}
        limit_up = latest.limit_up if _valid_dashboard_number(latest.limit_up, integer=True) else None
        limit_down = latest.limit_down if _valid_dashboard_number(latest.limit_down, integer=True) else None
        margin_balance = (
            latest.margin_balance
            if _valid_dashboard_number(latest.margin_balance, strictly_positive=True)
            else None
        )
        if limit_status.get("anomalous"):
            limit_up = limit_down = None
        if margin_status.get("anomalous"):
            margin_balance = None

        overview.update(
            {
                "available": True,
                "updated": latest.date,
                "source": latest.source,
                "factors": factors,
                "limit_up": limit_up,
                "limit_down": limit_down,
                "limit_observed_date": limit_status.get("observed_date"),
                "limit_stale": bool(limit_status.get("stale")),
                "margin_balance": margin_balance,
                "margin_observed_date": margin_status.get("observed_date"),
                "margin_stale": bool(margin_status.get("stale")),
                "environment_state": environment_state,
                "environment_ready": environment_state in {"normal", "extreme"},
                "environment_stale": slice_stale or any(state == "stale" for state in states),
                "environment_message": environment_message,
                "environment_slice_stale": slice_stale,
                "environment_expected_date": expected_date,
            }
        )
    except Exception as exc:  # pragma: no cover - 防御
        logger.warning("load_swing_overview 失败: %s", exc)
        overview["error"] = str(exc)
    return overview


def _valid_dashboard_number(
    value: object,
    *,
    integer: bool = False,
    strictly_positive: bool = False,
) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    if strictly_positive and number <= 0:
        return False
    if not strictly_positive and number < 0:
        return False
    return not integer or number == int(number)
