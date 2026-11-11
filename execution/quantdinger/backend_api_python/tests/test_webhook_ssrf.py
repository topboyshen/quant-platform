import hashlib
import hmac
import socket
from unittest import mock

import pytest

from app.services.signal_notifier import SignalNotifier


def _addrinfo(address, port=443, family=socket.AF_INET):
    return [(family, socket.SOCK_STREAM, 6, "", (address, port))]


def _response(status_code=200, text="ok", location=None):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    response.headers = {"Location": location} if location else {}
    return response


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/hook",
        "http://172.16.0.1/hook",
        "http://192.168.1.1/hook",
        "http://[::1]/hook",
        "http://0.0.0.0/hook",
        "http://224.0.0.1/hook",
        "http://198.51.100.1/hook",
    ],
)
def test_webhook_rejects_non_public_ip_literals_before_http(url):
    post = mock.Mock()
    resolve = mock.Mock(side_effect=AssertionError("IP literals must not need DNS"))

    with mock.patch("app.services.signal_notifier.requests.post", post), mock.patch(
        "app.services.signal_notifier.socket.getaddrinfo", resolve
    ):
        ok, error = SignalNotifier()._notify_webhook(url=url, payload={"event": "test"})

    assert not ok
    assert error == "unsafe_webhook_url"
    post.assert_not_called()
    resolve.assert_not_called()


def test_webhook_rejects_hostname_resolving_to_private_address():
    post = mock.Mock()
    resolve = mock.Mock(return_value=_addrinfo("10.20.30.40"))

    with mock.patch("app.services.signal_notifier.requests.post", post), mock.patch(
        "app.services.signal_notifier.socket.getaddrinfo", resolve
    ):
        ok, error = SignalNotifier()._notify_webhook(
            url="https://internal.example/hook", payload={"event": "test"}
        )

    assert not ok
    assert error == "unsafe_webhook_url"
    post.assert_not_called()
    resolve.assert_called_once()


def test_webhook_revalidates_redirect_target_before_following_it():
    post = mock.Mock(return_value=_response(302, location="http://redirected.internal/hook"))

    def resolve(host, port, *args, **kwargs):
        addresses = {
            "public.example": "93.184.216.34",
            "redirected.internal": "192.168.1.20",
        }
        return _addrinfo(addresses[host], port)

    with mock.patch("app.services.signal_notifier.requests.post", post), mock.patch(
        "app.services.signal_notifier.socket.getaddrinfo", side_effect=resolve
    ) as resolver:
        ok, error = SignalNotifier()._notify_webhook(
            url="https://public.example/hook", payload={"event": "test"}
        )

    assert not ok
    assert error == "unsafe_webhook_url"
    post.assert_called_once()
    assert post.call_args.kwargs["allow_redirects"] is False
    assert resolver.call_count == 2


def test_webhook_allows_public_url_and_preserves_generic_signing():
    post = mock.Mock(return_value=_response())
    resolve = mock.Mock(return_value=_addrinfo("93.184.216.34"))

    with mock.patch("app.services.signal_notifier.requests.post", post), mock.patch(
        "app.services.signal_notifier.socket.getaddrinfo", resolve
    ), mock.patch("app.services.signal_notifier.time.time", return_value=1700000000):
        ok, error = SignalNotifier()._notify_webhook(
            url="https://public.example/hook",
            payload={"event": "test"},
            signing_secret_override="signing-secret",
        )

    assert ok, error
    post.assert_called_once()
    kwargs = post.call_args.kwargs
    assert kwargs["allow_redirects"] is False
    body = kwargs["data"]
    timestamp = kwargs["headers"]["X-QD-Timestamp"]
    expected = hmac.new(
        b"signing-secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert kwargs["headers"]["X-QD-Signature"] == expected


def test_discord_webhook_rejects_private_ip_before_http():
    post = mock.Mock()
    with mock.patch("app.services.signal_notifier.requests.post", post):
        ok, error = SignalNotifier()._notify_discord(
            url="http://127.0.0.1/discord", payload={}, fallback_text="test"
        )

    assert not ok
    assert error == "unsafe_webhook_url"
    post.assert_not_called()


def test_discord_webhook_allows_public_url_without_following_redirects():
    post = mock.Mock(return_value=_response(204))
    resolve = mock.Mock(return_value=_addrinfo("93.184.216.34"))
    with mock.patch("app.services.signal_notifier.requests.post", post), mock.patch(
        "app.services.signal_notifier.socket.getaddrinfo", resolve
    ):
        ok, error = SignalNotifier()._notify_discord(
            url="https://discord.example/webhook", payload={}, fallback_text="test"
        )

    assert ok, error
    assert post.call_args.kwargs["allow_redirects"] is False
