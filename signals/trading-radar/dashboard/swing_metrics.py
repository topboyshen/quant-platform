"""Prometheus exposition helpers for the Swing dashboard."""

from __future__ import annotations

from datetime import datetime


def render_swing_metrics(data: dict) -> str:
    """Render one valid Prometheus text snapshot from a Swing overview."""
    lines = [
        "# HELP radar_swing_macro_factor 宏观情绪因子数值",
        "# TYPE radar_swing_macro_factor gauge",
        "# HELP radar_swing_macro_percentile 宏观情绪因子历史分位数",
        "# TYPE radar_swing_macro_percentile gauge",
        "# HELP radar_swing_macro_level 宏观情绪极值等级（0=正常 1=冰点 2=沸点）",
        "# TYPE radar_swing_macro_level gauge",
        "# HELP radar_swing_signal_count 当日趋势信号数量",
        "# TYPE radar_swing_signal_count gauge",
        "# HELP radar_swing_limit_up 涨停家数",
        "# TYPE radar_swing_limit_up gauge",
        "# HELP radar_swing_limit_down 跌停家数",
        "# TYPE radar_swing_limit_down gauge",
        "# HELP radar_swing_margin_balance 两融余额（亿元）",
        "# TYPE radar_swing_margin_balance gauge",
        "# HELP radar_swing_data_available 波段数据是否可用（1=可用 0=不可用）",
        "# TYPE radar_swing_data_available gauge",
        "# HELP radar_swing_macro_factor_available 宏观因子当前是否有可展示数值（1=有 0=无）",
        "# TYPE radar_swing_macro_factor_available gauge",
        "# HELP radar_swing_macro_factor_age_days 宏观因子显示值距当前切片日期的天数",
        "# TYPE radar_swing_macro_factor_age_days gauge",
        "# HELP radar_swing_macro_collect_error 宏观因子采集是否失败（1=失败 0=正常）",
        "# TYPE radar_swing_macro_collect_error gauge",
        "# HELP radar_swing_factor_sample_size 宏观因子有效历史样本数",
        "# TYPE radar_swing_factor_sample_size gauge",
        "# HELP radar_swing_factor_coverage_ratio 宏观因子相对20日最低门槛的覆盖率",
        "# TYPE radar_swing_factor_coverage_ratio gauge",
        "# HELP radar_swing_audit_status 最新EOD审计状态",
        "# TYPE radar_swing_audit_status gauge",
        "# HELP radar_swing_task_duration_seconds 最新EOD任务耗时",
        "# TYPE radar_swing_task_duration_seconds gauge",
        "# HELP radar_swing_decision_count 最新决策日逐标的状态数量",
        "# TYPE radar_swing_decision_count gauge",
        "# HELP radar_swing_risk_blocked_count 最新决策日被风控拦截标的数",
        "# TYPE radar_swing_risk_blocked_count gauge",
        "# HELP radar_swing_notification_outbox 通知outbox状态数量",
        "# TYPE radar_swing_notification_outbox gauge",
        "# HELP radar_swing_notification_backlog 通知待处理、失败或投递未知总数",
        "# TYPE radar_swing_notification_backlog gauge",
        f'radar_swing_data_available {1.0 if data.get("available") else 0.0}',
    ]
    for key in ("limit_up", "limit_down", "margin_balance"):
        if data.get(key) is not None:
            lines.append(f'radar_swing_{key} {data[key]}')

    level_map = {"正常": 0, "冰点": 1, "沸点": 2}
    for factor_data in data.get("factors", []):
        factor = factor_data.get("factor", "unknown")
        value = factor_data.get("value")
        if value is not None:
            lines.append(f'radar_swing_macro_factor{{factor="{factor}"}} {value}')
        if factor_data.get("percentile") is not None:
            lines.append(
                f'radar_swing_macro_percentile{{factor="{factor}"}} '
                f'{factor_data["percentile"]}'
            )
        lines.append(
            f'radar_swing_macro_level{{factor="{factor}"}} '
            f'{level_map.get(factor_data.get("level", "正常"), 0)}'
        )
        available = 1.0 if value is not None else 0.0
        lines.append(f'radar_swing_macro_factor_available{{factor="{factor}"}} {available}')
        age_days = _age_days(data.get("updated"), factor_data.get("value_date"))
        lines.append(f'radar_swing_macro_factor_age_days{{factor="{factor}"}} {age_days}')
        source = str(factor_data.get("collect_source") or "unknown").replace('"', "'")
        error = 0.0 if factor_data.get("collect_available", available > 0) else 1.0
        lines.append(
            f'radar_swing_macro_collect_error{{factor="{factor}",source="{source}"}} {error}'
        )
        sample_size = max(0, int(factor_data.get("sample_size") or 0))
        lines.append(
            f'radar_swing_factor_sample_size{{factor="{factor}"}} {sample_size}'
        )
        lines.append(
            f'radar_swing_factor_coverage_ratio{{factor="{factor}"}} '
            f'{min(1.0, sample_size / 20.0)}'
        )

    signals = data.get("signals", [])
    latest_date = signals[0].get("date") if signals else None
    latest_signals = [signal for signal in signals if signal.get("date") == latest_date]
    lines.append(f'radar_swing_signal_count {len(latest_signals)}')
    seen = set()
    for signal in latest_signals:
        labels = (signal.get("code", "unknown"), signal.get("signal_type", "unknown"))
        if labels in seen:
            continue
        seen.add(labels)
        lines.append(
            f'radar_swing_signal{{code="{labels[0]}",signal_type="{labels[1]}"}} '
            f'{signal.get("confidence", 0)}'
        )
    source = _label(data.get("source") or "unknown")
    lines.extend(
        (
            "# HELP radar_swing_data_source_info 最新宏观切片数据源",
            "# TYPE radar_swing_data_source_info gauge",
            f'radar_swing_data_source_info{{source="{source}"}} 1',
        )
    )
    daily_run = data.get("daily_run") if isinstance(data.get("daily_run"), dict) else {}
    if daily_run:
        status = _label(daily_run.get("status") or "unknown")
        lines.append(f'radar_swing_audit_status{{status="{status}"}} 1')
        duration = _duration_seconds(daily_run.get("started_at"), daily_run.get("completed_at"))
        if duration is not None:
            lines.append(f"radar_swing_task_duration_seconds {duration}")
    for state, count in sorted((data.get("decision_state_counts") or {}).items()):
        lines.append(
            f'radar_swing_decision_count{{state="{_label(state)}"}} {max(0, int(count))}'
        )
    lines.append(
        "radar_swing_risk_blocked_count "
        f'{max(0, int((data.get("decision_state_counts") or {}).get("blocked", 0)))}'
    )
    outbox_counts = data.get("outbox_counts") or {}
    for status, count in sorted(outbox_counts.items()):
        lines.append(
            f'radar_swing_notification_outbox{{status="{_label(status)}"}} '
            f'{max(0, int(count))}'
        )
    backlog = sum(int(outbox_counts.get(status, 0) or 0) for status in ("pending", "failed", "sending"))
    health = data.get("notification_health") if isinstance(data.get("notification_health"), dict) else {}
    health_available = bool(health.get("available"))
    # v6 dispatch problems are backlog too: uncertain / stuck / exhausted / over-budget /
    # quarantined / conflicting rows are "needs review", never silently healthy.
    review_needed = max(0, int(health.get("review_needed", 0) or 0)) if health_available else 0
    in_flight = max(0, int(health.get("in_flight", 0) or 0)) if health_available else 0
    lines.append(f"radar_swing_notification_backlog {max(0, backlog) + review_needed + in_flight}")
    lines.extend(
        (
            "# HELP radar_swing_notification_dispatch_available 通知dispatch表(v6)可用且结构校验通过（1=是 0=否，0 不代表健康）",
            "# TYPE radar_swing_notification_dispatch_available gauge",
            f"radar_swing_notification_dispatch_available {1 if health_available else 0}",
            "# HELP radar_swing_notification_review_needed 需要人工核查的通知数（uncertain/超时重试/耗尽/限流超预算/隔离/正文冲突等）",
            "# TYPE radar_swing_notification_review_needed gauge",
            "# HELP radar_swing_notification_problem 按原因拆分的未处理通知问题数",
            "# TYPE radar_swing_notification_problem gauge",
        )
    )
    if health_available:
        lines.append(f"radar_swing_notification_review_needed {review_needed}")
        for reason in (
            "uncertain", "sending_expired", "retry_wait_stuck", "pending_stale",
            "failed_exhausted", "failed_permanent", "rate_limited_over_budget",
            "quarantined_legacy", "conflicts",
        ):
            lines.append(
                f'radar_swing_notification_problem{{reason="{reason}"}} '
                f'{max(0, int(health.get(reason, 0) or 0))}'
            )
    return "\n".join(lines) + "\n"


def _label(value: object) -> str:
    return str(value).replace("\\", "_").replace('"', "'").replace("\n", " ")[:96]


def _duration_seconds(started: object, completed: object) -> int | None:
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return None


def _age_days(updated: object, value_date: object) -> int:
    try:
        if not updated:
            return 0
        effective_value_date = value_date or updated
        return max(
            0,
            (datetime.fromisoformat(str(updated)) - datetime.fromisoformat(str(effective_value_date))).days,
        )
    except (TypeError, ValueError):
        return 0
