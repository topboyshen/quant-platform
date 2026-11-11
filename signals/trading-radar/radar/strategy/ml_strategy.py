"""
策略层 - ML预测策略（轻量推理版）
职责：加载预训练 sklearn 模型（.pkl）推理，不训练
进程内共享 MLPredictor，避免每标的重复扫描/加载。
价格序列写入 PriceHistoryStore（与 RL 的 code 键隔离），重启后可尽快恢复 ML 特征长度。
"""

import logging
import os
import threading
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from radar.ml.predictor import MLPredictor

_shared_predictors: Dict[str, "MLPredictor"] = {}
_shared_lock = threading.Lock()


# sklearn 特征工程硬下限（再小则 extract_features 恒为 None）
ML_FEATURE_ENGINE_MIN_BARS = 30


def get_shared_mlpredictor(model_dir: str, logger: logging.Logger) -> "MLPredictor":
    """同一 model_dir 共用一个 MLPredictor（sklearn 仅在此首次调用时导入）。"""
    from radar.ml.predictor import MLPredictor as _MLP

    resolved = os.path.abspath(model_dir)
    with _shared_lock:
        if resolved not in _shared_predictors:
            _shared_predictors[resolved] = _MLP(resolved, logger)
        return _shared_predictors[resolved]


class MLStrategy:
    """ML策略：使用与训练脚本一致的 {code}_{direction}_model.pkl + _scaler.pkl"""

    def __init__(self, code: str, direction: str, trigger_price: float,
                 model_dir: str = "models", threshold: float = 0.7,
                 min_history_bars: int = ML_FEATURE_ENGINE_MIN_BARS,
                 logger: logging.Logger = None):
        self.code = code
        self.direction = direction
        self.trigger_price = trigger_price
        self.threshold = threshold
        self.logger = logger or logging.getLogger(__name__)
        self._model_dir = model_dir
        self._predictor: Optional["MLPredictor"] = None
        self._min_history = max(ML_FEATURE_ENGINE_MIN_BARS, int(min_history_bars))

        self._price_history: List[float] = []
        self._volume_history: List[float] = []
        self._max_history = max(120, self._min_history + 32)
        # 与 RL 的 IncrementalFeatureExtractor（按 code 存）区分开，避免互相覆盖
        self._store_key = f"ml:{code}:{direction}"

        from radar.data.price_history_store import get_price_store
        self._price_store = get_price_store()
        self._restore_history_from_store()

    def _get_predictor(self) -> "MLPredictor":
        if self._predictor is None:
            self._predictor = get_shared_mlpredictor(self._model_dir, self.logger)
        return self._predictor

    def _ml_serve_base_url(self) -> str:
        return (os.getenv("RADAR_ML_SERVE_URL") or "").strip().rstrip("/")

    def _ml_grpc_url(self) -> str:
        return (os.getenv("RADAR_ML_GRPC_URL") or "").strip()

    def _predict_via_grpc(self) -> Optional[Dict]:
        """gRPC 推理（优先于 HTTP）。"""
        url = self._ml_grpc_url()
        if not url:
            return None
        try:
            from radar.ml_serve.grpc_client import MLGrpcClient
        except ImportError:
            self.logger.warning("RADAR_ML_GRPC_URL 已设置但 grpc_client 导入失败，跳过 gRPC")
            return None
        client = MLGrpcClient(url)
        try:
            result = client.predict(
                code=self.code,
                direction=self.direction,
                prices=self._price_history,
                volumes=self._volume_history,
            )
        finally:
            client.close()
        return result

    def _predict_via_http(self) -> Optional[Dict]:
        """独立 ML 服务（serve_ml）；与本地 MLPredictor 二选一。"""
        base = self._ml_serve_base_url()
        if not base:
            return None
        try:
            import requests
        except ImportError:
            self.logger.warning("RADAR_ML_SERVE_URL 已设置但未安装 requests，跳过远程推理")
            return None
        timeout = float(os.getenv("RADAR_ML_SERVE_TIMEOUT_SEC", "15"))
        payload = {
            "code": self.code,
            "direction": self.direction,
            "prices": self._price_history,
            "volumes": self._volume_history,
        }
        try:
            r = requests.post(
                f"{base}/v1/predict",
                json=payload,
                timeout=timeout,
            )
        except Exception as e:
            self.logger.warning("ML 服务请求失败: %s", e)
            return None
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            self.logger.warning(
                "ML 服务 HTTP %s: %s", r.status_code, (r.text or "")[:300]
            )
            return None
        try:
            data = r.json()
        except (ValueError, TypeError):
            # ML serve JSON解析失败，安全返回None  # 安全忽略
            return None
        if not isinstance(data, dict) or "probability" not in data:
            return None
        return {
            "probability": float(data["probability"]),
            "will_trigger": bool(data.get("will_trigger", False)),
            "confidence": str(data.get("confidence", "medium")),
            "features": data.get("features") or {},
        }

    def _restore_history_from_store(self) -> None:
        try:
            hist = self._price_store.get_history(self._store_key, n=self._max_history)
            if len(hist) < 2:
                return
            self._price_history = [float(h[1]) for h in hist]
            self._volume_history = [float(h[2]) for h in hist]
            self.logger.debug(
                f"{self._store_key} 从 PriceHistoryStore 恢复 {len(self._price_history)} 条"
            )
        except Exception as e:
            self.logger.debug(f"ML 价格历史恢复跳过: {e}")

    def update_history(self, price: float, volume: float = 0) -> None:
        self._price_history.append(price)
        self._volume_history.append(volume)

        if len(self._price_history) > self._max_history:
            self._price_history = self._price_history[-self._max_history:]
            self._volume_history = self._volume_history[-self._max_history:]

        try:
            self._price_store.append(self._store_key, float(price), float(volume))
        except Exception as e:
            self.logger.debug(f"ML 价格历史持久化 append 失败: {e}")

    def should_alert(self, data: Dict) -> Optional[Dict]:
        """是否触发 ML 告警（真实模型 predict_proba，需至少约 30 根历史）"""
        price = data.get('price', 0)
        volume = data.get('volume', 0)

        self.update_history(price, volume)

        if len(self._price_history) < self._min_history:
            return None

        # 新增：价格远离触发价时不预警（避免明显误报）
        # 当价格距离触发价超过 15% 时，认为是安全区域，不触发预测
        if self.trigger_price > 0 and price > 0:
            if self.direction == 'below':
                # 监控跌破：当前价远高于触发价时不预警
                distance_pct = (price - self.trigger_price) / self.trigger_price
                if distance_pct > 0.15:  # 高于触发价 15% 以上
                    self.logger.debug(
                        f"{self.code} 价格¥{price:.2f} 远高于触发价¥{self.trigger_price:.2f} "
                        f"(高{distance_pct:.1%})，跳过ML预测"
                    )
                    return None
            else:  # above
                # 监控突破：当前价远低于触发价时不预警
                distance_pct = (self.trigger_price - price) / self.trigger_price
                if distance_pct > 0.15:  # 低于触发价 15% 以上
                    self.logger.debug(
                        f"{self.code} 价格¥{price:.2f} 远低于触发价¥{self.trigger_price:.2f} "
                        f"(低{distance_pct:.1%})，跳过ML预测"
                    )
                    return None

        if self._ml_grpc_url():
            raw = self._predict_via_grpc()
        elif self._ml_serve_base_url():
            raw = self._predict_via_http()
        else:
            raw = self._get_predictor().predict(
                self.code, self.direction,
                self._price_history, self._volume_history
            )
        if raw is None:
            return None

        prob = raw['probability']
        if prob < self.threshold:
            return None

        # 告警仅以用户配置的 threshold 为准，不沿用分类器默认 0.5 的 will_trigger
        dir_word = '下破' if self.direction == 'below' else '上破'

        # 判断当前价格是否已经触发监控价
        triggered_now = False
        if self.direction == 'below' and price <= self.trigger_price:
            triggered_now = True
        elif self.direction == 'above' and price >= self.trigger_price:
            triggered_now = True

        if triggered_now:
            prediction = (
                f"✅ 已{dir_word}监控价 ¥{self.trigger_price:.2f}（当前 ¥{price:.2f}），"
                f"模型确认趋势持续 概率{prob:.0%}（已超告警线 {self.threshold:.0%}）"
            )
        else:
            prediction = (
                f"未来可能{dir_word}监控价 ¥{self.trigger_price:.2f} "
                f"（模型估计概率{prob:.0%}，已超告警线 {self.threshold:.0%}）"
            )
        return {
            'prediction': prediction,
            'probability': prob,
            'trigger_price': self.trigger_price,
            'direction': self.direction,
            'confidence': raw.get('confidence', 'medium'),
            'triggered': triggered_now,
            'current_price': price,
        }
