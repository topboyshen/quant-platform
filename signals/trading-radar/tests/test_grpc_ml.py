"""gRPC ML Serve 与客户端测试（需要 grpcio）。"""

import time

import pytest

try:
    import grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False


@pytest.mark.skipif(not GRPC_AVAILABLE, reason="grpcio 未安装")
class TestGrpcMLServe:
    @pytest.fixture(scope="class")
    def server(self):
        """启动 gRPC 服务（使用 FakePredictor）。"""
        # mock predictor
        import radar.ml_serve.grpc_serve as grpc_mod
        from radar.ml_serve.grpc_serve import _make_generic_handler

        class FakePredictor:
            def predict(self, code, direction, prices, volumes=None):
                if code == "none":
                    return None
                return {
                    "probability": 0.85,
                    "will_trigger": True,
                    "confidence": "high",
                    "features": {"rsi_14": 55.0},
                }

        grpc_mod._predictor = FakePredictor()

        server = grpc.server(grpc_mod.futures.ThreadPoolExecutor(max_workers=2))
        server.add_generic_rpc_handlers((_make_generic_handler(),))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()

        # 获取实际绑定端口
        actual_port = None
        for _ in range(10):
            try:
                # 通过 channel 测试连通性
                test_channel = grpc.insecure_channel(f"127.0.0.1:{port}")
                grpc.channel_ready_future(test_channel).result(timeout=1.0)
                actual_port = port
                test_channel.close()
                break
            except (ConnectionError, grpc.RpcError):
                # gRPC服务启动等待中，安全重试  # 安全忽略
                time.sleep(0.1)

        yield actual_port or port

        server.stop(0)
        grpc_mod._predictor = None

    def test_health(self, server):
        from radar.ml_serve.grpc_client import MLGrpcClient

        client = MLGrpcClient(f"127.0.0.1:{server}")
        result = client.health()
        assert result is not None
        assert result.get("status") == "ok"
        client.close()

    def test_predict_ok(self, server):
        from radar.ml_serve.grpc_client import MLGrpcClient

        client = MLGrpcClient(f"127.0.0.1:{server}")
        result = client.predict(
            code="000001",
            direction="below",
            prices=[float(i % 10 + 10) for i in range(35)],
        )
        assert result is not None
        assert result["probability"] == pytest.approx(0.85)
        assert result["will_trigger"] is True
        assert result["confidence"] == "high"
        client.close()

    def test_predict_no_model(self, server):
        from radar.ml_serve.grpc_client import MLGrpcClient

        client = MLGrpcClient(f"127.0.0.1:{server}")
        result = client.predict(
            code="none",
            direction="below",
            prices=[float(i % 10 + 10) for i in range(35)],
        )
        assert result is None
        client.close()

    def test_predict_short_prices(self, server):
        from radar.ml_serve.grpc_client import MLGrpcClient

        client = MLGrpcClient(f"127.0.0.1:{server}")
        result = client.predict(
            code="000001",
            direction="below",
            prices=[1.0, 2.0],
        )
        assert result is None
        client.close()
