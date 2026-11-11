"""radar.ml_serve HTTP 推理服务。"""

from __future__ import annotations

import json

import pytest

from radar.ml_serve.app import create_app


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    class FakePredictor:
        def predict(self, code, direction, prices, volumes=None):
            if code == "none":
                return None
            return {
                "probability": 0.7,
                "will_trigger": True,
                "confidence": "medium",
                "features": {"rsi_14": 50.0},
            }

    import radar.ml_serve.app as mod

    monkeypatch.setattr(mod, "_get_predictor", lambda force_new=False: FakePredictor())
    return app.test_client()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.get_json()
    assert data.get("status") == "ok"
    assert "model_dir" in data


def test_predict_ok(client):
    r = client.post(
        "/v1/predict",
        data=json.dumps(
            {
                "code": "000001",
                "direction": "below",
                "prices": [float(i % 10 + 10) for i in range(35)],
            }
        ),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["probability"] == pytest.approx(0.7)
    assert "latency_sec" in data
    assert data.get("cache") == "miss"


def test_predict_short_prices(client):
    r = client.post(
        "/v1/predict",
        data=json.dumps({"code": "000001", "prices": [1.0, 2.0]}),
        content_type="application/json",
    )
    assert r.status_code == 400


def test_predict_no_model(monkeypatch):
    import radar.ml_serve.app as mod

    class NoModel:
        def predict(self, *a, **k):
            return None

    monkeypatch.setattr(mod, "_get_predictor", lambda force_new=False: NoModel())
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.post(
        "/v1/predict",
        data=json.dumps(
            {
                "code": "000001",
                "prices": [float(i % 10 + 10) for i in range(35)],
            }
        ),
        content_type="application/json",
    )
    assert r.status_code == 404


def test_reload_without_token(client, monkeypatch):
    monkeypatch.delenv("ML_SERVE_RELOAD_TOKEN", raising=False)
    r = client.post("/v1/reload")
    assert r.status_code == 200
    assert r.get_json().get("ok") is True


def test_reload_with_bad_token(client, monkeypatch):
    monkeypatch.setenv("ML_SERVE_RELOAD_TOKEN", "secret")
    r = client.post("/v1/reload")
    assert r.status_code == 401
