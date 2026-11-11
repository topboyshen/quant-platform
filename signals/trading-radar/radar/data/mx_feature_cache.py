"""
妙想外部特征短期内存缓存（减秒级重复 query，省日配额）。

用于 rl_features.ExternalFeatureProvider：同一进程内、TTL 内的相同请求直接返回缓存，
不再次调用 MXDataClient.query（也就不再扣 mx_quota）。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Tuple, TypeVar

T = TypeVar("T")

_lock = threading.RLock()
_store: Dict[Tuple[str, Tuple[Any, ...]], Tuple[float, Any]] = {}

_MAX_DEFAULT = 256


def mx_feature_cache_disabled() -> bool:
    return os.getenv("MX_FEATURE_CACHE_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def mx_feature_cache_ttl_sec() -> float:
    return float(os.getenv("MX_FEATURE_CACHE_TTL_SEC", "300"))


def mx_feature_cache_max_entries() -> int:
    return int(os.getenv("MX_FEATURE_CACHE_MAX_ENTRIES", str(_MAX_DEFAULT)))


def _prune_unlocked(now: float) -> None:
    max_e = max(32, mx_feature_cache_max_entries())
    dead = [k for k, (exp, _) in _store.items() if exp <= now]
    for k in dead:
        del _store[k]
    if len(_store) <= max_e:
        return
    # 仍过大：按过期时间淘汰最早的一批
    n_drop = len(_store) - max_e + 32
    for k in sorted(_store.keys(), key=lambda x: _store[x][0])[:n_drop]:
        del _store[k]


def mx_cached(namespace: str, key_parts: Tuple[Any, ...], factory: Callable[[], T]) -> T:
    """
    :param namespace: 逻辑分组，如 ``mx_feat``
    :param key_parts: 可哈希参数元组
    :param factory: 缓存未命中时调用（内部应执行妙想请求）
    """
    if mx_feature_cache_disabled():
        return factory()

    key = (namespace, key_parts)
    ttl = mx_feature_cache_ttl_sec()
    now = time.monotonic()

    with _lock:
        ent = _store.get(key)
        if ent is not None and ent[0] > now:
            return ent[1]

    val = factory()

    with _lock:
        _store[key] = (now + ttl, val)
        _prune_unlocked(time.monotonic())

    return val


def mx_cache_clear() -> None:
    """测试或热重置用。"""
    with _lock:
        _store.clear()
