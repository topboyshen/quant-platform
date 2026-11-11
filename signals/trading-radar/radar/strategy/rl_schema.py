"""Canonical RL observation schema shared by training, backtesting, and live inference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

RL_FEATURE_SCHEMA_VERSION = "rl_live_v1"
RL_FEATURE_NAMES: tuple[str, ...] = (
    "price_change_pct",
    "dev_ma5_pct",
    "dev_ma10_pct",
    "dev_ma20_pct",
    "price_range_pct",
    "volume_ratio",
    "ma_trend_pct",
    "reserved_1",
    "reserved_2",
    "reserved_3",
    "position_flag",
    "position_pnl_pct",
    "distance_to_trigger_pct",
    "volume_trend_pct",
    "momentum_pct",
)
RL_OBS_DIM = len(RL_FEATURE_NAMES)


@dataclass(frozen=True)
class RLFeatureSchema:
    version: str = RL_FEATURE_SCHEMA_VERSION
    names: tuple[str, ...] = RL_FEATURE_NAMES

    @property
    def dim(self) -> int:
        return len(self.names)


CANONICAL_RL_SCHEMA = RLFeatureSchema()


def validate_rl_observation(obs: Sequence[float], expected_dim: int = RL_OBS_DIM) -> np.ndarray:
    arr = np.asarray(obs, dtype=np.float32)
    if arr.shape != (expected_dim,):
        raise ValueError(f"RL observation schema mismatch: expected {(expected_dim,)}, got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("RL observation contains NaN or infinite values")
    return arr


def build_live_observation(
    *,
    prices: Sequence[float],
    volumes: Sequence[float] | None,
    trigger_price: float,
    position_shares: int = 0,
    position_cost: float = 0.0,
) -> np.ndarray:
    prices_arr = np.asarray(list(prices), dtype=np.float64)
    if prices_arr.size == 0:
        return np.zeros(RL_OBS_DIM, dtype=np.float32)

    volumes_arr = np.asarray(list(volumes or []), dtype=np.float64)
    if volumes_arr.size < prices_arr.size:
        pad_len = prices_arr.size - volumes_arr.size
        volumes_arr = np.concatenate([np.zeros(pad_len, dtype=np.float64), volumes_arr])
    elif volumes_arr.size > prices_arr.size:
        volumes_arr = volumes_arr[-prices_arr.size:]

    current = float(prices_arr[-1])
    prev = float(prices_arr[-2]) if prices_arr.size >= 2 else current
    price_change = (current - prev) / prev if prev > 0 else 0.0

    def ma(window: int) -> float:
        return float(np.mean(prices_arr[-window:])) if prices_arr.size else current

    ma5 = ma(5)
    ma10 = ma(10)
    ma20 = ma(20)

    dev_ma5 = (current - ma5) / ma5 if ma5 > 0 else 0.0
    dev_ma10 = (current - ma10) / ma10 if ma10 > 0 else 0.0
    dev_ma20 = (current - ma20) / ma20 if ma20 > 0 else 0.0

    recent = prices_arr[-5:]
    price_range = (float(np.max(recent)) - float(np.min(recent))) / current if current > 0 and recent.size else 0.0

    current_volume = float(volumes_arr[-1]) if volumes_arr.size else 0.0
    vol_tail = volumes_arr[-5:] if volumes_arr.size else np.array([current_volume], dtype=np.float64)
    vol_ma5 = float(np.mean(vol_tail)) if vol_tail.size else current_volume
    volume_ratio = current_volume / vol_ma5 if vol_ma5 > 0 else 1.0

    ma_trend = (ma5 - ma20) / ma20 if ma20 > 0 else 0.0
    position_flag = 1.0 if position_shares > 0 else 0.0
    position_pnl = (current - position_cost) / position_cost * 100.0 if position_shares > 0 and position_cost > 0 else 0.0
    distance_to_trigger = (current - trigger_price) / trigger_price * 100.0 if trigger_price > 0 else 0.0

    if volumes_arr.size >= 10:
        recent_vol = float(np.mean(volumes_arr[-5:]))
        past_vol = float(np.mean(volumes_arr[-10:-5]))
        volume_trend = (recent_vol - past_vol) / past_vol * 100.0 if past_vol > 0 else 0.0
    else:
        volume_trend = 0.0

    if prices_arr.size >= 20:
        base = float(prices_arr[-20])
        momentum = (current - base) / base * 100.0 if base > 0 else 0.0
    elif prices_arr.size >= 2:
        momentum = price_change * 100.0
    else:
        momentum = 0.0

    obs = np.array(
        [
            price_change * 100.0,
            dev_ma5 * 100.0,
            dev_ma10 * 100.0,
            dev_ma20 * 100.0,
            price_range * 100.0,
            volume_ratio,
            ma_trend * 100.0,
            0.0,
            0.0,
            0.0,
            position_flag,
            position_pnl,
            distance_to_trigger,
            volume_trend,
            momentum,
        ],
        dtype=np.float32,
    )
    return validate_rl_observation(np.clip(obs, -100.0, 100.0))
