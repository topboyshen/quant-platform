"""
gRPC ML 推理客户端：MLStrategy 调用 gRPC 替代 HTTP requests。

环境变量：
- RADAR_ML_GRPC_URL   默认 localhost:8091

用法：
    client = MLGrpcClient()
    result = client.predict(code="000001", direction="below", prices=[...], volumes=[...])
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

try:
    import grpc
except ImportError as _grpc_import_err:  # noqa: F841
    grpc = None  # type: ignore

logger = logging.getLogger(__name__)


class MLGrpcClient:
    """gRPC ML 推理客户端（零 .proto 编译依赖）。"""

    def __init__(self, url: Optional[str] = None):
        self._url = (url or os.getenv("RADAR_ML_GRPC_URL", "localhost:8091")).strip()
        self._channel: Any = None
        self._stub: Any = None
        self._connected = False

    def _ensure_channel(self) -> bool:
        if grpc is None:
            logger.warning("grpcio 未安装，无法使用 gRPC 客户端")
            return False
        if self._connected and self._channel is not None:
            return True
        try:
            self._channel = grpc.insecure_channel(self._url)
            # 健康检查
            grpc.channel_ready_future(self._channel).result(timeout=2.0)
            self._connected = True
            return True
        except Exception as e:
            logger.warning("gRPC 连接失败 %s: %s", self._url, e)
            self._channel = None
            self._connected = False
            return False

    def predict(
        self,
        code: str,
        direction: str,
        prices: List[float],
        volumes: Optional[List[float]] = None,
        timeout: float = 15.0,
    ) -> Optional[Dict[str, Any]]:
        if not self._ensure_channel():
            return None

        payload = json.dumps(
            {
                "code": code,
                "direction": direction,
                "prices": prices,
                "volumes": volumes,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        try:
            # 使用 generic unary_unary 调用
            response = self._channel.unary_unary(
                "/radar.ml_serve.MLPredictorService/Predict",
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )(payload, timeout=timeout)
            data = json.loads(response.decode("utf-8"))
            if "probability" not in data:
                return None
            return {
                "probability": float(data["probability"]),
                "will_trigger": bool(data.get("will_trigger", False)),
                "confidence": str(data.get("confidence", "medium")),
                "features": data.get("features") or {},
                "latency_sec": float(data.get("latency_sec", 0)),
                "cache": str(data.get("cache", "miss")),
            }
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return None
            logger.warning("gRPC predict RPC 错误 %s: %s", e.code(), e.details())
            return None
        except Exception as e:
            logger.warning("gRPC predict 异常: %s", e)
            return None

    def health(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        if not self._ensure_channel():
            return None
        try:
            response = self._channel.unary_unary(
                "/radar.ml_serve.MLPredictorService/Health",
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )(b"{}", timeout=timeout)
            return json.loads(response.decode("utf-8"))
        except Exception as e:
            logger.debug("gRPC health 检查失败: %s", e)
            return None

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except (ImportError, AttributeError):
                # 关闭阶段 gRPC 通道清理失败可忽略  # 安全忽略
                pass
            self._channel = None
            self._connected = False
