"""ML Serve 进程内 Prometheus 指标（独立 Registry，避免与 monitor 冲突）。"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_HAVE = False
_REGISTRY: Optional[object] = None
_PREDICT_TOTAL: Optional[object] = None
_PREDICT_FAIL: Optional[object] = None
_PREDICT_LATENCY: Optional[object] = None  # Histogram(cache_state)
_PREDICT_PROBABILITY: Optional[object] = None  # Histogram — 置信度 / 概率分布
_RELOAD_TOTAL: Optional[object] = None
_CACHE_HIT: Optional[object] = None
_CACHE_MISS: Optional[object] = None

_VALID_CACHE_STATES = frozenset({"hit", "miss", "noop"})

try:
    from prometheus_client import CollectorRegistry, Counter, Histogram

    _HAVE = True
    _REGISTRY = CollectorRegistry()
    _PREDICT_TOTAL = Counter(
        "radar_ml_serve_predict_requests_total",
        "ML 推理请求次数（命中缓存亦计数）",
        registry=_REGISTRY,
    )
    _PREDICT_FAIL = Counter(
        "radar_ml_serve_predict_errors_total",
        "ML 推理失败次数",
        registry=_REGISTRY,
    )
    # hit=缓存直达 miss=缓存未命中走模型 noop=未启用缓存时的推理延迟
    _PREDICT_LATENCY = Histogram(
        "radar_ml_serve_predict_latency_seconds",
        "单次推理端到端耗时（按缓存路径拆分，便于区分「缓存拖慢」与「模型拖慢」）",
        ["cache_state"],
        buckets=(
            0.001,
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.2,
            0.25,
            0.5,
            1.0,
            2.0,
            3.0,
            5.0,
        ),
        registry=_REGISTRY,
    )
    _PROB_BUCKETS = (
        0.05,
        0.1,
        0.15,
        0.2,
        0.25,
        0.3,
        0.35,
        0.4,
        0.45,
        0.5,
        0.55,
        0.6,
        0.65,
        0.7,
        0.75,
        0.8,
        0.85,
        0.9,
        0.95,
        1.0,
    )
    _PREDICT_PROBABILITY = Histogram(
        "radar_ml_serve_prediction_probability",
        "模型输出的主概率（如上涨概率），用于观察置信度分布与漂移（热力图 / sum/count 均值）",
        buckets=_PROB_BUCKETS,
        registry=_REGISTRY,
    )
    _RELOAD_TOTAL = Counter(
        "radar_ml_serve_reload_total",
        "模型热重载调用次数",
        registry=_REGISTRY,
    )
    _CACHE_HIT = Counter(
        "radar_ml_serve_cache_hits_total",
        "推理结果缓存命中次数（Redis/Memcached 等预测缓存）",
        registry=_REGISTRY,
    )
    _CACHE_MISS = Counter(
        "radar_ml_serve_cache_misses_total",
        "推理缓存未命中次数（需加载模型计算）",
        registry=_REGISTRY,
    )
except ImportError:
    _CACHE_HIT = None
    _CACHE_MISS = None


def observe_predict_ok(duration_sec: float, cache_state: str = "miss") -> None:
    """cache_state: hit | miss | noop"""
    if not _HAVE or _PREDICT_TOTAL is None:
        return
    state = cache_state if cache_state in _VALID_CACHE_STATES else "miss"
    _PREDICT_TOTAL.inc()
    if _PREDICT_LATENCY is not None:
        _PREDICT_LATENCY.labels(cache_state=state).observe(max(0.0, float(duration_sec)))


def observe_predict_error() -> None:
    if not _HAVE or _PREDICT_FAIL is None:
        return
    _PREDICT_FAIL.inc()


def observe_reload() -> None:
    if not _HAVE or _RELOAD_TOTAL is None:
        return
    _RELOAD_TOTAL.inc()


def observe_cache_hit() -> None:
    if not _HAVE or _CACHE_HIT is None:
        return
    _CACHE_HIT.inc()


def observe_cache_miss() -> None:
    if not _HAVE or _CACHE_MISS is None:
        return
    _CACHE_MISS.inc()


def observe_prediction_probability(probability: float) -> None:
    """记录一次推理输出的概率（0~1），聚合成分布而非逐标的序列。"""
    if not _HAVE or _PREDICT_PROBABILITY is None:
        return
    try:
        x = float(probability)
    except (TypeError, ValueError):
        return
    if x != x:  # NaN
        return
    x = max(0.0, min(1.0, x))
    _PREDICT_PROBABILITY.observe(x)


def metrics_text() -> str:
    if not _HAVE or _REGISTRY is None:
        return ""
    try:
        from prometheus_client import generate_latest

        return generate_latest(_REGISTRY).decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("ml_serve metrics: %s", e)
        return ""
