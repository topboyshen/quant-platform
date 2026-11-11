"""Performance guardrails for caching, sessions, and async boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from requests.adapters import HTTPAdapter

from radar.data.market_data_v2 import MarketTimeChecker, parse_market_time
from radar.data.session_pool import create_pooled_session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NON_SOURCE_PREFIXES = (
    ".venv/",
    "venv/",
    ".trash/",
    ".codex-deploy/",
    "QuantDinger/",  # Covered by the QuantDinger backend suite.
    "backups/",
    "data/",
    "deploy/personal-",
    "deployments/",
    "releases/",
    "runtime/",
    "secrets/",
)


def _is_project_source(relative_path: str) -> bool:
    return not relative_path.startswith(NON_SOURCE_PREFIXES)


def test_parse_market_time_uses_lru_cache(null_logger):
    parse_market_time.cache_clear()
    cfg = {
        "morning_start": "09:30",
        "morning_end": "11:30",
        "afternoon_start": "13:00",
        "afternoon_end": "15:00",
    }

    MarketTimeChecker(cfg, null_logger)
    first = parse_market_time.cache_info()
    MarketTimeChecker(cfg, null_logger)
    second = parse_market_time.cache_info()

    assert first.misses == 4
    assert second.hits >= 4


def test_create_pooled_session_mounts_http_adapters():
    session = create_pooled_session({"X-Test": "1"}, pool_connections=3, pool_maxsize=7)

    assert isinstance(session.get_adapter("http://example.com"), HTTPAdapter)
    assert isinstance(session.get_adapter("https://example.com"), HTTPAdapter)
    assert session.headers["X-Test"] == "1"


def test_market_data_session_ignores_ambient_proxy_by_default(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("RADAR_HTTP_PROXY", raising=False)
    monkeypatch.delenv("RADAR_HTTPS_PROXY", raising=False)
    monkeypatch.delenv("RADAR_ALL_PROXY", raising=False)
    monkeypatch.delenv("RADAR_REQUESTS_TRUST_ENV", raising=False)

    session = create_pooled_session()

    assert session.trust_env is False
    assert session.proxies == {}


def test_market_data_session_uses_explicit_radar_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("RADAR_ALL_PROXY", "http://127.0.0.1:7890")
    monkeypatch.delenv("RADAR_HTTP_PROXY", raising=False)
    monkeypatch.delenv("RADAR_HTTPS_PROXY", raising=False)

    session = create_pooled_session()

    assert session.trust_env is False
    assert session.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_project_keeps_asyncio_boundary_modules_explicit():
    async_files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if not _is_project_source(relative_path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree)):
            async_files.append(relative_path)

    assert {
        "radar/core/health_server.py",
        "radar/data/market_data_concurrent.py",
        "tests/test_health_server_http.py",
    }.issubset(set(async_files))


def test_async_source_scan_excludes_operational_trees():
    for path in (
        "deploy/personal-20260729/backend/._strategy.py",
        "deployments/snapshot/backend/._indicator.py",
        "runtime/venvs/swing/lib/python.py",
        "data/grafana/plugin.py",
        "secrets/local.py",
    ):
        assert not _is_project_source(path)

    assert _is_project_source("radar/core/health_server.py")
