"""
日 K 数据「物理防洪坝」：训练/回测侧统一落盘为 Parquet，读回时强制校验。

- 写入前：data_schema.normalize_dataframe + validate_dataframe，不通过则拒绝写入。
- 读入后：再次 validate_dataframe，防止磁盘篡改或版本漂移。
- 元数据：pyarrow schema metadata 中存放 radar_lineage（JSON）。

环境变量：
- RADAR_KLINE_PARQUET_DIR  目录，默认 data/kline_parquet
- RADAR_KLINE_PARQUET_PREFER_READ  训练拉数时优先读本地 Parquet（1/true/yes）
- RADAR_KLINE_PARQUET_AUTO_WRITE   拉数成功后自动写入（默认 1）
- RADAR_KLINE_PARQUET_MAX_AGE_DAYS 优先读盘时文件最老允许天数，未设置则不限制
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

META_KEY_LINEAGE = b"radar_lineage"
META_KEY_FORMAT = b"radar_kline_format"
FORMAT_VERSION = "1"


def kline_parquet_root() -> Path:
    return Path(os.getenv("RADAR_KLINE_PARQUET_DIR", "data/kline_parquet")).expanduser()


def kline_parquet_path(code: str) -> Path:
    c = str(code).strip().zfill(6)
    return kline_parquet_root() / f"{c}.parquet"


def _pyarrow() -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        return pa, pq
    except ImportError as e:
        raise ImportError(
            "写入/读取 K 线 Parquet 需要 pyarrow，请 pip install pyarrow>=15"
        ) from e


def write_kline_parquet_barrier(
    df: pd.DataFrame,
    path: Path | str,
    *,
    lineage: Optional[Dict[str, Any]] = None,
    normalize_source: str = "parquet_barrier_in",
) -> Path:
    """
    标准化 + 校验通过后写入 Parquet；否则抛 ValueError。
    """
    from radar.schemas.data_schema import normalize_dataframe, validate_dataframe

    path = Path(path)
    if df is None or df.empty:
        raise ValueError("拒绝写入空 DataFrame")

    out = normalize_dataframe(df.copy(), source=normalize_source)
    ok, errs = validate_dataframe(out, strict=False)
    if not ok:
        raise ValueError(f"Parquet 写入前校验失败: {errs}")

    meta_lineage: Dict[str, Any] = dict(lineage or {})
    if "radar_lineage" in getattr(out, "attrs", {}):
        meta_lineage = {**dict(out.attrs.get("radar_lineage", {})), **meta_lineage}
    meta_lineage.setdefault("stored_at_utc", datetime.now(timezone.utc).isoformat())
    meta_lineage["format"] = FORMAT_VERSION

    path.parent.mkdir(parents=True, exist_ok=True)
    pa, pq = _pyarrow()
    table = pa.Table.from_pandas(out, preserve_index=False)
    raw_meta = dict(table.schema.metadata or {})
    raw_meta[META_KEY_LINEAGE] = json.dumps(meta_lineage, ensure_ascii=False).encode("utf-8")
    raw_meta[META_KEY_FORMAT] = FORMAT_VERSION.encode("utf-8")
    table = table.replace_schema_metadata(raw_meta)
    pq.write_table(table, path, compression="zstd")
    logger.info("K 线已写入 Parquet（防洪坝校验通过）: %s rows=%s", path, len(out))
    return path


def read_kline_parquet_barrier(path: Path | str) -> pd.DataFrame:
    """读取 Parquet 并做与训练侧一致的 Schema 校验。"""
    from radar.schemas.data_schema import validate_dataframe

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))

    _, pq = _pyarrow()
    table = pq.read_table(path)
    df = table.to_pandas()
    ok, errs = validate_dataframe(df, strict=False)
    if not ok:
        raise ValueError(f"Parquet 读入校验失败（文件可能损坏或被篡改）: {errs}")

    md = table.schema.metadata or {}
    if META_KEY_LINEAGE in md:
        try:
            lineage = json.loads(md[META_KEY_LINEAGE].decode("utf-8"))
            df.attrs["radar_lineage"] = lineage
        except (OSError, ImportError):
            # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
            pass
    return df


def try_load_kline_parquet(
    code: str,
    *,
    min_rows: int = 30,
    max_age_days: Optional[float] = None,
) -> Optional[pd.DataFrame]:
    """
    若存在合法 Parquet 且满足行数/时效，返回 DataFrame；否则 None。
    校验失败记 warning 并返回 None（可触发上游重新拉数）。
    """
    path = kline_parquet_path(code)
    if not path.is_file():
        return None
    if max_age_days is not None:
        age_days = (time.time() - path.stat().st_mtime) / 86400.0
        if age_days > max_age_days:
            logger.debug("Parquet 过旧跳过 code=%s age_days=%.2f", code, age_days)
            return None
    try:
        df = read_kline_parquet_barrier(path)
        if len(df) < min_rows:
            logger.info(
                "Parquet 行数不足 code=%s need>=%s got=%s", code, min_rows, len(df)
            )
            return None
        return df
    except Exception as e:
        logger.warning("读取 Parquet 失败 code=%s path=%s: %s", code, path, e)
        return None


def maybe_persist_fetched_kline(
    code: str,
    df: Optional[pd.DataFrame],
    *,
    persist_reason: str = "post_fetch",
) -> None:
    """拉数成功后按需写入 Parquet（默认开启）。"""
    if df is None or df.empty:
        return
    if os.getenv("RADAR_KLINE_PARQUET_AUTO_WRITE", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return
    try:
        lineage = {}
        if hasattr(df, "attrs") and df.attrs.get("radar_lineage"):
            lineage = dict(df.attrs["radar_lineage"])
        lineage["persist_reason"] = persist_reason
        write_kline_parquet_barrier(df, kline_parquet_path(code), lineage=lineage)
    except Exception as e:
        logger.warning("自动写入 K 线 Parquet 失败 code=%s: %s", code, e)


def prefer_read_enabled() -> bool:
    return os.getenv("RADAR_KLINE_PARQUET_PREFER_READ", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def max_age_days_from_env() -> Optional[float]:
    raw = os.getenv("RADAR_KLINE_PARQUET_MAX_AGE_DAYS", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
