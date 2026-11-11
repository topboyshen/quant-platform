"""
高性能增量特征提取模块
职责：O(1) 复杂度实时特征计算，无Env依赖
"""

import logging
import os
from collections import deque
from typing import Deque, Dict, List, Optional

import numpy as np

from radar.data.price_history_store import PriceHistoryStore, get_price_store
from radar.strategy.rl_schema import RL_OBS_DIM, build_live_observation, validate_rl_observation


class IncrementalFeatureExtractor:
    """
    增量特征提取器 - 每次更新 O(1)
    使用环形缓冲区避免内存分配
    支持持久化价格历史（Redis/PVC/InfluxDB），解决 Pod 重启数据丢失
    """

    def __init__(self, code: str = None, window_sizes: List[int] = [5, 10, 20],
                 max_history: int = 100, store: PriceHistoryStore = None):
        """
        初始化特征提取器
        :param code: 股票代码（用于持久化存储）
        :param window_sizes: 移动平均窗口大小
        :param max_history: 最大历史数据长度
        :param store: 价格历史存储实例（默认从全局工厂获取）
        """
        self.code = code
        self.window_sizes = window_sizes
        self.max_history = max_history

        # 持久化存储
        self._store = store or get_price_store()
        self._logger = logging.getLogger(__name__)

        # 环形缓冲区 - 避免频繁内存分配
        self._prices: Deque[float] = deque(maxlen=max_history)
        self._volumes: Deque[float] = deque(maxlen=max_history)
        self._vol_tail: Deque[float] = deque(maxlen=5)

        # 增量状态缓存
        self._window_sums = {w: 0.0 for w in window_sizes}
        self._window_counts = {w: 0 for w in window_sizes}

        # 预分配输出数组
        self._feature_dim = RL_OBS_DIM

        # 如果有 code 且存储中有历史数据，尝试恢复状态
        if code:
            self._restore_from_store()

    def _restore_from_store(self) -> None:
        """从持久化存储恢复历史数据"""
        try:
            history = self._store.get_history(self.code, n=self.max_history)
            if len(history) < 2:
                return  # 数据不足，保持空状态

            self._logger.debug(f"{self.code} 从存储恢复 {len(history)} 条历史记录")

            # 清空当前状态
            self._prices.clear()
            self._volumes.clear()
            self._window_sums = {w: 0.0 for w in self.window_sizes}
            self._window_counts = {w: 0 for w in self.window_sizes}

            # 恢复所有历史（除了最后一条会由 update 处理）
            for ts, price, volume in history[:-1]:
                self._prices.append(price)
                self._volumes.append(volume)

                # 增量恢复窗口和
                n = len(self._prices)
                for w in self.window_sizes:
                    if n > w:
                        # 滑动窗口
                        pass  # 不需要加，因为后续会重新计算
                    else:
                        self._window_sums[w] += price
                        self._window_counts[w] = n

            # 重新精确计算窗口和（避免恢复过程中累积误差）
            price_list = list(self._prices)
            for w in self.window_sizes:
                self._window_counts[w] = min(len(price_list), w)
                self._window_sums[w] = sum(price_list[-w:]) if price_list else 0.0

            self._vol_tail.clear()
            for v in list(self._volumes)[-5:]:
                self._vol_tail.append(v)

        except Exception as e:
            self._logger.warning(f"从存储恢复 {self.code} 历史数据失败: {e}")

    def update(self, price: float, volume: float = 0) -> np.ndarray:
        """
        增量更新特征 - O(1) 复杂度
        :param price: 最新价格
        :param volume: 最新成交量
        :return: 特征向量
        """
        # 0. 持久化存储（如果配置了 code）
        if self.code:
            try:
                self._store.append(self.code, price, volume)
            except Exception as e:
                self._logger.warning(f"持久化 {self.code} 价格失败: {e}")

        # 1. 更新缓冲区
        old_price = self._prices[0] if len(self._prices) == self.max_history else None
        self._prices.append(price)
        self._volumes.append(volume)
        self._vol_tail.append(volume)

        # 2. 增量更新窗口和
        for w in self.window_sizes:
            if old_price is not None and self._window_counts[w] >= w:
                # 滑动窗口：减去旧值，加上新值
                self._window_sums[w] += price - old_price
            else:
                # 累积窗口
                self._window_sums[w] += price
                self._window_counts[w] = min(self._window_counts[w] + 1, w)

        # 3. 计算特征（全部基于增量状态，无循环）
        return self._compute_features(price, volume)

    def _compute_features(self, current_price: float, current_volume: float) -> np.ndarray:
        """
        计算特征向量 - O(1)
        基于当前增量状态直接计算
        """
        return build_live_observation(
            prices=self._prices,
            volumes=self._volumes,
            trigger_price=0.0,
        )

    def reset(self) -> None:
        """重置状态"""
        self._prices.clear()
        self._volumes.clear()
        self._vol_tail.clear()
        self._window_sums = {w: 0.0 for w in self.window_sizes}
        self._window_counts = {w: 0 for w in self.window_sizes}

        # 同时清空持久化存储
        if self.code:
            try:
                self._store.clear(self.code)
            except Exception as e:
                self._logger.warning(f"清空 {self.code} 持久化数据失败: {e}")

    def get_price_history(self, n: int = 20) -> List[float]:
        """获取最近N个价格"""
        return list(self._prices)[-n:]


class CachedModelPredictor:
    """
    模型预测缓存器 - 按 key 懒加载 + 进程内缓存
    磁盘约定：{model_dir}/{key}.zip，其中 key 与 _make_key() 一致（如 ppo_300033_below_ext）。
    仅在实际用到某支模型时才 PPO.load，避免首次 RL 调用把目录下全部 zip 拉进内存。
    """

    _global_cache: Dict[str, Optional[object]] = {}
    _global_model_dir: Optional[str] = None
    _obs_dims: Dict[str, int] = {}
    _scratch: Optional[np.ndarray] = None
    _scratch_cap: int = 0

    def __init__(self, model_dir: str = "rl_models"):
        self.model_dir = os.path.abspath(model_dir)
        self._log = logging.getLogger(__name__)

    def _sync_model_dir(self) -> None:
        if CachedModelPredictor._global_model_dir is not None:
            if CachedModelPredictor._global_model_dir != self.model_dir:
                self._log.info(
                    f"RL 模型目录切换，清空缓存: "
                    f"{CachedModelPredictor._global_model_dir} -> {self.model_dir}"
                )
                CachedModelPredictor._global_cache.clear()
                CachedModelPredictor._obs_dims.clear()
        CachedModelPredictor._global_model_dir = self.model_dir

    def _lazy_load_key(self, key: str) -> None:
        """首次访问某 key 时加载对应单个 zip；失败则缓存 None，避免反复打盘。"""
        self._sync_model_dir()
        if key in CachedModelPredictor._global_cache:
            return
        filepath = os.path.join(self.model_dir, f"{key}.zip")
        if not os.path.isfile(filepath):
            CachedModelPredictor._global_cache[key] = None
            CachedModelPredictor._obs_dims.pop(key, None)
            return
        try:
            from stable_baselines3 import PPO
            model = PPO.load(filepath)
            CachedModelPredictor._global_cache[key] = model
            shp = model.observation_space.shape
            CachedModelPredictor._obs_dims[key] = int(
                shp[1] if len(shp) > 1 else shp[0]
            )
            self._log.info(f"RL 模型已按需加载: {key} ({filepath})")
        except Exception as e:
            self._log.warning(f"RL 模型加载失败 {key}: {e}")
            CachedModelPredictor._global_cache[key] = None
            CachedModelPredictor._obs_dims.pop(key, None)

    def _make_key(self, direction: str, universal: bool = True,
                  code: str = "", model_suffix: str = "") -> str:
        """生成模型缓存 key"""
        if universal:
            key = f"ppo_universal_{direction}"
        else:
            key = f"ppo_{code}_{direction}" if code else f"ppo_{direction}"
        if model_suffix:
            key = f"{key}_{model_suffix}"
        return key

    def is_available(self, direction: str, universal: bool = True,
                      code: str = "", model_suffix: str = "") -> bool:
        """检查模型是否可用（未加载时会尝试加载一次）"""
        key = self._make_key(direction, universal, code, model_suffix)
        self._lazy_load_key(key)
        return CachedModelPredictor._global_cache.get(key) is not None

    @classmethod
    def _ensure_scratch(cls, n: int) -> np.ndarray:
        if cls._scratch is None or cls._scratch_cap < n:
            cls._scratch = np.zeros(n, dtype=np.float32)
            cls._scratch_cap = n
        return cls._scratch

    def predict(self, direction: str, features: np.ndarray,
                universal: bool = True, code: str = "",
                model_suffix: str = "") -> int:
        """模型预测"""
        key = self._make_key(direction, universal, code, model_suffix)
        self._lazy_load_key(key)
        model = CachedModelPredictor._global_cache.get(key)

        if model is None:
            return 0  # hold

        obs_dim = CachedModelPredictor._obs_dims.get(key)
        if obs_dim is None:
            shp = model.observation_space.shape
            obs_dim = int(shp[1] if len(shp) > 1 else shp[0])
            CachedModelPredictor._obs_dims[key] = obs_dim

        feat1 = np.ravel(np.asarray(features, dtype=np.float32, order='C'))
        n = int(feat1.shape[0])

        if n != obs_dim:
            self._log.error(
                "RL observation schema mismatch for %s: model_dim=%s feature_dim=%s",
                key,
                obs_dim,
                n,
            )
            return 0  # hold

        try:
            obs = validate_rl_observation(feat1, expected_dim=obs_dim).reshape(1, -1)
        except ValueError as e:
            self._log.error("RL observation validation failed for %s: %s", key, e)
            return 0

        action, _ = model.predict(obs, deterministic=True)
        return int(action[0])


# 全局预测器实例（单例模式）
_global_predictor: Optional[CachedModelPredictor] = None


def get_global_predictor(model_dir: str = "rl_models") -> CachedModelPredictor:
    """获取全局预测器实例"""
    global _global_predictor
    if _global_predictor is None:
        _global_predictor = CachedModelPredictor(model_dir)
    return _global_predictor
