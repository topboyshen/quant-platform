"""ohlcv_parquet_store：Parquet 读写与校验（需 pyarrow）。"""

from __future__ import annotations

import json
import logging
import time

import pandas as pd
import pytest

pytest.importorskip("pyarrow")

from data_schema import ColumnNames, normalize_dataframe
from radar.data.ohlcv_parquet_store import (
    META_KEY_LINEAGE,
    read_kline_parquet_barrier,
    try_load_kline_parquet,
    write_kline_parquet_barrier,
)


def _sample_ohlcv(n: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    base = 10.0
    rows = []
    for i in range(n):
        o = base + i * 0.1
        low = o - 0.5
        high = o + 0.8
        c = o + 0.2
        rows.append(
            {
                ColumnNames.DATE: idx[i],
                ColumnNames.OPEN: float(o),
                ColumnNames.HIGH: float(high),
                ColumnNames.LOW: float(low),
                ColumnNames.CLOSE: float(c),
                ColumnNames.VOLUME: float(1_000_000 + i * 1000),
            }
        )
    return normalize_dataframe(pd.DataFrame(rows), source="test_parquet")


def test_parquet_roundtrip_and_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_KLINE_PARQUET_DIR", str(tmp_path))
    from radar.data import ohlcv_parquet_store as ops

    df = _sample_ohlcv(35)
    df.attrs["radar_lineage"] = {"source": "test", "pipeline": "pytest"}
    path = ops.kline_parquet_path("000001")
    write_kline_parquet_barrier(df, path, lineage={"note": "extra"})
    back = read_kline_parquet_barrier(path)
    assert len(back) == len(df)
    for col in [
        ColumnNames.DATE,
        ColumnNames.OPEN,
        ColumnNames.HIGH,
        ColumnNames.LOW,
        ColumnNames.CLOSE,
        ColumnNames.VOLUME,
    ]:
        pd.testing.assert_series_equal(
            back[col].reset_index(drop=True),
            df[col].reset_index(drop=True),
            check_names=False,
        )
    assert "source" in back.attrs.get("radar_lineage", {})
    assert back.attrs["radar_lineage"].get("note") == "extra"


def test_write_rejects_empty():
    with pytest.raises(ValueError, match="空"):
        write_kline_parquet_barrier(pd.DataFrame(), "x.parquet")


def test_read_rejects_corrupt(tmp_path):
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet")
    with pytest.raises(Exception):
        read_kline_parquet_barrier(bad)


def test_try_load_respects_max_age(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_KLINE_PARQUET_DIR", str(tmp_path))
    from radar.data import ohlcv_parquet_store as ops

    df = _sample_ohlcv(40)
    path = ops.kline_parquet_path("600000")
    write_kline_parquet_barrier(df, path)
    old = time.time() - 10 * 86400
    import os

    os.utime(path, (old, old))
    assert try_load_kline_parquet("600000", min_rows=30, max_age_days=5.0) is None
    assert try_load_kline_parquet("600000", min_rows=30, max_age_days=15.0) is not None


def test_lineage_in_arrow_metadata(tmp_path):
    import pyarrow.parquet as pq

    df = _sample_ohlcv(10)
    path = tmp_path / "m.parquet"
    write_kline_parquet_barrier(df, path, lineage={"k": "v"})
    table = pq.read_table(path)
    raw = table.schema.metadata or {}
    assert META_KEY_LINEAGE in raw
    meta = json.loads(raw[META_KEY_LINEAGE].decode("utf-8"))
    assert meta.get("k") == "v"


def test_market_fetcher_prefers_parquet_no_network(tmp_path, monkeypatch):
    """回测同源入口 get_historical_data 在 PREFER_READ 时不应打东财。"""
    monkeypatch.setenv("RADAR_KLINE_PARQUET_PREFER_READ", "1")
    monkeypatch.setenv("RADAR_KLINE_PARQUET_DIR", str(tmp_path))

    import radar.data.market_data as mdm
    from radar.data.market_data import MarketDataFetcher
    from radar.data.ohlcv_parquet_store import kline_parquet_path, write_kline_parquet_barrier

    df = _sample_ohlcv(50)
    df[ColumnNames.DATE] = pd.date_range(end=pd.Timestamp.now().normalize(), periods=50, freq="D")
    write_kline_parquet_barrier(df, kline_parquet_path("000001"))

    def boom(*_a, **_k):
        raise AssertionError("eastmoney HTTP should not be called when parquet hit")

    monkeypatch.setattr(mdm.requests, "get", boom)
    fetcher = MarketDataFetcher(logging.getLogger("pytest.mkt"))
    out = fetcher.get_historical_data("000001", days=100)
    assert len(out) == 50
