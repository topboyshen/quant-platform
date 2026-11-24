"""Telegram sender: secret loading, payload errors and retry classification."""

from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace

import pytest

from qp.notify.telegram import NotifyConfigError, TelegramError, TelegramSender, _read_secret


class Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Opener:
    def __init__(self, *, body=None, error=None):
        self.body = body
        self.error = error
        self.request = None

    def open(self, request, timeout=None):
        self.request = request
        if self.error:
            raise self.error
        return Response(self.body)


def sender(opener):
    s = TelegramSender(token="T", chat_id="C", api_base="https://tg.test")
    s._opener = opener
    return s


def test_read_secret_missing_and_empty(tmp_path):
    with pytest.raises(NotifyConfigError, match="telegram_token_unreadable"):
        _read_secret(str(tmp_path / "missing"), "telegram_token")
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(NotifyConfigError, match="telegram_token_empty"):
        _read_secret(str(empty), "telegram_token")
    good = tmp_path / "good"
    good.write_text(" value \n")
    assert _read_secret(str(good), "telegram_token") == "value"


def test_from_settings_reads_files(tmp_path):
    token = tmp_path / "token"
    chat = tmp_path / "chat"
    token.write_text("abc\n")
    chat.write_text("42\n")
    st = SimpleNamespace(
        telegram_bot_token_file=str(token),
        telegram_chat_id_file=str(chat),
        telegram_api_base="https://tg.test/",
        telegram_proxy=None,
        telegram_timeout_sec=5,
    )
    s = TelegramSender.from_settings(st)
    assert s._token == "abc" and s._chat_id == "42" and s._api_base == "https://tg.test"


def test_send_success_and_payload_errors():
    opener = Opener(body=json.dumps({"ok": True, "result": {"message_id": 7}}).encode())
    got = sender(opener).send("hello")
    assert got.message_id == 7
    assert opener.request.full_url.endswith("/botT/sendMessage")

    with pytest.raises(TelegramError) as bad_json:
        sender(Opener(body=b"not json")).send("x")
    assert bad_json.value.code == "bad_response" and bad_json.value.retryable is True

    with pytest.raises(TelegramError) as api_429:
        sender(Opener(body=b'{"ok": false, "error_code": 429, "parameters": {"retry_after": 3}}')).send("x")
    assert api_429.value.code == "api_429" and api_429.value.retryable is True
    assert api_429.value.retry_after == 3

    with pytest.raises(TelegramError) as api_400:
        sender(Opener(body=b'{"ok": false, "error_code": 400, "description": "bad"}')).send("x")
    assert api_400.value.code == "api_400" and api_400.value.retryable is False


def test_send_maps_http_and_network_errors():
    payload = b'{"description": "slow down", "parameters": {"retry_after": 9}}'
    http_error = urllib.error.HTTPError("https://tg.test", 429, "rate", {}, None)
    http_error.read = lambda: payload  # type: ignore[method-assign]
    with pytest.raises(TelegramError) as rate:
        sender(Opener(error=http_error)).send("x")
    assert rate.value.code == "http_429" and rate.value.retryable is True
    assert rate.value.retry_after == 9

    network = urllib.error.URLError(TimeoutError("timeout"))
    with pytest.raises(TelegramError) as err:
        sender(Opener(error=network)).send("x")
    assert err.value.code == "network_error" and err.value.retryable is True


def test_http_error_with_unreadable_body_is_tolerated():
    error = urllib.error.HTTPError("https://tg.test", 500, "server", {}, None)
    error.read = lambda: (_ for _ in ()).throw(ValueError("bad body"))  # type: ignore[method-assign]
    mapped = TelegramSender._http_error(error)
    assert mapped.code == "http_500" and mapped.retryable is True and mapped.detail == ""
