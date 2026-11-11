import json

import pytest

from app.services import radar_context


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, _limit):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _configure(monkeypatch, tmp_path):
    token_file = tmp_path / "radar-token"
    token_file.write_text("test-token", encoding="utf-8")
    monkeypatch.setenv(
        "RADAR_CONTEXT_URL",
        "http://integration-api:8092/integration/v1/market-context",
    )
    monkeypatch.setenv("RADAR_CONTEXT_TOKEN_FILE", str(token_file))


def test_fetch_market_context_uses_bearer_file(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response({"status": "ok", "signals": []})

    monkeypatch.setattr(radar_context, "urlopen", fake_urlopen)

    result = radar_context.fetch_market_context()

    assert result == {"status": "ok", "signals": []}
    assert captured["authorization"] == "Bearer test-token"
    assert captured["timeout"] == radar_context.DEFAULT_TIMEOUT_SECONDS


def test_missing_token_file_fails_closed(monkeypatch):
    monkeypatch.setenv(
        "RADAR_CONTEXT_URL",
        "http://integration-api:8092/integration/v1/market-context",
    )
    monkeypatch.delenv("RADAR_CONTEXT_TOKEN_FILE", raising=False)

    with pytest.raises(radar_context.RadarContextError) as raised:
        radar_context.fetch_market_context()

    assert raised.value.code == "radar_not_configured"
    assert raised.value.http_status == 503
    assert raised.value.retriable is False


def test_non_object_response_is_rejected(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        radar_context,
        "urlopen",
        lambda *_args, **_kwargs: _Response(["unexpected"]),
    )

    with pytest.raises(radar_context.RadarContextError) as raised:
        radar_context.fetch_market_context()

    assert raised.value.code == "radar_invalid_response"


def test_route_requires_quantdinger_login(client, monkeypatch):
    called = False

    def should_not_call():
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("app.routes.radar.fetch_market_context", should_not_call)

    response = client.get("/api/radar/market-context")

    assert response.status_code == 401
    assert response.get_json()["code"] == 401
    assert called is False
