"""TelegramNotifier: one POST per call, structured four-way classification (mock HTTP)."""

import logging
from unittest.mock import MagicMock

import pytest
import requests
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError
from urllib3.exceptions import ProxyError as Urllib3ProxyError

from radar.notify.telegram import (
    CONFIRMED,
    NOT_SENT_PERMANENT,
    NOT_SENT_RETRYABLE,
    UNCERTAIN,
    SendResult,
    TelegramNotifier,
)

TOKEN = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234"
CHAT = "987654321"


def _resp(status=200, body=None, json_error=False):
    response = MagicMock(status_code=status)
    if json_error:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    return response


def _ok_body(message_id=77, chat_id=int(CHAT)):
    return {"ok": True, "result": {"message_id": message_id, "chat": {"id": chat_id}}}


def _notifier(session, logger, chat=CHAT):
    return TelegramNotifier(TOKEN, chat, logger, session=session)


def _conn_error(inner):
    return requests.ConnectionError(MaxRetryError(None, "/sendMessage", inner))


def test_confirmed_requires_ok_integer_message_id_and_matching_chat(null_logger):
    session = MagicMock()
    session.post.return_value = _resp(200, _ok_body())
    result = _notifier(session, null_logger).send_once("t", "body")
    assert result == SendResult(CONFIRMED, "response", 200, message_id=77)
    assert bool(result) is True


@pytest.mark.parametrize(
    "body, error_class",
    [
        (None, "invalid_receipt"),
        ({"ok": False}, "invalid_receipt"),
        ({"ok": True, "result": {"message_id": "77", "chat": {"id": int(CHAT)}}}, "invalid_receipt"),
        ({"ok": True, "result": {"message_id": True, "chat": {"id": int(CHAT)}}}, "invalid_receipt"),
        ({"ok": True, "result": {"chat": {"id": int(CHAT)}}}, "invalid_receipt"),
        (_ok_body(chat_id=111), "chat_mismatch"),
        ({"ok": True, "result": {"message_id": 5}}, "chat_mismatch"),
    ],
)
def test_malformed_or_mismatched_200_is_uncertain(body, error_class, null_logger):
    session = MagicMock()
    session.post.return_value = _resp(200, body)
    result = _notifier(session, null_logger).send_once("t", "body")
    assert result.outcome == UNCERTAIN and result.error_class == error_class
    assert bool(result) is False


def test_status_200_without_json_body_is_uncertain(null_logger):
    session = MagicMock()
    session.post.return_value = _resp(200, json_error=True)
    assert _notifier(session, null_logger).send_once("t", "b").outcome == UNCERTAIN


def test_channel_username_target_is_matched_case_insensitively(null_logger):
    session = MagicMock()
    session.post.return_value = _resp(
        200, {"ok": True, "result": {"message_id": 3, "chat": {"id": -100123, "username": "MyChan"}}}
    )
    assert _notifier(session, null_logger, chat="@mychan").send_once("t", "b").confirmed


def test_429_with_retry_after_is_retryable_and_exposes_the_wait(null_logger):
    session = MagicMock()
    session.post.return_value = _resp(
        429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}
    )
    result = _notifier(session, null_logger).send_once("t", "b")
    assert (result.outcome, result.retry_after, result.http_status) == (NOT_SENT_RETRYABLE, 17.0, 429)


@pytest.mark.parametrize(
    "response",
    [
        _resp(429, json_error=True),
        _resp(429, {"ok": False, "error_code": 429}),
        _resp(429, {"ok": False, "parameters": {"retry_after": "soon"}}),
        _resp(429, {"ok": False, "parameters": {"retry_after": True}}),
        _resp(429, {"ok": False, "parameters": {"retry_after": -1}}),
    ],
)
def test_429_without_a_usable_json_retry_after_is_uncertain(response, null_logger):
    session = MagicMock()
    session.post.return_value = response
    assert _notifier(session, null_logger).send_once("t", "b").outcome == UNCERTAIN


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_telegram_json_rejection_is_permanent(status, null_logger):
    session = MagicMock()
    session.post.return_value = _resp(status, {"ok": False, "error_code": status, "description": "x"})
    result = _notifier(session, null_logger).send_once("t", "b")
    assert result.outcome == NOT_SENT_PERMANENT and result.http_status == status


@pytest.mark.parametrize("status", [400, 404])
def test_gateway_style_4xx_without_telegram_json_is_uncertain(status, null_logger):
    session = MagicMock()
    session.post.return_value = _resp(status, json_error=True)
    assert _notifier(session, null_logger).send_once("t", "b").outcome == UNCERTAIN


@pytest.mark.parametrize("status", [409, 500, 502, 503, 504])
def test_other_statuses_are_uncertain(status, null_logger):
    session = MagicMock()
    session.post.return_value = _resp(status, {"ok": False})
    assert _notifier(session, null_logger).send_once("t", "b").outcome == UNCERTAIN


def test_text_too_long_is_rejected_locally_without_posting(null_logger):
    session = MagicMock()
    result = _notifier(session, null_logger).send_once("t", "x" * 5000)
    assert (result.outcome, result.stage, result.error_class) == (
        NOT_SENT_PERMANENT, "preflight", "text_too_long")
    session.post.assert_not_called()


def test_disabled_notifier_is_permanent_and_never_posts(null_logger):
    session = MagicMock()
    n = TelegramNotifier("DISABLED", "DISABLED", null_logger, session=session)
    assert n.enabled is False
    assert n.send_once("t", "b").outcome == NOT_SENT_PERMANENT
    assert n.send("t", "b") is False
    session.post.assert_not_called()


@pytest.mark.parametrize(
    "exc, outcome",
    [
        (_conn_error(NewConnectionError(None, "dns failure")), NOT_SENT_RETRYABLE),
        (requests.ConnectTimeout(MaxRetryError(None, "/x", ConnectTimeoutError(None, "slow"))), NOT_SENT_RETRYABLE),
        (_conn_error(Urllib3ProxyError("Unable to connect to proxy", OSError("Tunnel connection failed: 502 Bad Gateway"))), NOT_SENT_RETRYABLE),
        (requests.exceptions.ProxyError(MaxRetryError(None, "/x", Urllib3ProxyError("Unable to connect to proxy", NewConnectionError(None, "refused")))), NOT_SENT_RETRYABLE),
        (requests.ReadTimeout("read timed out"), UNCERTAIN),
        (requests.ConnectionError("Connection aborted: RemoteDisconnected"), UNCERTAIN),
        (requests.ConnectionError("offline"), UNCERTAIN),
        (requests.exceptions.SSLError("EOF occurred in violation of protocol (SSLEOFError)"), UNCERTAIN),
        (requests.exceptions.ProxyError(MaxRetryError(None, "/x", Urllib3ProxyError("Unable to connect to proxy", OSError("handshake EOF")))), UNCERTAIN),
        (requests.exceptions.InvalidURL("bad"), NOT_SENT_PERMANENT),
        (RuntimeError("boom"), UNCERTAIN),
    ],
)
def test_transport_exception_classification(exc, outcome, null_logger):
    session = MagicMock()
    session.post.side_effect = exc
    assert _notifier(session, null_logger).send_once("t", "b").outcome == outcome


@pytest.mark.parametrize(
    "side_effect",
    [
        requests.ConnectionError("offline"),
        _conn_error(NewConnectionError(None, "dns")),
        requests.Timeout("slow"),
    ],
)
def test_send_makes_exactly_one_post_and_never_sleeps(side_effect, monkeypatch, null_logger):
    sleeper = MagicMock()
    monkeypatch.setattr("time.sleep", sleeper)
    session = MagicMock()
    session.post.side_effect = side_effect
    assert _notifier(session, null_logger).send("t", "b") is False
    assert session.post.call_count == 1
    sleeper.assert_not_called()


@pytest.mark.parametrize("status", [429, 503])
def test_bool_wrapper_has_no_internal_retry_on_http_status(status, monkeypatch, null_logger):
    sleeper = MagicMock()
    monkeypatch.setattr("time.sleep", sleeper)
    session = MagicMock()
    session.post.return_value = _resp(status, {"ok": False, "parameters": {"retry_after": 1}})
    assert _notifier(session, null_logger).send_trend_alert("n", "c", "s", "D") is False
    assert session.post.call_count == 1
    sleeper.assert_not_called()


def test_every_send_has_a_structured_ex_twin_and_bool_wrapper(null_logger):
    session = MagicMock()
    session.post.return_value = _resp(200, _ok_body())
    n = _notifier(session, null_logger)
    calls = [
        ("send_price_alert", ("N", "C", 1.0, 2.0, "below")),
        ("send_ml_alert", ("N", "C", "下破", 0.5, 1.0)),
        ("send_rl_alert", ("N", "C", "open")),
        ("send_trend_alert", ("N", "C", "S", "D")),
        ("send_macro_alert", ("fact",)),
        ("send_system_alert", ("t", "m")),
        ("send_datasource_alert", ("src", 3)),
    ]
    for name, args in calls:
        assert getattr(n, name)(*args) is True
        result = getattr(n, name + "_ex")(*args)
        assert isinstance(result, SendResult) and result.confirmed
    assert session.post.call_count == 2 * len(calls)


def test_logs_and_results_never_contain_token_chat_id_or_body(caplog, null_logger):
    body_marker = "SYNTHETIC-BODY-MARKER-XYZ"
    session = MagicMock()
    n = _notifier(session, null_logger)
    caplog.set_level(logging.DEBUG)
    null_logger.propagate = True
    null_logger.setLevel(logging.DEBUG)
    results = []
    for side_effect in (
        requests.ConnectionError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage chat {CHAT}"),
        requests.exceptions.SSLError(f"bot{TOKEN} {CHAT} {body_marker}"),
    ):
        session.post.side_effect = side_effect
        results.append(n.send_once("title", body_marker))
    session.post.side_effect = None
    for status in (400, 429, 503):
        session.post.return_value = _resp(
            status, {"ok": False, "description": f"chat {CHAT} {TOKEN} {body_marker}",
                     "parameters": {"retry_after": 3}})
        results.append(n.send_once("title", body_marker))
    text = "\n".join(r.getMessage() for r in caplog.records) + "\n".join(repr(r) for r in results)
    for secret in (TOKEN, CHAT, body_marker, TOKEN.split(":")[1]):
        assert secret not in text
