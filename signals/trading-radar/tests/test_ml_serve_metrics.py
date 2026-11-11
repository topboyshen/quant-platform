"""radar.ml_serve Prometheus 指标扩展（缓存路径、置信度 Histogram）。"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")


def test_observe_prediction_probability_clamped_and_histogram():
    from radar.ml_serve.metrics import metrics_text, observe_prediction_probability

    observe_prediction_probability(0.923)
    observe_prediction_probability(float("nan"))
    observe_prediction_probability(-5.0)
    text = metrics_text()
    assert "radar_ml_serve_prediction_probability" in text
    assert "radar_ml_serve_prediction_probability_bucket" in text


def test_observe_predict_ok_latency_labels():
    from radar.ml_serve.metrics import metrics_text, observe_predict_ok

    observe_predict_ok(0.012, "hit")
    observe_predict_ok(0.35, "miss")
    observe_predict_ok(0.01, "noop")
    observe_predict_ok(0.02, "invalid-state-falls-back")
    body = metrics_text()
    assert 'cache_state="hit"' in body
    assert 'cache_state="miss"' in body
    assert 'cache_state="noop"' in body
