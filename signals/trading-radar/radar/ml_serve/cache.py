"""
推理结果缓存：相同 code / direction / 价量序列 → 复用上次预测。

环境变量：
- ML_SERVE_CACHE_BACKEND   none（默认）| redis | memcached
- ML_SERVE_CACHE_TTL_SEC   默认 300
- ML_SERVE_REDIS_URL       默认 redis://127.0.0.1:6379/0
- ML_SERVE_MEMCACHED_HOST  默认 127.0.0.1
- ML_SERVE_MEMCACHED_PORT  默认 11211

模型热重载时递增 epoch，使旧缓存键立即失效（键内包含 epoch）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_epoch_lock = threading.Lock()
_predict_cache_epoch = 0


def bump_predict_cache_epoch() -> int:
    """热重载后调用，使缓存键空间轮换。"""
    global _predict_cache_epoch
    with _epoch_lock:
        _predict_cache_epoch += 1
        return _predict_cache_epoch


def predict_cache_epoch() -> int:
    with _epoch_lock:
        return _predict_cache_epoch


def build_predict_cache_key(
    code: str,
    direction: str,
    prices: List[float],
    volumes: Optional[List[float]],
    epoch: int,
) -> str:
    vol: Optional[List[float]]
    if volumes is None:
        vol = None
    else:
        vol = [round(float(v), 6) for v in volumes]
    payload = {
        "e": int(epoch),
        "c": str(code).strip(),
        "d": str(direction).strip().lower(),
        "p": [round(float(x), 6) for x in prices],
        "v": vol,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"radar:pred:v1:{h}"


class _NullCache:
    """未启用缓存。"""

    enabled = False

    def epoch(self) -> int:
        return predict_cache_epoch()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return None

    def set(self, key: str, value: Dict[str, Any], ttl_sec: int) -> None:
        return None


class _RedisCache:
    enabled = True

    def __init__(self, url: str):
        import redis

        self._r = redis.Redis.from_url(url, decode_responses=True)

    def epoch(self) -> int:
        return predict_cache_epoch()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._r.get(key)
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            logger.debug("redis cache get: %s", e)
            return None

    def set(self, key: str, value: Dict[str, Any], ttl_sec: int) -> None:
        try:
            self._r.setex(key, int(ttl_sec), json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        except Exception as e:
            logger.debug("redis cache set: %s", e)


class _MemcachedCache:
    enabled = True

    def __init__(self, host: str, port: int):
        from pymemcache.client.base import Client

        self._c = Client((host, int(port)))

    def epoch(self) -> int:
        return predict_cache_epoch()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._c.get(key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except Exception as e:
            logger.debug("memcached cache get: %s", e)
            return None

    def set(self, key: str, value: Dict[str, Any], ttl_sec: int) -> None:
        try:
            payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self._c.set(key, payload, expire=int(ttl_sec))
        except Exception as e:
            logger.debug("memcached cache set: %s", e)


_cache_instance: Optional[Any] = None
_cache_lock = threading.Lock()


def get_predict_cache() -> None:
    """返回带 epoch/get/set 的缓存后端；未配置时返回 Null。"""
    global _cache_instance
    if _cache_instance is not None:
        return _cache_instance
    with _cache_lock:
        if _cache_instance is not None:
            return _cache_instance
        _cache_instance = _build_cache_from_env()
        return _cache_instance


def _build_cache_from_env() -> None:
    import os

    backend = (os.getenv("ML_SERVE_CACHE_BACKEND") or "none").strip().lower()
    if backend in ("", "none", "off", "false", "0"):
        return _NullCache()
    if backend == "redis":
        url = os.getenv("ML_SERVE_REDIS_URL", "redis://127.0.0.1:6379/0").strip()
        try:
            return _RedisCache(url)
        except Exception as e:
            logger.warning("ML_SERVE_CACHE_BACKEND=redis 初始化失败，关闭缓存: %s", e)
            return _NullCache()
    if backend in ("memcached", "memcache"):
        host = os.getenv("ML_SERVE_MEMCACHED_HOST", "127.0.0.1").strip()
        port = int(os.getenv("ML_SERVE_MEMCACHED_PORT", "11211"))
        try:
            return _MemcachedCache(host, port)
        except Exception as e:
            logger.warning("ML_SERVE_CACHE_BACKEND=memcached 初始化失败，关闭缓存: %s", e)
            return _NullCache()
    logger.warning("未知 ML_SERVE_CACHE_BACKEND=%s，关闭缓存", backend)
    return _NullCache()


def reset_predict_cache_singleton_for_tests() -> None:
    """测试用：清空懒加载单例。"""
    global _cache_instance
    with _cache_lock:
        _cache_instance = None
