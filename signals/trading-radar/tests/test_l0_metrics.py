"""L0 上帝视角指标：当日盈亏 Gauge、外部依赖红绿灯。"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")


def test_daily_pnl_and_l0_dependency_gauges():
    from radar.core import prometheus_extra as pe

    assert getattr(pe, "_HAVE", False)

    pe.observe_daily_pnl_cny(1234.56)
    pe.observe_l0_dependency_up("qmt_adapter", True)
    pe.observe_l0_dependency_up("database", False)

    body = pe.metrics_exposition_suffix()
    assert "radar_daily_pnl_cny" in body
    assert "radar_l0_dependency_up" in body
    assert 'dependency="qmt_adapter"' in body
    assert 'dependency="database"' in body


def test_refresh_l0_dependency_from_env(monkeypatch):
    monkeypatch.setenv("RADAR_L0_QMT_ADAPTER_UP", "false")
    monkeypatch.setenv("RADAR_L0_AWS_DATABASE_UP", "yes")

    from radar.core import prometheus_extra as pe

    body = pe.metrics_exposition_suffix()
    assert "radar_l0_dependency_up" in body


def test_unknown_dependency_no_series():
    from radar.core import prometheus_extra as pe

    pe.observe_l0_dependency_up("unknown_bridge", True)
    body = pe.metrics_exposition_suffix()
    assert "unknown_bridge" not in body


def test_dependency_aliases_resolve():
    from radar.core import prometheus_extra as pe

    pe.observe_l0_dependency_up("qmt", True)
    pe.observe_l0_dependency_up("rds", False)
    body = pe.metrics_exposition_suffix()
    assert 'dependency="qmt_adapter"' in body
    assert 'dependency="database"' in body
