"""Minimal Telegram Bot API sender used by ``notify.send`` in live mode.

Only ``sendMessage`` is called.  The bot token is read from a root:qp 0640 file and is
never logged, embedded in an exception message, or written to the database.  Network
errors and Telegram 429/5xx responses are retryable; other 4xx responses are permanent.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from qp.config import Settings


class NotifyConfigError(RuntimeError):
    """Live delivery is configured but a required secret file is missing/empty."""


class TelegramError(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None, retryable: bool = False,
                 detail: str = "", retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail[:300]
        self.retry_after = retry_after


@dataclass(frozen=True)
class SendResult:
    message_id: int | None
    latency_ms: int


def _read_secret(path: str, label: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise NotifyConfigError(f"{label}_unreadable") from exc
    if not value:
        raise NotifyConfigError(f"{label}_empty")
    return value


class TelegramSender:
    def __init__(self, *, token: str, chat_id: str, api_base: str = "https://api.telegram.org",
                 proxy: str | None = None, timeout: float = 10.0):
        self._token = token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_settings(cls, st: Settings) -> "TelegramSender":
        token = _read_secret(st.telegram_bot_token_file, "telegram_token")
        chat_id = _read_secret(st.telegram_chat_id_file, "telegram_chat_id")
        return cls(token=token, chat_id=chat_id, api_base=st.telegram_api_base,
                   proxy=st.telegram_proxy, timeout=st.telegram_timeout_sec)

    def send(self, text: str) -> SendResult:
        data = urllib.parse.urlencode({
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self._api_base}/bot{self._token}/sendMessage",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Never include the request URL: it contains the bot token.
            raise TelegramError("network_error", retryable=True,
                                detail=type(exc).__name__) from None
        latency = int((time.monotonic() - started) * 1000)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise TelegramError("bad_response", retryable=True, detail="non-json") from None
        if not payload.get("ok"):
            code = str(payload.get("error_code") or "api_error")
            retryable = code == "429" or (code.isdigit() and int(code) >= 500)
            params = payload.get("parameters") or {}
            retry_after = params.get("retry_after")
            raise TelegramError(f"api_{code}", retryable=retryable,
                                detail=str(payload.get("description") or ""),
                                retry_after=int(retry_after) if isinstance(retry_after, int) else None)
        result = payload.get("result") or {}
        message_id = result.get("message_id")
        return SendResult(message_id=int(message_id) if isinstance(message_id, int) else None,
                          latency_ms=latency)

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> TelegramError:
        detail = ""
        retry_after = None
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str(payload.get("description") or "")
            params = payload.get("parameters") or {}
            if isinstance(params.get("retry_after"), int):
                retry_after = int(params["retry_after"])
        except Exception:
            pass
        return TelegramError(f"http_{exc.code}", status_code=exc.code,
                             retryable=(exc.code == 429 or exc.code >= 500),
                             detail=detail, retry_after=retry_after)
