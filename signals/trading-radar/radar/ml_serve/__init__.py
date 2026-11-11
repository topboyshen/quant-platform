"""ML 模型 HTTP 推理服务（.pkl + MLPredictor），支持热重载。"""

try:
    from radar.ml_serve.app import create_app
    __all__ = ["create_app"]
except ImportError:
    __all__ = []
