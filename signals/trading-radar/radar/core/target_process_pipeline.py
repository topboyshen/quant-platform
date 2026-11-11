"""
单标的告警处理链：价格 → ML → RL。
将原 process_target 内联逻辑拆出，便于单测与后续替换/插入步骤。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from radar.alert.batcher import AlertItem, AlertType
from radar.core.alert_payloads import PriceTriggerAlertDict


@dataclass
class TargetProcessContext:
    """一次 process_target 的共享状态（链上各步可读写字段）。"""

    target: Any
    data: Dict[str, Any]
    current_price: float
    price_alert: Optional[PriceTriggerAlertDict] = None
    alerts: List[AlertItem] = field(default_factory=list)


def run_price_alert_step(engine: Any, ctx: TargetProcessContext) -> None:
    price_alert = engine.check_price_trigger(ctx.target, ctx.data)
    ctx.price_alert = price_alert
    item = engine._dispatch_alert(
        ctx.target,
        AlertType.PRICE,
        "price",
        price_alert,
        lambda: engine.notifier.send_price_alert(
            name=ctx.target.name,
            code=ctx.target.code,
            price=ctx.current_price,
            trigger=ctx.target.trigger_price,
            direction=ctx.target.direction,
        ),
        batch_data={"trigger": ctx.target.trigger_price, "direction": ctx.target.direction},
        current_price=ctx.current_price,
    )
    if item:
        ctx.alerts.append(item)


def run_ml_alert_step(engine: Any, ctx: TargetProcessContext) -> None:
    ml_alert = engine.check_ml_prediction(ctx.target, ctx.data)
    item = engine._dispatch_alert(
        ctx.target,
        AlertType.ML,
        "ml",
        ml_alert,
        lambda: engine.notifier.send_ml_alert(
            name=ctx.target.name,
            code=ctx.target.code,
            prediction=ml_alert["prediction"],
            probability=ml_alert["probability"],
            target_price=ml_alert["trigger_price"],
            model_confidence=ml_alert.get("confidence"),
            triggered=ml_alert.get("triggered", False),
            current_price=ml_alert.get("current_price", 0.0),
        ),
        log_msg=(
            f"[ML信号] {ctx.target.name}: {ml_alert['prediction']} "
            f"({ml_alert['probability']:.1%})"
            if ml_alert
            else None
        ),
        current_price=ctx.current_price,
    )
    if item:
        ctx.alerts.append(item)


def run_rl_alert_step(engine: Any, ctx: TargetProcessContext) -> None:
    rl_alert = None
    if engine.config.rl_mode == "confirm":
        # 确认模式：仅在价格触发时，RL提供辅助建议
        if ctx.price_alert:
            rl_alert = engine.check_rl_signal(ctx.target, ctx.data)
            # 修复：确认模式下，RL只返回信息，不单独触发告警
            # 只有当RL建议与价格触发方向一致时才增强告警
            if rl_alert:
                # 检查RL建议是否与价格触发方向一致
                direction = ctx.target.direction
                action = rl_alert.get('rl_action', '')
                # hold(0)=观望, open(1)=建议建仓, add(2)=建议加仓
                # reduce(3)=建议减仓, close(4)=建议平仓
                is_aligned = False
                if direction == 'below' and action in ['open', 'add']:
                    is_aligned = True
                elif direction == 'above' and action in ['open', 'add']:
                    is_aligned = True
                # 只有当RL建议与触发方向一致时才告警，否则忽略
                if not is_aligned:
                    rl_alert = None
    else:
        # 独立模式：RL自己做决策
        rl_alert = engine.check_rl_signal(ctx.target, ctx.data)
        if rl_alert and rl_alert.get("action_id") not in [1, 4]:
            rl_alert = None

    if not rl_alert:
        return
    item = engine._dispatch_alert(
        ctx.target,
        AlertType.RL,
        "rl",
        rl_alert,
        lambda: engine.notifier.send_rl_alert(
            name=ctx.target.name,
            code=ctx.target.code,
            suggestion=rl_alert.get("rl_suggestion"),
            position_status=rl_alert.get("position_status"),
            position_pnl=rl_alert.get("position_pnl"),
        ),
        log_msg=f"[RL建议] {ctx.target.name}: {rl_alert.get('rl_suggestion')}",
        current_price=ctx.current_price,
    )
    if item:
        ctx.alerts.append(item)


# 默认链顺序；MonitorEngine 引用此元组，便于测试替换。
TARGET_PROCESS_STEPS: Tuple[Any, ...] = (
    run_price_alert_step,
    run_ml_alert_step,
    run_rl_alert_step,
)


def run_target_pipeline(
    engine: Any,
    ctx: TargetProcessContext,
    steps: Optional[Sequence[Any]] = None,
) -> List[AlertItem]:
    """执行处理链，返回 ctx.alerts（与逐步 append 一致）。"""
    for step in steps or TARGET_PROCESS_STEPS:
        step(engine, ctx)
    return ctx.alerts
