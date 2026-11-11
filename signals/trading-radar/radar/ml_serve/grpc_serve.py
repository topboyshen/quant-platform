"""
gRPC ML 推理服务：替代 Flask HTTP，二进制序列化 + 流式支持。

环境变量：
- ML_SERVE_GRPC_PORT   默认 8091
- RADAR_ML_MODEL_DIR   模型目录

proto 定义（内嵌，无需 protoc 编译）：
    service MLPredictorService {
        rpc Predict(PredictRequest) returns (PredictResponse);
        rpc Health(HealthRequest) returns (HealthResponse);
        rpc StreamPredict(stream PredictRequest) returns (stream PredictResponse);
    }

用法：
    python -m radar.ml_serve.grpc_serve
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent import futures
from typing import Any, Dict, Optional

# 延迟导入 grpc —— 未安装时优雅降级
try:
    import grpc
    from grpc import ServicerContext  # noqa: F401  # 公开类型，供下游 import
except ImportError as _grpc_import_err:  # noqa: F841
    grpc = None  # type: ignore

logger = logging.getLogger(__name__)

# ── proto 内嵌定义（避免 protoc 编译依赖）──
# 实际生产建议用 .proto + grpcio-tools 生成，这里用纯 Python 实现兼容。

_PREDICTOR_LOCK = threading.RLock()
_predictor: Any = None


def _model_dir() -> str:
    return os.path.expanduser(os.getenv("RADAR_ML_MODEL_DIR", "models"))


def _get_predictor(force_new: bool = False) -> None:
    global _predictor
    with _PREDICTOR_LOCK:
        if force_new:
            _predictor = None
        if _predictor is None:
            from radar.ml.predictor import MLPredictor

            _predictor = MLPredictor(model_dir=_model_dir(), logger=logger)
        return _predictor


# ── 纯 Python gRPC 服务实现（无 protoc）──
# 使用 grpc 的 generic handler + 自定义序列化，保持零编译依赖。


def _serialize_predict_response(
    probability: float,
    will_trigger: bool,
    confidence: str,
    features: Dict[str, Any],
    latency_sec: float,
    cache: str = "miss",
) -> bytes:
    """简单的 JSON 序列化（grpc 传输层仍是 HTTP/2，payload 用 JSON 便于调试）。"""
    import json

    payload = {
        "probability": probability,
        "will_trigger": will_trigger,
        "confidence": confidence,
        "features": features,
        "latency_sec": round(latency_sec, 6),
        "cache": cache,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_predict_request(body: bytes) -> Dict[str, Any]:
    import json

    data = json.loads(body.decode("utf-8"))
    return {
        "code": str(data.get("code", "")).strip(),
        "direction": (data.get("direction") or "below").strip().lower(),
        "prices": [float(x) for x in (data.get("prices") or [])],
        "volumes": [float(x) for x in (data.get("volumes") or [])] if data.get("volumes") else None,
    }


def _do_predict(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """执行推理，返回字典或 None。"""
    code = req["code"]
    direction = req["direction"]
    prices = req["prices"]
    volumes = req.get("volumes")

    if not code or len(prices) < 30:
        return None

    # 尝试缓存（与 Flask /metrics 打点一致）
    from radar.ml_serve.cache import build_predict_cache_key, get_predict_cache

    cache = get_predict_cache()
    cache_key: Optional[str] = None
    ttl_sec = int(os.getenv("ML_SERVE_CACHE_TTL_SEC", "300"))
    cache_enabled = getattr(cache, "enabled", False)

    t0 = time.perf_counter()
    if cache_enabled:
        cache_key = build_predict_cache_key(code, direction, prices, volumes, cache.epoch())
        try:
            cached = cache.get(cache_key)
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
                    # Prometheus 指标上报失败不应阻塞 gRPC 响应  # 安全忽略
                    pass
                out_hit = dict(cached)
                out_hit["latency_sec"] = round(elapsed, 6)
                out_hit["cache"] = "hit"
                return out_hit
        except (ImportError, AttributeError):
            # 缓存读取异常不应阻塞预测流程  # 安全忽略
            pass

    try:
        from radar.ml_serve.metrics import observe_cache_miss

        if cache_enabled:
            observe_cache_miss()
    except (ImportError, AttributeError):
        # Prometheus 指标上报失败不应阻塞 gRPC 响应  # 安全忽略
        pass

    # 注意：_do_predict 不感知 gRPC context；错误/未命中统一返回 None，
    # 由上层 _predict_handler / _stream_predict_handler 设置 grpc status code。
    # 旧实现误植入了 context.set_code / PredictResponse(...) —— 但 context
    # 没在签名里、PredictResponse 也未 import，错误路径会直接 NameError。
    try:
        p = _get_predictor()
        out = p.predict(code, direction, prices, volumes)
    except Exception as e:
        logger.exception("gRPC predict failed: %s", e)
        try:
            from radar.ml_serve.metrics import observe_predict_error

            observe_predict_error()
        except (ImportError, AttributeError):
            # Prometheus 指标上报失败不应阻塞错误响应  # 安全忽略
            pass
        return None

    elapsed = time.perf_counter() - t0
    latency_state = "miss" if cache_enabled else "noop"

    if out is None:
        try:
            from radar.ml_serve.metrics import observe_predict_error

            observe_predict_error()
        except (ImportError, AttributeError):
            # Prometheus 指标上报失败不应阻塞错误响应  # 安全忽略
            pass
        return None

    try:
        from radar.ml_serve.metrics import observe_predict_ok, observe_prediction_probability

        observe_predict_ok(elapsed, latency_state)
        observe_prediction_probability(float(out.get("probability", 0)))
    except (ImportError, AttributeError):
        # Prometheus 指标上报失败不应阻塞 gRPC 响应  # 安全忽略
        pass

    if getattr(cache, "enabled", False) and cache_key:
        try:
            to_store = {k: v for k, v in out.items() if k != "latency_sec"}
            cache.set(cache_key, to_store, ttl_sec)
        except (ImportError, AttributeError):
            # 缓存写入失败不应阻塞 gRPC 响应  # 安全忽略
            pass

    out["latency_sec"] = round(elapsed, 6)
    return out


def _predict_handler(request: bytes, context: Any) -> bytes:
    """Unary Predict RPC handler。"""
    try:
        req = _parse_predict_request(request)
    except Exception as e:
        context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        context.set_details(f"bad request: {e}")
        return b"{}"

    out = _do_predict(req)
    if out is None:
        context.set_code(grpc.StatusCode.NOT_FOUND)
        context.set_details("no model or prediction failed")
        return b"{}"

    return _serialize_predict_response(
        probability=out["probability"],
        will_trigger=out.get("will_trigger", False),
        confidence=out.get("confidence", "medium"),
        features=out.get("features") or {},
        latency_sec=out["latency_sec"],
        cache=out.get("cache", "miss"),
    )


def _health_handler(request: bytes, context: Any) -> bytes:
    import json

    cache_backend = (os.getenv("ML_SERVE_CACHE_BACKEND") or "none").strip().lower()
    payload = {
        "status": "ok",
        "service": "radar-ml-serve-grpc",
        "model_dir": _model_dir(),
        "cache_backend": cache_backend,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _stream_predict_handler(request_iterator: Any, context: Any) -> None:
    """Server Streaming Predict：接收流式请求，返回流式响应。"""
    for request in request_iterator:
        try:
            req = _parse_predict_request(request)
        except Exception as e:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"bad request: {e}")
            yield b"{}"
            continue

        out = _do_predict(req)
        if out is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("no model or prediction failed")
            yield b"{}"
            continue

        yield _serialize_predict_response(
            probability=out["probability"],
            will_trigger=out.get("will_trigger", False),
            confidence=out.get("confidence", "medium"),
            features=out.get("features") or {},
            latency_sec=out["latency_sec"],
            cache=out.get("cache", "miss"),
        )


def _make_generic_handler() -> Any:
    """创建 generic RPC handler（无 .proto 编译）。"""
    if grpc is None:
        raise ImportError("grpcio 未安装，无法启动 gRPC 服务")

    class _Handler(grpc.GenericRpcHandler):
        def service(self, handler_call_details) -> None:
            method = handler_call_details.method
            if method == "/radar.ml_serve.MLPredictorService/Predict":
                return grpc.unary_unary_rpc_method_handler(
                    _predict_handler,
                    request_deserializer=lambda x: x,
                    response_serializer=lambda x: x,
                )
            elif method == "/radar.ml_serve.MLPredictorService/Health":
                return grpc.unary_unary_rpc_method_handler(
                    _health_handler,
                    request_deserializer=lambda x: x,
                    response_serializer=lambda x: x,
                )
            elif method == "/radar.ml_serve.MLPredictorService/StreamPredict":
                return grpc.stream_stream_rpc_method_handler(
                    _stream_predict_handler,
                    request_deserializer=lambda x: x,
                    response_serializer=lambda x: x,
                )
            return None

    return _Handler()


def serve() -> None:
    if grpc is None:
        logger.error("grpcio 未安装，请运行: pip install grpcio")
        return

    port = int(os.getenv("ML_SERVE_GRPC_PORT", "8091"))
    host = os.getenv("ML_SERVE_GRPC_HOST", "0.0.0.0")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    server.add_generic_rpc_handlers((_make_generic_handler(),))
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    logger.info("radar-ml-serve gRPC on %s:%s model_dir=%s", host, port, _model_dir())
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("gRPC server stopping...")
        server.stop(0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()
