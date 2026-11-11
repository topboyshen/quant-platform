"""行情并发模块：代码规范化静态逻辑。"""

import pytest

from radar.data.market_data_concurrent import ConcurrentMarketDataFetcher


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", "000001"),
        ("300033", "300033"),
        (" 920429 ", "920429"),
    ],
)
def test_normalize_listed_code(raw, expected):
    assert ConcurrentMarketDataFetcher._normalize_listed_code(raw) == expected
