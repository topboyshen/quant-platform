"""
ML 模型版本清单：models/manifest.json

记录每次成功训练的时间、数据日期范围、验证集指标与产物路径。
路径可通过环境变量 RADAR_MODEL_MANIFEST 覆盖。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

DEFAULT_MANIFEST = "models/manifest.json"
SCHEMA_VERSION = 1


def manifest_path() -> Path:
    return Path(os.environ.get("RADAR_MODEL_MANIFEST", DEFAULT_MANIFEST))


def load_manifest() -> Dict[str, Any]:
    path = manifest_path()
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": None,
            "models": {},
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"schema_version": SCHEMA_VERSION, "updated_at_utc": None, "models": {}}
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("models", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": None,
            "models": {},
        }


def _iso_data_range(df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"start": None, "end": None, "rows": 0}
    if df is None or df.empty:
        return out
    out["rows"] = int(len(df))
    col = None
    for name in ("date", "Date", "DATE"):
        if name in df.columns:
            col = name
            break
    if col is None:
        return out
    try:
        s = pd.to_datetime(df[col], errors="coerce")
        out["start"] = s.min().isoformat() if pd.notna(s.min()) else None
        out["end"] = s.max().isoformat() if pd.notna(s.max()) else None
    except (OSError, json.JSONDecodeError):
        # 异常被静默忽略，如需排查请开启调试日志  # 安全忽略
        pass
    return out


def record_ml_training_run(
    *,
    model_dir: str,
    code: str,
    direction: str,
    prediction_window: int,
    df: pd.DataFrame,
    train_result: Dict[str, Any],
) -> None:
    """训练成功并落盘 pkl 后写入/更新 manifest。"""
    if not train_result.get("success"):
        return

    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    c = str(code).strip()
    key = f"{c}_{direction}"
    now = datetime.now(timezone.utc).isoformat()

    manifest["models"][key] = {
        "code": c,
        "direction": direction,
        "trained_at_utc": now,
        "prediction_window": int(prediction_window),
        "data_range": _iso_data_range(df),
        "metrics": {
            "accuracy": float(train_result.get("accuracy", 0) or 0),
            "precision": float(train_result.get("precision", 0) or 0),
            "recall": float(train_result.get("recall", 0) or 0),
            "samples": int(train_result.get("samples", 0) or 0),
            "positive_samples": int(train_result.get("positive_samples", 0) or 0),
        },
        "artifacts": {
            "model": str(Path(model_dir) / f"{c}_{direction}_model.pkl"),
            "scaler": str(Path(model_dir) / f"{c}_{direction}_scaler.pkl"),
            "features": str(Path(model_dir) / f"{c}_{direction}_features.json"),
        },
    }
    manifest["updated_at_utc"] = now
    manifest["schema_version"] = SCHEMA_VERSION

    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
