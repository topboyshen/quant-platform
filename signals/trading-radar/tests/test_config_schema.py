"""配置结构校验。"""

import logging

import pytest

from radar.config_schema import (
    ConfigSchemaError,
    audit_optional_fields,
    log_optional_field_audit,
    validate_monitor_config_dict,
)


def _minimal_valid():
    return {
        "telegram": {"bot_token": "t", "chat_id": "1"},
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
        "targets": [],
    }


def test_validate_accepts_empty_targets():
    validate_monitor_config_dict(_minimal_valid())


def test_validate_rejects_empty_token(monkeypatch):
    d = _minimal_valid()
    d["telegram"]["bot_token"] = ""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigSchemaError, match="bot_token"):
        validate_monitor_config_dict(d)


def test_validate_accepts_disabled_telegram_placeholders(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for value in ("DISABLED", "REPLACE", "REPLACE_ME", "NONE"):
        d = _minimal_valid()
        d["telegram"]["bot_token"] = value
        d["telegram"]["chat_id"] = value
        validate_monitor_config_dict(d)


def test_validate_rejects_bad_target():
    d = _minimal_valid()
    d["targets"] = [{"code": "1", "name": "x"}]
    with pytest.raises(ConfigSchemaError, match="trigger_price"):
        validate_monitor_config_dict(d)


def test_validate_rejects_nonpositive_trigger_price():
    d = _minimal_valid()
    d["targets"] = [{"code": "000001", "name": "x", "trigger_price": 0}]
    with pytest.raises(ConfigSchemaError, match="必须大于 0"):
        validate_monitor_config_dict(d)


def test_validate_rejects_unknown_direction():
    d = _minimal_valid()
    d["targets"] = [
        {"code": "000001", "name": "x", "trigger_price": 1.0, "direction": "sideways"}
    ]
    with pytest.raises(ConfigSchemaError, match="direction"):
        validate_monitor_config_dict(d)


def test_load_monitor_bundle_env_token_fills_before_validate(tmp_path, monkeypatch):
    """与 radar_monitor 一致：先 merge 环境变量，再校验。"""
    import json

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "envtok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    d = _minimal_valid()
    d["telegram"]["bot_token"] = ""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    from radar_monitor_v3 import load_monitor_bundle

    conf, targets = load_monitor_bundle(str(p))
    assert conf.bot_token == "envtok"
    assert targets == []


def test_validate_rejects_missing_market_key():
    d = _minimal_valid()
    del d["market"]["morning_start"]
    with pytest.raises(ConfigSchemaError, match="morning_start"):
        validate_monitor_config_dict(d)


def test_validate_accepts_target_ml_rl_flags():
    d = _minimal_valid()
    d["targets"] = [
        {
            "code": "000001",
            "name": "x",
            "trigger_price": 1.0,
            "ml_enabled": True,
            "rl_enabled": False,
        }
    ]
    validate_monitor_config_dict(d)


def test_validate_rejects_nonbool_ml_enabled():
    d = _minimal_valid()
    d["targets"] = [
        {"code": "000001", "name": "x", "trigger_price": 1.0, "ml_enabled": "yes"}
    ]
    with pytest.raises(ConfigSchemaError, match="ml_enabled"):
        validate_monitor_config_dict(d)


def test_audit_reports_missing_optional_fields():
    issues = audit_optional_fields(_minimal_valid())
    msg = "\n".join(issues)
    for key in ("ml_model_dir", "rl_model_dir", "rl_mode", "initial_cash", "alerts.priorities"):
        assert key in msg


def test_audit_clean_config_returns_no_issues():
    d = _minimal_valid()
    d["ml_model_dir"] = "models"
    d["rl_model_dir"] = "rl_models"
    d["rl_mode"] = "confirm"
    d["initial_cash"] = 100000.0
    d["alerts"] = {"priorities": {"price": "HIGH"}}
    assert audit_optional_fields(d) == []


def test_audit_flags_telegram_placeholder():
    d = _minimal_valid()
    d["telegram"]["bot_token"] = "REPLACE_OR_USE_ENV_TELEGRAM_BOT_TOKEN"
    issues = audit_optional_fields(d)
    assert any("telegram.bot_token" in m and "占位符" in m for m in issues)


def test_audit_skips_telegram_placeholder_when_env_override_applied():
    raw = _minimal_valid()
    raw["telegram"]["bot_token"] = "REPLACE_OR_USE_ENV_TELEGRAM_BOT_TOKEN"
    raw["telegram"]["chat_id"] = "REPLACE_OR_USE_ENV_TELEGRAM_CHAT_ID"
    merged = _minimal_valid()
    merged["telegram"]["bot_token"] = "env-token"
    merged["telegram"]["chat_id"] = "123456"

    issues = audit_optional_fields(merged, raw_data=raw)
    assert not any("telegram.bot_token" in m and "占位符" in m for m in issues)
    assert not any("telegram.chat_id" in m and "占位符" in m for m in issues)


def test_audit_flags_llm_placeholder_api_key():
    d = _minimal_valid()
    d["llm"] = {"api_key": "***"}
    issues = audit_optional_fields(d)
    assert any("llm.api_key" in m for m in issues)


def test_log_optional_field_audit_emits_warnings(caplog):
    with caplog.at_level(logging.WARNING):
        issues = log_optional_field_audit(_minimal_valid(), logging.getLogger("test-audit"))
    assert issues  # non-empty
    assert any("[ConfigAudit]" in r.message for r in caplog.records)


def test_audit_flags_deprecated_blocks():
    d = _minimal_valid()
    d["data_source_timeouts"] = {"timeout": 10}
    d["akshare_daily"] = {"enabled": True}
    d["llm"] = {"enabled": True}
    d["backtest"] = {"enabled": True}
    issues = audit_optional_fields(d)
    msg = "\n".join(issues)
    for key in ("data_source_timeouts", "akshare_daily", "llm", "backtest"):
        assert f"deprecated 顶级字段 {key}" in msg


def test_audit_no_deprecated_when_keys_absent():
    d = _minimal_valid()
    d["ml_model_dir"] = "models"
    d["rl_model_dir"] = "rl_models"
    d["rl_mode"] = "confirm"
    d["initial_cash"] = 100000.0
    d["alerts"] = {"priorities": {}}
    # 完全没有 deprecated 块
    issues = audit_optional_fields(d)
    assert not any("deprecated" in m for m in issues)
