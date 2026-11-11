import json

import pytest

from radar.config_loader import load_config
from radar.config_schema import ConfigSchemaError


def _base_config():
    return {
        "telegram": {"bot_token": "DISABLED", "chat_id": "DISABLED"},
        "radar": {
            "cooldown_minutes": 5,
            "scan_interval_seconds": 60,
            "cache_file": "cache.json",
            "log_file": "radar.log",
        },
        "market": {
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        "targets": [],
    }


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_local_targets_replace_tracked_targets(tmp_path, monkeypatch):
    base = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    payload = _base_config()
    payload["targets"] = [{"code": "old", "name": "old", "trigger_price": 1}]
    targets = [{"code": "new", "name": "new", "trigger_price": 2}]
    _write(base, payload)
    _write(local, {"targets": targets})
    monkeypatch.delenv("RADAR_LOCAL_CONFIG", raising=False)

    assert load_config(base)["targets"] == targets


def test_local_overlay_rejects_other_top_level_keys(tmp_path, monkeypatch):
    base = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    _write(base, _base_config())
    _write(local, {"targets": [], "telegram": {}})
    monkeypatch.delenv("RADAR_LOCAL_CONFIG", raising=False)

    with pytest.raises(ConfigSchemaError, match="仅允许顶层 targets"):
        load_config(base)


def test_env_selects_local_overlay(tmp_path, monkeypatch):
    base = tmp_path / "config.json"
    local = tmp_path / "private.json"
    targets = [{"code": "000001", "name": "x", "trigger_price": 1}]
    _write(base, _base_config())
    _write(local, {"targets": targets})
    monkeypatch.setenv("RADAR_LOCAL_CONFIG", str(local))

    assert load_config(base)["targets"] == targets
