"""Dashboard 与监控共用配置校验：仅告警路径。"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

import pytest

from dashboard.config_guard import maybe_warn_monitor_config_schema


def _load_dashboard_app_module(project_root: Path):
    path = project_root / "dashboard" / "app.py"
    name = "dashboard_app_test_mod"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    old_cwd = os.getcwd()
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(old_cwd)
    return mod


@pytest.fixture(scope="module")
def dash_app_module():
    project_root = Path(__file__).resolve().parents[1]
    return _load_dashboard_app_module(project_root)


@pytest.fixture(autouse=True)
def _reset_dash_schema_warn_clock(dash_app_module):
    dash_app_module._config_schema_last_warn_monotonic = 0.0
    yield


def _schema_warn_records(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and "未通过监控进程配置校验" in r.getMessage()
    ]


def test_first_warning_is_not_suppressed_on_fresh_process(caplog):
    logger = logging.getLogger("test.dashboard.config_guard.fresh_process")
    bad = {"telegram": {"bot_token": "t", "chat_id": "1"}}

    with caplog.at_level(logging.WARNING, logger=logger.name):
        updated = maybe_warn_monitor_config_schema(
            bad,
            logger=logger,
            last_warn_monotonic=0.0,
            interval_sec=300.0,
            monotonic=lambda: 1.0,
        )

    assert updated == 1.0
    assert len(_schema_warn_records(caplog)) == 1


def test_maybe_warn_valid_config_silent(
    dash_app_module, caplog, minimal_config_dict, monkeypatch
):
    monkeypatch.delenv("DASHBOARD_SKIP_CONFIG_SCHEMA", raising=False)
    with caplog.at_level(logging.WARNING):
        dash_app_module._maybe_warn_monitor_config_schema(minimal_config_dict)
    assert not _schema_warn_records(caplog)


def test_maybe_warn_invalid_logs_once(dash_app_module, caplog):
    bad = {"telegram": {"bot_token": "t", "chat_id": "1"}}
    with caplog.at_level(logging.WARNING):
        dash_app_module._maybe_warn_monitor_config_schema(bad)
    assert len(_schema_warn_records(caplog)) == 1


def test_maybe_warn_respects_rate_interval(dash_app_module, caplog):
    dash_app_module.CONFIG_SCHEMA_WARN_INTERVAL_SEC = 3600.0
    bad = {"telegram": {"bot_token": "t", "chat_id": "1"}}
    with caplog.at_level(logging.WARNING):
        dash_app_module._maybe_warn_monitor_config_schema(bad)
        dash_app_module._maybe_warn_monitor_config_schema(bad)
    assert len(_schema_warn_records(caplog)) == 1


def test_maybe_warn_skip_env(dash_app_module, caplog, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SKIP_CONFIG_SCHEMA", "1")
    bad = {"telegram": {"bot_token": "t", "chat_id": "1"}}
    with caplog.at_level(logging.WARNING):
        dash_app_module._maybe_warn_monitor_config_schema(bad)
    assert not _schema_warn_records(caplog)


def test_maybe_warn_empty_dict_no_log(dash_app_module, caplog):
    with caplog.at_level(logging.WARNING):
        dash_app_module._maybe_warn_monitor_config_schema({})
    assert not _schema_warn_records(caplog)
