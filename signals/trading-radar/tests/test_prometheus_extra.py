"""prometheus_extra：可选 PushAdapter 指标。"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")

from radar.core import prometheus_extra as pe


def test_observe_pushadapter_poll_increments_counters():
    pe.observe_pushadapter_poll(0.05, 2)
    pe.observe_pushadapter_poll(0.1, 0)
    text = pe.metrics_exposition_suffix()
    assert "radar_pushadapter_polls_total 2.0" in text or "radar_pushadapter_polls_total 2" in text
    assert "radar_pushadapter_market_events_emitted_total 2.0" in text or "_total 2" in text
