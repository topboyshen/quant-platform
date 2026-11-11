"""
ML 推理微服务：封装 MLPredictor（.pkl），与监控进程解耦。

环境变量：
- RADAR_ML_MODEL_DIR     模型目录，默认 models
- ML_SERVE_PORT          监听端口，默认 8090
- ML_SERVE_RELOAD_TOKEN  若设置，POST /v1/reload 须带 Header X-Reload-Token 相同值
- ML_SERVE_CACHE_*       见 radar/ml_serve/cache.py（Redis / Memcached）
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request

from radar.ml_serve.cache import predict_cache_epoch

logger = logging.getLogger(__name__)

_predictor_lock = threading.RLock()
_predictor: Any = None


def _model_dir() -> str:
    return os.path.expanduser(os.getenv("RADAR_ML_MODEL_DIR", "models"))


def _get_predictor(force_new: bool = False) -> Any:
    """懒加载；force_new 时丢弃缓存实例（热重载）。"""
    global _predictor
    with _predictor_lock:
        if force_new:
            _predictor = None
        if _predictor is None:
            from radar.ml.predictor import MLPredictor

            _predictor = MLPredictor(model_dir=_model_dir(), logger=logger)
        return _predictor


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/health")
    def health() -> Any:
        cache_backend = (os.getenv("ML_SERVE_CACHE_BACKEND") or "none").strip().lower()
        return jsonify(
            {
                "status": "ok",
                "service": "radar-ml-serve",
                "model_dir": _model_dir(),
                "cache_backend": cache_backend,
                "predict_cache_epoch": predict_cache_epoch(),
            }
        )

    @app.route("/metrics")
    def metrics() -> Any:
        try:
            from radar.ml_serve.metrics import metrics_text

            body = metrics_text()
        except (ImportError, AttributeError):
            # metrics模块未安装，安全返回空  # 安全忽略
            body = ""
        if not body.strip():
            return Response("# radar_ml_serve metrics (install prometheus-client)\n", mimetype="text/plain")
        return Response(body + "\n", mimetype="text/plain; version=0.0.4")

    @app.post("/v1/reload")
    def reload() -> Any:
        token = (os.getenv("ML_SERVE_RELOAD_TOKEN") or "").strip()
        if token and request.headers.get("X-Reload-Token", "") != token:
            return jsonify({"error": "unauthorized"}), 401
        try:
            from radar.ml_serve.metrics import observe_reload

            observe_reload()
        except (ImportError, AttributeError):
            # 指标上报失败不应阻塞热重载  # 安全忽略
            pass
        try:
            from radar.ml_serve.cache import bump_predict_cache_epoch

            bump_predict_cache_epoch()
        except (ImportError, AttributeError):
            # 缓存刷新失败不应阻塞热重载  # 安全忽略
            pass
        _get_predictor(force_new=True)
        logger.info("ML 模型已热重载 model_dir=%s", _model_dir())
        return jsonify({"ok": True, "model_dir": _model_dir()})

    @app.post("/v1/predict")
    def predict() -> Any:
        body: Dict[str, Any] = request.get_json(force=True, silent=True) or {}
        code = body.get("code")
        direction = (body.get("direction") or "below").strip().lower()
        prices = body.get("prices") or body.get("price_history")
        volumes_raw = body.get("volumes") or body.get("volume_history")

        if not code:
            return jsonify({"error": "code required"}), 400
        if not isinstance(prices, list) or len(prices) < 30:
            return jsonify({"error": "prices must be a list of at least 30 floats"}), 400

        prices_f = [float(x) for x in prices]
        vols: Optional[List[float]] = None
        if volumes_raw is not None:
            if not isinstance(volumes_raw, list):
                return jsonify({"error": "volumes must be a list of numbers"}), 400
            vols = [float(x) for x in volumes_raw]

        from radar.ml_serve.cache import build_predict_cache_key, get_predict_cache

        cache = get_predict_cache()
        cache_key: Optional[str] = None
        ttl_sec = int(os.getenv("ML_SERVE_CACHE_TTL_SEC", "300"))

        t0 = time.perf_counter()
        if getattr(cache, "enabled", False):
            cache_key = build_predict_cache_key(
                str(code).strip(), direction, prices_f, vols, cache.epoch()
            )
            try:
                cached = cache.get(cache_key)
            except (ImportError, AttributeError, TypeError):
                # 缓存读取失败，安全回退到None  # 安全忽略
                cached = None
            if cached is not None:
                elapsed = time.perf_counter() - t0
                try:
                    from radar.ml_serve.metrics import (
                        observe_cache_hit,
                        observe_predict_ok,
                        observe_prediction_probability,
                    )

                    observe_cache_hit()
                    observe_predict_ok(elapsed, "hit")
                    observe_prediction_probability(float(cached.get("probability", 0)))
                except (ImportError, AttributeError):
                    # Prometheus 指标上报失败不应阻塞预测响应  # 安全忽略
                    pass
                payload = dict(cached)
                payload["latency_sec"] = round(elapsed, 6)
                payload["cache"] = "hit"
                return jsonify(payload)

        cache_enabled = getattr(cache, "enabled", False)
        try:
            from radar.ml_serve.metrics import observe_cache_miss

            if cache_enabled:
                observe_cache_miss()
        except (ImportError, AttributeError):
            # Prometheus 指标上报失败不应阻塞预测响应  # 安全忽略
            pass

        try:
            p = _get_predictor()
            out = p.predict(str(code).strip(), direction, prices_f, vols)
        except Exception as e:
            logger.exception("predict failed: %s", e)
            try:
                from radar.ml_serve.metrics import observe_predict_error

                observe_predict_error()
            except (ImportError, AttributeError):
                # Prometheus 指标上报失败不应阻塞错误响应  # 安全忽略
                pass
            return jsonify({"error": str(e)}), 500

        elapsed = time.perf_counter() - t0
        latency_state = "miss" if cache_enabled else "noop"

        if out is None:
            try:
                from radar.ml_serve.metrics import observe_predict_error

                observe_predict_error()
            except (ImportError, AttributeError):
                # Prometheus 指标上报失败不应阻塞错误响应  # 安全忽略
                pass
            return jsonify({"error": "no model or prediction failed", "code": code, "direction": direction}), 404

        try:
            from radar.ml_serve.metrics import observe_predict_ok, observe_prediction_probability

            observe_predict_ok(elapsed, latency_state)
            observe_prediction_probability(float(out.get("probability", 0)))
        except (ImportError, AttributeError):
            # Prometheus 指标上报失败不应阻塞预测响应  # 安全忽略
            pass

        if getattr(cache, "enabled", False) and cache_key:
            try:
                to_store = {k: v for k, v in out.items() if k != "latency_sec"}
                cache.set(cache_key, to_store, ttl_sec)
            except (ImportError, AttributeError, TypeError):
                # 缓存写入失败不应阻塞预测响应  # 安全忽略
                pass

        out = dict(out)
        out["latency_sec"] = round(elapsed, 6)
        out["cache"] = "miss"
        return jsonify(out)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.getenv("ML_SERVE_PORT", "8090"))
    host = os.getenv("ML_SERVE_HOST", "0.0.0.0")
    app = create_app()
    logger.info("radar-ml-serve on http://%s:%s model_dir=%s", host, port, _model_dir())
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
