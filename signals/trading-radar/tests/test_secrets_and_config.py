"""环境与配置覆盖逻辑测试。"""

import json
import os

import pytest

from radar.secrets import SecretConfigurationError, merge_env_into_config_data

_SENSITIVE_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
)


def _clear_secret_env(monkeypatch):
    for name in _SENSITIVE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)


def test_merge_env_no_vars_returns_same_object(monkeypatch):
    _clear_secret_env(monkeypatch)
    d = {"telegram": {"bot_token": "x", "chat_id": "1"}}
    out = merge_env_into_config_data(d)
    assert out is d


def test_merge_env_overrides_token(monkeypatch):
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "9")
    d = {"telegram": {"bot_token": "x", "chat_id": "1"}}
    out = merge_env_into_config_data(d)
    assert out["telegram"]["bot_token"] == "tok"
    assert out["telegram"]["chat_id"] == "9"


def test_merge_env_creates_telegram_block(monkeypatch):
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    out = merge_env_into_config_data({})
    assert out["telegram"]["bot_token"] == "t"
    assert out["telegram"]["chat_id"] == "c"


def test_merge_file_overrides_token_without_copying_to_environment(
    tmp_path, monkeypatch
):
    _clear_secret_env(monkeypatch)
    token_file = tmp_path / "telegram-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    token_file.chmod(0o400)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(token_file))

    out = merge_env_into_config_data(
        {"telegram": {"bot_token": "placeholder", "chat_id": "1"}}
    )

    assert out["telegram"]["bot_token"] == "file-token"
    assert "TELEGRAM_BOT_TOKEN" not in os.environ


def test_merge_file_and_direct_value_are_mutually_exclusive(tmp_path, monkeypatch):
    _clear_secret_env(monkeypatch)
    token_file = tmp_path / "telegram-token"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "direct-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(token_file))

    with pytest.raises(SecretConfigurationError, match="mutually exclusive"):
        merge_env_into_config_data({})


@pytest.mark.parametrize("unsafe", ["symlink", "writable", "empty"])
def test_merge_file_rejects_unsafe_secret_sources(tmp_path, monkeypatch, unsafe):
    _clear_secret_env(monkeypatch)
    target = tmp_path / "target"
    target.write_text("file-token", encoding="utf-8")
    secret_path = target
    if unsafe == "symlink":
        secret_path = tmp_path / "link"
        secret_path.symlink_to(target)
    elif unsafe == "writable":
        target.chmod(0o622)
    else:
        target.write_text("", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(secret_path))

    with pytest.raises(SecretConfigurationError, match="unavailable or unsafe"):
        merge_env_into_config_data({})


def test_merge_env_overrides_llm_section(monkeypatch):
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-abc")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-x")
    d = {"llm": {"api_key": "REPLACE", "model": "old"}}
    out = merge_env_into_config_data(d)
    assert out["llm"]["api_key"] == "sk-abc"
    assert out["llm"]["base_url"] == "https://example.com/v1"
    assert out["llm"]["model"] == "deepseek-x"
    # 不污染原 dict
    assert d["llm"]["api_key"] == "REPLACE"


def test_merge_env_partial_llm_only_sets_provided(monkeypatch):
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "k")
    out = merge_env_into_config_data({"llm": {"model": "keep"}})
    assert out["llm"]["api_key"] == "k"
    assert out["llm"]["model"] == "keep"
    assert "base_url" not in out["llm"]


def test_radar_monitor_bundle_reads_json(tmp_path, monkeypatch):
    _clear_secret_env(monkeypatch)
    cfg = {
        "telegram": {"bot_token": "b", "chat_id": "1"},
        "radar": {
            "cooldown_minutes": 5,
            "scan_interval_seconds": 60,
            "cache_file": "c.json",
            "log_file": "l.log",
        },
        "market": {
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        "alerts": {"ml_prediction": {"probability_threshold": 0.5}},
        "targets": [
            {
                "code": "000001",
                "name": "t",
                "trigger_price": 1.0,
                "direction": "below",
            }
        ],
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    from radar_monitor_v3 import load_monitor_bundle

    conf, targets = load_monitor_bundle(str(p))
    assert conf.bot_token == "b"
    assert len(targets) == 1
    assert targets[0].code == "000001"


def test_global_ml_rl_flags_disable_target_strategies(tmp_path, monkeypatch):
    _clear_secret_env(monkeypatch)
    cfg = {
        "telegram": {"bot_token": "DISABLED", "chat_id": "DISABLED"},
        "radar": {
            "cooldown_minutes": 5,
            "scan_interval_seconds": 60,
            "cache_file": "c.json",
            "log_file": "l.log",
        },
        "market": {
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        "alerts": {
            "ml_prediction": {"enabled": False, "probability_threshold": 0.5},
            "rl_strategy": {"enabled": False},
        },
        "targets": [
            {
                "code": "000001",
                "name": "t",
                "trigger_price": 1.0,
                "direction": "below",
                "ml_enabled": True,
                "rl_enabled": True,
            }
        ],
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    from radar_monitor_v3 import load_monitor_bundle

    conf, targets = load_monitor_bundle(str(p))
    assert conf.ml_enabled is False
    assert conf.rl_enabled is False
    assert targets[0].ml_enabled is False
    assert targets[0].rl_enabled is False
