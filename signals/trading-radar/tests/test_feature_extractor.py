"""增量特征：形状与成交量尾窗。"""

import numpy as np

from radar.data.price_history_store import InMemoryPriceStore
from radar.strategy.feature_extractor import IncrementalFeatureExtractor


def test_update_returns_fixed_dim():
    store = InMemoryPriceStore(max_history=50)
    ex = IncrementalFeatureExtractor(
        code=None, window_sizes=[5, 10, 20], max_history=50, store=store
    )
    f = ex.update(10.0, 1000.0)
    assert f.shape == (15,)
    assert f.dtype == np.float32
    assert f[5] == 1.0


def test_vol_ratio_uses_tail_not_full_scan():
    store = InMemoryPriceStore(max_history=50)
    ex = IncrementalFeatureExtractor(
        code=None, window_sizes=[5, 10, 20], max_history=50, store=store
    )
    for p, v in [(10.0, 1000.0), (10.1, 1000.0), (10.2, 5000.0)]:
        ex.update(p, v)
    f = ex.update(10.3, 100.0)
    assert f[5] > 0
