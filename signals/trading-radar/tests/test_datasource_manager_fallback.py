"""DataSourceManager 默认降级链与行情有效性回归测试。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import radar.data.datasource_manager as datasource_module
import radar.data.market_data_v2 as market_data_module
from radar.data.datasource_manager import (
    CacheSource,
    DataSource,
    DataSourceManager,
    EastMoneySource,
    SinaSource,
    TencentSource,
)
from radar.data.market_data_v2 import MarketDataFetcher


def _build_default_manager(monkeypatch) -> DataSourceManager:
    """构造真实默认源，但隔离连接池、AkShare 导入与东财适配器。"""
    monkeypatch.setattr(datasource_module, "create_pooled_session", lambda *args, **kwargs: object())
    monkeypatch.setattr(datasource_module, "MarketDataFetcher", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        datasource_module,
        "ProtectedMarketDataFetcher",
        lambda **kwargs: kwargs["fetcher"],
    )
    monkeypatch.setattr(
        datasource_module.AkshareSource,
        "_init_akshare",
        lambda self: setattr(self, "_ak", None),
    )
    return DataSourceManager(logging.getLogger(__name__), max_workers=1)


def test_default_single_quote_fallback_order(monkeypatch):
    manager = _build_default_manager(monkeypatch)
    try:
        ordered_names = [
            source.name for source in sorted(manager._sources, key=lambda source: source.priority)
        ]
        assert ordered_names == ["tencent", "sina", "eastmoney", "akshare"]
    finally:
        manager.shutdown()


def test_tencent_success_short_circuits_all_fallback_sources(monkeypatch):
    manager = _build_default_manager(monkeypatch)
    calls = {}
    try:
        for source in manager._sources:
            result = None
            if source.name == "tencent":
                result = {
                    "code": "000001",
                    "name": "平安银行",
                    "price": 10.25,
                    "source": "tencent",
                }
            source.fetch_single = MagicMock(return_value=result)
            calls[source.name] = source.fetch_single

        quote = manager.fetch_single("000001", "平安银行")

        assert quote is not None
        assert quote["source"] == "tencent"
        calls["tencent"].assert_called_once_with("000001", "平安银行")
        calls["sina"].assert_not_called()
        calls["eastmoney"].assert_not_called()
        calls["akshare"].assert_not_called()
    finally:
        manager.shutdown()


class _StubSource:
    def __init__(self, name, priority, result):
        self.name = name
        self.priority = priority
        self.result = result
        self.is_healthy = True
        self.calls = 0

    def fetch_single(self, code, name):
        self.calls += 1
        return self.result


@pytest.mark.parametrize("bad_price", [0, -1, float("nan"), float("inf"), None, "invalid"])
def test_invalid_price_is_rejected_and_next_source_is_used(bad_price):
    tencent = _StubSource(
        "tencent",
        1,
        {"code": "000001", "name": "平安银行", "price": bad_price, "source": "tencent"},
    )
    sina = _StubSource(
        "sina",
        2,
        {"code": "000001", "name": "平安银行", "price": 10.25, "source": "sina"},
    )
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [tencent, sina]
    manager._cache = CacheSource(manager.logger)

    quote = manager.fetch_single("000001", "平安银行")

    assert quote is not None
    assert quote["source"] == "sina"
    assert tencent.calls == 1
    assert sina.calls == 1


@pytest.mark.parametrize(
    "bad_quote",
    [
        {"code": "600000", "price": 10.25, "source": "tencent"},
        {"code": "000001", "price": 10.25, "source": "tencent", "stale": True},
        {"code": "000001", "price": 10.25, "source": "tencent", "fresh": False},
    ],
)
def test_wrong_code_or_stale_quote_is_rejected(bad_quote):
    tencent = _StubSource("tencent", 1, bad_quote)
    sina = _StubSource(
        "sina",
        2,
        {"code": "000001", "price": 10.25, "source": "sina", "fresh": True},
    )
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [tencent, sina]
    manager._cache = CacheSource(manager.logger)

    quote = manager.fetch_single("000001", "平安银行")

    assert quote is not None
    assert quote["source"] == "sina"
    assert tencent.calls == 1
    assert sina.calls == 1


def test_validated_quote_preserves_real_inner_source():
    eastmoney_wrapper = _StubSource(
        "eastmoney",
        3,
        {
            "code": "000001",
            "price": 10.25,
            "source": "tencent",
            "fresh": True,
        },
    )
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [eastmoney_wrapper]
    manager._cache = CacheSource(manager.logger)

    quote = manager.fetch_single("000001", "平安银行")

    assert quote is not None
    assert quote["source"] == "tencent"


def test_unhealthy_source_automatically_enters_half_open_after_timeout(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(datasource_module.time, "monotonic", lambda: now[0])
    source = DataSource("test", 1, logging.getLogger(__name__))

    for _ in range(source.FAILURE_THRESHOLD):
        source.mark_failure()
    assert source.is_healthy is False

    now[0] += source.RECOVERY_TIMEOUT + 0.1
    assert source.is_healthy is True
    assert source.can_attempt() is True
    assert source.can_attempt() is False

    source.mark_failure()
    assert source.is_healthy is False
    now[0] += source.RECOVERY_TIMEOUT + 0.1
    assert source.can_attempt() is True
    source.mark_success()
    assert source.is_healthy is False
    source.mark_validated_success(half_open_attempt=True)
    assert source.is_healthy is True
    assert source._failure_count == 0


def test_eastmoney_rejects_expired_inner_batch_cache(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(datasource_module.time, "time", lambda: now[0])
    base = SimpleNamespace(
        _cache_time=now[0] - 10,
        _get_cache_ttl=lambda: 3.0,
    )
    wrapper = SimpleNamespace(
        fetcher=base,
        fetch_all=lambda: pd.DataFrame(
            {
                "code": ["000001"],
                "name": ["测试"],
                "close": [10.25],
            }
        ),
    )
    source = object.__new__(EastMoneySource)
    DataSource.__init__(source, "eastmoney", 3, logging.getLogger(__name__))
    source._fetcher = wrapper
    source._batch_cache = None
    source._batch_cache_time = 0.0
    # Simulate a cache created before the market opened with a 60s TTL.  The
    # current upstream TTL is 3s and must win at the boundary.
    source._batch_ttl = 60.0
    source._row_index = None
    source._batch_lock = threading.Lock()

    assert source._update_batch_cache() is False
    assert source._batch_cache is None


def test_eastmoney_cache_rejects_mismatched_dataframe_and_index():
    source = object.__new__(EastMoneySource)
    DataSource.__init__(source, "eastmoney", 3, logging.getLogger(__name__))
    source._fetcher = SimpleNamespace(fetcher=SimpleNamespace(_get_cache_ttl=lambda: 3.0))
    source._batch_cache = pd.DataFrame(
        {"code": ["600000"], "name": ["错误标的"], "close": [20.0]}
    )
    source._batch_cache_time = time.time()
    source._batch_ttl = 3.0
    source._row_index = {"000001": 0}
    source._batch_lock = threading.Lock()

    assert source._get_from_cache("000001") is None


def test_eastmoney_dataframe_and_index_are_published_atomically():
    now = time.time()
    old_df = pd.DataFrame(
        {
            "code": ["000001", "600000"],
            "name": ["旧一", "旧二"],
            "close": [10.0, 20.0],
        }
    )
    new_df = pd.DataFrame(
        {
            "code": ["600000", "000001"],
            "name": ["新二", "新一"],
            "close": [21.0, 11.0],
        }
    )
    base = SimpleNamespace(_cache_time=now, _get_cache_ttl=lambda: 3.0)
    source = object.__new__(EastMoneySource)
    DataSource.__init__(source, "eastmoney", 3, logging.getLogger(__name__))
    source._fetcher = SimpleNamespace(fetcher=base, fetch_all=lambda: new_df)
    source._batch_cache = old_df
    source._batch_cache_time = now - 10
    source._batch_ttl = 3.0
    source._row_index = EastMoneySource._build_index(old_df)
    source._batch_lock = threading.Lock()

    build_started = threading.Event()
    allow_publish = threading.Event()
    real_build = EastMoneySource._build_index

    def blocked_build(dataframe):
        build_started.set()
        assert allow_publish.wait(timeout=2)
        return real_build(dataframe)

    source._build_index = blocked_build
    result = []
    worker = threading.Thread(target=lambda: result.append(source._update_batch_cache()))
    worker.start()
    assert build_started.wait(timeout=2)

    during_update = source._get_from_cache("000001")
    assert during_update is not None
    assert during_update["code"] == "000001"
    assert during_update["price"] == 10.0

    allow_publish.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == [True]
    after_update = source._get_from_cache("000001")
    assert after_update is not None
    assert after_update["code"] == "000001"
    assert after_update["price"] == 11.0


def test_market_fetcher_does_not_return_expired_batch_row(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(market_data_module.time, "time", lambda: now[0])
    fetcher = object.__new__(MarketDataFetcher)
    fetcher.logger = logging.getLogger(__name__)
    fetcher._cache_time = now[0] - 10
    fetcher._cache_data = pd.DataFrame(
        {
            "code": ["000001"],
            "name": ["测试"],
            "close": [10.25],
        }
    )
    fetcher._get_cache_ttl = lambda: 3.0

    assert fetcher._get_from_batch_cache("000001") is None


def test_market_fetcher_tencent_fallback_reports_real_source():
    parts = [""] * 45
    parts[1] = "测试"
    parts[2] = "000001"
    parts[3] = "10.25"
    parts[4] = "10.00"
    parts[5] = "10.10"
    parts[33] = "10.30"
    parts[34] = "9.90"
    parts[36] = "1000"
    response = MagicMock(status_code=200, text=f'v_sz000001="{"~".join(parts)}";')
    fetcher = object.__new__(MarketDataFetcher)
    fetcher.logger = logging.getLogger(__name__)
    fetcher._backup_session = MagicMock()
    fetcher._backup_session.get.return_value = response

    quote = fetcher._get_backup_stock_data("000001", "测试")

    assert quote is not None
    assert quote["source"] == "tencent"


def test_market_fetcher_tencent_fallback_rejects_wrong_code():
    parts = [""] * 45
    parts[1] = "错误标的"
    parts[2] = "600000"
    parts[3] = "10.25"
    response = MagicMock(status_code=200, text=f'v_sz000001="{"~".join(parts)}";')
    fetcher = object.__new__(MarketDataFetcher)
    fetcher.logger = logging.getLogger(__name__)
    fetcher._backup_session = MagicMock()
    fetcher._backup_session.get.return_value = response

    assert fetcher._get_backup_stock_data("000001", "测试") is None


def test_malformed_tencent_batch_falls_back_to_parallel_quotes():
    tencent = SimpleNamespace(
        name="tencent",
        is_healthy=True,
        fetch_batch=lambda _codes: pd.DataFrame({"wrong": [1]}),
    )
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [tencent]
    manager._fetch_parallel = MagicMock(return_value={"fallback": {"price": 1}})
    stocks = [(f"{index:06d}", "测试") for index in range(1, 17)]

    result = manager._fetch_batch_first(stocks)

    assert result == {"fallback": {"price": 1}}
    manager._fetch_parallel.assert_called_once_with(stocks)


def test_invalid_half_open_probe_immediately_reopens_source(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(datasource_module.time, "monotonic", lambda: now[0])
    source = DataSource("tencent", 1, logging.getLogger(__name__))
    for _ in range(source.FAILURE_THRESHOLD):
        source.mark_failure()
    now[0] += source.RECOVERY_TIMEOUT + 0.1

    def invalid_quote(_code, _name):
        # Some adapters optimistically mark transport success before manager
        # validation detects a bad price/code.
        source.mark_success()
        return {"code": "000001", "price": 0, "source": "tencent"}

    source.fetch_single = invalid_quote
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [source]
    manager._cache = CacheSource(manager.logger)

    assert manager.fetch_single("000001", "测试") is None
    assert source.is_healthy is False
    assert source._failure_count == source.FAILURE_THRESHOLD


def test_adapter_success_cannot_release_unvalidated_half_open_probe(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(datasource_module.time, "monotonic", lambda: now[0])
    source = DataSource("tencent", 1, logging.getLogger(__name__))
    for _ in range(source.FAILURE_THRESHOLD):
        source.mark_failure()
    now[0] += source.RECOVERY_TIMEOUT + 0.1

    adapter_succeeded = threading.Event()
    finish_adapter = threading.Event()

    def delayed_invalid_quote(_code, _name):
        source.mark_success()
        adapter_succeeded.set()
        assert finish_adapter.wait(timeout=2)
        return {"code": "000001", "price": 0, "source": "tencent"}

    source.fetch_single = delayed_invalid_quote
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [source]
    manager._cache = CacheSource(manager.logger)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(manager.fetch_single("000001", "测试"))
    )
    worker.start()
    assert adapter_succeeded.wait(timeout=2)

    assert source.can_attempt() is False
    finish_adapter.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == [None]
    assert source.is_healthy is False


def test_invalid_quote_restores_failure_count_after_adapter_reset():
    source = DataSource("tencent", 1, logging.getLogger(__name__))
    source._failure_count = source.FAILURE_THRESHOLD - 1

    def invalid_quote(_code, _name):
        source.mark_success()
        return {"code": "000001", "price": float("nan"), "source": "tencent"}

    source.fetch_single = invalid_quote
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._sources = [source]
    manager._cache = CacheSource(manager.logger)

    assert manager.fetch_single("000001", "测试") is None
    assert source._failure_count == source.FAILURE_THRESHOLD
    assert source.is_healthy is False


def test_sina_rejects_response_for_another_symbol():
    parts = ["错误标的", "10", "10", "10.25", "10.3", "9.9", "", "", "1000"]
    response = MagicMock(
        status_code=200,
        text=f'var hq_str_sh600000="{",".join(parts)}";',
    )
    source = object.__new__(SinaSource)
    DataSource.__init__(source, "sina", 2, logging.getLogger(__name__))
    source._session = MagicMock()
    source._session.get.return_value = response

    assert source.fetch_single("000001", "测试") is None


def test_tencent_batch_keeps_valid_rows_when_another_row_is_malformed():
    valid = [""] * 45
    valid[1] = "有效"
    valid[2] = "000001"
    valid[3] = "10.25"
    valid[4] = "10.00"
    valid[5] = "10.10"
    valid[32] = "1.5"
    valid[33] = "10.30"
    valid[34] = "9.90"
    valid[36] = "1000"
    malformed = list(valid)
    malformed[1] = "畸形"
    malformed[2] = "600000"
    malformed[3] = "not-a-price"
    response = MagicMock(
        status_code=200,
        text=(
            f'v_sz000001="{"~".join(valid)}";'
            f'v_sh600000="{"~".join(malformed)}";'
        ),
    )
    source = object.__new__(TencentSource)
    DataSource.__init__(source, "tencent", 1, logging.getLogger(__name__))
    source._session = MagicMock()
    source._session.get.return_value = response

    dataframe = source.fetch_batch(["000001", "600000"])

    assert dataframe is not None
    assert dataframe["code"].tolist() == ["000001"]


def test_fetch_all_negative_cooldown_never_returns_expired_cache(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(market_data_module.time, "time", lambda: now[0])
    fetcher = object.__new__(MarketDataFetcher)
    fetcher.logger = logging.getLogger(__name__)
    fetcher._cache_data = pd.DataFrame({"code": ["000001"], "close": [10.25]})
    fetcher._cache_time = now[0] - 10
    fetcher._fetch_all_lock = threading.Lock()
    fetcher._last_fetch_all_attempt = now[0] - 1
    fetcher._failed_fetch_cooldown = 2.0
    fetcher._get_cache_ttl = lambda: 3.0
    fetcher._fetch_all_from_eastmoney = MagicMock()

    assert fetcher.fetch_all() is None
    fetcher._fetch_all_from_eastmoney.assert_not_called()


def test_fetch_all_exception_never_returns_expired_cache(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(market_data_module.time, "time", lambda: now[0])
    fetcher = object.__new__(MarketDataFetcher)
    fetcher.logger = logging.getLogger(__name__)
    fetcher._cache_data = pd.DataFrame({"code": ["000001"], "close": [10.25]})
    fetcher._cache_time = now[0] - 10
    fetcher._fetch_all_lock = threading.Lock()
    fetcher._last_fetch_all_attempt = 0.0
    fetcher._failed_fetch_cooldown = 2.0
    fetcher._get_cache_ttl = lambda: 3.0
    fetcher._fetch_all_from_eastmoney = MagicMock(side_effect=RuntimeError("boom"))

    assert fetcher.fetch_all() is None


def test_parallel_fetch_has_one_global_deadline_and_cancels_queued_work():
    manager = object.__new__(DataSourceManager)
    manager.logger = logging.getLogger(__name__)
    manager._executor = ThreadPoolExecutor(max_workers=1)
    manager._parallel_timeout = 0.05
    release = threading.Event()
    started = threading.Event()
    started_codes = []
    started_lock = threading.Lock()

    def blocked_fetch(code, _name):
        with started_lock:
            started_codes.append(code)
        started.set()
        release.wait(timeout=1)
        return {"code": code, "price": 10.0}

    manager.fetch_single = blocked_fetch
    stocks = [(f"{index:06d}", "测试") for index in range(1, 4)]
    before = time.monotonic()
    try:
        result = manager._fetch_parallel(stocks)
        elapsed = time.monotonic() - before
        assert started.is_set()
        assert result == {}
        assert elapsed < 0.5
    finally:
        release.set()
        manager._executor.shutdown(wait=True)
    assert started_codes == ["000001"]
