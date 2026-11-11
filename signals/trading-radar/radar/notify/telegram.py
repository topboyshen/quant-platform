"""
通知层 - Telegram 通知模块
职责：发送告警消息，与业务逻辑解耦

配置来源（优先级）：
1. 环境变量 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（systemd 推荐方式）
2. config.json 中的 telegram 字段
"""

import html
import logging
import math
import re
from typing import Any, Optional, Tuple

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.exceptions import ProxyError as _Urllib3ProxyError

from radar.data.session_pool import create_pooled_session
from radar.notify.result import (  # noqa: F401 - re-exported for callers
    CONFIRMED,
    MAX_TEXT_UTF16_UNITS,
    NOT_SENT_PERMANENT,
    NOT_SENT_RETRYABLE,
    OUTCOMES,
    UNCERTAIN,
    SendResult,
)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class TelegramNotifier:
    """Telegram 通知器"""

    _TOKEN_IN_URL_RE = re.compile(r"/bot[^/\s]+")

    @classmethod
    def _redact_error(cls, value: object) -> str:
        return cls._TOKEN_IN_URL_RE.sub('/bot***REDACTED***', str(value))

    @staticmethod
    def _response_status(response: object) -> Optional[int]:
        """Return a real HTTP status without treating ``MagicMock`` as one."""
        status = getattr(response, "status_code", None)
        return status if isinstance(status, int) and not isinstance(status, bool) else None

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        logger: logging.Logger,
        session: Optional[requests.Session] = None,
    ):
        disabled_values = {'DISABLED', 'REPLACE', 'REPLACE_ME', 'NONE', ''}
        token_text = str(bot_token or '').strip()
        chat_text = str(chat_id or '').strip()
        self.enabled = not (
            token_text.upper() in disabled_values
            or chat_text.upper() in disabled_values
        )
        if self.enabled and (len(token_text) <= 20 or 'REPLACE' in token_text):
            raise ValueError("无效的 bot_token: ***REDACTED***")
        self.bot_token = token_text
        self.chat_id = chat_text
        self.logger = logger
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        if session is None:
            session = create_pooled_session()
            # Session-level backstop for the sessions we own: if anything ever tried to follow
            # a redirect, requests would raise before re-sending the request.
            session.max_redirects = 0
        self._session = session

    # ------------------------------------------------------------------ transport
    def target_digest(self) -> str:
        from radar.notify.identity import target_digest

        return target_digest("telegram", self.bot_token, self.chat_id)

    def _chat_matches(self, chat: object) -> bool:
        if not isinstance(chat, dict):
            return False
        configured = self.chat_id
        chat_id = chat.get("id")
        if re.fullmatch(r"-?\d+", configured):
            return type(chat_id) is int and chat_id == int(configured)
        if configured.startswith("@"):
            username = chat.get("username")
            return isinstance(username, str) and username.lower() == configured[1:].lower()
        return False

    @staticmethod
    def _error_chain(exc: BaseException):
        """Yield every exception reachable through args/cause/context/reason/original_error."""
        seen = set()
        stack = [exc]
        while stack:
            item = stack.pop()
            if id(item) in seen or not isinstance(item, BaseException):
                continue
            seen.add(id(item))
            yield item
            stack.extend(a for a in getattr(item, "args", ()) if isinstance(a, BaseException))
            for attr in ("__cause__", "__context__", "reason", "original_error"):
                nested = getattr(item, attr, None)
                if isinstance(nested, BaseException):
                    stack.append(nested)

    @classmethod
    def classify_exception(cls, exc: BaseException) -> SendResult:
        """Map a transport exception to an outcome.  Only *evidence that the request was
        never sent* (TCP connect never completed, DNS failure, proxy tunnel never opened)
        is retryable; anything else may have reached Telegram and is ``uncertain``."""
        error_class = type(exc).__name__
        chain = list(cls._error_chain(exc))
        if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                            requests.exceptions.InvalidSchema, requests.exceptions.InvalidHeader,
                            requests.exceptions.InvalidProxyURL)):
            return SendResult(NOT_SENT_PERMANENT, "preflight", error_class=error_class)
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return SendResult(NOT_SENT_RETRYABLE, "connect", error_class="ConnectTimeout")
        for item in chain:
            if isinstance(item, NewConnectionError):
                return SendResult(NOT_SENT_RETRYABLE, "connect", error_class=type(item).__name__)
            if isinstance(item, ConnectTimeoutError):
                return SendResult(NOT_SENT_RETRYABLE, "connect", error_class="ConnectTimeoutError")
        for item in chain:
            if isinstance(item, _Urllib3ProxyError):
                inner = getattr(item, "original_error", None)
                if isinstance(inner, OSError) and "Tunnel connection failed" in str(inner):
                    return SendResult(NOT_SENT_RETRYABLE, "proxy_connect", error_class="ProxyTunnelFailed")
        return SendResult(UNCERTAIN, "unknown", error_class=error_class)

    def _interpret_response(self, response: object) -> SendResult:
        status = self._response_status(response)
        if status is None:
            return SendResult(UNCERTAIN, "response", error_class="no_http_status")
        if 300 <= status < 400:
            # Never followed (see ``_send_once``).  Whether the redirecting hop already acted
            # on the request is unknown, so this is not evidence of non-delivery.
            return SendResult(UNCERTAIN, "redirect", status, f"redirect_{status}")
        body: Any = None
        try:
            body = response.json()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a non-JSON body is itself a signal
            body = None
        if not isinstance(body, dict):
            body = None
        if status == 200:
            result = body.get("result") if body else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not (body and body.get("ok") is True and type(message_id) is int):
                return SendResult(UNCERTAIN, "response", status, "invalid_receipt")
            if not self._chat_matches(result.get("chat")):
                return SendResult(UNCERTAIN, "response", status, "chat_mismatch", message_id=message_id)
            return SendResult(CONFIRMED, "response", status, message_id=message_id)
        if status == 429:
            parameters = body.get("parameters") if body else None
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if (body and body.get("ok") is False and isinstance(retry_after, (int, float))
                    and not isinstance(retry_after, bool) and math.isfinite(retry_after)
                    and retry_after >= 0):
                return SendResult(NOT_SENT_RETRYABLE, "rate_limit", 429, "telegram_429",
                                  retry_after=float(retry_after))
            return SendResult(UNCERTAIN, "response", 429, "rate_limit_without_retry_after")
        if status in (400, 401, 403, 404):
            if body and body.get("ok") is False:
                return SendResult(NOT_SENT_PERMANENT, "platform_reject", status, f"telegram_{status}")
            return SendResult(UNCERTAIN, "response", status, "non_telegram_rejection")
        return SendResult(UNCERTAIN, "response", status, f"http_{status}")

    def send_once(self, title: str, content: str) -> SendResult:
        """Make exactly one POST and classify the result.  Never retries, never sleeps."""
        self.logger.info(f"[🔴 警报中] {title}")
        result = self._send_once(title, content)
        log = self.logger.info if result.confirmed else self.logger.warning
        log(
            "Telegram send_once outcome=%s stage=%s http_status=%s error_class=%s retry_after=%s",
            result.outcome, result.stage, result.http_status, result.error_class, result.retry_after,
        )
        return result

    def _send_once(self, title: str, content: str) -> SendResult:
        if not self.enabled:
            return SendResult(NOT_SENT_PERMANENT, "preflight", error_class="disabled")
        if not self.bot_token or "REPLACE" in self.bot_token:
            return SendResult(NOT_SENT_PERMANENT, "preflight", error_class="token_not_configured")
        plain_text = f"⚠️ *{title}*\n\n{content}"
        if _utf16_len(plain_text) > MAX_TEXT_UTF16_UNITS:
            return SendResult(NOT_SENT_PERMANENT, "preflight", error_class="text_too_long")
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": f"⚠️ *{html.escape(title)}*\n\n{html.escape(content)}",
            "parse_mode": "HTML",
        }
        try:
            # allow_redirects=False: a 307/308 would re-POST the same message to another URL
            # (301/302 would turn it into a GET) behind the outbox's attempt accounting.
            response = self._session.post(url, json=payload, timeout=10, allow_redirects=False)
        except requests.exceptions.TooManyRedirects as exc:
            # The owned session has max_redirects=0: requests raises *before* sending the
            # follow-up request, so this is the same "redirect seen, not followed" case.
            status = self._response_status(getattr(exc, "response", None))
            return SendResult(UNCERTAIN, "redirect", status,
                              f"redirect_{status}" if status else "redirect")
        except Exception as exc:  # noqa: BLE001 - classified below; message never logged
            return self.classify_exception(exc)
        try:
            return self._interpret_response(response)
        except Exception as exc:  # noqa: BLE001
            return SendResult(UNCERTAIN, "response", error_class=type(exc).__name__)

    def send(self, title: str, content: str) -> bool:
        """Boolean compatibility wrapper: exactly one attempt, True only if confirmed."""
        return bool(self.send_once(title, content))

    def _build_price_alert(self, name: str, code: str, price: float,
                         trigger: float, direction: str, **kwargs) -> Tuple[str, str]:
        """发送价格触发告警"""
        direction_text = "跌破" if direction == 'below' else "突破"
        action_word = "卖出/减仓" if direction == 'below' else "买入/加仓"
        direction_emoji = "📉" if direction == 'below' else "📈"

        # 动态触发价信息
        original_trigger = kwargs.get('original_trigger', trigger)
        is_adaptive = kwargs.get('is_adaptive', False)
        adaptive_hint = ""
        if is_adaptive and abs(original_trigger - trigger) > 0.01:
            adaptive_hint = f" (原始触发价: ¥{original_trigger:.2f})"

        content = (
            f"📌 {name} ({code})\n"
            f"{direction_emoji} 方向: {action_word}（{direction_text}监控价）\n"
            f"💰 当前价: ¥{price:.2f}\n"
            f"🎯 监控价: ¥{trigger:.2f}{adaptive_hint}\n"
            f"📉 方向: {direction_text}"
        )

        # 添加可选信息
        if 'volume_spike' in kwargs:
            content += f"\n📊 成交量异常: {kwargs['volume_spike']:.1f}倍"
        if 'ml_prediction' in kwargs:
            content += f"\n🤖 ML预测: {kwargs['ml_prediction']}"
        if 'rl_signal' in kwargs:
            content += f"\n🧠 RL信号: {kwargs['rl_signal']}"

        return (f"价格触发提醒 - {name}", content)

    def _build_ml_alert(self, name: str, code: str, prediction: str,
                      probability: float, target_price: float,
                      model_confidence: str = None,
                      triggered: bool = False,
                      current_price: float = 0.0) -> Tuple[str, str]:
        """发送 ML 预测告警（probability 为「未来窗口触发」的估计概率，非校准置信度）"""
        # 根据触发状态选择标题
        if triggered:
            title = f"✅ ML确认 - {name}（已触发）"
        else:
            title = f"ML预测提醒 - {name}"

        # 提取方向语义：prediction 字符串中包含 "下破"/"上破"
        direction_emoji = "📉"
        action_word = "卖出/减仓"
        if "上破" in prediction or "above" in prediction.lower():
            direction_emoji = "📈"
            action_word = "买入/加仓"

        content = (
            f"📌 {name} ({code})\n"
            f"{direction_emoji} 方向: {action_word}\n"
            f"🤖 {prediction}\n"
            f"📊 触发概率: {probability:.1%}\n"
            f"🎯 监控价: ¥{target_price:.2f}"
        )
        if triggered and current_price > 0:
            content += f"\n💰 当前价: ¥{current_price:.2f}"
        if model_confidence:
            mc = str(model_confidence).lower()
            mc_zh = {'high': '高', 'medium': '中', 'low': '低'}.get(mc, model_confidence)
            content += f"\n🏷 信号强度: {mc_zh}"
        return (title, content)

    def _build_rl_alert(self, name: str, code: str, action: str,
                      confidence: Optional[float] = None, position_status: Optional[str] = None,
                      position_pnl: Optional[str] = None, suggestion: Optional[str] = None) -> Tuple[str, str]:
        """发送 RL 信号告警"""
        action_map = {
            'hold': '持有',
            'open': '开仓',
            'add': '加仓',
            'reduce': '减仓',
            'close': '平仓'
        }

        # 判断是确认模式还是独立模式
        if suggestion:
            # 确认模式：RL 作为辅助建议
            content = (
                f"📌 股票: {name} ({code})\n"
                f"🧠 RL建议: {suggestion}\n"
            )
            if position_status:
                content += f"📊 持仓状态: {position_status}\n"
            if position_pnl:
                content += f"💹 持仓盈亏: {position_pnl}\n"
        else:
            # 独立模式：RL 自主决策
            action_zh = action_map.get(action, action)
            action_emoji = "📈" if action in ('open', 'add') else "📉" if action in ('reduce', 'close') else "➖"
            content = (
                f"📌 股票: {name} ({code})\n"
                f"{action_emoji} 方向: {action_zh}\n"
                f"🧠 RL决策: {action_zh}"
            )
            if position_status:
                content += f"\n📊 持仓状态: {position_status}"
            if position_pnl:
                content += f"\n💹 持仓盈亏: {position_pnl}"

        if confidence is not None:
            content += f"\n📊 置信度: {confidence:.2f}"

        return (f"🧠 RL策略信号 - {name}", content)

    def _build_trend_alert(self, name: str, code: str, signal: str,
                         period: str, detail: Optional[dict] = None) -> Tuple[str, str]:
        """发送波段趋势确认告警（右侧拔剑通知器）。

        period: 'D' 日线 / 'W' 周线。detail 为信号细节（MA/MACD 数值等）。
        """
        period_text = {"D": "日线", "W": "周线"}.get(period, period)
        lines = [
            f"📈 {name} ({code})",
            f"🗡 {signal}",
            f"🕐 周期: {period_text}",
        ]
        detail = detail or {}
        for k, v in detail.items():
            lines.append(f"• {k}: {v}")
        return (f"趋势确认 - {name}", "\n".join(lines))

    def _build_macro_alert(self, fact: str, level: str = "info",
                         detail: Optional[dict] = None) -> Tuple[str, str]:
        """发送大盘宏观情绪告警（冰点 / 沸点）。

        level: '冰点' / '沸点' / 其它。fact 为一行冰冷事实。
        """
        icon = {"冰点": "🧊", "沸点": "🔥"}.get(level, "📊")
        lines = [f"{icon} {fact}"]
        detail = detail or {}
        for k, v in detail.items():
            lines.append(f"• {k}: {v}")
        return (f"大盘情绪{('' if level in ('冰点', '沸点') else '播报')}{level if level in ('冰点', '沸点') else ''}", "\n".join(lines))

    def _build_system_alert(self, title: str, message: str) -> Tuple[str, str]:
        """发送系统级告警"""
        return (f"系统通知 - {title}", message)

    def _build_datasource_alert(self, source_name: str, failure_count: int,
                              status: str = "abnormal") -> Tuple[str, str]:
        """发送数据源异常告警"""
        if status == "abnormal":
            title = f"🚨 数据源异常 - {source_name}"
            content = (
                f"⚠️ 数据源 {source_name} 连续失败 {failure_count} 次，已触发熔断保护。\n\n"
                f"📌 说明: 系统将在静默期后自动尝试恢复。\n"
                f"⏰ 静默时长: 30 秒\n"
                f"🔧 建议: 检查网络连接或 API 服务状态"
            )
        else:
            title = f"✅ 数据源恢复 - {source_name}"
            content = f"数据源 {source_name} 已恢复正常，熔断器关闭。"

        return (title, content)

    def send_price_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_price_alert(*args, **kwargs))

    def send_price_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_price_alert(*args, **kwargs))

    def send_ml_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_ml_alert(*args, **kwargs))

    def send_ml_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_ml_alert(*args, **kwargs))

    def send_rl_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_rl_alert(*args, **kwargs))

    def send_rl_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_rl_alert(*args, **kwargs))

    def send_trend_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_trend_alert(*args, **kwargs))

    def send_trend_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_trend_alert(*args, **kwargs))

    def send_macro_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_macro_alert(*args, **kwargs))

    def send_macro_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_macro_alert(*args, **kwargs))

    def send_system_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_system_alert(*args, **kwargs))

    def send_system_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_system_alert(*args, **kwargs))

    def send_datasource_alert(self, *args, **kwargs) -> bool:
        """Boolean wrapper (single attempt, True only when confirmed)."""
        return self.send(*self._build_datasource_alert(*args, **kwargs))

    def send_datasource_alert_ex(self, *args, **kwargs) -> SendResult:
        """Structured variant: one attempt, classified outcome."""
        return self.send_once(*self._build_datasource_alert(*args, **kwargs))
