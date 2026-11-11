from __future__ import annotations

import numpy as np
import pandas as pd

from radar.strategy.feature_extractor import IncrementalFeatureExtractor
from radar.strategy.rl_features import RLObservationBuilder
from radar.strategy.rl_schema import RL_FEATURE_SCHEMA_VERSION, RL_OBS_DIM, build_live_observation


def test_rl_training_and_live_observation_share_schema():
    df = pd.DataFrame(
        {
            "close": [10.0 + i * 0.1 for i in range(30)],
            "price": [10.0 + i * 0.1 for i in range(30)],
            "high": [10.2 + i * 0.1 for i in range(30)],
            "low": [9.8 + i * 0.1 for i in range(30)],
            "volume": [1000.0 + i * 10.0 for i in range(30)],
        }
    )

    builder = RLObservationBuilder(df, trigger_price=11.0)
    training_obs = builder.get_observation(29, position_shares=100, position_value=1200.0)
    live_obs = build_live_observation(
        prices=df["close"].tolist(),
        volumes=df["volume"].tolist(),
        trigger_price=11.0,
        position_shares=100,
        position_cost=12.0,
    )

    assert RL_FEATURE_SCHEMA_VERSION == "rl_live_v1"
    assert training_obs.shape == (RL_OBS_DIM,)
    assert live_obs.shape == (RL_OBS_DIM,)
    np.testing.assert_allclose(training_obs, live_obs, rtol=1e-6, atol=1e-6)


def test_incremental_feature_extractor_matches_canonical_schema_shape():
    ex = IncrementalFeatureExtractor(code=None, window_sizes=[5, 10, 20], max_history=100)
    obs = None
    for i in range(25):
        obs = ex.update(10.0 + i * 0.1, 1000.0 + i)
    assert obs is not None
    assert obs.shape == (RL_OBS_DIM,)
    assert obs.dtype == np.float32
