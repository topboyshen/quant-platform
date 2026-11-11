"""Startup guards for the web/worker process boundary."""

import importlib.util
from pathlib import Path

import pytest


CONFIG = Path(__file__).resolve().parents[1] / "gunicorn_config.py"


def _load_gunicorn_config(monkeypatch, workers: str, skip_hooks: str = "0"):
    monkeypatch.setenv("GUNICORN_WORKERS", workers)
    monkeypatch.setenv("SKIP_STARTUP_HOOKS", skip_hooks)
    spec = importlib.util.spec_from_file_location("gunicorn_config_under_test", CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multiple_workers_rejected_when_background_hooks_are_enabled(monkeypatch):
    with pytest.raises(RuntimeError, match="GUNICORN_WORKERS"):
        _load_gunicorn_config(monkeypatch, "2")


def test_multiple_workers_allowed_for_api_only_process(monkeypatch):
    module = _load_gunicorn_config(monkeypatch, "2", skip_hooks="1")
    assert module.workers == 2


def test_default_single_worker_remains_supported(monkeypatch):
    module = _load_gunicorn_config(monkeypatch, "1")
    assert module.workers == 1
