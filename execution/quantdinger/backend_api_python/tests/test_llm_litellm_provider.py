import sys
from types import SimpleNamespace

import pytest
import requests

from app.services.llm import LLMProvider, LLMService
from app.utils.config_loader import clear_config_cache, load_addon_config


def _reset_config_cache():
    clear_config_cache()


def test_litellm_env_mapping(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "litellm-key")
    monkeypatch.setenv("LITELLM_MODEL", "anthropic/claude-sonnet-4-20250514")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example/v1")
    _reset_config_cache()

    cfg = load_addon_config()

    assert cfg["litellm"]["api_key"] == "litellm-key"
    assert cfg["litellm"]["model"] == "anthropic/claude-sonnet-4-20250514"
    assert cfg["litellm"]["base_url"] == "https://litellm.example/v1"


def test_atlascloud_env_mapping(monkeypatch):
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "atlas-key")
    monkeypatch.setenv("ATLASCLOUD_MODEL", "deepseek-v3")
    monkeypatch.setenv("ATLASCLOUD_BASE_URL", "https://api.atlascloud.ai/v1")
    _reset_config_cache()

    cfg = load_addon_config()

    assert cfg["atlascloud"]["api_key"] == "atlas-key"
    assert cfg["atlascloud"]["model"] == "deepseek-v3"
    assert cfg["atlascloud"]["base_url"] == "https://api.atlascloud.ai/v1"


def test_atlascloud_provider_defaults_and_model_prefix(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "atlascloud")
    monkeypatch.setenv("ATLASCLOUD_MODEL", "deepseek-v3")
    _reset_config_cache()

    service = LLMService()

    assert service.provider == LLMProvider.ATLASCLOUD
    assert service.get_default_model() == "deepseek-v3"
    assert service.get_base_url() == "https://api.atlascloud.ai/v1"
    assert (
        service._normalize_model_for_provider("atlascloud/deepseek-v3", LLMProvider.ATLASCLOUD)
        == "deepseek-v3"
    )
    assert service._detect_provider_from_model("atlascloud/deepseek-v3") == LLMProvider.ATLASCLOUD


def test_atlascloud_openai_compatible_call_skips_response_format(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("app.services.llm.requests.post", fake_post)
    service = LLMService(provider="atlascloud")

    out = service._call_openai_compatible(
        [{"role": "user", "content": "hello"}],
        "deepseek-v3",
        0.7,
        "atlas-key",
        "https://api.atlascloud.ai/v1",
        30,
        use_json_mode=True,
    )

    assert out == "{\"ok\": true}"
    assert captured["url"] == "https://api.atlascloud.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer atlas-key"
    assert captured["json"]["model"] == "deepseek-v3"
    assert "response_format" not in captured["json"]


def test_litellm_keeps_provider_prefixed_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "litellm")
    monkeypatch.setenv("LITELLM_MODEL", "anthropic/claude-sonnet-4-20250514")
    _reset_config_cache()

    service = LLMService()

    assert service.provider == LLMProvider.LITELLM
    assert service.get_default_model() == "anthropic/claude-sonnet-4-20250514"
    assert (
        service._normalize_model_for_provider("anthropic/claude-sonnet-4-20250514", LLMProvider.LITELLM)
        == "anthropic/claude-sonnet-4-20250514"
    )


def test_litellm_provider_can_call_without_litellm_api_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "litellm")
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    _reset_config_cache()

    captured = {}

    def fake_call(messages, model, temperature, api_key, base_url, timeout, use_json_mode=True):
        captured.update({"model": model, "api_key": api_key, "base_url": base_url})
        return "ok"

    service = LLMService()
    monkeypatch.setattr(service, "_call_litellm", fake_call)

    out = service.call_llm_api(
        [{"role": "user", "content": "hello"}],
        model="openai/gpt-4o-mini",
        try_alternative_providers=False,
        use_json_mode=False,
    )

    assert out == "ok"
    assert captured["model"] == "openai/gpt-4o-mini"
    assert captured["api_key"] == ""


def test_litellm_sdk_error_is_wrapped(monkeypatch):
    class FakeLiteLLM:
        @staticmethod
        def completion(**kwargs):
            raise RuntimeError("provider exploded")

    monkeypatch.setitem(sys.modules, "litellm", FakeLiteLLM)
    service = LLMService(provider="litellm")

    with pytest.raises(ValueError, match="LiteLLM API error"):
        service._call_litellm(
            [{"role": "user", "content": "hello"}],
            "openai/gpt-4o-mini",
            0.7,
            "",
            "",
            30,
            use_json_mode=False,
        )


def test_litellm_response_content(monkeypatch):
    class FakeLiteLLM:
        @staticmethod
        def completion(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))]
            )

    monkeypatch.setitem(sys.modules, "litellm", FakeLiteLLM)
    service = LLMService(provider="litellm")

    out = service._call_litellm(
        [{"role": "user", "content": "hello"}],
        "openai/gpt-4o-mini",
        0.7,
        "",
        "",
        30,
        use_json_mode=False,
    )

    assert out == "hello"


def test_kimi_coding_forces_supported_temperature(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("app.services.llm.requests.post", fake_post)
    service = LLMService(provider="openai")

    out = service._call_openai_compatible(
        [{"role": "user", "content": "hello"}],
        "kimi-for-coding",
        0.2,
        "kimi-key",
        "https://api.kimi.com/coding/v1",
        30,
        use_json_mode=False,
    )

    assert out == "ok"
    assert captured["json"]["temperature"] == 1


def test_kimi_coding_uses_supported_fallback_and_default_timeout(monkeypatch):
    calls = []

    def fake_call(messages, model, temperature, api_key, base_url, timeout, use_json_mode=True):
        calls.append({"model": model, "timeout": timeout})
        if len(calls) == 1:
            error = requests.exceptions.HTTPError("model not found")
            error.response = SimpleNamespace(status_code=404, text="model not found")
            raise error
        return "ok"

    monkeypatch.setattr("app.services.llm.load_addon_config", lambda: {})
    service = LLMService(provider="openai")
    monkeypatch.setattr(service, "get_api_key", lambda provider=None: "kimi-key")
    monkeypatch.setattr(
        service,
        "get_base_url",
        lambda provider=None: "https://api.kimi.com/coding/v1",
    )
    monkeypatch.setattr(service, "_call_openai_compatible", fake_call)

    out = service.call_llm_api(
        [{"role": "user", "content": "hello"}],
        model="legacy-model",
        provider=LLMProvider.OPENAI,
        try_alternative_providers=False,
        use_json_mode=False,
    )

    assert out == "ok"
    assert [call["model"] for call in calls] == ["legacy-model", "kimi-for-coding"]
    assert [call["timeout"] for call in calls] == [300, 300]
