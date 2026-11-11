"""
监控配置结构校验（无第三方 schema 依赖）
在解析 JSON 并合并环境变量后调用，尽早暴露缺字段/类型问题。

约定：
- `validate_monitor_config_dict` 仍然做"启动前的硬校验"，缺关键字段会抛 ConfigSchemaError。
- `audit_optional_fields` 做软校验：返回缺失/类型异常的可选字段告警列表，
  调用方决定怎么处理（启动日志、Dashboard 横幅等），不抛异常。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List


class ConfigSchemaError(ValueError):
    """配置不符合监控进程最低要求。"""


def validate_monitor_config_dict(data: Dict[str, Any]) -> None:
    """
    校验 radar_monitor 所需字段。
    :raises ConfigSchemaError: 缺关键块或类型不对
    """
    if not isinstance(data, dict):
        raise ConfigSchemaError("根配置必须是 JSON 对象")

    tg = data.get("telegram")
    if not isinstance(tg, dict):
        raise ConfigSchemaError("缺少 telegram 对象")
    token = (tg.get("bot_token") or "").strip()
    chat = str(tg.get("chat_id") or "").strip()
    disabled_values = {"DISABLED", "REPLACE", "REPLACE_ME", "NONE"}
    if token.upper() not in disabled_values and not token:
        raise ConfigSchemaError("telegram.bot_token 为空（可设置 DISABLED 或环境变量 TELEGRAM_BOT_TOKEN）")
    if chat.upper() not in disabled_values and not chat:
        raise ConfigSchemaError("telegram.chat_id 为空（可设置 DISABLED 或环境变量 TELEGRAM_CHAT_ID）")

    rd = data.get("radar")
    if not isinstance(rd, dict):
        raise ConfigSchemaError("缺少 radar 对象")
    for k in ("cooldown_minutes", "scan_interval_seconds", "cache_file", "log_file"):
        if k not in rd:
            raise ConfigSchemaError(f"radar 缺少字段: {k}")
    try:
        int(rd["cooldown_minutes"])
        int(rd["scan_interval_seconds"])
    except (TypeError, ValueError) as e:
        raise ConfigSchemaError("radar.cooldown_minutes / scan_interval_seconds 须为整数") from e

    mk = data.get("market")
    if not isinstance(mk, dict):
        raise ConfigSchemaError("缺少 market 对象")
    for k in ("morning_start", "morning_end", "afternoon_start", "afternoon_end"):
        if k not in mk:
            raise ConfigSchemaError(f"market 缺少字段: {k}")

    targets = data.get("targets")
    if not isinstance(targets, list):
        raise ConfigSchemaError("targets 必须是数组")
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise ConfigSchemaError(f"targets[{i}] 必须是对象")
        for k in ("code", "name", "trigger_price"):
            if k not in t:
                raise ConfigSchemaError(f"targets[{i}] 缺少字段: {k}")
        try:
            trigger_price = float(t["trigger_price"])
        except (TypeError, ValueError) as e:
            raise ConfigSchemaError(f"targets[{i}].trigger_price 须为数字") from e
        if trigger_price <= 0:
            raise ConfigSchemaError(f"targets[{i}].trigger_price 必须大于 0")
        direction = t.get("direction", "below")
        if direction not in {"below", "above"}:
            raise ConfigSchemaError(f"targets[{i}].direction 只能是 below 或 above")
        for opt in ("ml_enabled", "rl_enabled"):
            if opt not in t:
                continue
            if not isinstance(t[opt], bool):
                raise ConfigSchemaError(
                    f"targets[{i}].{opt} 须为 JSON 布尔或省略（true/false）"
                )
        if "strategy_tag" in t and t["strategy_tag"] is not None:
            if not isinstance(t["strategy_tag"], str):
                raise ConfigSchemaError(f"targets[{i}].strategy_tag 须为字符串或省略")


def validate_swing_config_dict(data: Dict[str, Any]) -> None:
    """
    校验「波段化收盘后批处理」(radar_swing_eod) 所需字段。

    与 ``validate_monitor_config_dict`` 的关键差异：
    - 标的允许 ``mode == "trend"``：此类标的为「趋势标的」（宽基/行业 ETF），
      只靠日线/周线技术信号触发，**不强制** ``trigger_price``。
    - 仅当 ``mode`` 不是 "trend" 时才回退到「价格触发」语义，要求 ``trigger_price > 0``。

    :raises ConfigSchemaError: 缺关键块或类型不对
    """
    if not isinstance(data, dict):
        raise ConfigSchemaError("根配置必须是 JSON 对象")

    tg = data.get("telegram")
    if not isinstance(tg, dict):
        raise ConfigSchemaError("缺少 telegram 对象")

    mk = data.get("market")
    if not isinstance(mk, dict):
        raise ConfigSchemaError("缺少 market 对象")
    for k in ("morning_start", "morning_end", "afternoon_start", "afternoon_end"):
        if k not in mk:
            raise ConfigSchemaError(f"market 缺少字段: {k}")

    sw = data.get("swing")
    if not isinstance(sw, dict):
        raise ConfigSchemaError("缺少 swing 对象（波段化批处理参数）")
    for k in ("log_file", "store_file"):
        if k not in sw:
            raise ConfigSchemaError(f"swing 缺少字段: {k}")
    if not isinstance(sw.get("system_role", "core_trend_swing_eod"), str):
        raise ConfigSchemaError("swing.system_role 必须是字符串")
    if sw.get("system_role", "core_trend_swing_eod") != "core_trend_swing_eod":
        raise ConfigSchemaError("swing.system_role 必须是 core_trend_swing_eod")
    if not isinstance(sw.get("decision_time", "15:30 Asia/Shanghai"), str):
        raise ConfigSchemaError("swing.decision_time 必须是字符串")
    if str(sw.get("decision_time", "15:30 Asia/Shanghai")).strip() != "15:30 Asia/Shanghai":
        raise ConfigSchemaError("swing.decision_time 必须是 15:30 Asia/Shanghai")
    for path_key in ("log_file", "store_file"):
        if not isinstance(sw.get(path_key), str) or not sw[path_key].strip():
            raise ConfigSchemaError(f"swing.{path_key} 必须是非空字符串")

    if "history_days" not in sw:
        history_days = 250
    else:
        try:
            history_days = int(sw["history_days"])
        except (TypeError, ValueError) as e:
            raise ConfigSchemaError("swing.history_days 须为整数") from e
    if history_days < 120:
        raise ConfigSchemaError("swing.history_days 至少 120，才能支撑周线 MACD 与历史极值")

    ma_periods = sw.get("ma_periods")
    if not isinstance(ma_periods, list) or not ma_periods:
        raise ConfigSchemaError("swing.ma_periods 必须是非空数组")
    try:
        ma_values = sorted({int(v) for v in ma_periods})
    except (TypeError, ValueError) as e:
        raise ConfigSchemaError("swing.ma_periods 只能包含整数") from e
    if 60 not in ma_values:
        raise ConfigSchemaError("swing.ma_periods 必须包含 MA60")

    if "volume_breakout_ratio" not in sw:
        volume_ratio = 1.5
    else:
        try:
            volume_ratio = float(sw["volume_breakout_ratio"])
        except (TypeError, ValueError) as e:
            raise ConfigSchemaError("swing.volume_breakout_ratio 须为数字") from e
    if volume_ratio <= 1.0:
        raise ConfigSchemaError("swing.volume_breakout_ratio 必须大于 1.0")

    if "technical" in sw:
        technical = sw.get("technical")
        if not isinstance(technical, dict):
            raise ConfigSchemaError("swing.technical 必须是对象")
        if technical.get("weekly_macd_golden_cross") is not True:
            raise ConfigSchemaError("swing.technical.weekly_macd_golden_cross 必须为 true")
        # breakout_ma 为新键名；兼容读取旧键 volume_breakout_ma（二者同时存在时必须一致）
        keys = [k for k in ("breakout_ma", "volume_breakout_ma") if k in technical]
        if not keys:
            raise ConfigSchemaError("swing.technical.breakout_ma 必须为整数")
        values = []
        for key in keys:
            try:
                values.append(int(technical[key]))
            except (TypeError, ValueError) as e:
                raise ConfigSchemaError(f"swing.technical.{key} 必须为整数") from e
        if len(set(values)) != 1:
            raise ConfigSchemaError("swing.technical.breakout_ma 与 volume_breakout_ma 不一致")
        if values[0] != 60:
            raise ConfigSchemaError("swing.technical.breakout_ma 必须为 60")
        if "daily_volume_breakout" in technical and not isinstance(technical["daily_volume_breakout"], bool):
            raise ConfigSchemaError("swing.technical.daily_volume_breakout 须为布尔")
        if "volume_window" in technical:
            vw = technical["volume_window"]
            if isinstance(vw, bool) or not isinstance(vw, int) or not 5 <= vw <= 120:
                raise ConfigSchemaError("swing.technical.volume_window 须为 5..120 的整数")

    macro = sw.get("macro")
    if macro is None:
        macro = {}
    if not isinstance(macro, dict):
        raise ConfigSchemaError("swing.macro 必须是对象")
    try:
        extreme_low = float(macro.get("extreme_percentile_low", 5))
        extreme_high = float(macro.get("extreme_percentile_high", 95))
        min_history_days = int(macro.get("min_history_days", 20))
    except (TypeError, ValueError) as e:
        raise ConfigSchemaError("swing.macro 极值字段必须为数字") from e
    if extreme_low != 5 or extreme_high != 95:
        raise ConfigSchemaError("swing.macro 极值分位必须固定为 5/95")
    if min_history_days < 20:
        raise ConfigSchemaError("swing.macro.min_history_days 至少 20")

    risk_gate = sw.get("risk_gate")
    if risk_gate is not None:
        if not isinstance(risk_gate, dict):
            raise ConfigSchemaError("swing.risk_gate 必须是对象")
        if not isinstance(risk_gate.get("enabled", False), bool):
            raise ConfigSchemaError("swing.risk_gate.enabled 必须是布尔值")
        for path_key in ("state_file", "override_file"):
            if path_key in risk_gate and (
                not isinstance(risk_gate[path_key], str) or not risk_gate[path_key].strip()
            ):
                raise ConfigSchemaError(f"swing.risk_gate.{path_key} 必须是非空字符串")

    outcome = sw.get("outcome_evaluation", {})
    if not isinstance(outcome, dict):
        raise ConfigSchemaError("swing.outcome_evaluation 必须是对象")
    try:
        commission_rate = float(outcome.get("commission_rate", 0.0003))
        slippage_rate = float(outcome.get("slippage_rate", 0.0005))
        notional = float(outcome.get("notional", 100_000))
    except (TypeError, ValueError) as e:
        raise ConfigSchemaError("swing.outcome_evaluation 数值字段非法") from e
    if not (0 <= commission_rate <= 0.01 and 0 <= slippage_rate <= 0.05):
        raise ConfigSchemaError("手续费或滑点假设越界")
    if notional < 1_000:
        raise ConfigSchemaError("swing.outcome_evaluation.notional 至少为 1000")

    validity = sw.get("validity", {})
    if not isinstance(validity, dict):
        raise ConfigSchemaError("swing.validity 必须是对象")
    try:
        validity_samples = int(validity.get("min_samples", 30))
        primary_horizon = int(validity.get("primary_horizon_sessions", 20))
    except (TypeError, ValueError) as e:
        raise ConfigSchemaError("swing.validity 数值字段非法") from e
    if validity_samples < 20:
        raise ConfigSchemaError("swing.validity.min_samples 不得低于 20")
    if primary_horizon not in {1, 3, 5, 10, 20}:
        raise ConfigSchemaError("swing.validity.primary_horizon_sessions 非法")

    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ConfigSchemaError("targets 必须是非空数组")
    trend_count = 0
    required_trend_codes = {"510300", "510500", "588000", "512480", "512890", "518880"}
    seen_trend_codes: set[str] = set()
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise ConfigSchemaError(f"targets[{i}] 必须是对象")
        for k in ("code", "name"):
            if k not in t:
                raise ConfigSchemaError(f"targets[{i}] 缺少字段: {k}")
        mode = t.get("mode", "trend")
        if mode not in {"trend", "price"}:
            raise ConfigSchemaError(f"targets[{i}].mode 只能是 trend 或 price")
        if mode == "trend":
            trend_count += 1
            seen_trend_codes.add(str(t["code"]))
            if "sector" not in t or not isinstance(t["sector"], str) or not t["sector"].strip():
                raise ConfigSchemaError(f"targets[{i}] mode=trend 必须提供 sector")
        # 价格标的仍走旧语义：必须有正的 trigger_price
        if mode == "price":
            try:
                trigger_price = float(t["trigger_price"])
            except (KeyError, TypeError, ValueError) as e:
                raise ConfigSchemaError(
                    f"targets[{i}] mode=price 须提供数字 trigger_price"
                ) from e
            if trigger_price <= 0:
                raise ConfigSchemaError(f"targets[{i}].trigger_price 必须大于 0")
        # 趋势标的：trigger_price 可省略；若给了也校验类型
        elif "trigger_price" in t and t["trigger_price"] is not None:
            try:
                float(t["trigger_price"])
            except (TypeError, ValueError) as e:
                raise ConfigSchemaError(f"targets[{i}].trigger_price 须为数字或省略") from e

    # 2026-10-17 扩池：允许 10..50 只 trend ETF；核心标的（required_trend_codes）仍必须在列
    if not 10 <= trend_count <= 50:
        raise ConfigSchemaError("config.swing.json 必须维护 10 只至 50 只 mode=trend ETF 标的")
    if len(seen_trend_codes) != trend_count:
        raise ConfigSchemaError("config.swing.json trend ETF 代码不得重复")
    missing_codes = required_trend_codes - seen_trend_codes
    if missing_codes:
        missing = ", ".join(sorted(missing_codes))
        raise ConfigSchemaError(f"核心 trend ETF 标的缺失: {missing}")


# 顶级可选字段：缺省时由代码用默认值兜底，但显式写在 config.json 里更利于维护。
# 取值为 (key_path, expected_type, default, description)
_OPTIONAL_TOP_LEVEL_FIELDS = (
    ("ml_model_dir", str, "models", "ML 模型目录"),
    ("rl_model_dir", str, "rl_models", "RL 模型目录"),
    ("rl_mode", str, "confirm", "RL 模式（off/confirm/independent）"),
    ("initial_cash", (int, float), 100000.0, "模拟初始资金"),
)

# 在 config.json 中存在但当前代码（监控主进程）已不读取的顶级块。
# 标注 reason 用于运维日志，不会影响行为。
_DEPRECATED_TOP_LEVEL_FIELDS = (
    ("data_source_timeouts", "未被任何代码读取（超时/重试已硬编码在 fetcher 内）"),
    ("akshare_daily", "未被任何代码读取（akshare 报告由 cron + tools/akshare_daily.py 控制）"),
    ("llm", "未被监控主进程读取；如启用 LLM 集成，凭据放 LLM_API_KEY 环境变量"),
    ("backtest", "未被监控主进程读取（仅 radar_trainer.py / radar_pro.py 等离线 CLI 使用）"),
)


def audit_optional_fields(data: Dict[str, Any], raw_data: Dict[str, Any] | None = None) -> List[str]:
    """
    返回可选字段缺失或类型不符的告警列表。空列表代表全部显式声明且类型正确。
    不抛异常，调用方负责 logging / 上报。

    raw_data 可传入环境变量覆盖前的原始配置，用于区分“仓库里安全占位符”
    和“运行时仍未被环境变量覆盖”的真实漏配。
    """
    issues: List[str] = []
    if not isinstance(data, dict):
        return ["根配置不是对象，跳过软校验"]

    for key, expected_type, default, desc in _OPTIONAL_TOP_LEVEL_FIELDS:
        if key not in data:
            issues.append(
                f"缺少顶级字段 {key}（{desc}）；当前用代码默认值 {default!r} 兜底"
            )
            continue
        if not isinstance(data[key], expected_type):
            issues.append(
                f"{key} 类型应为 {expected_type}，实际为 {type(data[key]).__name__}"
            )

    rl_mode = data.get("rl_mode", "confirm")
    if rl_mode not in {"off", "confirm", "independent"}:
        issues.append("rl_mode 只能是 off / confirm / independent；未知值将导致策略语义不清晰")

    alerts = data.get("alerts")
    if not isinstance(alerts, dict) or "priorities" not in alerts:
        issues.append(
            "缺少 alerts.priorities；当前用代码默认值 "
            "{price: HIGH, ml: MEDIUM, rl: MEDIUM} 兜底"
        )
    elif not isinstance(alerts["priorities"], dict):
        issues.append("alerts.priorities 类型应为 dict")

    tg = data.get("telegram")
    raw_tg = raw_data.get("telegram") if isinstance(raw_data, dict) else None
    env_names = {
        "bot_token": "TELEGRAM_BOT_TOKEN",
        "chat_id": "TELEGRAM_CHAT_ID",
    }
    if isinstance(tg, dict):
        for k in ("bot_token", "chat_id"):
            v = tg.get(k)
            raw_v = raw_tg.get(k) if isinstance(raw_tg, dict) else v
            env_name = env_names[k]
            if isinstance(raw_v, str) and "REPLACE" in raw_v:
                if isinstance(v, str) and v and "REPLACE" not in v:
                    continue
                issues.append(
                    f"telegram.{k} 仍是占位符；建议放到环境变量 {env_name}"
                )

    llm = data.get("llm")
    if isinstance(llm, dict):
        api_key = llm.get("api_key")
        if isinstance(api_key, str) and (api_key in ("", "***") or "REPLACE" in api_key):
            issues.append(
                "llm.api_key 是占位符或被掩码；建议放到环境变量 LLM_API_KEY"
            )

    for key, reason in _DEPRECATED_TOP_LEVEL_FIELDS:
        if key in data:
            issues.append(f"deprecated 顶级字段 {key}：{reason}")

    return issues


def log_optional_field_audit(
    data: Dict[str, Any],
    logger: logging.Logger,
    *,
    prefix: str = "[ConfigAudit]",
    raw_data: Dict[str, Any] | None = None,
) -> List[str]:
    """跑软校验并按 WARNING 输出，返回 issues 列表（便于测试断言）。"""
    issues = audit_optional_fields(data, raw_data=raw_data)
    for msg in issues:
        logger.warning("%s %s", prefix, msg)
    return issues
