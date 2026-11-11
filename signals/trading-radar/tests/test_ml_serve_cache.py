"""ML Serve 推理缓存。"""

from __future__ import annotations

import json

import pytest

from radar.ml_serve.app import create_app
from radar.ml_serve.cache import reset_predict_cache_singleton_for_tests


@pytest.fixture(autouse=True)
def _reset_cache_singleton():
    reset_predict_cache_singleton_for_tests()
    yield
    reset_predict_cache_singleton_for_tests()


def test_predict_second_request_cache_hit(monkeypatch):
    store: dict = {}

    class MemCache:
        enabled = True

        def epoch(self):
            return 0

        def get(self, k):
            return store.get(k)

        def set(self, k, v, ttl_sec):
            store[k] = v

    mem = MemCache()
    monkeypatch.setattr("radar.ml_serve.cache.get_predict_cache", lambda: mem)

    import radar.ml_serve.app as mod

    class FP:
        def predict(self, code, direction, prices, volumes=None):
            return {
                "probability": 0.55,
                "will_trigger": False,
                "confidence": "low",
                "features": {"x": 1},
            }

    monkeypatch.setattr(mod, "_get_predictor", lambda force_new=False: FP())

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    body = {
        "code": "000001",
        "direction": "below",
        "prices": [float(i % 10 + 10) for i in range(35)],
    }
    r1 = c.post("/v1/predict", data=json.dumps(body), content_type="application/json")
    assert r1.status_code == 200
    assert r1.get_json().get("cache") == "miss"
    r2 = c.post("/v1/predict", data=json.dumps(body), content_type="application/json")
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2.get("cache") == "hit"
    assert d2["probability"] == pytest.approx(0.55)


def test_reload_invalidates_cache(monkeypatch):
    store: dict = {}

    class MemCache:
        enabled = True

        def epoch(self):
            from radar.ml_serve.cache import predict_cache_epoch

            return predict_cache_epoch()

        def get(self, k):
            return store.get(k)

        def set(self, k, v, ttl_sec):
            store[k] = v

    mem = MemCache()
    monkeypatch.setattr("radar.ml_serve.cache.get_predict_cache", lambda: mem)

    import radar.ml_serve.app as mod

    calls = {"n": 0}

    class P:
        def predict(self, code, direction, prices, volumes=None):
            calls["n"] += 1
            return {
                "probability": 0.5,
                "will_trigger": False,
                "confidence": "low",
                "features": {},
            }

    monkeypatch.setattr(mod, "_get_predictor", lambda force_new=False: P())

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    body = {
        "code": "000002",
        "direction": "below",
        "prices": [float(i % 7 + 5) for i in range(35)],
    }
    r1 = c.post("/v1/predict", data=json.dumps(body), content_type="application/json")
    assert r1.get_json().get("cache") == "miss"
    assert calls["n"] == 1
    r2 = c.post("/v1/predict", data=json.dumps(body), content_type="application/json")
    assert r2.get_json().get("cache") == "hit"
    assert calls["n"] == 1
    c.post("/v1/reload")
    r3 = c.post("/v1/predict", data=json.dumps(body), content_type="application/json")
    assert r3.get_json().get("cache") == "miss"
    assert calls["n"] == 2
