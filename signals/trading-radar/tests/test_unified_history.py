"""统一历史 K 线：标准化 + 血缘 attrs。"""

import pandas as pd

from radar.data.unified_history import get_lineage, normalize_history_df


def test_normalize_history_attaches_lineage():
    df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-02"],
            "open": [10.0, 10.1],
            "high": [10.5, 10.6],
            "low": [9.9, 10.0],
            "close": [10.2, 10.3],
            "volume": [1000, 2000],
        }
    )
    out = normalize_history_df(
        df, source="test_src", pipeline="pytest", adjustment="none"
    )
    assert out is not None
    meta = get_lineage(out)
    assert meta is not None
    assert meta["source"] == "test_src"
    assert meta["pipeline"] == "pytest"
    assert meta["adjustment"] == "none"
    assert meta["bars"] == 2
