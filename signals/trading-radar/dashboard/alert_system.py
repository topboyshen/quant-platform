"""
Radar 告警系统 v3.1
多级告警通道：Telegram / Webhook
"""

import json
import math
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

import requests

from radar.data.session_pool import create_pooled_session

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AlertEvent:
    """告警事件"""
    type: str  # price_trigger, strategy_signal, system_alert
    symbol: str
    message: str
    severity: str = "info"  # info, warning, critical
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    data: Dict[str, Any] = field(default_factory=dict)


class AlertChannel:
    """告警通道抽象基类"""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    def send(self, event: AlertEvent) -> bool:
        """发送告警，返回是否成功"""
        if not self.enabled:
            return False
        try:
            return self._do_send(event)
        except Exception as e:
            logger.error(f"[{self.name}] 告警发送失败: {e}")
            return False

    def _do_send(self, event: AlertEvent) -> bool:
        raise NotImplementedError


class TelegramAlertChannel(AlertChannel):
    """Telegram Bot 告警通道"""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        super().__init__("telegram", enabled)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session = create_pooled_session()

    def _do_send(self, event: AlertEvent) -> bool:
        emoji_map = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        emoji = emoji_map.get(event.severity, "📢")

        text = (
            f"{emoji} <b>股票监控告警</b>\n"
            f"<b>类型:</b> {event.type}\n"
            f"<b>股票:</b> {event.symbol}\n"
            f"<b>严重级:</b> {event.severity.upper()}\n"
            f"<b>时间:</b> {event.timestamp[:19]}\n"
            f"<b>消息:</b> {event.message}"
        )

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        params = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        retry_delays = (1.0, 2.0)
        for attempt in range(3):
            try:
                resp = self._session.post(url, json=params, timeout=10)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt == 2:
                    logger.error("[Telegram] 3 次发送均失败: %s", exc)
                    return False
                delay = retry_delays[attempt]
                logger.warning(
                    "[Telegram] 连接失败，%.0f 秒后重试 (%d/3): %s",
                    delay,
                    attempt + 1,
                    exc,
                )
                time.sleep(delay)
                continue
            except requests.exceptions.RequestException as exc:
                logger.error("[Telegram] 请求失败（不重试）: %s", exc)
                return False

            if resp.status_code == 200:
                logger.info(f"[Telegram] 告警发送成功: {event.symbol}")
                return True

            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            if not retryable or attempt == 2:
                logger.warning(
                    "[Telegram] API 返回 %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False

            delay = retry_delays[attempt]
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = min(max(float(retry_after), 0.0), 10.0)
                except (TypeError, ValueError):
                    pass
            logger.warning(
                "[Telegram] API 返回 %s，%.0f 秒后重试 (%d/3)",
                resp.status_code,
                delay,
                attempt + 1,
            )
            time.sleep(delay)

        return False


class WebhookAlertChannel(AlertChannel):
    """Webhook 告警通道（支持钉钉/飞书/自定义）"""

    def __init__(self, webhook_url: str, headers: Optional[Dict] = None,
                 template: Optional[str] = None, enabled: bool = True):
        super().__init__("webhook", enabled)
        self.webhook_url = webhook_url
        self.headers = headers or {"Content-Type": "application/json"}
        self.template = template
        self._session = create_pooled_session()

    def _do_send(self, event: AlertEvent) -> bool:
        if self.template:
            payload = self.template.format(
                type=event.type,
                symbol=event.symbol,
                message=event.message,
                severity=event.severity,
                timestamp=event.timestamp,
                data=json.dumps(event.data)
            )
            try:
                body = json.loads(payload)
            except json.JSONDecodeError:
                body = {"text": payload}
        else:
            body = {
                "msgtype": "text",
                "text": {
                    "content": f"[股票监控] {event.severity.upper()} | {event.symbol}\n{event.message}"
                }
            }

        resp = self._session.post(
            self.webhook_url,
            json=body,
            headers=self.headers,
            timeout=10
        )
        if resp.status_code in (200, 204):
            logger.info(f"[Webhook] 告警发送成功: {event.symbol}")
            return True
        else:
            logger.warning(f"[Webhook] 返回 {resp.status_code}: {resp.text[:200]}")
            return False


class AlertManager:
    """告警管理器 - 管理多个告警通道"""

    def __init__(self):
        self._channels: List[AlertChannel] = []
        self._cooldown: Dict[str, float] = {}
        self._cooldown_seconds = 300
        self._failure_retry_seconds = 60
        self._retry_after: Dict[str, float] = {}

    def add_channel(self, channel: AlertChannel) -> None:
        self._channels.append(channel)

    def send(
        self, event: AlertEvent, bypass_cooldown: bool = False
    ) -> Optional[Dict[str, bool]]:
        now = time.time()
        key = f"{event.symbol}:{event.type}"
        if not bypass_cooldown:
            if now < self._retry_after.get(key, 0):
                return None

        results = {}
        for ch in self._channels:
            results[ch.name] = ch.send(event)

        if not bypass_cooldown:
            if not self._channels or any(results.values()):
                # 只有实际送达（或未配置外部通道）才消耗正常冷却。
                self._cooldown[key] = now
                self._retry_after[key] = now + self._cooldown_seconds
            else:
                # 代理短暂故障时保留重试机会，同时避免每个扫描周期轰炸上游。
                self._retry_after[key] = now + self._failure_retry_seconds
        return results

    def check_price_alert(self, target: Dict[str, Any], tick: Any) -> Optional[AlertEvent]:
        """检查是否触发价格告警

        Args:
            target: 监控目标配置，含 trigger_price, direction, name, code
            tick: TickData 对象，含 price

        Returns:
            AlertEvent if triggered, else None
        """
        trigger_price = target.get('trigger_price')
        direction = target.get('direction')
        current_price = getattr(tick, 'price', None)

        if trigger_price is None or direction is None or current_price is None:
            return None
        if getattr(tick, "is_stale", False) or getattr(tick, "source", "") == "error":
            return None
        try:
            current_price = float(current_price)
            trigger_price = float(trigger_price)
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(current_price)
            or current_price <= 0
            or not math.isfinite(trigger_price)
            or trigger_price <= 0
            or direction not in {"above", "below"}
        ):
            return None

        triggered = False
        if direction == 'below' and current_price <= trigger_price:
            triggered = True
        elif direction == 'above' and current_price >= trigger_price:
            triggered = True

        if not triggered:
            return None

        name = target.get('name', target.get('code', ''))
        message = (
            f"{name}({target['code']}) 触发价格告警: "
            f"当前 {current_price:.2f} { '≤' if direction == 'below' else '≥' } "
            f"目标 {trigger_price:.2f}"
        )

        return AlertEvent(
            type='price_trigger',
            symbol=target['code'],
            message=message,
            severity='warning',
            data={
                'trigger_price': trigger_price,
                'current_price': current_price,
                'direction': direction,
                'name': name,
            }
        )
