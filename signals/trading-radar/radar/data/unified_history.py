"""
统一日频历史 K 线处理：列名标准化、可选校验、血缘元数据（df.attrs）。

目的：缓解训练 / 回测 / 脚本各自拉数导致的「暗池分叉」——至少保证
- 列名与 data_schema 一致；
- 每次 DataFrame 携带 source / pipeline / 复权说明，便于日志与排障。

说明：
- 实盘 MonitorEngine 使用的是「实时快照 dict」，与日 K 的复权口径可能不同；
  这是业务层已知差异，见同目录 PIPELINE.txt。本模块聚焦日 K 对齐。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

LINEAGE_ATTR = "radar_lineage"


def attach_lineage(
    df: pd.DataFrame,
    *,
    source: str,
    pipeline: str,
    adjustment: str = "unknown",
    extra: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """写入 df.attrs['radar_lineage']，不改动列数据。"""
    meta: Dict[str, Any] = {
        "source": source,
        "pipeline": pipeline,
        "adjustment": adjustment,
        "bars": int(len(df)),
    }
    if extra:
        meta.update(extra)
    df.attrs[LINEAGE_ATTR] = meta
    return df


def get_lineage(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    if df is None or not hasattr(df, "attrs"):
        return None
    return df.attrs.get(LINEAGE_ATTR)


def normalize_history_df(
    df: Optional[pd.DataFrame],
    *,
    source: str,
    pipeline: str,
    adjustment: str = "unknown",
    validate: bool = True,
) -> Optional[pd.DataFrame]:
    """
    对原始日 K 做 data_schema 标准化，并打上血缘。

    标准化失败时保留原列并记录 normalize_error，避免训练脚本直接中断。
    """
    if df is None:
        return None
    if hasattr(df, "empty") and df.empty:
        return df

    normalize_error: Optional[str] = None
    try:
        from radar.schemas.data_schema import normalize_dataframe

        out = normalize_dataframe(df.copy(), source=source)
    except Exception as e:
        normalize_error = str(e)
        logger.warning(
            "历史 K 线标准化失败 source=%s pipeline=%s: %s",
            source,
            pipeline,
            e,
        )
        out = df.copy()

    extra: Dict[str, Any] = {}
    if normalize_error:
        extra["normalize_error"] = normalize_error

    out = attach_lineage(
        out, source=source, pipeline=pipeline, adjustment=adjustment, extra=extra or None
    )

    if validate and not normalize_error:
        try:
            from radar.schemas.data_schema import validate_dataframe

            ok, errs = validate_dataframe(out, strict=False)
            if not ok:
                logger.warning("标准化后校验未通过 pipeline=%s: %s", pipeline, errs)
                extra_fail = dict(out.attrs.get(LINEAGE_ATTR, {}))
                extra_fail["validate_errors"] = errs
                out.attrs[LINEAGE_ATTR] = extra_fail
        except Exception as e:
            logger.debug("校验步骤跳过: %s", e)

    return out


def log_history_lineage(log: logging.Logger, prefix: str, df: pd.DataFrame) -> None:
    """在训练/回测脚本中打印一行血缘，便于与实盘日志对照。"""
    meta = get_lineage(df)
    if meta:
        log.info("%s radar_lineage=%s", prefix, meta)
