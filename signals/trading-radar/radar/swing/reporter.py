"""SwingReporter - 右侧拔剑通知器。

过滤掉所有微观波动，只让它在「大趋势确立」或「大盘情绪触及历史极值」时发声：
平时静默，收盘后静静跑完；一旦触发，Telegram 弹出一行冰冷 Fact。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from radar.swing.macro_snapshot import MacroExtreme
from radar.swing.trend_strategy import TrendSignal


def _deliver(notifier: Any, operation: str, *args: Any, **kwargs: Any):
    """Return ``(confirmed, summary)``.  Uses the structured ``*_ex`` API when the notifier
    class provides it; otherwise falls back to a bool that only counts when exactly True."""
    ex = getattr(type(notifier), operation + "_ex", None)
    if ex is not None:
        result = getattr(notifier, operation + "_ex")(*args, **kwargs)
        confirmed = bool(result)
        summary = result.summary() if hasattr(result, "summary") else {
            "outcome": getattr(result, "outcome", "confirmed" if confirmed else "uncertain"),
            "attempts": 0,
            "confirmed": confirmed,
        }
        return confirmed, {"operation": operation, **summary}
    confirmed = getattr(notifier, operation)(*args, **kwargs)
    confirmed = bool(confirmed)
    return confirmed, {
        "operation": operation,
        "outcome": "confirmed" if confirmed else "unconfirmed",
        "attempts": 0,
        "confirmed": confirmed,
    }


class SwingReporter:
    """组装并发送波段化告警；无信号则保持静默。"""

    def __init__(self, notifier: Any, logger: Optional[logging.Logger] = None):
        self.notifier = notifier
        self.logger = logger or logging.getLogger(__name__)

    def report(
        self,
        extremes: Optional[List[MacroExtreme]] = None,
        signals: Optional[List[TrendSignal]] = None,
        *,
        dry_run: bool = False,
        dispatch_macro: bool = True,
    ) -> Dict[str, Any]:
        """过滤后发送。返回发送统计。

        只在 (a) 宏观情绪极值 或 (b) 趋势信号 存在时发声；否则静默。
        """
        extremes = extremes or []
        signals = signals or []

        facts: List[str] = []
        sent = 0
        sent_signal_keys = []
        deliveries: List[Dict[str, Any]] = []

        if not extremes and not signals:
            self.logger.info("[SwingReporter] 无趋势确立 / 无情绪极值，保持静默")
            return {"sent": 0, "silent": True, "facts": [], "sent_signal_keys": [], "deliveries": []}

        # 1. 大盘情绪极值（冰点 / 沸点）
        for ex in extremes:
            fact = (
                f"{ex.label} 触及历史{ex.level}："
                f"{ex.value}（分位 {ex.percentile}%，样本 {ex.sample_size}）"
            )
            facts.append(fact)
            # Ice-point/boil facts stay in the 15:50 daily report unless explicitly dispatched.
            if not dry_run and dispatch_macro:
                confirmed, summary = _deliver(
                    self.notifier, "send_macro_alert", fact, level=ex.level,
                    detail={"factor": ex.factor},
                )
                deliveries.append(summary)
                if confirmed:
                    sent += 1

        # 2. 趋势确立（周线金叉 / 放量破 MA60）
        for sig in signals:
            facts.append(f"{sig.title}（{sig.period}，置信度 {sig.confidence}）")
            if not dry_run:
                confirmed, summary = _deliver(
                    self.notifier, "send_trend_alert",
                    sig.name, sig.code, sig.title, sig.period, sig.detail,
                )
                deliveries.append(summary)
                # Only a confirmed delivery may consume the signal identity; uncertain /
                # not-sent / paused results leave it for review instead of marking it notified.
                if confirmed:
                    sent += 1
                    data_date = sig.detail.get("data_date") if isinstance(sig.detail, dict) else None
                    if data_date:
                        sent_signal_keys.append(
                            (sig.code, sig.signal_type, sig.period, str(data_date))
                        )

        self.logger.info(
            "[SwingReporter] 极值 %d 条 / 趋势 %d 条，已发送 %d 条 (dry_run=%s)",
            len(extremes),
            len(signals),
            sent,
            dry_run,
        )
        return {
            "sent": sent,
            "silent": False,
            "facts": facts,
            "sent_signal_keys": sent_signal_keys,
            "deliveries": deliveries,
            "unconfirmed": sum(1 for item in deliveries if not item.get("confirmed")),
        }
