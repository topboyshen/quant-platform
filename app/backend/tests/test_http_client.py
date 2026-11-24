"""qp.market.http: request shaping, response parsing, errors and TCP probe."""

from __future__ import annotations

import urllib.error

import pytest

from qp.market import http


class FakeResponse:
    status = 200
    headers = {"X-Test": "yes"}

    def __init__(self, body: bytes):
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(http, "MIN_INTERVAL_S", 0)
    http._last.clear()


def test_fetch_builds_get_and_post_and_parses(monkeypatch):
    opener = FakeOpener(FakeResponse(b'{"ok": true}'))
    monkeypatch.setattr(http, "_opener", lambda proxy: opener)
    got = http.fetch("https://example.test/path?a=1", params={"b": "2"}, headers={"X-A": "b"}, proxy=False)
    assert opener.request.full_url == "https://example.test/path?a=1&b=2"
    assert opener.request.get_method() == "GET"
    assert opener.request.get_header("X-a") == "b"
    assert got.json() == {"ok": True}
    assert got.text() == '{"ok": true}'
    assert got.via_proxy is False and got.headers == {"x-test": "yes"}

    post = http.fetch("https://example.test/post", data={"a": "b"})
    assert post.json() == {"ok": True}
    assert opener.request.get_method() == "POST"
    assert opener.request.data == b"a=b"


def test_fetch_rejects_large_body(monkeypatch):
    monkeypatch.setattr(http, "_opener", lambda proxy: FakeOpener(FakeResponse(b"1234567890")))
    with pytest.raises(http.FetchError, match="response_too_large"):
        http.fetch("https://example.test/", max_bytes=5)


def test_fetch_maps_http_and_transport_errors(monkeypatch):
    http_error = urllib.error.HTTPError("https://example.test/", 404, "nope", {}, None)
    monkeypatch.setattr(http, "_opener", lambda proxy: FakeOpener(error=http_error))
    with pytest.raises(http.FetchError, match="http_404"):
        http.fetch("https://example.test/")

    url_error = urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(http, "_opener", lambda proxy: FakeOpener(error=url_error))
    with pytest.raises(http.FetchError, match="TimeoutError"):
        http.fetch("https://example.test/")


def test_tcp_probe_success_and_failure(monkeypatch):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(http.socket, "create_connection", lambda *_a, **_kw: Conn())
    ok, ms, error = http.tcp_probe("127.0.0.1", 1, timeout=0.1)
    assert ok is True and error is None and ms >= 0

    def refuse(*_a, **_kw):
        raise ConnectionRefusedError("no")

    monkeypatch.setattr(http.socket, "create_connection", refuse)
    ok, _ms, error = http.tcp_probe("127.0.0.1", 1, timeout=0.1)
    assert ok is False and error == "ConnectionRefusedError"


def test_opener_and_rate_limit(monkeypatch):
    assert http._opener(False) is not None
    opener = FakeOpener(FakeResponse(b"ok"))
    monkeypatch.setattr(http, "_opener", lambda proxy: opener)
    monkeypatch.setattr(http, "MIN_INTERVAL_S", 1.0)
    monkeypatch.setattr(http.time, "monotonic", lambda: 100.0)
    sleeps = []
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    http._last["example.test"] = 99.5
    http.fetch("https://example.test/")
    assert sleeps == [0.5]
